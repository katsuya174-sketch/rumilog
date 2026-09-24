"""
weekly_usage_planの美容液use_days無視バグの修正、および刺激性ケア
(ピーリング/レチノール/AHA・BHA・PHA/高濃度ビタミンC)の週間スケジュール
安全性のテスト。

対応する不具合:
build_weekly_usage_plan()のNIGHT_ALWAYS_DAILY={"化粧水","美容液"}により、
conflict resolver(resolve_night_irritant_conflicts/
resolve_weekly_care_day_conflicts)やGemini自身が美容液のuse_daysを
非毎日(例:["月","水","金"])に確定させていても、週間表示では
use_daysを無視して毎日表示されてしまっていた
(評価/スケジュール決定は正しいが表示層がそれを裏切る不具合)。

修正: 美容液の日別表示判定はNIGHT_DAILY_REGARDLESS_OF_USE_DAYS
(={"化粧水"}のみ)を使い、use_daysを尊重するようにした。
化粧水の「use_days無関係に毎日表示」という既存仕様は変更していない。

実DBが必要なため、DATABASE_URL(環境変数)でテスト専用DBを指定して実行する。
"""

import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402


def _empty_data(**overrides):
    base = {
        "morning": {"steps": []},
        "night": {"steps": []},
        "weekly_care": [],
        "routine_strategy": {},
        "beauty_devices": [],
    }
    base.update(overrides)
    return base


class SerumUseDaysRespectedInWeeklyPlanTests(unittest.TestCase):
    """B: 美容液のuse_daysが週間表示で尊重されること。"""

    def test_serum_with_restricted_use_days_shown_only_on_those_days(self):
        data = _empty_data(night={"steps": [
            {"category": "美容液", "product": "レチノール美容液", "use_days": ["月", "水", "金"], "ingredient_focus": "retinol"},
        ]})
        plan = app.build_weekly_usage_plan(data)
        by_day = {d["day"]: d["night"] for d in plan}
        for day in ["月", "水", "金"]:
            self.assertTrue(any("レチノール美容液" in x for x in by_day[day]), day)
        for day in ["火", "木", "土", "日"]:
            self.assertFalse(any("レチノール美容液" in x for x in by_day[day]), day)

    def test_serum_with_empty_use_days_still_shown_daily(self):
        # use_days=[] は「AIが毎日可と判断した」既存の意味そのまま維持すること
        data = _empty_data(night={"steps": [
            {"category": "美容液", "product": "デイリー美容液", "use_days": [], "ingredient_focus": ""},
        ]})
        plan = app.build_weekly_usage_plan(data)
        for d in plan:
            self.assertIn("デイリー美容液", "".join(d["night"]))

    def test_toner_still_shown_daily_regardless_of_use_days(self):
        # 化粧水の既存仕様(use_days無関係に毎日表示)は変更しないこと
        data = _empty_data(night={"steps": [
            {"category": "化粧水", "product": "限定化粧水", "use_days": ["月"], "ingredient_focus": ""},
        ]})
        plan = app.build_weekly_usage_plan(data)
        for d in plan:
            self.assertIn("限定化粧水", "".join(d["night"]))


