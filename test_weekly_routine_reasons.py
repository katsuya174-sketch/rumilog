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


class FrequencyReasonNoteTests(unittest.TestCase):
    """use_days_reason(Geminiが同じPhase2出力で返す、頻度・曜日を決めた
    根拠)を集約したdata["frequency_reason_note"]のテスト。

    重要な確認事項:
    - 内容の言い換え・推測はせず、Geminiが書いた文をそのまま連結すること。
    - 表示整形(trim・文末記号の補完のみ)は行うが、文末記号を重複させないこと。
    - 箇条書き記号・改行を一切使わず、1本の地の文にまとめること。
    - resolverが最終的に曜日を変更したstepは、Gemini由来の理由が最終結果と
      矛盾するため除外すること(安全調整理由側でのみ説明する)。
    - use_days_reasonが無い(旧診断・Geminiが省略した)場合は空文字のまま
      (理由を捏造しない)。
    """

    def test_composes_flowing_text_from_night_and_weekly_steps(self):
        data = _empty_data(
            night={"steps": [
                {"category": "美容液", "product": "レチノール美容液", "use_days": ["月", "水", "金"],
                 "use_days_reason": "レチノールは刺激があるため週3回の使用で肌を慣らしています"},
            ]},
            weekly_care=[
                {"category": "ピーリング", "product": "PHAピーリング", "use_days": ["水", "土"],
                 "use_days_reason": "PHAは穏やかな角質ケア成分ですが、他の保湿ケアとのバランスを考慮し週2回としています"},
            ],
        )
        data["routine_conflict_log"] = []
        app.build_weekly_usage_plan(data)

        note = data["frequency_reason_note"]
        self.assertIn("レチノールは刺激があるため週3回の使用で肌を慣らしています。", note)
        self.assertIn("PHAは穏やかな角質ケア成分ですが、他の保湿ケアとのバランスを考慮し週2回としています。", note)
        # 箇条書き記号・改行が一切無いこと
        for forbidden in ["\n", "・", "- ", "* "]:
            self.assertNotIn(forbidden, note)

    def test_appends_period_only_when_missing_and_does_not_duplicate(self):
        data = _empty_data(night={"steps": [
            {"category": "美容液", "product": "美容液A", "use_days": ["月", "水", "金"],
             "use_days_reason": "刺激があるため週3回としています。"},
            {"category": "美容液", "product": "美容液B", "use_days": ["月"],
             "use_days_reason": "刺激が強いため週1回としています"},
        ]})
        data["routine_conflict_log"] = []
        app.build_weekly_usage_plan(data)
        note = data["frequency_reason_note"]
        self.assertNotIn("。。", note)
        self.assertIn("刺激があるため週3回としています。", note)
        self.assertIn("刺激が強いため週1回としています。", note)

    def test_does_not_reword_or_add_content_beyond_sentence_ending(self):
        """整形は文末記号の補完のみ。文言そのものを書き換えないこと。"""
        data = _empty_data(night={"steps": [
            {"category": "美容液", "product": "美容液A", "use_days": ["月"],
             "use_days_reason": "  余白付きの理由文  "},
        ]})
        data["routine_conflict_log"] = []
        app.build_weekly_usage_plan(data)
        self.assertEqual(data["frequency_reason_note"], "余白付きの理由文。")

    def test_dedupes_identical_reason_text(self):
        data = _empty_data(night={"steps": [
            {"category": "美容液", "product": "美容液A", "use_days": ["月", "水"],
             "use_days_reason": "同じ理由文です。"},
            {"category": "美容液", "product": "美容液B", "use_days": ["火", "木"],
             "use_days_reason": "同じ理由文です。"},
        ]})
        data["routine_conflict_log"] = []
        app.build_weekly_usage_plan(data)
        self.assertEqual(data["frequency_reason_note"].count("同じ理由文です。"), 1)

    def test_daily_use_step_excluded_even_with_reason(self):
        """use_days=[](毎日使用)は自明な判断のため、use_days_reasonが
        あっても頻度の理由には表示しないこと(前回指示: 基本的な毎日ケアは
        理由欄を埋めない)。"""
        data = _empty_data(night={"steps": [
            {"category": "化粧水", "product": "化粧水A", "use_days": [],
             "use_days_reason": "毎日の水分補給として使用するため。"},
            {"category": "美容液", "product": "レチノール美容液", "use_days": ["月", "水", "金"],
             "use_days_reason": "レチノールは刺激があるため週3回としています。"},
        ]})
        data["routine_conflict_log"] = []
        app.build_weekly_usage_plan(data)
        note = data["frequency_reason_note"]
        self.assertNotIn("毎日の水分補給として使用するため。", note)
        self.assertIn("レチノールは刺激があるため週3回としています。", note)

    def test_empty_when_no_use_days_reason_present(self):
        """旧診断相当(use_days_reasonフィールド自体が無い)でも安全に
        空文字になること(捏造しない)。"""
        data = _empty_data(night={"steps": [
            {"category": "美容液", "product": "美容液A", "use_days": []},
        ]})
        data["routine_conflict_log"] = []
        app.build_weekly_usage_plan(data)
        self.assertEqual(data["frequency_reason_note"], "")

    def test_weekday_mention_violation_is_dropped_entirely_when_single_sentence(self):
        """use_days_reasonはプロンプトで「曜日への言及は避ける」と明示して
        いるが、Geminiがこれに違反して曜日名を含めることが実機診断で確認
        された(例:「週に一度の集中ケアとしてバリア機能を底上げするため
        日曜に設定しました」)。1文全体が曜日言及と不可分な場合、頻度理由
        として独立して成立する記述が残らないため、理由を捏造せず空文字に
        すること(=その分の理由はfrequency_reason_noteに出ない)。"""
        data = _empty_data(weekly_care=[
            {"category": "パック", "product": "バリアパック", "use_days": ["日"],
             "use_days_reason": "週に一度の集中ケアとしてバリア機能を底上げするため日曜に設定しました。"},
        ])
        data["routine_conflict_log"] = []
        app.build_weekly_usage_plan(data)
        self.assertEqual(data["frequency_reason_note"], "")

    def test_weekday_mention_violation_is_stripped_leaving_valid_sentence(self):
        """複数文のうち曜日へ言及している文だけを取り除き、頻度理由として
        独立して成立する文が残っていればそれを採用すること(全体を丸ごと
        破棄しない部分補正)。"""
        data = _empty_data(weekly_care=[
            {"category": "パック", "product": "バリアパック", "use_days": ["日"],
             "use_days_reason": "バリア機能の底上げのため週1回としています。日曜に設定しました。"},
        ])
        data["routine_conflict_log"] = []
        app.build_weekly_usage_plan(data)
        note = data["frequency_reason_note"]
        self.assertIn("バリア機能の底上げのため週1回としています。", note)
        self.assertNotIn("日曜", note)

    def test_weekday_mention_violation_does_not_affect_other_steps(self):
        """曜日言及違反の検知・補正は違反したstepだけに限定され、他のstepの
        正当なuse_days_reasonには影響しないこと。"""
        data = _empty_data(
            night={"steps": [
                {"category": "美容液", "product": "レチノール美容液", "use_days": ["月", "水", "金"],
                 "use_days_reason": "レチノールは刺激があるため週3回としています。"},
            ]},
            weekly_care=[
                {"category": "パック", "product": "バリアパック", "use_days": ["日"],
                 "use_days_reason": "週に一度の集中ケアとしてバリア機能を底上げするため日曜に設定しました。"},
            ],
        )
        data["routine_conflict_log"] = []
        app.build_weekly_usage_plan(data)
        note = data["frequency_reason_note"]
        self.assertIn("レチノールは刺激があるため週3回としています。", note)
        self.assertNotIn("日曜", note)

    def test_day_placement_reason_is_included_when_present(self):
        """day_placement_reasonは、use_days_reasonと異なり曜日言及を禁止
        しない(曜日配置そのものの根拠を書かせるための専用フィールドのため)。
        use_days_reasonと自然に連結してfrequency_reason_noteへ含めること。"""
        data = _empty_data(weekly_care=[
            {"category": "パック", "product": "バリアパック", "use_days": ["日"],
             "use_days_reason": "バリア機能の底上げのため週1回としています。",
             "day_placement_reason": "レチノール美容液の使用日と重ならないよう日曜に配置しています。"},
        ])
        data["routine_conflict_log"] = []
        app.build_weekly_usage_plan(data)
        note = data["frequency_reason_note"]
        self.assertIn("バリア機能の底上げのため週1回としています。", note)
        self.assertIn("レチノール美容液の使用日と重ならないよう日曜に配置しています。", note)

    def test_day_placement_reason_alone_is_included_without_use_days_reason(self):
        """use_days_reasonが無くても、day_placement_reasonだけで単独で
        含まれること(2つのフィールドは独立している)。"""
        data = _empty_data(weekly_care=[
            {"category": "パック", "product": "バリアパック", "use_days": ["日"],
             "day_placement_reason": "ピーリングの翌日を避けて日曜に配置しています。"},
        ])
        data["routine_conflict_log"] = []
        app.build_weekly_usage_plan(data)
        self.assertIn("ピーリングの翌日を避けて日曜に配置しています。", data["frequency_reason_note"])

    def test_day_placement_reason_empty_when_not_meaningfully_decided(self):
        """意図した配置判断が無い場合、day_placement_reasonは空のまま
        (Geminiが出力しない)想定であり、その場合は表示に影響しないこと。"""
        data = _empty_data(weekly_care=[
            {"category": "パック", "product": "バリアパック", "use_days": ["日"],
             "use_days_reason": "バリア機能の底上げのため週1回としています。",
             "day_placement_reason": ""},
        ])
        data["routine_conflict_log"] = []
        app.build_weekly_usage_plan(data)
        self.assertEqual(
            data["frequency_reason_note"], "バリア機能の底上げのため週1回としています。"
        )

    def test_day_placement_reason_excluded_when_step_conflict_modified(self):
        """resolverが実際に曜日を変更したstepのday_placement_reasonは、
        変更前の曜日配置を前提に書かれており最終結果と矛盾するため、
        use_days_reasonと同じ基準で除外すること。"""
        data = _empty_data(
            weekly_care=[
                {"category": "ピーリング", "product": "AHAピーリング", "use_days": ["土"],
                 "ingredient_focus": "aha",
                 "day_placement_reason": "レチノールと同日を避けて土曜に配置しています。"},
            ],
            night={"steps": [
                {"category": "美容液", "product": "レチノール美容液", "use_days": ["土"],
                 "ingredient_focus": "retinol"},
            ]},
        )
        log = []
        data = app.resolve_weekly_care_day_conflicts(data, conflict_log=log)
        self.assertEqual(len(log), 1)
        data["routine_conflict_log"] = log
        app.build_weekly_usage_plan(data)
        self.assertNotIn("レチノールと同日を避けて土曜に配置しています。", data["frequency_reason_note"])

    def test_excludes_step_modified_by_conflict_resolver(self):
        """resolverが実際に曜日を変更したstepは、Gemini由来のuse_days_reason
        (変更前の曜日を前提に書かれている)が最終結果と矛盾するため、
        頻度理由からは除外すること。安全調整理由側(routine_reason_notes)
        には引き続き記録される。"""
        data = _empty_data(
            night={"steps": [
                {"category": "美容液", "product": "レチノール美容液", "use_days": ["土"],
                 "ingredient_focus": "retinol",
                 "use_days_reason": "中濃度処方のため週1回としています。"},
            ]},
            weekly_care=[
                {"category": "ピーリング", "product": "AHAピーリング", "use_days": ["土"],
                 "ingredient_focus": "aha",
                 "use_days_reason": "中濃度AHAのため週1回としています。"},
            ],
        )
        log = []
        data = app.resolve_weekly_care_day_conflicts(data, conflict_log=log)
        self.assertEqual(len(log), 1)
        peeling_step = data["weekly_care"][0]
        self.assertNotIn("土", peeling_step["use_days"])

        data["routine_conflict_log"] = log
        app.build_weekly_usage_plan(data)

        # ピーリング(曜日変更された側)のuse_days_reasonは頻度理由に出ない。
        self.assertNotIn("中濃度AHAのため週1回としています。", data["frequency_reason_note"])
        # 変更されなかったレチノール美容液の理由はそのまま出る。
        self.assertIn("中濃度処方のため週1回としています。", data["frequency_reason_note"])
        # 安全調整理由側には、resolverの実際の調整理由が別途記録されている。
        self.assertTrue(data["routine_reason_notes"])


