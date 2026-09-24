"""
「なぜこの商品が1位か」(build_candidate_comparison_notes、プレミアム機能
「AI比較」)の個別化のテスト。

- normalize_candidate()が商品固有データ(active_ingredients等)を欠落させずに
  保持することの確認(finalize_step_data経由の統合テスト)
- build_candidate_comparison_notes()が商品ごとに実質的に異なる文章を生成する
  こと
- 成分データが無い商品でもクラッシュせず妥当な文章になること
- 商品データに存在しない成分名を理由に使わない(捏造しない)こと
- 実際のスコア関係と説明内容が矛盾しないこと

実DBが必要なため、DATABASE_URL(環境変数)でテスト専用DBを指定して実行する。

実行方法:
    DATABASE_URL=postgresql://localhost/rumilog_test python3 -m pytest test_candidate_comparison_notes.py -v
"""

import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402  (DATABASE_URL設定後にimportする必要がある)


def _candidate(
    name,
    brand="テストブランド",
    score=80,
    base_score=80,
    improve_score=0,
    routine_score=0,
    price_ref=2000,
    active_ingredients=None,
    support_ingredients=None,
    main_functions=None,
    concerns=None,
    source="db",
):
    """normalize_candidate()通過後の形(必要フィールドのみ)を模したdict。"""
    return {
        "brand": brand,
        "name": name,
        "score": score,
        "base_score": base_score,
        "improve_score": improve_score,
        "routine_score": routine_score,
        "source": source,
        "price_ref": price_ref,
        "active_ingredients": active_ingredients or [],
        "support_ingredients": support_ingredients or [],
        "main_functions": main_functions or [],
        "skin_types": [],
        "concerns": concerns or [],
        "texture": "",
        "formulation": [],
    }