class DayConflictResolverAndDisplayConsistencyTests(unittest.TestCase):
    """D: conflict resolverの結果(use_days)と週間表示が完全一致すること。"""

    def test_peeling_and_daily_retinol_never_share_a_day_in_weekly_plan(self):
        data = _empty_data(
            night={"steps": [
                {"category": "美容液", "product": "レチノール美容液", "use_days": [], "ingredient_focus": "retinol"},
            ]},
            weekly_care=[
                {"category": "ピーリング", "product": "AHAピーリング", "use_days": ["土"], "ingredient_focus": "aha"},
            ],
        )
        data = app.resolve_weekly_care_day_conflicts(data)

        retinol_step = data["night"]["steps"][0]
        self.assertNotIn("土", retinol_step.get("use_days") or [])

        plan = app.build_weekly_usage_plan(data)
        sat = next(d for d in plan if d["day"] == "土")
        self.assertTrue(any("AHAピーリング" in x for x in sat["special_care"]))
        self.assertFalse(any("レチノール美容液" in x for x in sat["night"]))

        # 修正前の挙動(NIGHT_ALWAYS_DAILYが美容液のuse_daysを無視する)なら
        # ここでレチノール美容液も土に表示されてしまっていたはず。
        for day_entry in plan:
            peeling_shown = any("AHAピーリング" in x for x in day_entry["special_care"])
            retinol_shown = any("レチノール美容液" in x for x in day_entry["night"])
            self.assertFalse(peeling_shown and retinol_shown, day_entry["day"])

    def test_peeling_and_aha_bha_serum_conflict_resolved_and_never_shown_same_day(self):
        data = _empty_data(
            night={"steps": [
                {"category": "美容液", "product": "BHA美容液", "use_days": ["土"], "ingredient_focus": "bha"},
            ]},
            weekly_care=[
                {"category": "ピーリング", "product": "酵素ピーリング", "use_days": ["土"], "ingredient_focus": "enzyme"},
            ],
        )
        data = app.resolve_weekly_care_day_conflicts(data)

        peeling_step = data["weekly_care"][0]
        self.assertNotIn("土", peeling_step.get("use_days") or [])

        plan = app.build_weekly_usage_plan(data)
        for day_entry in plan:
            bha_shown = any("BHA美容液" in x for x in day_entry["night"])
            peeling_shown = any("酵素ピーリング" in x for x in day_entry["special_care"])
            self.assertFalse(bha_shown and peeling_shown, day_entry["day"])

    def test_retinol_and_high_concentration_vitamin_c_conflict_resolved_by_priority(self):
        data = _empty_data(night={"steps": [
            {"category": "美容液", "product": "レチノール美容液", "use_days": ["月", "水", "金"], "ingredient_focus": "retinol"},
            {"category": "美容液", "product": "高濃度VC美容液", "use_days": ["月", "水", "金"], "ingredient_focus": "vitamin_c"},
        ]})
        data = app.resolve_night_irritant_conflicts(data)

        steps_by_name = {s["product"]: s for s in data["night"]["steps"]}
        retinol_days = set(steps_by_name["レチノール美容液"].get("use_days") or [])
        vc_days = set(steps_by_name["高濃度VC美容液"].get("use_days") or [])
        self.assertEqual(retinol_days & vc_days, set())

        # 解消後のuse_daysが週間表示にもそのまま反映されること
        plan = app.build_weekly_usage_plan(data)
        for day_entry in plan:
            retinol_shown = any("レチノール美容液" in x for x in day_entry["night"])
            vc_shown = any("高濃度VC美容液" in x for x in day_entry["night"])
            self.assertFalse(retinol_shown and vc_shown, day_entry["day"])
            if retinol_shown:
                self.assertIn(day_entry["day"], retinol_days)
            if vc_shown:
                self.assertIn(day_entry["day"], vc_days)

    def test_multiple_irritant_serums_use_days_all_reflected_correctly(self):
        data = _empty_data(night={"steps": [
            {"category": "美容液", "product": "レチノール美容液", "use_days": ["月", "木"], "ingredient_focus": "retinol"},
            {"category": "美容液", "product": "BHA美容液", "use_days": ["火", "金"], "ingredient_focus": "bha"},
        ]})
        plan = app.build_weekly_usage_plan(data)
        by_day = {d["day"]: d["night"] for d in plan}
        self.assertTrue(any("レチノール美容液" in x for x in by_day["月"]))
        self.assertFalse(any("レチノール美容液" in x for x in by_day["火"]))
        self.assertTrue(any("BHA美容液" in x for x in by_day["火"]))
        self.assertFalse(any("BHA美容液" in x for x in by_day["月"]))


class StepOrderUnaffectedByUseDaysFixTests(unittest.TestCase):
    """C: CATEGORY_ORDER/use_timing/_serum_sub_sort_keyによる並び順が維持されること。"""

    def test_category_order_unaffected(self):
        data = _empty_data(night={"steps": [
            {"category": "美容液", "product": "美容液X", "use_days": ["月"], "ingredient_focus": "retinol"},
            {"category": "洗顔", "product": "洗顔料Y", "use_days": []},
            {"category": "化粧水", "product": "化粧水Z", "use_days": []},
            {"category": "クリーム", "product": "クリームW", "use_days": []},
            {"category": "ピーリング", "product": "ピーリングV", "use_days": ["土"]},
        ]})
        app.sort_steps(data)
        cats = [s["category"] for s in data["night"]["steps"]]
        self.assertEqual(cats, ["洗顔", "ピーリング", "化粧水", "美容液", "クリーム"])

    def test_weekly_plan_night_items_follow_sorted_step_order_on_display_day(self):
        data = _empty_data(night={"steps": [
            {"category": "美容液", "product": "美容液X", "use_days": ["月"], "ingredient_focus": ""},
            {"category": "洗顔", "product": "洗顔料Y", "use_days": []},
            {"category": "化粧水", "product": "化粧水Z", "use_days": []},
        ]})
        app.sort_steps(data)
        plan = app.build_weekly_usage_plan(data)
        monday = next(d for d in plan if d["day"] == "月")
        # 洗顔はNIGHT_EXCLUDE_CATEGORIESで非表示、化粧水→美容液の順で並ぶこと
        toner_idx = next(i for i, x in enumerate(monday["night"]) if "化粧水Z" in x)
        serum_idx = next(i for i, x in enumerate(monday["night"]) if "美容液X" in x)
        self.assertLess(toner_idx, serum_idx)

    def test_serum_sub_sort_key_irritant_after_hydrating(self):
        irritant = {"category": "美容液", "ingredient_focus": "retinol", "texture": "essence"}
        hydrating = {"category": "美容液", "ingredient_focus": "hyaluronic_acid", "texture": "essence"}
        self.assertGreater(
            app._serum_sub_sort_key(irritant),
            app._serum_sub_sort_key(hydrating),
        )


if __name__ == "__main__":
    unittest.main()
