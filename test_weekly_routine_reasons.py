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
            {"category": "美容液", "product": "高濃度VC美容液", "use_days": ["月", "水", "金"], "ingredient_focus": "vitamin_c",
             "ingredient_strength": {"vitamin_c": "high"}},
        ]})
        log = []
        data = app.resolve_night_irritant_conflicts(data, conflict_log=log)

        self.assertEqual(len(log), 1)
        entry = log[0]
        self.assertEqual(entry["type"], "night_irritant_priority_conflict")
        self.assertEqual(entry["product"], "高濃度VC美容液")
        # 「移動履歴」ではなく「なぜ同日にしないか→結果どう配置したか」を
        # 説明する文言になっていること。実際に競合した相手(レチノール
        # 美容液)の製品名を挙げ、内部タグ(retinoid等)は出さない。
        self.assertIn("レチノール美容液", entry["reason_text"])
        self.assertIn("別日にしています", entry["reason_text"])
        self.assertNotIn("retinoid", entry["reason_text"])
        self.assertNotIn("vitamin_c", entry["reason_text"])
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
        # 「最終配置理由」(なぜ同日にしないか)を説明する文言になっている
        # こと。device_item["reason"](既存の「〜は使用を避けてください」
        # 注意書きフィールド)自体は今回変更していないので、両者が同じ
        # 文言である必要はない。
        self.assertIn("レチノール", entry["reason_text"])
        self.assertIn("同日使用にならないよう調整しています", entry["reason_text"])
        self.assertIn("レチノールを使用する日は使用を避けてください", device_item["reason"])

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


class PeelingHighConcentrationVitaminCConflictReasonTests(unittest.TestCase):
    """ピーリング×高濃度ビタミンCの曜日衝突(週ケア↔夜ステップ間)について、
    実際の既存ロジックの対応範囲を確認する。

    重要な発見(今回のSTOP対象): resolve_weekly_care_day_conflicts()が
    週ケア(ピーリング)と夜の刺激成分の衝突を検出する際に使う
    _IRRITANT_FOCUS_TAGS = {"retinoid","retinol","retinal","aha_bha","aha",
    "bha","pha"} には vitamin_c/azelaic_acid が含まれておらず、ピーリング×
    高濃度VC・ピーリング×アゼライン酸の週ケア↔夜ステップ間衝突は現状
    検出・解消されない(resolve_night_irritant_conflicts側の優先度グループ
    はvitamin_c/azelaic_acidを含むが、これは夜ステップ同士の衝突専用で、
    週ケア(weekly_care)のピーリングとは比較しない)。_IRRITANT_FOCUS_TAGSへ
    vitamin_c/azelaic_acidを追加すれば検出できるが、それは既存の曜日安全
    ロジック自体の変更(どの組み合わせを衝突とみなすか)にあたるため、
    今回のタスクでは変更せず、ユーザーへ報告のうえ承認を待つ
    (「根拠不明な理由を捏造しない」の原則により、現状は衝突ログが
    作られないことをそのまま確認するテストとする)。"""

    def test_peeling_and_high_concentration_vc_conflict_is_not_yet_detected(self):
        data = _empty_data(
            night={"steps": [
                {"category": "美容液", "product": "高濃度VC美容液", "use_days": ["土"], "ingredient_focus": "vitamin_c"},
            ]},
            weekly_care=[
                {"category": "ピーリング", "product": "AHAピーリング", "use_days": ["土"], "ingredient_focus": "aha_bha"},
            ],
        )
        log = []
        data = app.resolve_weekly_care_day_conflicts(data, conflict_log=log)
        # 現状の_IRRITANT_FOCUS_TAGSの範囲では、ピーリングと高濃度VCは
        # 衝突として検出されない(=use_daysは変更されず、conflict_logにも
        # 何も記録されない)。曜日安全ロジック自体は今回変更していないため、
        # この挙動が「正しい現状」であることを確認する。
        self.assertEqual(log, [])
        self.assertEqual(data["weekly_care"][0]["use_days"], ["土"])
        self.assertEqual(data["night"]["steps"][0]["use_days"], ["土"])


