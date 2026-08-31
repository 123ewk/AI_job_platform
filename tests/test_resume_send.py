"""发简历确认链路回归（probe_resume_dialog.py 实测 DOM 反哺）。

2026-08 取证：确认键是简历选择浮层 div.panel-resume.sentence-popover 里的
span.btn-v2.btn-sure-v2（文本"确定"）；聊天输入框自己的发送键是
button.btn-v2.btn-sure-v2.btn-send（disabled 但常驻可见）。
旧兜底选择器 button:has-text("发送") 会误匹配后者 → 点了没反应却当成功，
resume_sent 被标记后永不重试，简历从未发出。
"""

from pathlib import Path

import boss_automation
from boss_automation import SELECTORS, BossAutomation

SRC = Path(boss_automation.__file__).read_text(encoding="utf-8")


def test_resume_confirm_selectors_come_from_probed_dom():
    sels = SELECTORS["resume_confirm_btn"]
    # 实测浮层结构：panel-resume 里的 btn-sure-v2
    assert ".panel-resume .btn-sure-v2" in sels
    # 必须显式排除聊天输入框的常驻发送键（disabled 也算 visible，会误匹配）
    assert any("btn-send" in s for s in sels)
    # 旧的误匹配兜底必须移除
    assert 'button:has-text("发送")' not in sels


def test_send_resume_source_has_unable_precheck_and_no_false_success():
    # unable 预检：双方未互回复时按钮点了没反应，必须跳过而不是空点
    assert "unable" in SRC
    # 旧的"找不到确认键就当成功"必须消失
    assert "无弹窗，直接完成" not in SRC


class _FakeLoc:
    def __init__(self, text="", cls=""):
        self.text = text
        self.cls = cls
        self.clicked = False

    def is_visible(self):
        return True

    def inner_text(self):
        return self.text

    def evaluate(self, _code):
        return self.cls

    def click(self):
        self.clicked = True


class _FakeKeyboard:
    def __init__(self, page):
        self._page = page

    def press(self, _key):
        self._page.escapes += 1


class _FakePage:
    def __init__(self):
        self.escapes = 0
        self.keyboard = _FakeKeyboard(self)


class _FakeAuto:
    """只提供 send_resume 用到的两个方法 + page。"""

    def __init__(self, attach, confirm):
        self._attach = attach
        self._confirm = confirm
        self.page = _FakePage()

    def _find_element(self, selectors, timeout_ms=5000):
        if "发简历" in selectors[0]:
            return self._attach
        return self._confirm


def _run(attach, confirm):
    fake = _FakeAuto(attach, confirm)
    ok = BossAutomation.send_resume(fake)
    return ok, fake


def test_send_resume_happy_path_clicks_confirm(monkeypatch):
    monkeypatch.setattr(boss_automation, "pause", lambda a, b: None)
    attach = _FakeLoc(text="发简历", cls="toolbar-btn tooltip")
    confirm = _FakeLoc(text="确定", cls="btn-v2 btn-sure-v2")
    ok, fake = _run(attach, confirm)
    assert ok is True
    assert attach.clicked and confirm.clicked
    assert fake.page.escapes == 0


def test_send_resume_unable_button_skips_without_click(monkeypatch):
    monkeypatch.setattr(boss_automation, "pause", lambda a, b: None)
    attach = _FakeLoc(text="发简历", cls="toolbar-btn tooltip tooltip-top unable")
    ok, fake = _run(attach, None)
    assert ok is False
    assert attach.clicked is False
    assert fake.page.escapes == 0


def test_send_resume_missing_confirm_is_failure_not_success(monkeypatch):
    monkeypatch.setattr(boss_automation, "pause", lambda a, b: None)
    attach = _FakeLoc(text="发简历", cls="toolbar-btn")
    ok, fake = _run(attach, None)
    assert ok is False
    assert attach.clicked is True  # 发简历按钮点了，但确认没成功
    assert fake.page.escapes == 1  # Esc 收起浮层，不留现场
