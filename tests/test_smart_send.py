"""tests/test_smart_send.py — 纯函数 + 实例方法单测（不依赖浏览器、不依赖 pytest fixtures）。

如果 pytest 在 Windows 上跑得起来，会正常执行；如果触发 pytest 自身的 teardown 异常，
可直接 `python -m unittest tests.test_smart_send` 跑。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def setup_module(module):
    """模块加载时：让 boss_state 用临时 db。"""
    import boss_state as bs

    bs._local.conn = None
    tmp = Path(tempfile.gettempdir()) / "boss_state_smart_send_test.db"
    if tmp.exists():
        tmp.unlink()
    bs.DB_PATH = tmp
    bs.init_db()


def teardown_module(module):
    import boss_state as bs

    if hasattr(bs._local, "conn") and bs._local.conn is not None:
        try:
            bs._local.conn.close()
        except Exception:
            pass
        bs._local.conn = None
    try:
        if bs.DB_PATH.exists():
            bs.DB_PATH.unlink()
    except Exception:
        pass


# ══════════════════════════════════════
#  pick_top_hr / _hr_title_score
# ══════════════════════════════════════


class TestPickTopHr(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from boss_automation import pick_top_hr, _hr_title_score

        cls.pick = staticmethod(pick_top_hr)
        cls.score = staticmethod(_hr_title_score)

    def test_empty(self):
        self.assertIsNone(self.pick([]))

    def test_basic(self):
        hrs = [
            {"name": "李四", "title": "HRBP"},
            {"name": "王五", "title": "招聘专员"},
            {"name": "张三", "title": "招聘经理"},
            {"name": "赵六", "title": "人事"},
        ]
        top = self.pick(hrs)
        self.assertEqual(top["name"], "张三")
        self.assertEqual(top["title"], "招聘经理")

    def test_unknown_title_falls_back_to_first_known(self):
        hrs = [
            {"name": "钱七", "title": "奇怪头衔"},
            {"name": "周八", "title": "HR"},
        ]
        top = self.pick(hrs)
        self.assertEqual(top["name"], "周八")

    def test_hrbp_beats_specialist(self):
        self.assertGreater(self.score("HRBP"), self.score("招聘专员"))
        self.assertEqual(self.score("HRBP"), 90)
        self.assertEqual(self.score("招聘专员"), 30)

    def test_stable_for_same_score(self):
        hrs = [{"name": "甲", "title": "HR"}, {"name": "乙", "title": "HR"}]
        self.assertEqual(self.pick(hrs)["name"], "甲")

    def test_unrelated_title_zero(self):
        self.assertEqual(self.score("产品经理"), 0)
        self.assertEqual(self.score(""), 0)
        self.assertEqual(self.score(None), 0)


# ══════════════════════════════════════
#  aggregate_company_hrs (实例方法, unbound 调用)
# ══════════════════════════════════════


class TestDetectBoss(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from boss_automation import detect_boss, _extract_surname, _extract_legal_rep

        cls.detect = staticmethod(detect_boss)
        cls.surname = staticmethod(_extract_surname)
        cls.rep = staticmethod(_extract_legal_rep)

    def test_full_name_match_high(self):
        r = self.detect("张伟", "张伟")
        self.assertTrue(r["is_boss"])
        self.assertEqual(r["confidence"], "high")
        self.assertGreaterEqual(r["score_bonus"], 1000)

    def test_compound_surname_medium(self):
        r = self.detect("欧阳娜", "欧阳震华")
        self.assertTrue(r["is_boss"])
        self.assertEqual(r["confidence"], "medium")

    def test_rare_surname_medium(self):
        # "笪" 不在常见姓表 → medium
        r = self.detect("笪小明", "笪大海")
        self.assertEqual(r["confidence"], "medium")

    def test_common_surname_low(self):
        r = self.detect("王芳", "王建国")
        self.assertTrue(r["is_boss"])
        self.assertEqual(r["confidence"], "low")
        self.assertLess(r["score_bonus"], 100)

    def test_different_surname_none(self):
        r = self.detect("李雷", "韩梅梅")
        self.assertFalse(r["is_boss"])
        self.assertEqual(r["confidence"], "none")

    def test_missing_data_none(self):
        self.assertEqual(self.detect("", "张三")["confidence"], "none")
        self.assertEqual(self.detect("张三", "")["confidence"], "none")

    def test_extract_surname_compound(self):
        self.assertEqual(self.surname("欧阳娜娜"), "欧阳")
        self.assertEqual(self.surname("司马懿"), "司马")
        self.assertEqual(self.surname("张三"), "张")
        self.assertEqual(self.surname(""), "")

    def test_extract_legal_rep_inline(self):
        self.assertEqual(self.rep("法定代表人：张三丰\n注册资本100万", None), "张三丰")
        self.assertEqual(self.rep("法定代表人  李四", None), "李四")

    def test_extract_legal_rep_nextline(self):
        lines = ["工商信息", "法定代表人", "王五", "注册资本"]
        self.assertEqual(self.rep("\n".join(lines), lines), "王五")

    def test_extract_legal_rep_noise_filtered(self):
        # "法定代表人变更" 不应被当成姓名
        self.assertEqual(self.rep("法定代表人变更记录", None), "")

    def test_extract_legal_rep_empty(self):
        self.assertEqual(self.rep("", None), "")
        self.assertEqual(self.rep("没有相关字段", None), "")


class TestPickTopHrWithBoss(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from boss_automation import pick_top_hr

        cls.pick = staticmethod(pick_top_hr)

    def test_boss_beats_higher_title(self):
        # 招聘专员但是法人本人，应胜过招聘经理
        hrs = [
            {"name": "张经理", "title": "招聘经理"},
            {"name": "王老板", "title": "招聘专员"},
        ]
        top = self.pick(hrs, legal_rep="王老板")
        self.assertEqual(top["name"], "王老板")
        self.assertTrue(top["is_boss"])
        self.assertEqual(top["boss_confidence"], "high")

    def test_no_legal_rep_falls_back_to_title(self):
        hrs = [
            {"name": "张经理", "title": "招聘经理"},
            {"name": "王老板", "title": "招聘专员"},
        ]
        top = self.pick(hrs)
        self.assertEqual(top["name"], "张经理")
        self.assertNotIn("is_boss", top)

    def test_common_surname_does_not_override_strong_title(self):
        # 同常见姓(low,+50) 不应翻盘 HRBP(90) vs 招聘专员(30)
        hrs = [
            {"name": "李四", "title": "HRBP"},
            {"name": "王芳", "title": "招聘专员"},
        ]
        top = self.pick(hrs, legal_rep="王建国")
        self.assertEqual(top["name"], "李四")


class TestCooldownBackoff(unittest.TestCase):
    """风控冷却退避逻辑（不依赖浏览器，用裸实例）。"""

    def _new(self):
        from boss_automation import BossAutomation

        a = BossAutomation.__new__(BossAutomation)
        a._cooldown_until = 0.0
        a._risk_strikes = 0
        a._last_action_ts = 0.0
        return a

    def test_rate_limit_exponential_backoff(self):
        a = self._new()
        a._trigger_cooldown("rate_limit")
        first = a._cooldown_remaining()
        self.assertGreater(first, 0)
        self.assertTrue(a.in_cooldown())
        # 连续命中：strike 增加
        a._trigger_cooldown("rate_limit")
        self.assertEqual(a._risk_strikes, 2)

    def test_banned_long_cooldown(self):
        a = self._new()
        a._trigger_cooldown("banned")
        self.assertGreaterEqual(a._cooldown_remaining(), 1000)

    def test_respect_cooldown_blocks(self):
        a = self._new()
        a._trigger_cooldown("rate_limit")
        self.assertFalse(a._respect_cooldown())

    def test_no_cooldown_allows(self):
        a = self._new()
        self.assertTrue(a._respect_cooldown())
        self.assertFalse(a.in_cooldown())

    def test_cap_at_30min(self):
        a = self._new()
        for _ in range(10):
            a._trigger_cooldown("rate_limit")
        self.assertLessEqual(a._cooldown_remaining(), 1800 + 1)


class TestInspectSafety(unittest.TestCase):
    """inspect_page_safety 的分类逻辑（mock page.inner_text）。"""

    def _new(self, body_text):
        from boss_automation import BossAutomation

        a = BossAutomation.__new__(BossAutomation)
        a._cooldown_until = 0.0
        a._risk_strikes = 0
        a._last_action_ts = 0.0

        class _FakePage:
            def inner_text(self, sel):
                return body_text

            url = "https://www.zhipin.com/web/geek/job"

        a.page = _FakePage()
        # _login_prompt_visible 依赖更多页面状态，这里打桩为 False
        a._login_prompt_visible = lambda: False
        return a

    def test_rate_limit_detected(self):
        a = self._new("操作太频繁，请稍后再试")
        r = a.inspect_page_safety()
        self.assertFalse(r["ok"])
        self.assertEqual(r["category"], "rate_limit")
        self.assertTrue(a.in_cooldown())

    def test_captcha_detected(self):
        a = self._new("请完成滑块验证")
        r = a.inspect_page_safety()
        self.assertFalse(r["ok"])
        self.assertEqual(r["category"], "captcha")

    def test_normal_page_ok(self):
        a = self._new("职位描述 岗位职责 立即沟通")
        r = a.inspect_page_safety()
        self.assertTrue(r["ok"])
        self.assertEqual(r["category"], "ok")


class TestGreetingAIFallback(unittest.TestCase):
    """generate_greeting_ai 在 AI 不可用时回退模板。"""

    def test_fallback_when_ai_fails(self):
        import boss_replier

        orig = boss_replier.llm_chat_deepseek
        try:
            boss_replier.llm_chat_deepseek = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no key"))
            g = boss_replier.generate_greeting_ai("Python工程师", "字节跳动", hr_name="张三")
            self.assertIn("Python工程师", g)
            self.assertTrue(g.startswith("张三"))
        finally:
            boss_replier.llm_chat_deepseek = orig

    def test_ai_result_strips_contact_info(self):
        import boss_replier

        orig = boss_replier.llm_chat_deepseek
        try:
            # AI 返回含微信号 → 应被判定不合格并回退模板
            boss_replier.llm_chat_deepseek = lambda *a, **k: "你好加我微信 wx12345 详聊"
            g = boss_replier.generate_greeting_ai("Java工程师", "腾讯")
            self.assertNotIn("微信", g)
            self.assertIn("Java工程师", g)
        finally:
            boss_replier.llm_chat_deepseek = orig


# ══════════════════════════════════════
#  aggregate_company_hrs (实例方法, unbound 调用)
# ══════════════════════════════════════


class TestAggregateCompanyHrs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from boss_automation import BossAutomation

        # 直接拿 __dict__ 里的 raw function；调用时传 (None, jobs) 当 self
        cls.agg = BossAutomation.__dict__["aggregate_company_hrs"]

    def test_dedup_same_hr_takes_higher_title(self):
        from boss_automation import BossAutomation

        agg = BossAutomation.__dict__["aggregate_company_hrs"]
        jobs = [
            {"title": "AI Agent", "hr_block": "张三 · 招聘专员"},
            {"title": "算法", "hr_block": "张三 · 招聘经理"},
        ]
        hrs = agg(None, jobs)
        self.assertEqual(len(hrs), 1)
        self.assertEqual(hrs[0]["title"], "招聘经理")
        self.assertIn("AI Agent", hrs[0]["associated_jobs"])
        self.assertIn("算法", hrs[0]["associated_jobs"])
        self.assertTrue(hrs[0]["is_top"])

    def test_multiple_hrs_sorted_by_priority(self):
        from boss_automation import BossAutomation

        agg = BossAutomation.__dict__["aggregate_company_hrs"]
        jobs = [
            {"title": "P", "hr_block": "李四 · HRBP"},
            {"title": "O", "hr_block": "王五 · 人事"},
            {"title": "A", "hr_block": "张三 · 招聘经理"},
        ]
        hrs = agg(None, jobs)
        self.assertEqual([h["name"] for h in hrs], ["张三", "李四", "王五"])
        self.assertTrue(hrs[0]["is_top"])
        self.assertFalse(hrs[1]["is_top"])

    def test_no_hr_block_returns_empty(self):
        from boss_automation import BossAutomation

        agg = BossAutomation.__dict__["aggregate_company_hrs"]
        self.assertEqual(agg(None, [{"title": "AI"}]), [])

    def test_pipe_separator(self):
        from boss_automation import BossAutomation

        agg = BossAutomation.__dict__["aggregate_company_hrs"]
        hrs = agg(None, [{"title": "AI", "hr_block": "张三|招聘经理"}])
        self.assertEqual(hrs[0]["name"], "张三")
        self.assertEqual(hrs[0]["title"], "招聘经理")

    def test_unknown_title_ranked_last(self):
        from boss_automation import BossAutomation

        agg = BossAutomation.__dict__["aggregate_company_hrs"]
        jobs = [
            {"title": "AI", "hr_block": "张三 · 招聘经理"},
            {"title": "BE", "hr_block": "李四 · 销售经理"},
        ]
        hrs = agg(None, jobs)
        self.assertEqual(hrs[0]["name"], "张三")
        self.assertEqual(hrs[1]["name"], "李四")
        self.assertEqual(hrs[1]["priority"], 0)


# ══════════════════════════════════════
#  build_company_preview
# ══════════════════════════════════════


class TestBuildCompanyPreview(unittest.TestCase):
    def test_marks_top_hr_on_jobs(self):
        from boss_company import build_company_preview

        jobs = [
            {"title": "AI", "url": "/job_detail/aaa", "hr_block": "张三 · 招聘经理"},
            {"title": "BE", "url": "/job_detail/bbb", "hr_block": "李四 · 招聘专员"},
        ]
        hrs = [
            {"name": "张三", "title": "招聘经理", "priority": 100, "associated_jobs": ["AI"]},
            {"name": "李四", "title": "招聘专员", "priority": 30, "associated_jobs": ["BE"]},
        ]
        p = build_company_preview(company="字节跳动", company_id="abc123~", open_count=42, jobs=jobs, hrs=hrs)
        self.assertEqual(p["company"], "字节跳动")
        self.assertEqual(p["company_id"], "abc123~")
        self.assertEqual(p["company_page_url"], "https://www.zhipin.com/gongsi/job/abc123~.html")
        self.assertEqual(p["stats"]["open_positions_count_official"], 42)
        self.assertEqual(p["stats"]["scraped_jobs_count"], 2)
        self.assertEqual(p["stats"]["hrs_count"], 2)
        self.assertEqual(p["top_hr"]["name"], "张三")
        self.assertTrue(p["jobs"][0]["is_top_hr_job"])
        self.assertFalse(p["jobs"][1]["is_top_hr_job"])
        self.assertTrue(p["jobs"][0]["url"].startswith("https://www.zhipin.com/"))

    def test_empty_inputs(self):
        from boss_company import build_company_preview

        p = build_company_preview(company="X", company_id="y~")
        self.assertIsNone(p["top_hr"])
        self.assertEqual(p["stats"]["scraped_jobs_count"], 0)
        self.assertEqual(p["jobs"], [])


# ══════════════════════════════════════
#  rank_companies_by_position_count
# ══════════════════════════════════════


class TestRankCompanies(unittest.TestCase):
    def setUp(self):
        from boss_state import get_db

        db = get_db()
        db.execute("DELETE FROM applications")
        db.commit()

    def test_groups_by_company(self):
        from boss_state import add_application
        from boss_company import rank_companies_by_position_count

        add_application(
            {"title": "A1", "company": "字节跳动", "company_id": "b1~", "url": "https://www.zhipin.com/job_detail/a1"}
        )
        add_application(
            {"title": "A2", "company": "字节跳动", "company_id": "b1~", "url": "https://www.zhipin.com/job_detail/a2"}
        )
        add_application(
            {"title": "B1", "company": "腾讯", "company_id": "t1~", "url": "https://www.zhipin.com/job_detail/b1"}
        )
        rows = rank_companies_by_position_count()
        names = {r["company"]: r["position_count"] for r in rows}
        self.assertEqual(names.get("字节跳动"), 2)
        self.assertEqual(names.get("腾讯"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