class BeautyDevicePeelingConflictReasonTests(unittest.TestCase):
    """美容機器×ピーリングの曜日衝突でも、最終配置理由がユーザー向けの
    文章になること(前回はレチノールのみテスト済み)。"""

    def test_device_peeling_conflict_reason_and_note(self):
        data = _empty_data(
            weekly_care=[
                {"category": "ピーリング", "product": "AHAピーリング", "use_days": ["土"], "ingredient_focus": "aha"},
            ],
            beauty_devices=[
                {"device_type": "RF", "product": "RF美顔器A"},
            ],
        )
        log = []
        data = app.resolve_beauty_device_day_conflicts(data, conflict_log=log)

        self.assertEqual(len(log), 1)
        entry = log[0]
        self.assertEqual(entry["product"], "RF美顔器A")
        self.assertEqual(entry["conflicts_with"], ["ピーリング"])
        self.assertIn("ピーリング", entry["reason_text"])
        self.assertIn("同日使用にならないよう調整しています", entry["reason_text"])
        device_item = data["beauty_devices"][0]
        self.assertIn("ピーリングを行う日は使用を避けてください", device_item["reason"])


class InternalIdentifiersNeverLeakToUserTextTests(unittest.TestCase):
    """週間ルーティンの理由文に、内部タグ・rule ID・デバッグ表現が
    そのまま表示されないこと。"""

    _FORBIDDEN_TOKENS = [
        "retinoid", "retinol", "retinal",
        "vitamin_c", "strong_vitamin_c", "high_concentration_vitamin_c",
        "azelaic_acid",
        "aha_bha",
        "sensitive_ok=yes", "sensitive_ok",
        "rule_id", "rule:",
        "_A", "_B",  # conflict_logの内部type名の断片
        "weekly_care_day_conflict", "night_irritant_priority_conflict",
        "night_irritant_narrowed_for_peeling", "beauty_device_conflict_note",
    ]

    def test_no_internal_tokens_in_any_generated_reason_text(self):
        data = _empty_data(
            night={"steps": [
                {"category": "美容液", "product": "レチノール美容液", "use_days": ["土"], "ingredient_focus": "retinol"},
                {"category": "美容液", "product": "高濃度VC美容液", "use_days": [], "ingredient_focus": "vitamin_c"},
                {"category": "美容液", "product": "アゼライン酸美容液", "use_days": [], "ingredient_focus": "azelaic_acid"},
            ]},
            weekly_care=[
                {"category": "ピーリング", "product": "AHAピーリング", "use_days": ["土"], "ingredient_focus": "aha"},
            ],
            beauty_devices=[
                {"device_type": "超音波洗浄", "product": "超音波洗浄機A"},
            ],
        )
        log = []
        data = app.resolve_weekly_care_day_conflicts(data, conflict_log=log)
        data = app.resolve_night_irritant_conflicts(data, conflict_log=log)
        data = app.resolve_beauty_device_day_conflicts(data, conflict_log=log)
        data["routine_conflict_log"] = log

        self.assertTrue(len(log) > 0)
        plan = app.build_weekly_usage_plan(data)

        all_texts = list(data["routine_reason_notes"])
        for day_entry in plan:
            all_texts.extend(day_entry["routine_reasons"])

        self.assertTrue(all_texts)
        for text in all_texts:
            for token in self._FORBIDDEN_TOKENS:
                self.assertNotIn(token, text, f"internal token {token!r} leaked into {text!r}")


class DuplicateReasonSuppressedInWeeklySummaryTests(unittest.TestCase):
    """同じ安全判断によって複数曜日に同じ説明が出る場合、routine_reason_notes
    (週全体のユーザー表示欄)では1件にまとめること。曜日ごとの内部ログ
    (routine_reasons)はそのまま保持してよい。"""

    def test_same_reason_appears_once_in_weekly_summary_but_per_day_log_kept(self):
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
        data["routine_conflict_log"] = log
        plan = app.build_weekly_usage_plan(data)

        # 移動元(土)・移動先の両方に同じreason_textが付くため、
        # 曜日ごとのrouine_reasonsには両方に現れてよい。
        reason_text = log[0]["reason_text"]
        days_with_reason = [d["day"] for d in plan if reason_text in d["routine_reasons"]]
        self.assertEqual(len(days_with_reason), 2)

        # だが週全体のサマリでは1回だけにまとめること。
        self.assertEqual(data["routine_reason_notes"].count(reason_text), 1)


