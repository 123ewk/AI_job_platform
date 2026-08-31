"""pw 线程身份守卫：sync Playwright 对象绑创建线程，线程身份变了必须明确报错。

背景（为什么检测而非重建）：
- CPython 的 ThreadPoolExecutor worker 不因任务异常被替换（异常装进 Future，线程
  存活，实测 3.10 验证过），boss_app._playwright_executor 也从不 shutdown——
  "线程被重建"在当前代码结构里发生不了，这套检测是给未来重构破坏该不变量时的兜底。
- 真发生时**拒绝自动重建**：旧对象绑在旧线程上，close 跨线程做不掉，旧 Firefox
  进程还活着并占着 profile 锁，此刻重开必撞 parent.lock 挂死（180s 握手超时）。
  所以 open_browser 撞上线程失效时返回明确报错，把恢复权交还给人（重启服务进程）。
"""

import threading

import pytest

import boss_automation
from agent.errors import friendly_error
from agent.tools import open_browser_factory
from boss_firefox import BossScraper, BrowserThreadMismatchError


def _scraped_on_other_thread() -> BossScraper:
    """构造一个"盖了别的线程的戳"的 scraper（不真起浏览器）。"""
    s = BossScraper()
    # get_ident()+1 与当前线程必然不同（同一时刻 ident 唯一），又不碰真实线程号
    s._pw_thread_id = threading.get_ident() + 1
    return s


class TestThreadStamp:
    def test_start_stamps_current_thread(self, monkeypatch):
        """start() 成功后必须盖上创建线程的 id——守卫的锚点。"""
        import boss_firefox

        class _FakePage:
            def set_default_timeout(self, ms):
                pass

        class _FakeCtx:
            pages = []

            def new_page(self):
                return _FakePage()

            def add_init_script(self, script):
                pass

            def add_cookies(self, cookies):
                pass

        class _FakeFirefox:
            @staticmethod
            def launch_persistent_context(path, **kw):
                return _FakeCtx()

        class _FakePw:
            firefox = _FakeFirefox

            def stop(self):
                pass

        class _FakeSyncPw:
            def start(self):
                return _FakePw()

        monkeypatch.setattr(boss_firefox, "sync_playwright", lambda: _FakeSyncPw())
        s = BossScraper()
        s.start()
        assert s._pw_thread_id == threading.get_ident()

    def test_same_thread_noop(self):
        s = BossScraper()
        s._pw_thread_id = threading.get_ident()
        s._ensure_same_thread()  # 同线程：不抛

    def test_unstamped_noop(self):
        """没 start 过（无戳）不拦截——测试假对象/未启动对象照常走。"""
        BossScraper()._ensure_same_thread()

    def test_mismatch_raises(self):
        with pytest.raises(BrowserThreadMismatchError):
            _scraped_on_other_thread()._ensure_same_thread()


class TestGuardEntryPoints:
    def test_close_raises_on_mismatch(self):
        with pytest.raises(BrowserThreadMismatchError):
            _scraped_on_other_thread().close()

    def test_heartbeat_raises_instead_of_swallowing(self, monkeypatch):
        """heartbeat 必须把线程失效抛出去：吞成 False 会被监控误判成 session_expired。"""
        monkeypatch.setattr(boss_automation, "init_db", lambda: None)
        a = boss_automation.BossAutomation()
        a._pw_thread_id = threading.get_ident() + 1
        with pytest.raises(BrowserThreadMismatchError):
            a.heartbeat()

    def test_heartbeat_same_thread_normal_path(self, monkeypatch):
        """同线程时走原有逻辑（此处 check_logged_in 抛异常 → 按原约定 False）。"""
        monkeypatch.setattr(boss_automation, "init_db", lambda: None)
        a = boss_automation.BossAutomation()
        a._pw_thread_id = threading.get_ident()
        monkeypatch.setattr(a, "check_logged_in", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert a.heartbeat() is False


class TestOpenBrowserRefusesRebuild:
    def test_mismatch_returns_error_without_rebuild(self):
        """线程失效时不得自动重开浏览器（旧 firefox 占着 profile 锁，重开必挂死）。"""
        started = {"n": 0}

        def starter():
            started["n"] += 1
            return object()

        class _Cur:
            page = object()  # 对象在 → 进心跳确认分支

            def heartbeat(self):
                raise BrowserThreadMismatchError("浏览器对象绑定在旧线程上，请重启服务进程恢复")

        def runner(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        open_browser = open_browser_factory(
            get_automation=lambda: _Cur(),
            pw_runner=runner,
            start_browser=starter,
            set_automation=lambda a: None,
        )
        out = open_browser()
        assert out["error"] == "浏览器线程失效"
        assert "重启服务进程" in out["message"]
        assert started["n"] == 0  # 关键契约：绝不自动重建

    def test_normal_stale_still_self_heals(self):
        """对照：普通探针失败（窗口被关等）仍走原自愈路径 close→重开。"""
        started = {"n": 0}

        def starter():
            started["n"] += 1
            fresh = type("Fresh", (), {"page": object()})()
            return fresh

        class _Cur:
            page = object()

            def heartbeat(self):
                raise RuntimeError("Target closed")  # 普通失效，非线程问题

            def close(self):
                pass

        set_calls = {"n": 0}

        def setter(a):
            set_calls["n"] += 1

        open_browser = open_browser_factory(
            get_automation=lambda: _Cur(),
            pw_runner=lambda fn, *a, **k: fn(*a, **k),
            start_browser=starter,
            set_automation=setter,
        )
        out = open_browser()
        assert out["status"] == "started"
        assert started["n"] == 1
        assert set_calls["n"] == 1


class TestFriendlyError:
    def test_thread_mismatch_passthrough(self):
        e = BrowserThreadMismatchError("浏览器对象绑定在旧线程(x)上，请重启服务进程恢复")
        assert "重启服务进程" in friendly_error(e)

    def test_greenlet_cross_thread_keyword(self):
        e = RuntimeError("greenlet.error: cannot switch to a different thread")
        out = friendly_error(e)
        assert "线程" in out and "重启服务进程" in out