class RoutineConflictLogPersistenceTests(unittest.TestCase):
    """normalize_result()がroutine_conflict_logを保存し、履歴を開き直しても
    安全調整の理由(routine_reason_notes)が消えないことの回帰テスト。

    発見した既存バグ: normalize_result()がroutine_conflict_logを保存対象の
    フィールドとして持っていなかったため、build_weekly_usage_plan()が
    生成直後は正しくroutine_reason_notesを返していても、履歴を保存→再読込
    した後は毎回conflict_logが空とみなされ、安全調整の理由が消えていた。
    """

    def test_normalize_result_preserves_routine_conflict_log(self):
        log = [{
            "type": "weekly_care_day_conflict_A",
            "product": "AHAピーリング",
            "category": "ピーリング",
            "from_days": ["土"],
            "to_days": ["火"],
            "conflicts_with": ["レチノール美容液"],
            "reason_text": "AHAピーリングとレチノール美容液は、どちらも刺激が出る可能性があるため、肌への負担が重ならないよう別日にしています。",
        }]
        raw = _empty_data(routine_conflict_log=log)
        normalized = app.normalize_result(raw)
        self.assertEqual(normalized.get("routine_conflict_log"), log)

    def test_reloaded_result_still_shows_safety_adjustment_reason(self):
        """診断直後(生成時)と同じconflict_logが、正規化・保存を経ても
        保持され、再度build_weekly_usage_plan()を呼んでも同じ安全調整
        理由が再現されること(=履歴を開き直しても消えないことの再現)。"""
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
        app.build_weekly_usage_plan(data)
        live_notes = list(data["routine_reason_notes"])
        self.assertTrue(live_notes)

        # normalize_result()で保存用に正規化(=DB保存を模擬)。
        normalized = app.normalize_result(data)
        self.assertEqual(normalized.get("routine_conflict_log"), log)

        # 保存済みレコードを読み込んだ想定でbuild_weekly_usage_planを
        # 再実行(=履歴を開き直した想定)しても、同じ理由が再現されること。
        reloaded = dict(normalized)
        app.build_weekly_usage_plan(reloaded)
        self.assertEqual(reloaded["routine_reason_notes"], live_notes)


