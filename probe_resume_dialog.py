#!/usr/bin/env python3
"""probe_resume_dialog.py — 只读取证「发简历」确认弹窗的真实 DOM 结构（B 方案）。

背景：BOSS 页面有反调试（打开 F12 会被踢回首页），人工拿不到确认按钮的 class。
本脚本用与主程序完全相同的 Playwright 火狐（同一持久化 profile，带登录态）打开聊天页，
在页面里直接读 DOM —— 不走 devtools，BOSS 的反调试管不到这条路。

会话筛选：只对「发简历」按钮可用的会话操作。按钮带 unable 类 = "求简历：双方回复后可用"，
即双方互回复过才解锁 —— 正是生产环境真正会走到弹窗那一步的场景。

安全边界（硬编码，不提供关闭开关）：
  1. 只允许点击两类元素：会话列表项（打开会话，无副作用）、「发简历」按钮
     （只弹出简历选择框，不点确认就不会发送任何东西）；
  2. 绝不点击 确认/确定/发送 等有真实副作用的按钮 —— 每次点击前做文本白名单校验，
     校验不过直接中止脚本；
  3. 取证完成后按 Esc 关闭弹窗，不留现场。

用法（先停掉主程序浏览器释放 profile：curl -X POST http://127.0.0.1:8010/api/system/stop）：
  .venv/Scripts/python.exe probe_resume_dialog.py              # 全流程取证（推荐）
  .venv/Scripts/python.exe probe_resume_dialog.py --no-click   # 零点击：只扫页面里隐藏的弹窗模板
  .venv/Scripts/python.exe probe_resume_dialog.py --first      # 从列表第一个（最新）会话开始试
                                                               # 默认从最后一个（最旧）开始，
                                                               # 避免把刚收到的未读消息标成已读

结果：控制台打印「确认按钮候选」，完整 DOM 取证存 probe_resume_dialog_result.json。
"""

import argparse
import io
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

# Windows 控制台非 UTF-8 时重包（与 boss_firefox 同款处理）
if (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "") != "utf8" and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

from boss_firefox import BossScraper  # 复用主程序同款启动参数 / 防检测脚本 / 持久化登录态

CHAT_URL = "https://www.zhipin.com/web/geek/chat"
RESULT_FILE = Path(__file__).parent / "probe_resume_dialog_result.json"

# 与 boss_automation.SELECTORS 保持同源（探测结论要反哺生产选择器，两边必须一致）
CONV_SELECTORS = ['li[role="listitem"]', ".friend-content", '[class*="chat-item"]']
RESUME_BTN_SELECTORS = [
    'div.toolbar-btn:has-text("发简历")',
    'button:has-text("发简历")',
    'span:has-text("发简历")',
    'div:has-text("发简历")',
]

# 有真实副作用的按钮文本 —— 点「发简历」时若混入这些词一律拒点
FORBIDDEN_TEXTS = ("确认", "发送", "确定", "立即发送", "发送简历")

# 弹窗容器 + 全页可见按钮 Dump（含 hidden，用于抓预渲染模板）
DIALOG_SCAN_JS = """() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const pick = (el) => ({
    tag: el.tagName.toLowerCase(),
    cls: ((el.className && el.className.toString) ? el.className.toString() : '').slice(0, 400),
    id: el.id || '',
    text: (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 80),
    visible: vis(el),
  });
  const out = { overlays: [], buttons: [] };
  const ovs = document.querySelectorAll('[class*="dialog"],[class*="modal"],[class*="popup"],[class*="choose"],[class*="resume"],[class*="sure"],[class*="attach"],[class*="picker"]');
  for (const el of ovs) {
    const p = pick(el);
    p.html = el.outerHTML.slice(0, 4000);
    out.overlays.push(p);
    if (out.overlays.length >= 15) break;
  }
  const btns = document.querySelectorAll('button, .btn, [class*="btn"], [role="button"]');
  for (const el of btns) {
    const p = pick(el);
    if (p.visible && p.text) out.buttons.push(p);
    if (out.buttons.length >= 100) break;
  }
  return out;
}"""

# 元素祖先链 CSS 路径（8 层，够到弹窗容器层）
DEEP_PATH_JS = """(el) => {
  const parts = [];
  let cur = el;
  for (let i = 0; i < 8 && cur && cur.nodeType === 1; i++) {
    const tag = cur.tagName.toLowerCase();
    const cls = ((cur.className && cur.className.toString) ? cur.className.toString() : '')
      .trim().split(/\\s+/).filter(Boolean).slice(0, 3).join('.');
    parts.unshift(cls ? tag + '.' + cls : tag);
    cur = cur.parentElement;
  }
  return parts.join(' > ');
}"""