class FinalWeeklyPlanMatchesConflictLogAndReasonsTests(unittest.TestCase):
    """複数の競合が同時に起きる現実的なケースで、最終的な週間プラン・
    このルーティンの理由・conflict_logの3つが矛盾しないこと。"""

    def test_multiple_conflicts_stay_consistent_end_to_end(self):
        data = _empty_data(
            night={"steps": [
                {"category": "美容液", "product": "レチノール美容液", "use_days": ["月", "水", "金"], "ingredient_focus": "retinol"},
                {"category": "美容液", "product": "高濃度VC美容液", "use_days": ["月", "水", "金"], "ingredient_focus": "vitamin_c",
                 "ingredient_strength": {"vitamin_c": "high"}},
            ]},
            weekly_care=[
                {"category": "ピーリング", "product": "AHAピーリング", "use_days": ["月"], "ingredient_focus": "aha"},
            ],
            beauty_devices=[
                {"device_type": "超音波洗浄", "product": "超音波洗浄機A"},
            ],
        )
        log = []
        data = app.resolve_weekly_care_day_conflicts(data, conflict_log=log)
        data = app.resolve_night_irritant_conflicts(data, conflict_log=log)
        data = app.resolve_beauty_device_day_conflicts(data, conflict_log=log)
        data["routine_conflict_log"] = log
        plan = app.build_weekly_usage_plan(data)

        peeling_step = data["weekly_care"][0]
        retinol_step = next(s for s in data["night"]["steps"] if s["product"] == "レチノール美容液")
        vc_step = next(s for s in data["night"]["steps"] if s["product"] == "高濃度VC美容液")

        by_day = {d["day"]: d for d in plan}

        # ピーリングが実際に表示される曜日にだけ special_care へ出て、
        # 元の曜日(月)には出ないこと。
        for day in peeling_step["use_days"]:
            self.assertTrue(any("AHAピーリング" in x for x in by_day[day]["special_care"]))
        self.assertFalse(any("AHAピーリング" in x for x in by_day["月"]["special_care"]))

        # レチノールと高濃度VCが同日に重ならないこと、かつ実際に表示される
        # 曜日と一致すること。
        self.assertEqual(set(retinol_step["use_days"]) & set(vc_step["use_days"]), set())
        for day in retinol_step["use_days"]:
            self.assertTrue(any("レチノール美容液" in x for x in by_day[day]["night"]))
        for day in vc_step["use_days"]:
            self.assertTrue(any("高濃度VC美容液" in x for x in by_day[day]["night"]))

        # 美容機器の注意書きが理由ノートに一致していること。
        device_reason_entries = [e for e in log if e["type"] == "beauty_device_conflict_note"]
        self.assertEqual(len(device_reason_entries), 1)
        self.assertIn(device_reason_entries[0]["reason_text"], data["routine_reason_notes"])

        # ルーティン全体の理由が空でないこと、かつ内部タグを含まないこと。
        self.assertTrue(data["routine_reason_notes"])
        for text in data["routine_reason_notes"]:
            self.assertNotIn("retinoid", text)
            self.assertNotIn("vitamin_c", text)