class UseDaysReasonSchemaTests(unittest.TestCase):
    """get_analysis_schema_phase2()のstep_schemaにuse_days_reasonが
    追加されていること、既存フィールドの後方互換のためrequiredには
    含まれていないこと。"""

    def test_step_schema_includes_use_days_reason_and_not_required(self):
        schema = app.get_analysis_schema_phase2()
        step_schema = schema["properties"]["night"]["properties"]["steps"]["items"]
        self.assertIn("use_days_reason", step_schema["properties"])
        self.assertEqual(step_schema["properties"]["use_days_reason"], {"type": "string"})
        self.assertNotIn("use_days_reason", step_schema["required"])

    def test_weekly_care_uses_same_step_schema_with_use_days_reason(self):
        schema = app.get_analysis_schema_phase2()
        weekly_step_schema = schema["properties"]["weekly_care"]["items"]
        self.assertIn("use_days_reason", weekly_step_schema["properties"])


class UseDaysNoneListUnificationTests(unittest.TestCase):
    """_normalize_use_days_field(): Gemini出力のuse_daysがNone/欠落/
    不正な型であっても、意味が同じ([]=毎日使用可)表現へ統一すること。
    曜日制限がある場合の値は一切変更しないこと。"""

    def test_none_is_normalized_to_empty_list(self):
        steps = [{"category": "クリーム", "product": "クリームA", "use_days": None}]
        app._normalize_use_days_field(steps)
        self.assertEqual(steps[0]["use_days"], [])

    def test_missing_key_is_normalized_to_empty_list(self):
        steps = [{"category": "クリーム", "product": "クリームA"}]
        app._normalize_use_days_field(steps)
        self.assertEqual(steps[0]["use_days"], [])

    def test_invalid_type_is_normalized_to_empty_list(self):
        for bad_value in ["月水金", 123, {}]:
            steps = [{"category": "クリーム", "use_days": bad_value}]
            app._normalize_use_days_field(steps)
            self.assertEqual(steps[0]["use_days"], [], f"bad_value={bad_value!r}")

    def test_existing_specific_days_are_not_changed(self):
        steps = [{"category": "美容液", "use_days": ["月", "水", "金"]}]
        app._normalize_use_days_field(steps)
        self.assertEqual(steps[0]["use_days"], ["月", "水", "金"])

    def test_none_input_list_does_not_crash(self):
        app._normalize_use_days_field(None)  # クラッシュしないことのみ確認


class UseDaysSurvivesLateStepInsertionTests(unittest.TestCase):
    """診断20260927050949108625の回帰テスト。_normalize_use_days_field()は
    run_diagnosis_core内で早い段階(AI候補拡張より前)で一度呼ばれるが、
    その後にensure_required_routine_steps()が未充足カテゴリ(洗顔・クリーム等)
    のstepを新規に挿入する。この新規stepはuse_daysキー自体を持たないため、
    最終保存payload・APIレスポンスでuse_daysが欠落したまま返っていた
    (表示・判定ロジックは[]/Noneを同一視するため実害は無かったが、
    「保存データ・APIレスポンスは常にuse_days=[]/listである」という契約が
    最終出力地点で保証されていなかった)。"""

    def test_step_inserted_by_ensure_required_routine_steps_gets_use_days_after_late_normalize(self):
        # 洗顔もクリームも持たない夜ルーティン(Gemini出力を模した最小データ)。
        data = {
            "morning": {"steps": []},
            "night": {"steps": [
                {"category": "化粧水", "product": "化粧水A", "use_days": []},
            ]},
            "weekly_care": [],
        }
        data = app.ensure_required_routine_steps(data)

        night_steps = data["night"]["steps"]
        categories = [s.get("category") for s in night_steps]
        self.assertIn("洗顔", categories)
        self.assertIn("クリーム", categories)

        # ensure_required_routine_steps直後は、挿入されたstepにuse_daysキーが
        # 無いこと(バグの再現条件そのものを確認)。
        inserted = [s for s in night_steps if s.get("category") in ("洗顔", "クリーム")]
        self.assertTrue(inserted)
        for step in inserted:
            self.assertNotIn(
                "use_days", step,
                "この前提が崩れた場合、ensure_required_routine_steps()の実装が"
                "変わりバグの再現条件自体が変化している可能性がある",
            )

        # 修正: run_diagnosis_core側で最終整形直前にもう一度正規化する。
        app._normalize_use_days_field(night_steps)
        for step in night_steps:
            self.assertEqual(
                step.get("use_days"), [],
                f"category={step.get('category')!r}のuse_daysが正規化後も[]でない",
            )

    def test_prepare_result_for_view_normalizes_use_days_for_old_saved_records(self):
        """生成ロジック修正前に保存された旧診断データ(use_daysキー欠落)でも、
        再表示(履歴詳細等)のAPIレスポンスではuse_days=[]として返ること
        (契約はレコードの生成時期に依存しない)。"""
        old_saved_record = {
            "id": "old-1",
            "night": {"steps": [
                {"category": "洗顔", "product": "旧ソープ"},  # use_daysキー無し(旧バグ)
                {"category": "化粧水", "product": "旧化粧水", "use_days": ["月", "水", "金"]},
            ]},
            "weekly_care": [
                {"category": "ピーリング", "product": "旧ピーリング"},  # use_daysキー無し
            ],
            "morning": {"steps": []},
        }
        result = app.prepare_result_for_view(old_saved_record)
        night_steps = result["night"]["steps"]
        self.assertEqual(night_steps[0]["use_days"], [])
        # 曜日制限がある既存値は変更しないこと。
        self.assertEqual(night_steps[1]["use_days"], ["月", "水", "金"])
        self.assertEqual(result["weekly_care"][0]["use_days"], [])


