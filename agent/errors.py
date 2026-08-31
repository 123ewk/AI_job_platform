"""agent/errors.py — 异常 → 用户能看懂的中文文案。

浏览器/工具层抛上来的原始异常（Playwright 的 TargetClosedError、TimeoutError 等）
直接给用户看是一整屏英文堆栈。这里统一翻译成「发生了什么 + 现在该怎么办」的一两句
中文，供各兜底出口共用：graph 工具执行异常回灌、/api/agent/chat 500 兜底、
executor 后台任务失败落库、boss_app 全局 500。

只按异常类名和消息关键字匹配，不 import playwright 私有路径——当前版本未从
playwright.sync_api 导出 TargetClosedError，而类名/消息文本跨版本最稳定。
"""

from __future__ import annotations

_BROWSER_CLOSED_MARKS = (
    "Target page, context or browser has been closed",
    "Target closed",
    "Browser has been closed",
)


def _first_line(msg: str) -> str:
    """取异常消息第一个非空行（甩掉 Call log 等多行尾巴）。"""
    for line in (msg or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def friendly_error(e: BaseException) -> str:
    """把异常翻译成用户可照做的一两句中文（所有兜底出口共用）。"""
    name = type(e).__name__
    msg = str(e) or ""

    # 浏览器/页面被关：最常见——窗口被手动关闭、浏览器崩溃、登录失效页面跳走
    if name == "TargetClosedError" or any(mark in msg for mark in _BROWSER_CLOSED_MARKS):
        return (
            "自动化浏览器或页面已被关闭（可能是 Firefox 窗口被手动关掉、浏览器崩溃，"
            "或登录失效后页面跳走了）。请回到控制台首页点「启动浏览器」，重新扫码登录，"
            "然后重试刚才的操作。"
        )
    # 超时：网站慢、网络差，或浏览器里弹了验证码/滑块在等人工处理
    if name == "TimeoutError" or "Timeout" in msg:
        return (
            "页面操作超时（网站加载慢，或浏览器里弹出了验证码/滑块在等人工处理）。"
            "请切到浏览器窗口看一眼，若有验证码先手动完成，再重试。"
        )
    detail = _first_line(msg)
    if name == "Error" and detail:
        # Playwright 基类错误的类名没有信息量，直接展示消息首行
        return f"浏览器操作失败：{detail}。请检查浏览器窗口是否正常，再重试。"
    if detail:
        return f"操作失败（{name}）：{detail}"
    return f"操作失败（{name}）。请重试；若反复失败，请检查浏览器是否已启动、登录是否有效。"
