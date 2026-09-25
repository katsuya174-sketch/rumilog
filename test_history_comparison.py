"""
履歴の前回比較強化(継続ケアの事実提示)のテスト。

要件(ユーザー指定):
- 保存済みデータから確認できる事実(ingredient_focusが新旧両方の診断に
  存在するか)だけを根拠にする。
- 「○○を使ったため改善した」という因果関係は一切断定しない。
- 説明可能な事実が無い場合は無理に理由を表示しない(空文字のまま)。
- 新規AI API呼び出し・新規外部API・DB schema変更は行っていない。

実DBが必要なため、DATABASE_URL(環境変数)でテスト専用DBを指定して実行する。
"""

import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402


def _diag_record(record_date, scores, night_focus=None, morning_focus=None, **overrides):
    """保存済み診断記録を模したdict(必要フィールドのみ)。"""
    night_steps = [
        {"category": "美容液", "ingredient_focus": tag}
        for tag in (night_focus or [])
    ]
    morning_steps = [
        {"category": "美容液", "ingredient_focus": tag}
        for tag in (morning_focus or [])
    ]
    base = {
        "id": record_date.replace("/", ""),
        "record_date": record_date,
        "saved_at": record_date,
        "skin_score": 60,
        "skin_summary": "",
        "scores": scores,
        "night": {"steps": night_steps},
        "morning": {"steps": morning_steps},
    }
    base.update(overrides)
    return base


class ExtractIngredientFocusLabelsTests(unittest.TestCase):
    def test_extracts_and_translates_known_tag(self):
        item = _diag_record("2026/09/01", {}, night_focus=["niacinamide"])
        self.assertEqual(app._extract_ingredient_focus_labels(item), ["ナイアシンアミド"])

    def test_dedups_same_label_across_multiple_steps(self):
        item = _diag_record("2026/09/01", {}, night_focus=["niacinamide", "niacinamide"])
        self.assertEqual(app._extract_ingredient_focus_labels(item), ["ナイアシンアミド"])

    def test_includes_morning_and_night(self):
        item = _diag_record("2026/09/01", {}, night_focus=["niacinamide"], morning_focus=["vitamin_c"])
        labels = app._extract_ingredient_focus_labels(item)
        self.assertIn("ナイアシンアミド", labels)
        self.assertIn("ビタミンC", labels)

    def test_unmappable_tag_is_ignored_not_guessed(self):
        item = _diag_record("2026/09/01", {}, night_focus=["totally_unknown_tag_xyz"])
        self.assertEqual(app._extract_ingredient_focus_labels(item), [])

    def test_missing_or_malformed_sections_do_not_crash(self):
        self.assertEqual(app._extract_ingredient_focus_labels({}), [])
        self.assertEqual(app._extract_ingredient_focus_labels({"night": None}), [])
        self.assertEqual(app._extract_ingredient_focus_labels({"night": {"steps": "not-a-list"}}), [])
        self.assertEqual(app._extract_ingredient_focus_labels(None), [])


class ContinuedCareFactsTests(unittest.TestCase):
    def test_only_labels_present_in_both_are_returned(self):
        newer = _diag_record("2026/09/15", {}, night_focus=["niacinamide", "retinol"])
        older = _diag_record("2026/09/01", {}, night_focus=["niacinamide", "vitamin_c"])
        self.assertEqual(app._continued_care_facts(newer, older), ["ナイアシンアミド"])

    def test_no_overlap_returns_empty(self):
        newer = _diag_record("2026/09/15", {}, night_focus=["retinol"])
        older = _diag_record("2026/09/01", {}, night_focus=["vitamin_c"])
        self.assertEqual(app._continued_care_facts(newer, older), [])

    def test_result_is_sorted_deterministically(self):
        newer = _diag_record("2026/09/15", {}, night_focus=["niacinamide", "retinol", "vitamin_c"])
        older = _diag_record("2026/09/01", {}, night_focus=["niacinamide", "retinol", "vitamin_c"])
        result = app._continued_care_facts(newer, older)
        self.assertEqual(result, sorted(result))


class ContinuedCareNoteTests(unittest.TestCase):
    def test_note_includes_labels_and_disclaimer(self):
        note = app._continued_care_note(["ナイアシンアミド"])
        self.assertIn("ナイアシンアミド", note)
        self.assertIn("継続されています", note)
        # 因果関係を断定しない旨の注記が必ず含まれること。
        self.assertIn("因果関係を断定するものではありません", note)
        # 「改善した」「治った」等の断定的な因果表現を含まないこと。
        self.assertNotIn("改善しました", note)
        self.assertNotIn("ため改善", note)

    def test_empty_facts_yields_empty_note_not_fabricated(self):
        self.assertEqual(app._continued_care_note([]), "")


class BuildHistoryDashboardContinuedCareIntegrationTests(unittest.TestCase):
    """build_history_dashboard()経由での結線確認。score_diffの既存ロジックは
    変更していないことも合わせて確認する。"""

    def test_newer_item_gets_continued_care_when_overlap_exists(self):
        history_data = [
            _diag_record("2026/09/15", {"pores": 67}, night_focus=["niacinamide"]),
            _diag_record("2026/09/01", {"pores": 58}, night_focus=["niacinamide"]),
        ]
        with app.app.test_request_context("/"):
            dashboard = app.build_history_dashboard(history_data, is_premium=True, is_creator=False)
        prepared = dashboard["prepared"]
        newest = prepared[0]
        self.assertEqual(newest["continued_care"], ["ナイアシンアミド"])
        self.assertIn("ナイアシンアミド", newest["continued_care_note"])
        self.assertIn("因果関係を断定するものではありません", newest["continued_care_note"])
        # 既存のscore_diffロジックは変更されていないこと。
        self.assertEqual(newest["score_diff"].get("毛穴"), 9)

    def test_no_overlap_yields_no_fabricated_note(self):
        history_data = [
            _diag_record("2026/09/15", {"pores": 67}, night_focus=["retinol"]),
            _diag_record("2026/09/01", {"pores": 58}, night_focus=["vitamin_c"]),
        ]
        with app.app.test_request_context("/"):
            dashboard = app.build_history_dashboard(history_data, is_premium=True, is_creator=False)
        newest = dashboard["prepared"][0]
        self.assertEqual(newest["continued_care"], [])
        self.assertEqual(newest["continued_care_note"], "")

    def test_oldest_item_has_safe_default_no_comparison_possible(self):
        history_data = [
            _diag_record("2026/09/15", {"pores": 67}, night_focus=["niacinamide"]),
            _diag_record("2026/09/01", {"pores": 58}, night_focus=["niacinamide"]),
        ]
        with app.app.test_request_context("/"):
            dashboard = app.build_history_dashboard(history_data, is_premium=True, is_creator=False)
        oldest = dashboard["prepared"][-1]
        self.assertEqual(oldest["continued_care"], [])
        self.assertEqual(oldest["continued_care_note"], "")

    def test_single_history_item_does_not_crash(self):
        history_data = [_diag_record("2026/09/15", {"pores": 67}, night_focus=["niacinamide"])]
        with app.app.test_request_context("/"):
            dashboard = app.build_history_dashboard(history_data, is_premium=True, is_creator=False)
        self.assertEqual(dashboard["prepared"][0]["continued_care"], [])
        self.assertEqual(dashboard["prepared"][0]["continued_care_note"], "")


if __name__ == "__main__":
    unittest.main()