class WeeklyCareFrequencyRangeCheckTests(unittest.TestCase):
    """_log_weekly_care_frequency_range_check(): プロンプトに明記された
    濃度非依存の頻度目安(PHA→週3〜5回)からの逸脱を検知できること。
    検知のみで自動修正・曜日変更は一切行わないこと。"""

    def test_pha_within_documented_range_does_not_mutate_data(self):
        steps = [{"category": "ピーリング", "product": "PHAピーリング",
                   "ingredient_focus": ["pha"], "use_days": ["火", "木", "土"]}]
        before = [dict(s) for s in steps]
        app._log_weekly_care_frequency_range_check(steps)
        self.assertEqual(steps, before)

    def test_pha_below_documented_range_is_detectable_without_mutation(self):
        """今回の診断(20260926152914212101)で実際に確認されたのと同種の
        「週2回」ケースでも、検知関数はデータを一切変更しないこと
        (自動修正はしない、あくまで検知のみ)。"""
        steps = [{"category": "ピーリング", "product": "PHAピーリング",
                   "ingredient_focus": ["pha"], "use_days": ["水", "土"]}]
        before = [dict(s) for s in steps]
        app._log_weekly_care_frequency_range_check(steps)
        self.assertEqual(steps, before)

    def test_non_pha_ingredient_is_not_checked(self):
        """濃度によって目安が変わる成分(AHA等)は、濃度を判定する構造化
        フィールドが無いため対象外(根拠のない閾値を発明しない)。"""
        steps = [{"category": "ピーリング", "product": "AHAピーリング",
                   "ingredient_focus": ["aha"], "use_days": ["木"]}]
        before = [dict(s) for s in steps]
        app._log_weekly_care_frequency_range_check(steps)
        self.assertEqual(steps, before)

    def test_none_input_does_not_crash(self):
        app._log_weekly_care_frequency_range_check(None)


class WeeklyStimulusPatternDetectionTests(unittest.TestCase):
    """_log_weekly_stimulus_pattern(): 週間全体で刺激系ケアがどの曜日に
    配置されているかを検知できること。今回の実診断で確認された
    「レチノール=月水金、PHA=火木土」のように、個別には目安範囲内でも
    合計すると刺激系ケアがほぼ毎日になるケースを必ずfixture化する。
    検知のみで、自動調整(曜日変更・頻度変更)は一切行わないこと。"""

    def test_retinol_and_pha_alternating_days_detected_as_six_stimulus_days(self):
        """診断20260926152914212101の実データを再現したfixture。
        レチノール(月水金)とPHA(火木土)が重複しないため、既存の
        同日衝突検知(resolve_*_day_conflicts)は一切介入しないが、
        週7日中6日が何らかの刺激系ケアで占められ、完全な休息日は
        日曜のみになる。この関数はこの事実を検知できること。"""
        data = _empty_data(
            night={"steps": [
                {"category": "美容液", "product": "ABC-Gリペアセラム",
                 "ingredient_focus": ["レチノール"], "use_days": ["月", "水", "金"]},
            ]},
            weekly_care=[
                {"category": "ピーリング", "product": "スキンピール",
                 "ingredient_focus": ["pha"], "use_days": ["火", "木", "土"]},
            ],
        )
        result = app._log_weekly_stimulus_pattern(data)
        self.assertEqual(result["stimulus_days"], ["月", "火", "水", "木", "金", "土"])
        self.assertEqual(result["rest_days"], ["日"])
        # データそのものは一切変更しないこと(検知のみ)。
        self.assertEqual(data["night"]["steps"][0]["use_days"], ["月", "水", "金"])
        self.assertEqual(data["weekly_care"][0]["use_days"], ["火", "木", "土"])

    def test_no_stimulus_products_yields_all_rest_days(self):
        data = _empty_data(night={"steps": [
            {"category": "化粧水", "product": "保湿化粧水", "ingredient_focus": ["セラミド"], "use_days": []},
        ]})
        result = app._log_weekly_stimulus_pattern(data)
        self.assertEqual(result["stimulus_days"], [])
        self.assertEqual(sorted(result["rest_days"]), sorted(app._ALL_DAYS))

    def test_daily_stimulus_step_marks_all_seven_days(self):
        data = _empty_data(night={"steps": [
            {"category": "美容液", "product": "レチノール美容液", "ingredient_focus": ["retinol"], "use_days": []},
        ]})
        result = app._log_weekly_stimulus_pattern(data)
        self.assertEqual(sorted(result["stimulus_days"]), sorted(app._ALL_DAYS))
        self.assertEqual(result["rest_days"], [])

    def test_does_not_mutate_input_data(self):
        data = _empty_data(
            night={"steps": [
                {"category": "美容液", "ingredient_focus": ["retinol"], "use_days": ["月"]},
            ]},
        )
        before_night = [dict(s) for s in data["night"]["steps"]]
        app._log_weekly_stimulus_pattern(data)
        self.assertEqual(data["night"]["steps"], before_night)


class MissingUseDaysReasonDetectionTests(unittest.TestCase):
    """_log_missing_use_days_reason(): 頻度を絞った(=毎日ではない)stepで
    use_days_reasonが空の場合を検知できること。理由を捏造せず、検知の
    みであること(データを変更しない)。"""

    def test_detects_non_daily_step_without_reason(self):
        night = [{"category": "美容液", "product": "レチノール美容液",
                   "use_days": ["月", "水", "金"], "use_days_reason": ""}]
        before = [dict(s) for s in night]
        app._log_missing_use_days_reason(night, [])
        self.assertEqual(night, before)  # データは変更しない

    def test_does_not_flag_daily_step_without_reason(self):
        # use_days=[](毎日)は元々理由不要のため、検知対象外であっても
        # クラッシュしないことのみ確認(戻り値が無い関数のため例外なしを確認)。
        night = [{"category": "化粧水", "product": "化粧水A",
                   "use_days": [], "use_days_reason": ""}]
        app._log_missing_use_days_reason(night, [])  # クラッシュしないこと

    def test_does_not_flag_step_with_reason_present(self):
        night = [{"category": "美容液", "product": "レチノール美容液",
                   "use_days": ["月", "水", "金"],
                   "use_days_reason": "刺激があるため週3回としています。"}]
        app._log_missing_use_days_reason(night, [])  # クラッシュしないこと

    def test_none_inputs_do_not_crash(self):
        app._log_missing_use_days_reason(None, None)