class VitaminCConcentrationDayConflictTests(unittest.TestCase):
    """2026-09の安全ロジック修正: Vitamin Cは既存フィールド
    ingredient_strength["vitamin_c"](infer_active_profile()と同じ
    フィールド・同じ閾値"high"/"strong")を見て、高濃度のみレチノール系
    との強制別日対象として扱い、通常濃度は対象外にすること。"""

    def test_regular_vitamin_c_is_not_forced_away_from_retinol(self):
        data = _empty_data(night={"steps": [
            {"category": "美容液", "product": "レチノール美容液", "use_days": ["月", "水", "金"], "ingredient_focus": "retinol"},
            {"category": "美容液", "product": "通常VC美容液", "use_days": ["月", "水", "金"], "ingredient_focus": "vitamin_c"},
        ]})
        log = []
        data = app.resolve_night_irritant_conflicts(data, conflict_log=log)
        self.assertEqual(log, [])
        vc_step = next(s for s in data["night"]["steps"] if s["product"] == "通常VC美容液")
        self.assertEqual(vc_step["use_days"], ["月", "水", "金"])

    def test_strong_vitamin_c_high_is_forced_away_from_retinol(self):
        data = _empty_data(night={"steps": [
            {"category": "美容液", "product": "レチノール美容液", "use_days": ["月", "水", "金"], "ingredient_focus": "retinol"},
            {"category": "美容液", "product": "高濃度VC美容液", "use_days": ["月", "水", "金"], "ingredient_focus": "vitamin_c",
             "ingredient_strength": {"vitamin_c": "high"}},
        ]})
        log = []
        data = app.resolve_night_irritant_conflicts(data, conflict_log=log)
        self.assertEqual(len(log), 1)
        vc_step = next(s for s in data["night"]["steps"] if s["product"] == "高濃度VC美容液")
        self.assertEqual(set(vc_step["use_days"]) & {"月", "水", "金"}, set())

    def test_strong_vitamin_c_strong_is_forced_away_from_retinol(self):
        """ingredient_strengthの値が"strong"表記の場合も高濃度として扱うこと。"""
        data = _empty_data(night={"steps": [
            {"category": "美容液", "product": "レチノール美容液", "use_days": ["月", "水", "金"], "ingredient_focus": "retinol"},
            {"category": "美容液", "product": "高濃度VC美容液", "use_days": ["月", "水", "金"], "ingredient_focus": "vitamin_c",
             "ingredient_strength": {"vitamin_c": "strong"}},
        ]})
        log = []
        data = app.resolve_night_irritant_conflicts(data, conflict_log=log)
        self.assertEqual(len(log), 1)
        vc_step = next(s for s in data["night"]["steps"] if s["product"] == "高濃度VC美容液")
        self.assertEqual(set(vc_step["use_days"]) & {"月", "水", "金"}, set())

    def test_missing_ingredient_strength_falls_back_to_regular_and_does_not_crash(self):
        data = _empty_data(night={"steps": [
            {"category": "美容液", "product": "レチノール美容液", "use_days": ["月"], "ingredient_focus": "retinol"},
            {"category": "美容液", "product": "VC美容液", "use_days": ["月"], "ingredient_focus": "vitamin_c"},
        ]})
        # ingredient_strengthキー自体が存在しない。
        self.assertNotIn("ingredient_strength", data["night"]["steps"][1])
        log = []
        data = app.resolve_night_irritant_conflicts(data, conflict_log=log)
        self.assertEqual(log, [])
        self.assertEqual(data["night"]["steps"][1]["use_days"], ["月"])

    def test_invalid_ingredient_strength_type_falls_back_to_regular_and_does_not_crash(self):
        """ingredient_strengthがdict以外(不正値)でもクラッシュせず、
        通常濃度として安全に扱うこと。"""
        for bad_value in [None, "high", ["vitamin_c", "high"], 123]:
            data = _empty_data(night={"steps": [
                {"category": "美容液", "product": "レチノール美容液", "use_days": ["月"], "ingredient_focus": "retinol"},
                {"category": "美容液", "product": "VC美容液", "use_days": ["月"], "ingredient_focus": "vitamin_c",
                 "ingredient_strength": bad_value},
            ]})
            log = []
            data = app.resolve_night_irritant_conflicts(data, conflict_log=log)
            self.assertEqual(log, [], f"bad_value={bad_value!r}")
            self.assertEqual(data["night"]["steps"][1]["use_days"], ["月"], f"bad_value={bad_value!r}")

    def test_unknown_strength_value_falls_back_to_regular(self):
        """"high"/"strong"以外の値(例: "low"/"medium"/未知の文字列)は
        高濃度として扱わないこと。"""
        for level in ["low", "medium", "とても高い", ""]:
            data = _empty_data(night={"steps": [
                {"category": "美容液", "product": "レチノール美容液", "use_days": ["月"], "ingredient_focus": "retinol"},
                {"category": "美容液", "product": "VC美容液", "use_days": ["月"], "ingredient_focus": "vitamin_c",
                 "ingredient_strength": {"vitamin_c": level}},
            ]})
            log = []
            data = app.resolve_night_irritant_conflicts(data, conflict_log=log)
            self.assertEqual(log, [], f"level={level!r}")