def guarded_click(loc, must_contain: str, what: str):
    """白名单点击：目标文本不含 must_contain、或含高危词，就拒绝。"""
    text = (loc.inner_text() or "").strip()
    if must_contain not in text:
        raise RuntimeError(f"安全拦截：{what} 的文本是 {text[:30]!r}，不含 {must_contain!r}，拒绝点击")
    for bad in FORBIDDEN_TEXTS:
        if must_contain != bad and bad in text:
            raise RuntimeError(f"安全拦截：{what} 的文本 {text[:30]!r} 含高危词 {bad!r}，拒绝点击")
    print(f"  [点击] {what}: {text[:24]!r}")
    loc.click()


def find_first_visible(page, selectors, timeout_s: float = 4.0):
    """与生产 _find_element 同逻辑：逐个选择器轮询到超时，返回第一个可见项。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.is_visible():
                    return loc
            except Exception:
                continue
        time.sleep(0.3)
    return None


def snapshot(page) -> dict:
    return page.evaluate(DIALOG_SCAN_JS)


def dialog_signal(snap: dict, baseline_cls: set) -> tuple:
    """返回 (新出现的弹窗容器, 可见的 btn-sure-v2 且非聊天发送键)。

    聊天输入框自己的发送键是 button.btn-v2.btn-sure-v2.btn-send —— 必须排除，
    它常驻可见，是上一版探测误报的元凶。
    """
    new_overlays = []
    sure_btns = []
    for o in snap.get("overlays", []):
        if not o["visible"]:
            continue
        if "btn-sure-v2" in o["cls"] and "btn-send" not in o["cls"]:
            sure_btns.append(o)
            continue
        if o["cls"] not in baseline_cls and any(
            k in o["cls"] for k in ("dialog", "modal", "popup", "choose", "picker")
        ):
            new_overlays.append(o)
    return new_overlays, sure_btns


def capture_confirm(page) -> dict:
    """对可见的 .btn-sure-v2（非聊天发送键）取证：标签/类/文本/祖先链/所在弹窗容器。"""
    info = {}
    try:
        loc = page.locator(".btn-sure-v2:not(.btn-send)").first
        if not loc.is_visible():
            return info
        info = {
            "tag_cls": loc.evaluate("el => el.tagName.toLowerCase() + ' ' + el.className.toString()"),
            "text": (loc.inner_text() or "").strip(),
            "css_path": loc.evaluate(DEEP_PATH_JS),
            "disabled_like": loc.evaluate(
                "el => el.disabled === true || el.getAttribute('aria-disabled') === 'true'"
                " || el.className.toString().includes('unable')"
            ),
            "dialog_container_html": loc.evaluate(
                "el => { const c = el.closest('[class*=\"dialog\"],[class*=\"wrap\"],"
                "[class*=\"popup\"],[class*=\"choose\"]');"
                " return c ? c.outerHTML.slice(0, 6000) : ''; }"
            ),
        }
    except Exception as e:
        info["error"] = str(e)
    return info


def main():
    ap = argparse.ArgumentParser(description="只读取证「发简历」确认弹窗 DOM")
    ap.add_argument("--no-click", action="store_true", help="零点击：只扫页面里隐藏的弹窗模板")
    ap.add_argument("--first", action="store_true", help="从列表第一个（最新）会话开始试，默认从最后一个开始")
    ap.add_argument("--max-conv", type=int, default=10, help="最多尝试几个会话（默认 10）")
    args = ap.parse_args()

    result = {"time": datetime.now().isoformat(timespec="seconds"), "notes": []}
    scraper = BossScraper(headless=False)
    print("[1/5] 启动浏览器（复用主程序 profile，带登录态）...")
    try:
        scraper.start()
    except Exception as e:
        print(f"✗ 浏览器启动失败: {e}")
        print("  多半是 profile 被占用：先停掉主程序浏览器（curl -X POST http://127.0.0.1:8010/api/system/stop）")
        return 1
    page = scraper.page

    try:
        print("[2/5] 打开聊天页并检查登录态...")
        page.goto(CHAT_URL, wait_until="load", timeout=30000)
        time.sleep(random.uniform(2.0, 3.0))

        def _logged_in() -> bool:
            try:
                b = page.inner_text("body") or ""
            except Exception:
                return False
            return "扫码登录" not in b and "验证码登录" not in b

        if not _logged_in():
            print(f"  ⚠ 登录态已失效（当前在 {page.url}）")
            print("  ⚠ 请在刚弹出的火狐窗口里扫码登录；若有验证码/滑块请手动完成")
            print("  脚本原地等待，登录成功后自动继续（最多等 6 分钟）...")
            wait_deadline = time.time() + 360
            while time.time() < wait_deadline:
                time.sleep(2)
                if _logged_in():
                    break
            else:
                print("✗ 等待扫码超时，请重新运行本脚本再扫")
                return 1
            print("  ✓ 登录成功，回到聊天页继续")
            page.goto(CHAT_URL, wait_until="load", timeout=30000)
            time.sleep(random.uniform(2.0, 3.0))
            if not _logged_in():
                print("✗ 登录后仍未进入聊天页，请重跑本脚本")
                return 1
        print("  ✓ 已登录，继续")

        # 阶段一：零点击，扫页面里预渲染的弹窗模板（hidden 也抓）
        snap0 = snapshot(page)
        result["phase1_hidden_scan"] = snap0
        hidden_hits = [o for o in snap0["overlays"] if not o["visible"]]
        print(f"  阶段一（零点击）：弹窗类容器 {len(snap0['overlays'])} 个"
              f"（可见 {len([o for o in snap0['overlays'] if o['visible']])} / 隐藏 {len(hidden_hits)}）")
        for o in hidden_hits[:5]:
            print(f"    - {o['tag']} .{o['cls'][:100]}")

        if args.no_click:
            print("[完成] --no-click 模式，未做任何点击，结果已存 JSON")
            return 0

        # 阶段二：挑「发简历」可用的会话（回复过的 HR），白名单点击，等真弹窗
        print("[3/5] 找「发简历」可用的会话（双方互回复过），点开选择框取证...")
        conv_sel = next((s for s in CONV_SELECTORS if page.locator(s).count() > 0), None)
        if not conv_sel:
            print("✗ 找不到会话列表（选择器全落空）")
            return 1
        items = page.locator(conv_sel)
        count = items.count()
        print(f"  会话列表选择器: {conv_sel}，共 {count} 项")
        baseline_cls = {o["cls"] for o in snap0["overlays"]}
        idx_seq = range(min(args.max_conv, count)) if args.first else range(min(args.max_conv, count) - 1, -1, -1)

        captured = False
        for k in idx_seq:
            if captured:
                break
            try:
                guarded_click(items.nth(k), "", f"会话#{k + 1}（打开会话，无副作用）")
            except Exception as e:
                print(f"  跳过会话#{k + 1}: {e}")
                continue
            time.sleep(random.uniform(1.5, 2.5))

            btn = find_first_visible(page, RESUME_BTN_SELECTORS, timeout_s=4.0)
            if not btn:
                print(f"  会话#{k + 1} 没有「发简历」按钮，换下一个")
                continue
            btn_cls = btn.evaluate("el => el.className.toString()")
            if "unable" in btn_cls:
                print(f"  会话#{k + 1} 发简历不可用（unable：双方还没互回复过），换下一个")
                continue
            result["resume_btn"] = {"cls": btn_cls, "html": btn.evaluate("el => el.outerHTML.slice(0, 1200)")}
            result["toolbar_html"] = btn.evaluate(
                "el => (el.closest('div[class*=toolbar],div[class*=tool]')||el.parentElement).outerHTML.slice(0, 2500)"
            )
            guarded_click(btn, "发简历", "「发简历」按钮")
            print("  等待简历选择弹窗渲染...")

            final_snap = None
            for _ in range(16):  # 最多 ~10s
                time.sleep(0.6)
                s = snapshot(page)
                new_ov, sure = dialog_signal(s, baseline_cls)
                if new_ov or sure:
                    final_snap = s
                    result["phase2_new_overlays"] = new_ov
                    result["phase2_sure_buttons"] = sure
                    break
            if not final_snap:
                print(f"  会话#{k + 1} 点了发简历但没等到弹窗，换下一个")
                page.keyboard.press("Escape")
                time.sleep(0.8)
                continue

            result["phase2_dialog_capture"] = final_snap
            result["phase2_confirm_element"] = capture_confirm(page)
            captured = True
            ce = result["phase2_confirm_element"]
            print("  ✓ 弹窗已捕获！确认按钮取证：")
            print(f"    - 元素: {ce.get('tag_cls', '?')}")
            print(f"    - 文本: 「{ce.get('text', '?')}」 disabled_like={ce.get('disabled_like')}")
            print(f"    - 祖先链: {ce.get('css_path', '?')}")
            if not ce:
                print("  ⚠ .btn-sure-v2 不可见，确认按钮结构请看 JSON 的 phase2_sure_buttons / new_overlays")
            page.keyboard.press("Escape")
            time.sleep(1.0)

        if not captured:
            print("✗ 尝试的会话里没有一个弹出简历选择框（发简历都不可用或已发过），")
            print("  换 --first 或加大 --max-conv 再试；若都提示 unable，先随便回一条消息解锁")
            return 1

        print("[4/5] 按 Esc 关闭弹窗完成，恢复现场")
        print(f"[5/5] 完整取证已存: {RESULT_FILE.name}")
        return 0
    finally:
        result["url"] = safe_url(page)
        try:
            RESULT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"⚠ 结果写盘失败: {e}")
        try:
            scraper.close()
        except Exception:
            pass


def safe_url(page):
    try:
        return page.url
    except Exception:
        return ""


if __name__ == "__main__":
    sys.exit(main())