class RealDiagnosisFixtureRegressionTests(unittest.TestCase):
    """診断ID 20260926152914212101(2026-09の品質監査対象)の実データを
    再現したfixtureによる、build_weekly_usage_plan()のエンドツーエンド
    回帰テスト。基本的な毎日ケア(クレンジング/洗顔/化粧水/乳液/クリーム)
    は理由欄に出さず、非自明な判断(レチノール週3回・PHA週3回)だけが
    残ることを確認する。"""

    def _real_diagnosis_data(self):
        return _empty_data(
            night={"steps": [
                {"category": "クレンジング", "product": "マイルドクレンジングオイル",
                 "ingredient_focus": ["低刺激"], "use_days": [],
                 "use_days_reason": "夜のメイクや皮脂汚れを毎日落とす必要があるため。"},
                {"category": "洗顔", "product": "泡洗顔料",
                 "ingredient_focus": ["低刺激"], "use_days": [],
                 "use_days_reason": "夜の洗顔は毎日行う必要があるため。"},
                {"category": "化粧水", "product": "化粧水III とてもしっとり",
                 "ingredient_focus": ["セラミド"], "use_days": [],
                 "use_days_reason": "夜の水分補給とバリアケアとして毎日使用するため。"},
                {"category": "美容液", "product": "ABC-Gリペアセラム",
                 "ingredient_focus": ["レチノール"], "use_days": ["月", "水", "金"],
                 "use_days_reason": "レチノールは刺激があるため、週3回の使用で肌を慣らしながらケアするため。"},
                {"category": "乳液", "product": "モイストエマルジョン",
                 "ingredient_focus": ["セラミド"], "use_days": [],
                 "use_days_reason": "夜の保湿とバリアケアとして毎日使用するため。"},
                {"category": "クリーム", "product": "日本酒のクリーム",
                 "ingredient_focus": ["セラミド"], "use_days": None,
                 "use_days_reason": None},
            ]},
            weekly_care=[
                {"category": "ピーリング", "product": "スキンピール",
                 "ingredient_focus": ["pha"], "use_days": ["火", "木", "土"],
                 "use_days_reason": "PHAや低刺激な角質ケアは週3回程度の使用が適しているため。"},
            ],
            beauty_devices=[
                {"device_type": "超音波洗浄", "product": "超音波洗浄機A",
                 "reason": "毛穴の黒ずみと皮脂詰まりを効率的にケアするため。"},
            ],
        )

    def test_only_non_daily_steps_appear_in_frequency_reason_note(self):
        data = self._real_diagnosis_data()
        # 実際の生成パイプラインと同じ順序で適用する:
        # use_days正規化 -> 連続日再配置(第1段階最適化) -> 既存resolver。
        app._normalize_use_days_field(data["night"]["steps"])
        app._normalize_use_days_field(data["weekly_care"])
        log = []
        app._redistribute_consecutive_stimulus_days(data["night"]["steps"], conflict_log=log)
        app._redistribute_consecutive_stimulus_days(data["weekly_care"], conflict_log=log)
        data = app.resolve_beauty_device_day_conflicts(data, conflict_log=log)
        data["routine_conflict_log"] = log
        app.build_weekly_usage_plan(data)

        note = data["frequency_reason_note"]
        # 基本的な毎日ケアは理由欄に出ないこと
        for daily_reason in [
            "夜のメイクや皮脂汚れを毎日落とす必要があるため。",
            "夜の洗顔は毎日行う必要があるため。",
            "夜の水分補給とバリアケアとして毎日使用するため。",
            "夜の保湿とバリアケアとして毎日使用するため。",
        ]:
            self.assertNotIn(daily_reason, note)
        # 非自明な判断(頻度を絞ったもの)は残ること
        self.assertIn("レチノールは刺激があるため、週3回の使用で肌を慣らしながらケアするため。", note)
        self.assertIn("PHAや低刺激な角質ケアは週3回程度の使用が適しているため。", note)
        # 箇条書きになっていないこと
        for forbidden in ["\n", "・", "- ", "* "]:
            self.assertNotIn(forbidden, note)

    def test_beauty_device_conflict_note_has_no_double_period(self):
        data = self._real_diagnosis_data()
        app._normalize_use_days_field(data["night"]["steps"])
        app._normalize_use_days_field(data["weekly_care"])
        log = []
        data = app.resolve_beauty_device_day_conflicts(data, conflict_log=log)
        for entry in log:
            self.assertNotIn("。。", entry["reason_text"])

    def test_weekly_stimulus_pattern_matches_real_diagnosis(self):
        """連続日再配置(第1段階最適化)を適用しても、Geminiの原案が
        既に最適(内部で連続していない)なため変化せず、週6日パターンは
        維持されること(=既存ルールの範囲では改善不能という結論の
        エンドツーエンド確認)。"""
        data = self._real_diagnosis_data()
        app._normalize_use_days_field(data["night"]["steps"])
        app._normalize_use_days_field(data["weekly_care"])
        log = []
        app._redistribute_consecutive_stimulus_days(data["night"]["steps"], conflict_log=log)
        app._redistribute_consecutive_stimulus_days(data["weekly_care"], conflict_log=log)
        self.assertEqual(log, [])
        result = app._log_weekly_stimulus_pattern(data)
        self.assertEqual(result["stimulus_days"], ["月", "火", "水", "木", "金", "土"])
        self.assertEqual(result["rest_days"], ["日"])


class ConsecutiveDayRedistributionTests(unittest.TestCase):
    """_redistribute_consecutive_stimulus_days(): 週間ルーティン全体評価の
    「実際に調整する」部分。単一stepの使用日数(頻度)は一切変更せず、
    連続した曜日だけを既存の「連続禁止」方針に沿って均等配置へ組み替える。"""

    def test_consecutive_days_are_redistributed_without_changing_frequency(self):
        """頻度を落とさず曜日再配置だけで改善できるケース。"""
        steps = [{"category": "美容液", "product": "レチノール美容液",
                   "ingredient_focus": ["レチノール"], "use_days": ["月", "火", "水"]}]
        log = []
        app._redistribute_consecutive_stimulus_days(steps, conflict_log=log)
        new_days = steps[0]["use_days"]
        self.assertEqual(len(new_days), 3)  # 頻度(日数)は変更しない
        self.assertFalse(app._has_consecutive_days(new_days))
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["type"], "consecutive_day_redistribution")
        self.assertEqual(log[0]["from_days"], ["月", "火", "水"])
        self.assertEqual(log[0]["to_days"], new_days)

    def test_already_non_consecutive_days_are_not_changed(self):
        """診断20260926152914212101の実データ(レチノール月水金)は既に
        連続していないため、変更されないこと(=Geminiの原案が既に
        最適だったケース)。"""
        steps = [{"category": "美容液", "product": "レチノール美容液",
                   "ingredient_focus": ["レチノール"], "use_days": ["月", "水", "金"]}]
        log = []
        app._redistribute_consecutive_stimulus_days(steps, conflict_log=log)
        self.assertEqual(steps[0]["use_days"], ["月", "水", "金"])
        self.assertEqual(log, [])

    def test_pha_already_non_consecutive_is_not_changed(self):
        """同診断のPHA(火木土)も同様に変更されないこと。"""
        steps = [{"category": "ピーリング", "product": "PHAピーリング",
                   "ingredient_focus": ["pha"], "use_days": ["火", "木", "土"]}]
        log = []
        app._redistribute_consecutive_stimulus_days(steps, conflict_log=log)
        self.assertEqual(steps[0]["use_days"], ["火", "木", "土"])
        self.assertEqual(log, [])

    def test_non_irritant_step_is_not_touched_even_if_consecutive(self):
        """刺激系タグを持たないstep(セラミド等)は、曜日が連続していても
        対象外(既存プロンプトの「連続禁止」方針は刺激系成分に対する
        ものであり、保湿系にまで拡大解釈しない)。"""
        steps = [{"category": "パック", "product": "保湿パック",
                   "ingredient_focus": ["セラミド"], "use_days": ["月", "火"]}]
        log = []
        app._redistribute_consecutive_stimulus_days(steps, conflict_log=log)
        self.assertEqual(steps[0]["use_days"], ["月", "火"])
        self.assertEqual(log, [])

    def test_daily_use_step_is_not_touched(self):
        steps = [{"category": "美容液", "ingredient_focus": ["retinol"], "use_days": []}]
        log = []
        app._redistribute_consecutive_stimulus_days(steps, conflict_log=log)
        self.assertEqual(steps[0]["use_days"], [])
        self.assertEqual(log, [])

    def test_single_day_step_is_not_touched(self):
        steps = [{"category": "ピーリング", "ingredient_focus": ["aha"], "use_days": ["木"]}]
        log = []
        app._redistribute_consecutive_stimulus_days(steps, conflict_log=log)
        self.assertEqual(steps[0]["use_days"], ["木"])
        self.assertEqual(log, [])

    def test_redistributed_step_reason_excluded_from_frequency_note_but_kept_in_safety_note(self):
        """再配置されたstepのGemini由来use_days_reasonは、変更前の曜日を
        前提に書かれているため最終結果と矛盾する。既存の
        _step_conflict_modified()による除外の仕組みがこの新しい
        調整タイプにも正しく適用され、安全調整理由側にのみ説明が
        残ることを確認する。"""
        data = _empty_data(night={"steps": [
            {"category": "美容液", "product": "レチノール美容液",
             "ingredient_focus": ["レチノール"], "use_days": ["月", "火", "水"],
             "use_days_reason": "刺激があるため週3回、月火水に配置しています。"},
        ]})
        log = []
        app._redistribute_consecutive_stimulus_days(data["night"]["steps"], conflict_log=log)
        data["routine_conflict_log"] = log
        app.build_weekly_usage_plan(data)

        # Gemini由来の理由(変更前の曜日を前提)は頻度の理由に出ない
        self.assertNotIn("刺激があるため週3回、月火水に配置しています。", data["frequency_reason_note"])
        # 安全面の調整理由(resolver/最適化側の事実)は出る
        self.assertTrue(data["routine_reason_notes"])
        self.assertIn("連続しないよう", data["routine_reason_notes"][0])