class BuildCandidateComparisonNotesTests(unittest.TestCase):
    def test_why_best_uses_products_own_main_function_and_user_concern(self):
        candidates = [
            _candidate(
                "モイストローション",
                main_functions=["高保湿ケア"],
                concerns=["dryness"],
                base_score=90,
            ),
        ]
        step = {"category": "化粧水", "purpose": "乾燥対策"}
        user_data = {"oil": "dry", "concerns": ["dryness"]}

        result = app.build_candidate_comparison_notes(candidates, step, user_data)
        self.assertIn("高保湿ケア", result["why_best"])
        self.assertIn("モイストローション", result["why_best"])

    # 異なる商品データを渡せば、why_bestが実質的に異なる文章になること
    # (同一の定型文が繰り返されない)
    def test_why_best_differs_for_different_products(self):
        step = {"category": "美容液", "purpose": ""}
        user_data = {"oil": "oily", "concerns": []}

        result_a = app.build_candidate_comparison_notes(
            [_candidate("商品A", main_functions=["毛穴引き締め"], base_score=90)],
            step, user_data,
        )
        result_b = app.build_candidate_comparison_notes(
            [_candidate("商品B", main_functions=["美白ケア"], improve_score=90, base_score=10)],
            step, user_data,
        )
        self.assertNotEqual(result_a["why_best"], result_b["why_best"])

    # 成分・機能データが無い商品でもクラッシュせず、価格・スコアのみで説明すること
    def test_why_best_falls_back_to_price_and_score_when_no_ingredient_data(self):
        candidates = [_candidate("シンプル乳液", main_functions=[], active_ingredients=[], price_ref=1500, base_score=70)]
        result = app.build_candidate_comparison_notes(candidates, {"category": "乳液", "purpose": ""}, {})
        self.assertNotEqual(result["why_best"], "")
        self.assertNotIn("None", result["why_best"])

    # 空リストでもクラッシュしない
    def test_empty_candidates_returns_empty_notes(self):
        result = app.build_candidate_comparison_notes([], {}, {})
        self.assertEqual(result, {"why_best": "", "diffs": []})

    # 全スコアが0の候補ではwhy_bestを空にする(根拠のない断定をしない)
    def test_all_zero_scores_produces_empty_why_best(self):
        candidates = [_candidate("スコア無し商品", base_score=0, improve_score=0, routine_score=0, main_functions=["何か"])]
        result = app.build_candidate_comparison_notes(candidates, {}, {})
        self.assertEqual(result["why_best"], "")

    # 商品データに存在しない成分名を理由に使わない(捏造しない)こと
    def test_does_not_mention_ingredients_absent_from_product_data(self):
        candidates = [
            _candidate("無成分商品", active_ingredients=[], support_ingredients=[], main_functions=[], base_score=80),
        ]
        result = app.build_candidate_comparison_notes(candidates, {}, {})
        for fake_ingredient_label in ["レチノール", "ビタミンC", "ナイアシンアミド"]:
            self.assertNotIn(fake_ingredient_label, result["why_best"])

    # diffs: 2位・3位それぞれの説明が、商品固有データにより実質的に異なること
    def test_diffs_are_distinct_for_different_runner_up_products(self):
        candidates = [
            _candidate("1位商品", score=90, base_score=90, active_ingredients=["niacinamide"], price_ref=3000),
            _candidate("2位商品", score=70, base_score=70, active_ingredients=["retinol"], price_ref=2000),
            _candidate("3位商品", score=60, base_score=60, active_ingredients=[], price_ref=5000),
        ]
        result = app.build_candidate_comparison_notes(candidates, {}, {})
        self.assertEqual(len(result["diffs"]), 2)
        self.assertNotEqual(result["diffs"][0]["text"], result["diffs"][1]["text"])
        # 1位のみが持つ成分(niacinamide→ナイアシンアミド)が2位との差分として言及される
        self.assertIn("ナイアシンアミド", result["diffs"][0]["text"])

    # 価格が高いのに「優れている」と断定しない(中立的な事実表現であること)
    def test_price_difference_phrasing_does_not_overclaim_superiority(self):
        candidates = [
            _candidate("高価格1位商品", score=90, base_score=90, price_ref=5000, active_ingredients=[]),
            _candidate("安価2位商品", score=60, base_score=60, price_ref=1000, active_ingredients=[]),
        ]
        result = app.build_candidate_comparison_notes(candidates, {}, {})
        text = result["diffs"][0]["text"]
        self.assertIn("高いが総合スコアでは上回る", text)
        self.assertNotIn("優れている", text)
        self.assertNotIn("優位", text)

    # 実際のスコア関係と説明内容が矛盾しない: improve_scoreが支配的なら
    # 「改善適合スコア」に言及し、base_scoreが支配的な場合の文言(基本適合)は使わない
    def test_dominant_score_component_matches_actual_scores(self):
        candidates = [_candidate("改善重視商品", base_score=10, improve_score=90, routine_score=5, main_functions=["集中改善ケア"])]
        result = app.build_candidate_comparison_notes(candidates, {}, {})
        self.assertIn("改善適合スコア", result["why_best"])
        self.assertNotIn("基本適合スコア", result["why_best"])


class NormalizeCandidateFieldRetentionTests(unittest.TestCase):
    """
    normalize_candidate()(finalize_step_data内のクロージャ)が商品固有データを
    落とさずに保持することを、公開関数finalize_step_data経由で確認する。
    """

    def test_finalize_step_data_preserves_active_ingredients_into_candidate_comparison(self):
        step = {
            "category": "美容液",
            "purpose": "",
            "product": "ナイアシンアミド美容液",
            "brand": "テストブランド",
            "top_candidates": [
                {
                    "brand": "テストブランド",
                    "name": "ナイアシンアミド美容液",
                    "score": 90,
                    "base_score": 90,
                    "improve_score": 0,
                    "routine_score": 0,
                    "price_ref": 2500,
                    "active_ingredients": ["niacinamide"],
                    "main_functions": ["毛穴ケア"],
                    "concerns": ["pores"],
                    "source": "db",
                },
            ],
        }
        result_step = app.finalize_step_data(dict(step), {"oil": "oily", "concerns": []})
        why_best = result_step.get("candidate_comparison", {}).get("why_best", "")
        # active_ingredients/main_functionsがnormalize_candidate通過後も保持されて
        # いなければ、この特徴語は理由文に一切現れないはず。
        self.assertTrue(
            ("毛穴ケア" in why_best) or ("ナイアシンアミド" in why_best),
            f"商品固有データがwhy_bestへ反映されていません: {why_best!r}",
        )


if __name__ == "__main__":
    unittest.main()