class AzelaicRetinoidConflictRemovedTests(unittest.TestCase):
    """2026-09の安全ロジック修正: アゼライン酸×レチノール系の一律強制
    別日を解除したこと(このペアをhard conflictとする判定源が無かった
    ため)。"""

    def test_azelaic_is_not_forced_away_from_retinol(self):
        data = _empty_data(night={"steps": [
            {"category": "美容液", "product": "レチノール美容液", "use_days": ["月", "水", "金"], "ingredient_focus": "retinol"},
            {"category": "美容液", "product": "アゼライン酸美容液", "use_days": ["月", "水", "金"], "ingredient_focus": "azelaic_acid"},
        ]})
        log = []
        data = app.resolve_night_irritant_conflicts(data, conflict_log=log)
        self.assertEqual(log, [])
        az_step = next(s for s in data["night"]["steps"] if s["product"] == "アゼライン酸美容液")
        self.assertEqual(az_step["use_days"], ["月", "水", "金"])

    def test_azelaic_still_conflicts_with_aha_bha_night_step(self):
        """アゼライン酸×AHA/BHA/PHA(夜ステップ同士)は引き続き競合対象
        であること(Geminiのmandatory hard avoid_combinationsに根拠あり、
        今回変更していない)。"""
        data = _empty_data(night={"steps": [
            {"category": "美容液", "product": "アゼライン酸美容液", "use_days": ["月"], "ingredient_focus": "azelaic_acid"},
            {"category": "美容液", "product": "BHA美容液", "use_days": ["月"], "ingredient_focus": "bha"},
        ]})
        log = []
        data = app.resolve_night_irritant_conflicts(data, conflict_log=log)
        self.assertEqual(len(log), 1)
        bha_step = next(s for s in data["night"]["steps"] if s["product"] == "BHA美容液")
        self.assertNotIn("月", bha_step["use_days"])

    def test_azelaic_still_conflicts_with_strong_vitamin_c(self):
        """アゼライン酸×高濃度VCは引き続き競合対象であること(Geminiの
        mandatory hard avoid_combinationsに根拠あり)。"""
        data = _empty_data(night={"steps": [
            {"category": "美容液", "product": "高濃度VC美容液", "use_days": ["月"], "ingredient_focus": "vitamin_c",
             "ingredient_strength": {"vitamin_c": "high"}},
            {"category": "美容液", "product": "アゼライン酸美容液", "use_days": ["月"], "ingredient_focus": "azelaic_acid"},
        ]})
        log = []
        data = app.resolve_night_irritant_conflicts(data, conflict_log=log)
        self.assertEqual(len(log), 1)
        az_step = next(s for s in data["night"]["steps"] if s["product"] == "アゼライン酸美容液")
        self.assertNotIn("月", az_step["use_days"])


class AzelaicPeelingNoNewConflictTests(unittest.TestCase):
    """アゼライン酸×ピーリング(weekly_care)は今回新たなhard conflictに
    しないこと(_IRRITANT_FOCUS_TAGSは変更していない)。"""

    def test_azelaic_and_peeling_do_not_conflict_across_weekly_care(self):
        data = _empty_data(
            night={"steps": [
                {"category": "美容液", "product": "アゼライン酸美容液", "use_days": ["土"], "ingredient_focus": "azelaic_acid"},
            ]},
            weekly_care=[
                {"category": "ピーリング", "product": "AHAピーリング", "use_days": ["土"], "ingredient_focus": "aha_bha"},
            ],
        )
        log = []
        data = app.resolve_weekly_care_day_conflicts(data, conflict_log=log)
        self.assertEqual(log, [])
        self.assertEqual(data["weekly_care"][0]["use_days"], ["土"])
        self.assertEqual(data["night"]["steps"][0]["use_days"], ["土"])