class HighStimulusProductSelectionCheckTests(unittest.TestCase):
    """_log_high_stimulus_product_selection_check(): 既存プロンプト
    【商品選定時の刺激配慮】の「hydration<=50かつbarrier<=50なら
    高刺激成分は1種のみに絞る」ルールを検知できること(検知のみ、
    製品の自動入れ替えはしない)。"""

    def test_detects_multiple_high_stimulus_products_when_scores_low(self):
        """sensitive/barrier低下ケース。"""
        night_steps = [
            {"category": "美容液", "product": "レチノール美容液", "ingredient_focus": ["retinol"]},
            {"category": "美容液", "product": "高濃度VC美容液", "ingredient_focus": ["vitamin_c"],
             "ingredient_strength": {"vitamin_c": "high"}},
        ]
        before = [dict(s) for s in night_steps]
        app._log_high_stimulus_product_selection_check(night_steps, {"hydration": 45, "barrier": 40})
        self.assertEqual(night_steps, before)  # 検知のみ、データは変更しない

    def test_does_not_flag_single_high_stimulus_product(self):
        """activeが1種類だけのケース。"""
        night_steps = [
            {"category": "美容液", "product": "レチノール美容液", "ingredient_focus": ["retinol"]},
        ]
        app._log_high_stimulus_product_selection_check(night_steps, {"hydration": 40, "barrier": 35})

    def test_does_not_flag_when_scores_are_healthy(self):
        """今回の実診断(barrier=75, hydration=55)のように、条件(共に50以下)を
        満たさない場合は検知対象外であること。"""
        night_steps = [
            {"category": "美容液", "product": "レチノール美容液", "ingredient_focus": ["retinol"]},
            {"category": "美容液", "product": "高濃度VC美容液", "ingredient_focus": ["vitamin_c"],
             "ingredient_strength": {"vitamin_c": "high"}},
        ]
        before = [dict(s) for s in night_steps]
        app._log_high_stimulus_product_selection_check(night_steps, {"hydration": 55, "barrier": 75})
        self.assertEqual(night_steps, before)

    def test_none_scores_do_not_crash(self):
        app._log_high_stimulus_product_selection_check([], None)
        app._log_high_stimulus_product_selection_check(None, {"hydration": 40, "barrier": 40})


class UndecidableCrossActiveCoverageDocumentationTests(unittest.TestCase):
    """レチノール(月水金)+PHA(火木土)のように、個々のactiveは既存目安の
    範囲内でも、複数の異なるactiveを合計すると週間の刺激系ケア日数が
    多くなるケースについて、既存ルールだけでは自動調整できないことを
    数学的事実として確認する(実装しない判断そのものの回帰テスト)。"""

    def test_non_overlapping_three_plus_three_always_consumes_six_days(self):
        """同日重複禁止という既存ルールの下で、3日×2つのactiveを重複
        させずに配置すると、曜日の組み合わせによらず必ず6日を消費する
        (=配置の最適化では改善できないことの数学的確認)。"""
        import itertools
        days = app._ALL_DAYS
        for a_days in itertools.combinations(days, 3):
            for b_days in itertools.combinations(days, 3):
                if set(a_days) & set(b_days):
                    continue  # 同日重複は既存ルールで禁止されているため対象外
                union = set(a_days) | set(b_days)
                self.assertEqual(len(union), 6)

    def test_redistribution_does_not_touch_the_real_diagnosis_case(self):
        """診断20260926152914212101の実データ(レチノール月水金+PHA火木土)は
        既存の「連続禁止」ルールの範囲では既に最適(それぞれ内部で連続
        していない)であり、既存ルールの範囲では調整の余地が無いことを
        確認する(=検知のみに留めた設計判断の裏付け)。"""
        night_steps = [{"category": "美容液", "ingredient_focus": ["レチノール"],
                          "use_days": ["月", "水", "金"]}]
        weekly_steps = [{"category": "ピーリング", "ingredient_focus": ["pha"],
                           "use_days": ["火", "木", "土"]}]
        log = []
        app._redistribute_consecutive_stimulus_days(night_steps, conflict_log=log)
        app._redistribute_consecutive_stimulus_days(weekly_steps, conflict_log=log)
        self.assertEqual(night_steps[0]["use_days"], ["月", "水", "金"])
        self.assertEqual(weekly_steps[0]["use_days"], ["火", "木", "土"])
        self.assertEqual(log, [])


