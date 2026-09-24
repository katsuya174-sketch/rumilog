"""
週間ルーティン「このルーティンの理由」(前回指示G)のテスト。

resolve_weekly_care_day_conflicts / resolve_night_irritant_conflicts /
resolve_beauty_device_day_conflicts が実際に行った曜日調整・注意書き付与を
conflict_log引数へ記録し、build_weekly_usage_plan()がそのログだけを根拠に
各曜日のroutine_reasons(と曜日非依存のroutine_reason_notes)を組み立てる
ことを確認する。

重要な確認事項:
- 曜日決定・安全ロジック自体(use_daysの値)は変更していないこと。
- 表示される理由が、実際にresolverが行った調整と一致すること。
- conflictで移動/除外された曜日と、元々use_days対象外だった曜日を、
  reasonsの有無で区別できること(無い場合に理由を捏造しない)。
- routine_conflict_logが無い(古い履歴の再計算等)場合はreasonsが空になり、
  クラッシュしないこと。

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


class WeeklyCareConflictReasonCaseATests(unittest.TestCase):
    """ケースA: ピーリングが刺激成分の使用日と重複して移動した場合の理由。"""

    def test_reason_records_actual_move_and_matches_displayed_day(self):
        data = _empty_data(
            night={"steps": [
                {"category": "美容液", "product": "レチノール美容液", "use_days": ["土"], "ingredient_focus": "retinol"},
            ]},
            weekly_care=[
                {"category": "ピーリング", "product": "AHAピーリング", "use_days": ["土"], "ingredient_focus": "aha"},
            ],
        )
        log = []
        data = app.resolve_weekly_care_day_conflicts(data, conflict_log=log)

        self.assertEqual(len(log), 1)
        entry = log[0]
        self.assertEqual(entry["type"], "weekly_care_day_conflict_A")
        self.assertEqual(entry["product"], "AHAピーリング")
        self.assertEqual(entry["from_days"], ["土"])
        peeling_step = data["weekly_care"][0]
        self.assertEqual(entry["to_days"], peeling_step["use_days"])
        self.assertNotIn("土", peeling_step["use_days"])

        data["routine_conflict_log"] = log
        plan = app.build_weekly_usage_plan(data)
        new_day = peeling_step["use_days"][0]
        new_day_entry = next(d for d in plan if d["day"] == new_day)
        sat_entry = next(d for d in plan if d["day"] == "土")

        # 移動先の曜日には、実際に移動した理由が表示され、かつ
        # AHAピーリング自体もその曜日のspecial_careに実在すること
        # (理由と表示結果が矛盾しない)。
        self.assertTrue(any("AHAピーリング" in r for r in new_day_entry["routine_reasons"]))
        self.assertTrue(any("AHAピーリング" in x for x in new_day_entry["special_care"]))
        # 移動元(土)にはAHAピーリングは表示されないが、なぜ無いのか(conflict
        # で移動した)という理由自体は土にも添えられること
        # (「元々use_days対象外」と区別できるように)。
        self.assertFalse(any("AHAピーリング" in x for x in sat_entry["special_care"]))
        self.assertTrue(any("AHAピーリング" in r for r in sat_entry["routine_reasons"]))


class WeeklyCareConflictReasonCaseBTests(unittest.TestCase):
    """ケースB: 毎日使用の刺激成分がピーリング日を避けて調整された場合の理由。"""

    def test_reason_records_narrowed_days_for_daily_irritant(self):
        data = _empty_data(
            night={"steps": [
                {"category": "美容液", "product": "デイリーレチノール美容液", "use_days": [], "ingredient_focus": "retinol"},
            ]},
            weekly_care=[
                {"category": "ピーリング", "product": "AHAピーリング", "use_days": ["土"], "ingredient_focus": "aha"},
            ],
        )
        log = []
        data = app.resolve_weekly_care_day_conflicts(data, conflict_log=log)

        self.assertEqual(len(log), 1)
        entry = log[0]
        self.assertEqual(entry["type"], "night_irritant_narrowed_for_peeling_B")
        self.assertEqual(entry["product"], "デイリーレチノール美容液")
        self.assertEqual(entry["from_days"], [])
        retinol_step = data["night"]["steps"][0]
        self.assertEqual(entry["to_days"], retinol_step["use_days"])
        self.assertNotIn("土", retinol_step["use_days"])

        data["routine_conflict_log"] = log
        plan = app.build_weekly_usage_plan(data)
        sat_entry = next(d for d in plan if d["day"] == "土")
        mon_entry = next(d for d in plan if d["day"] == "月")

        # 土(除外された曜日)には理由が表示され、レチノール美容液は
        # 実際に表示されないこと。
        self.assertTrue(any("デイリーレチノール美容液" in r for r in sat_entry["routine_reasons"]))
        self.assertFalse(any("デイリーレチノール美容液" in x for x in sat_entry["night"]))
        # 月(引き続き使用する曜日)には表示されるが、この曜日自体は
        # 移動先ではないため(元々毎日使用可能な範囲内)、reasonsは空でよい。
        self.assertTrue(any("デイリーレチノール美容液" in x for x in mon_entry["night"]))


class NightIrritantPriorityConflictReasonTests(unittest.TestCase):
    """夜ルーティン内の刺激成分同士の優先度衝突が実際に移動した場合の理由。"""

    def test_reason_matches_actual_priority_move(self):
        data = _empty_data(night={"steps": [
            {"category": "美容液", "product": "レチノール美容液", "use_days": ["月", "水", "金"], "ingredient_focus": "retinol"},
            {"category": "美容液", "product": "高濃度VC美容液", "use_days": ["月", "水", "金"], "ingredient_focus": "vitamin_c"},
        ]})
        log = []
        data = app.resolve_night_irritant_conflicts(data, conflict_log=log)

        self.assertEqual(len(log), 1)
        entry = log[0]
        self.assertEqual(entry["type"], "night_irritant_priority_conflict")
        self.assertEqual(entry["product"], "高濃度VC美容液")
        self.assertIn("優先度がより高い", entry["reason_text"])
        vc_step = next(s for s in data["night"]["steps"] if s["product"] == "高濃度VC美容液")
        self.assertEqual(entry["to_days"], vc_step["use_days"])

        data["routine_conflict_log"] = log
        plan = app.build_weekly_usage_plan(data)
        for day_entry in plan:
            vc_shown = any("高濃度VC美容液" in x for x in day_entry["night"])
            if day_entry["day"] in entry["to_days"]:
                self.assertTrue(vc_shown, day_entry["day"])
                self.assertTrue(
                    any("高濃度VC美容液" in r for r in day_entry["routine_reasons"]),
                    day_entry["day"],
                )


class BeautyDeviceConflictReasonNoteTests(unittest.TestCase):
    """美容機器×レチノール/ピーリングの注意書きが理由ノートに記録されること。"""

    def test_device_conflict_note_recorded_and_matches_reason_field(self):
        data = _empty_data(
            night={"steps": [
                {"category": "美容液", "product": "レチノール美容液", "use_days": ["月"], "ingredient_focus": "retinol"},
            ]},
            beauty_devices=[
                {"device_type": "超音波洗浄", "product": "超音波洗浄機A"},
            ],
        )
        log = []
        data = app.resolve_beauty_device_day_conflicts(data, conflict_log=log)

        self.assertEqual(len(log), 1)
        entry = log[0]
        self.assertEqual(entry["type"], "beauty_device_conflict_note")
        self.assertIsNone(entry["from_days"])
        self.assertIsNone(entry["to_days"])
        device_item = data["beauty_devices"][0]
        self.assertIn(entry["reason_text"].split("は、", 1)[-1].rstrip("。"), device_item["reason"])

        data["routine_conflict_log"] = log
        app.build_weekly_usage_plan(data)
        self.assertIn(entry["reason_text"], data["routine_reason_notes"])


class NoFabricatedReasonForUseDaysDesignExclusionTests(unittest.TestCase):
    """conflictが一切発生していない曜日には、理由を捏造しないこと
    (=元々use_days対象外だっただけの曜日と、conflictで除外された曜日を、
    reasonsの有無で区別できること)。"""

    def test_days_without_any_conflict_have_empty_routine_reasons(self):
        data = _empty_data(night={"steps": [
            {"category": "美容液", "product": "週3美容液", "use_days": ["月", "水", "金"], "ingredient_focus": ""},
        ]})
        # conflict resolverを一切通していない(=衝突が起きていない)ケース。
        data["routine_conflict_log"] = []
        plan = app.build_weekly_usage_plan(data)
        for day_entry in plan:
            self.assertEqual(day_entry["routine_reasons"], [], day_entry["day"])

    def test_missing_conflict_log_key_does_not_crash_and_yields_no_reasons(self):
        """古い履歴の再計算等、routine_conflict_logがdataに無い場合でも
        安全に動作し、理由を捏造しないこと。"""
        data = _empty_data(night={"steps": [
            {"category": "美容液", "product": "美容液X", "use_days": ["火"], "ingredient_focus": "retinol"},
        ]})
        self.assertNotIn("routine_conflict_log", data)
        plan = app.build_weekly_usage_plan(data)
        for day_entry in plan:
            self.assertEqual(day_entry["routine_reasons"], [], day_entry["day"])
        self.assertEqual(data.get("routine_reason_notes"), [])


class ConflictResolversUnchangedWithoutConflictLogTests(unittest.TestCase):
    """conflict_log引数を渡さない既存呼び出し(後方互換)は、曜日決定ロジック
    自体に一切影響しないこと。"""

    def test_day_decisions_identical_with_and_without_conflict_log(self):
        def build():
            return _empty_data(
                night={"steps": [
                    {"category": "美容液", "product": "レチノール美容液", "use_days": ["土"], "ingredient_focus": "retinol"},
                ]},
                weekly_care=[
                    {"category": "ピーリング", "product": "AHAピーリング", "use_days": ["土"], "ingredient_focus": "aha"},
                ],
            )

        without_log = app.resolve_weekly_care_day_conflicts(build())
        with_log = app.resolve_weekly_care_day_conflicts(build(), conflict_log=[])

        self.assertEqual(
            without_log["weekly_care"][0]["use_days"],
            with_log["weekly_care"][0]["use_days"],
        )
        self.assertEqual(
            without_log["night"]["steps"][0]["use_days"],
            with_log["night"]["steps"][0]["use_days"],
        )


if __name__ == "__main__":
    unittest.main()