class RetinoidPeelingConflictStillEnforcedTests(unittest.TestCase):
    """レチノール系×ピーリング(weekly_care)は従来どおり競合として維持
    されること(今回変更していない_IRRITANT_FOCUS_TAGS/
    resolve_weekly_care_day_conflictsの回帰確認)。"""

    def test_retinol_and_peeling_still_conflict(self):
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
        self.assertNotIn("土", data["weekly_care"][0]["use_days"])


class BeautyDeviceRegressionAfterIrritantFixTests(unittest.TestCase):
    """美容機器×レチノール/ピーリングの既存ルールが今回の修正で回帰
    していないこと。"""

    def test_device_retinol_conflict_unaffected(self):
        data = _empty_data(
            night={"steps": [
                {"category": "美容液", "product": "レチノール美容液", "use_days": ["月"], "ingredient_focus": "retinol"},
            ]},
            beauty_devices=[{"device_type": "超音波洗浄", "product": "超音波洗浄機A"}],
        )
        log = []
        data = app.resolve_beauty_device_day_conflicts(data, conflict_log=log)
        self.assertEqual(len(log), 1)
        self.assertIn("レチノールを使用する日は使用を避けてください", data["beauty_devices"][0]["reason"])

    def test_device_peeling_conflict_unaffected(self):
        data = _empty_data(
            weekly_care=[
                {"category": "ピーリング", "product": "AHAピーリング", "use_days": ["土"], "ingredient_focus": "aha"},
            ],
            beauty_devices=[{"device_type": "RF", "product": "RF美顔器A"}],
        )
        log = []
        data = app.resolve_beauty_device_day_conflicts(data, conflict_log=log)
        self.assertEqual(len(log), 1)
        self.assertIn("ピーリングを行う日は使用を避けてください", data["beauty_devices"][0]["reason"])


class NoFabricatedReasonForRemovedConflictsTests(unittest.TestCase):
    """解除した競合(通常濃度VC×レチノール、アゼライン酸×レチノール)に
    ついて、架空の「別日にしています」等の理由が出ないこと。conflict_logに
    記録が無い=build_weekly_usage_plan側でも理由が生成されないことを
    週間プラン全体で確認する。"""

    def test_no_fabricated_reason_for_regular_vc_and_azelaic_vs_retinol(self):
        data = _empty_data(night={"steps": [
            {"category": "美容液", "product": "レチノール美容液", "use_days": ["月"], "ingredient_focus": "retinol"},
            {"category": "美容液", "product": "通常VC美容液", "use_days": ["月"], "ingredient_focus": "vitamin_c"},
            {"category": "美容液", "product": "アゼライン酸美容液", "use_days": ["月"], "ingredient_focus": "azelaic_acid"},
        ]})
        log = []
        data = app.resolve_weekly_care_day_conflicts(data, conflict_log=log)
        data = app.resolve_night_irritant_conflicts(data, conflict_log=log)
        data = app.resolve_beauty_device_day_conflicts(data, conflict_log=log)
        self.assertEqual(log, [])

        data["routine_conflict_log"] = log
        plan = app.build_weekly_usage_plan(data)
        mon_entry = next(d for d in plan if d["day"] == "月")
        self.assertEqual(mon_entry["routine_reasons"], [])
        self.assertEqual(data["routine_reason_notes"], [])
        # 3製品とも指定通り月曜に表示されること(架空の別日移動が起きていない)。
        self.assertTrue(any("レチノール美容液" in x for x in mon_entry["night"]))
        self.assertTrue(any("通常VC美容液" in x for x in mon_entry["night"]))
        self.assertTrue(any("アゼライン酸美容液" in x for x in mon_entry["night"]))


if __name__ == "__main__":
    unittest.main()