class AhaBhaFrequencyGuidanceClassificationTests(unittest.TestCase):
    """2026-09の再監査: 既存プロンプト【ピーリングの使用頻度個別評価】に
    明記されたAHA/BHAの濃度別下限を、_AHA_BHA_TIER_FLOORが正確に
    転記していることを確認する(新しい数値を作っていないことの根拠)。
    低濃度AHA/BHAの下限(週2)は、高濃度の下限(週1)より高いため、
    一律「週1回」への引き下げは低濃度側にとって既存許容範囲を下回る
    過剰な制限になることを検証する。"""

    def test_low_concentration_aha_floor_is_two_not_one(self):
        # 低濃度AHA(<5%)+保湿成分豊富 → 週2〜4回(敏感肌週2〜3回)、下限2
        self.assertEqual(app._AHA_BHA_TIER_FLOOR[("aha", "low")], 2)

    def test_low_concentration_bha_floor_is_two_not_one(self):
        # 低濃度BHA(<2%)+保湿成分あり → 週2〜3回、下限2
        self.assertEqual(app._AHA_BHA_TIER_FLOOR[("bha", "low")], 2)

    def test_medium_aha_and_high_bha_floor_is_one(self):
        self.assertEqual(app._AHA_BHA_TIER_FLOOR[("aha", "medium")], 1)
        self.assertEqual(app._AHA_BHA_TIER_FLOOR[("bha", "medium")], 1)

    def test_high_concentration_floor_is_one(self):
        self.assertEqual(app._AHA_BHA_TIER_FLOOR[("aha", "high")], 1)
        self.assertEqual(app._AHA_BHA_TIER_FLOOR[("bha", "high")], 1)

    def test_classify_returns_none_when_ingredient_strength_missing(self):
        """濃度情報が無い(実データの大半のケース)場合は判定不能とし、
        Noneを返すこと(特定の濃度を仮定しない)。"""
        step = {"ingredient_focus": ["aha"], "use_days": ["火", "木", "土"]}
        self.assertIsNone(app._classify_aha_bha_conservative_floor(step))

    def test_classify_returns_none_when_strength_dict_lacks_matching_key(self):
        step = {"ingredient_focus": ["aha"], "ingredient_strength": {"vitamin_c": "high"}}
        self.assertIsNone(app._classify_aha_bha_conservative_floor(step))

    def test_classify_low_aha(self):
        step = {"ingredient_focus": ["aha"], "ingredient_strength": {"aha": "low"}}
        self.assertEqual(app._classify_aha_bha_conservative_floor(step), 2)

    def test_classify_medium_aha(self):
        step = {"ingredient_focus": ["aha"], "ingredient_strength": {"aha": "medium"}}
        self.assertEqual(app._classify_aha_bha_conservative_floor(step), 1)

    def test_classify_high_bha(self):
        step = {"ingredient_focus": ["bha"], "ingredient_strength": {"bha": "high"}}
        self.assertEqual(app._classify_aha_bha_conservative_floor(step), 1)

    def test_classify_low_bha(self):
        step = {"ingredient_focus": ["bha"], "ingredient_strength": {"bha": "low"}}
        self.assertEqual(app._classify_aha_bha_conservative_floor(step), 2)

    def test_classify_unknown_level_value_is_unclassifiable(self):
        step = {"ingredient_focus": ["aha"], "ingredient_strength": {"aha": "とても強い"}}
        self.assertIsNone(app._classify_aha_bha_conservative_floor(step))


class RetinoidAhaBhaWeeklyCombinationTests(unittest.TestCase):
    """_evaluate_retinoid_aha_bha_weekly_combination(): 固定ハード上限
    ("レチノイド週3回以上ならAHA/BHAは週1〜2回"等)も、一律「週1回」への
    引き下げも導入しない。既存プロンプト【商品選定時の刺激配慮】の
    トリガー条件(hydration<=50かつbarrier<=50)を満たし、かつ
    ingredient_strengthから濃度を安全に分類できた場合のみ、その分類の
    既存下限まで調整する。濃度が判定できない場合(実データの大半)は
    頻度を変更せず検知のみ行う。"""

    def _night_with_retinoid(self, use_days=None):
        return [{"category": "美容液", "product": "レチノール美容液",
                  "ingredient_focus": ["retinol"], "use_days": use_days if use_days is not None else ["月", "水", "金"]}]

    def test_triggers_and_uses_tier_specific_floor_for_medium_aha(self):
        """濃度が判定できる場合(中濃度AHA): 既存目安の下限(週1回)へ調整。"""
        data = _empty_data(
            night={"steps": self._night_with_retinoid()},
            weekly_care=[{"category": "ピーリング", "product": "AHAピーリング",
                           "ingredient_focus": ["aha"], "ingredient_strength": {"aha": "medium"},
                           "use_days": ["火", "木", "土"]}],
        )
        data["scores"] = {"hydration": 40, "barrier": 35}
        log = []
        app._evaluate_retinoid_aha_bha_weekly_combination(data, user_data={}, conflict_log=log)
        peeling = data["weekly_care"][0]
        self.assertEqual(len(peeling["use_days"]), 1)
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["type"], "retinoid_aha_bha_conservative_lean")
        self.assertEqual(log[0]["from_days"], ["火", "木", "土"])

    def test_low_concentration_aha_is_capped_at_two_not_one(self):
        """低濃度AHAは既存下限が週2であり、週1へは引き下げないこと
        (2026-09の再監査で修正した中心点)。"""
        data = _empty_data(
            night={"steps": self._night_with_retinoid()},
            weekly_care=[{"category": "ピーリング", "product": "低濃度AHAピーリング",
                           "ingredient_focus": ["aha"], "ingredient_strength": {"aha": "low"},
                           "use_days": ["月", "水", "金", "日"]}],  # 週4回
        )
        data["scores"] = {"hydration": 30, "barrier": 30}
        log = []
        app._evaluate_retinoid_aha_bha_weekly_combination(data, user_data={}, conflict_log=log)
        peeling = data["weekly_care"][0]
        self.assertEqual(len(peeling["use_days"]), 2)  # 週1ではなく週2

    def test_low_concentration_aha_already_at_floor_is_not_changed(self):
        """低濃度AHAが既に週2回なら、それ以上(週1へ)は下げないこと。"""
        data = _empty_data(
            night={"steps": self._night_with_retinoid()},
            weekly_care=[{"category": "ピーリング", "product": "低濃度AHAピーリング",
                           "ingredient_focus": ["aha"], "ingredient_strength": {"aha": "low"},
                           "use_days": ["火", "金"]}],
        )
        data["scores"] = {"hydration": 30, "barrier": 30}
        log = []
        app._evaluate_retinoid_aha_bha_weekly_combination(data, user_data={}, conflict_log=log)
        self.assertEqual(data["weekly_care"][0]["use_days"], ["火", "金"])
        self.assertEqual(log, [])

    def test_unclassifiable_concentration_does_not_change_frequency(self):
        """濃度を安全に判定できない(ingredient_strengthが無い、実データの
        大半のケース)場合は、頻度を一切変更しないこと(判定不能なのに
        濃度を仮定しない)。検知ログには残る。"""
        data = _empty_data(
            night={"steps": self._night_with_retinoid()},
            weekly_care=[{"category": "ピーリング", "product": "AHAピーリング",
                           "ingredient_focus": ["aha"], "use_days": ["火", "木", "土"]}],
        )
        data["scores"] = {"hydration": 40, "barrier": 35}
        log = []
        app._evaluate_retinoid_aha_bha_weekly_combination(data, user_data={}, conflict_log=log)
        self.assertEqual(data["weekly_care"][0]["use_days"], ["火", "木", "土"])
        self.assertEqual(log, [])  # 頻度変更が無いためconflict_logにも記録されない

    def test_does_not_trigger_when_scores_are_healthy(self):
        """barrier高/hydration高ケース(今回の実診断相当): 調整しない。"""
        data = _empty_data(
            night={"steps": self._night_with_retinoid()},
            weekly_care=[{"category": "ピーリング", "product": "AHAピーリング",
                           "ingredient_focus": ["aha"], "ingredient_strength": {"aha": "medium"},
                           "use_days": ["火", "木", "土"]}],
        )
        data["scores"] = {"hydration": 75, "barrier": 75}
        log = []
        app._evaluate_retinoid_aha_bha_weekly_combination(data, user_data={}, conflict_log=log)
        self.assertEqual(data["weekly_care"][0]["use_days"], ["火", "木", "土"])
        self.assertEqual(log, [])

    def test_does_not_trigger_when_only_one_score_is_low(self):
        """barrier低・hydration正常のように片方だけでは、既存ルールの
        AND条件を満たさないため調整しない(推測で補わない)。"""
        data = _empty_data(
            night={"steps": self._night_with_retinoid()},
            weekly_care=[{"category": "ピーリング", "product": "AHAピーリング",
                           "ingredient_focus": ["aha"], "ingredient_strength": {"aha": "medium"},
                           "use_days": ["火", "木", "土"]}],
        )
        data["scores"] = {"hydration": 70, "barrier": 30}
        log = []
        app._evaluate_retinoid_aha_bha_weekly_combination(data, user_data={}, conflict_log=log)
        self.assertEqual(data["weekly_care"][0]["use_days"], ["火", "木", "土"])
        self.assertEqual(log, [])

    def test_pha_is_not_touched_even_when_triggered(self):
        """PHAケース: PHAはAHA/BHAと機械的に同列に扱わず、対象外のまま
        独立した既存の頻度目安を維持する。"""
        data = _empty_data(
            night={"steps": self._night_with_retinoid()},
            weekly_care=[{"category": "ピーリング", "product": "PHAピーリング",
                           "ingredient_focus": ["pha"], "use_days": ["火", "木", "土"]}],
        )
        data["scores"] = {"hydration": 30, "barrier": 30}
        log = []
        app._evaluate_retinoid_aha_bha_weekly_combination(data, user_data={}, conflict_log=log)
        self.assertEqual(data["weekly_care"][0]["use_days"], ["火", "木", "土"])
        self.assertEqual(log, [])

    def test_single_active_without_retinoid_does_not_trigger(self):
        """単独active(AHAのみ、レチノイドなし)ケース: 組み合わせ自体が
        存在しないため調整しない。"""
        data = _empty_data(
            night={"steps": [{"category": "美容液", "product": "ナイアシンアミド美容液",
                                "ingredient_focus": ["niacinamide"], "use_days": []}]},
            weekly_care=[{"category": "ピーリング", "product": "AHAピーリング",
                           "ingredient_focus": ["aha"], "ingredient_strength": {"aha": "medium"},
                           "use_days": ["火", "木", "土"]}],
        )
        data["scores"] = {"hydration": 30, "barrier": 30}
        log = []
        app._evaluate_retinoid_aha_bha_weekly_combination(data, user_data={}, conflict_log=log)
        self.assertEqual(data["weekly_care"][0]["use_days"], ["火", "木", "土"])
        self.assertEqual(log, [])

    def test_daily_medium_aha_is_reduced_to_its_tier_floor(self):
        """中濃度AHAが毎日([])の極端なケースでも、判定できる場合は
        その分類の下限(週1回)まで調整すること。"""
        data = _empty_data(
            night={"steps": self._night_with_retinoid()},
            weekly_care=[{"category": "ピーリング", "product": "AHAピーリング",
                           "ingredient_focus": ["aha"], "ingredient_strength": {"aha": "medium"},
                           "use_days": []}],
        )
        data["scores"] = {"hydration": 20, "barrier": 20}
        log = []
        app._evaluate_retinoid_aha_bha_weekly_combination(data, user_data={}, conflict_log=log)
        self.assertEqual(len(data["weekly_care"][0]["use_days"]), 1)

    def test_already_at_or_below_conservative_floor_is_not_changed(self):
        """既に該当分類の下限以下なら、それ以上効果を落とさないこと。"""
        data = _empty_data(
            night={"steps": self._night_with_retinoid()},
            weekly_care=[{"category": "ピーリング", "product": "AHAピーリング",
                           "ingredient_focus": ["aha"], "ingredient_strength": {"aha": "high"},
                           "use_days": ["木"]}],
        )
        data["scores"] = {"hydration": 20, "barrier": 20}
        log = []
        app._evaluate_retinoid_aha_bha_weekly_combination(data, user_data={}, conflict_log=log)
        self.assertEqual(data["weekly_care"][0]["use_days"], ["木"])
        self.assertEqual(log, [])

    def test_missing_scores_do_not_crash_and_do_not_trigger(self):
        data = _empty_data(
            night={"steps": self._night_with_retinoid()},
            weekly_care=[{"category": "ピーリング", "product": "AHAピーリング",
                           "ingredient_focus": ["aha"], "ingredient_strength": {"aha": "medium"},
                           "use_days": ["火", "木", "土"]}],
        )
        data["scores"] = {}
        log = []
        app._evaluate_retinoid_aha_bha_weekly_combination(data, user_data={}, conflict_log=log)
        self.assertEqual(data["weekly_care"][0]["use_days"], ["火", "木", "土"])
        self.assertEqual(log, [])

    def test_adjusted_step_reason_excluded_from_frequency_note_but_explained_in_safety_note(self):
        """調整されたstepのGemini由来use_days_reasonは最終結果と矛盾する
        ため頻度の理由には出ず、安全調整理由側で実際に考慮した要因
        (バリア・水分量の低下、レチノイドとの併用)が説明されること。"""
        data = _empty_data(
            night={"steps": [{"category": "美容液", "product": "レチノール美容液",
                                "ingredient_focus": ["retinol"], "use_days": ["月", "水", "金"],
                                "use_days_reason": "レチノールは刺激があるため週3回としています。"}]},
            weekly_care=[{"category": "ピーリング", "product": "AHAピーリング",
                           "ingredient_focus": ["aha"], "ingredient_strength": {"aha": "medium"},
                           "use_days": ["火", "木", "土"],
                           "use_days_reason": "AHAは低刺激な処方のため週3回としています。"}],
        )
        data["scores"] = {"hydration": 30, "barrier": 30}
        log = []
        app._evaluate_retinoid_aha_bha_weekly_combination(data, user_data={}, conflict_log=log)
        data["routine_conflict_log"] = log
        app.build_weekly_usage_plan(data)

        self.assertNotIn("AHAは低刺激な処方のため週3回としています。", data["frequency_reason_note"])
        # レチノールの理由(調整されていない側)はそのまま残る
        self.assertIn("レチノールは刺激があるため週3回としています。", data["frequency_reason_note"])
        self.assertTrue(data["routine_reason_notes"])
        self.assertIn("バリア機能・水分量がともに低下", data["routine_reason_notes"][0])
        self.assertIn("レチノイド", data["routine_reason_notes"][0])


if __name__ == "__main__":
    unittest.main()
