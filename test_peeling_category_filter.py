"""
ピーリング候補集合の誤分類修正のテスト。

実機診断で「雪肌精 クリアウェルネス ジェントル ウォッシュ」(KOSÉ公式では
低刺激性泡洗顔料)がピーリングの1位として選定された不具合の回帰テスト。

原因は3点:
1. 楽天criteria検索経路の_CATEGORY_CROSS_REJECT["ピーリング"]に「ウォッシュ」
   「フォーム」等の除外語が無く、Gemini候補用の_CANDIDATE_CATEGORY_FORBIDDEN
   ["ピーリング"]にだけ存在していた(経路間の判定基準の乖離)。
2. is_candidate_wrong_for_category()がselect_best_market_candidate()の
   候補収集ループ内で一切呼ばれておらず、1位選定そのものにカテゴリ不適合
   チェックが効いていなかった(表示用top_candidatesの整形時にしか適用されて
   いなかった)。

修正: _CATEGORY_CROSS_REJECT["ピーリング"]を_CANDIDATE_CATEGORY_FORBIDDEN
["ピーリング"]と統合し、select_best_market_candidate()の2つの候補収集
ループ双方でis_candidate_wrong_for_category()を1位選定前に適用する。

「角質」という必須キーワード自体は削除・縮小していない
(正当なピーリング商品の再現率を落とすリスクを避けるため)。
disambiguationは統合後のcross-reject/is_candidate_wrong_for_categoryが担う。

実DBが必要なため、DATABASE_URL(環境変数)でテスト専用DBを指定して実行する。
"""

import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402


class RakutenCriteriaPeelingValidationTests(unittest.TestCase):
    """_is_rakuten_item_valid_for_category()(楽天criteria検索経路)のテスト。"""

    def test_gentle_wash_is_excluded_from_peeling(self):
        self.assertFalse(
            app._is_rakuten_item_valid_for_category(
                "雪肌精 クリアウェルネス ジェントル ウォッシュ", "ピーリング"
            )
        )

    def test_ordinary_cleanser_mentioning_old_keratin_is_excluded(self):
        # 「古い角質を落とす」は通常の洗顔料に極めて一般的な訴求文言であり、
        # ピーリング固有ではない。
        self.assertFalse(
            app._is_rakuten_item_valid_for_category(
                "モイストクリア 古い角質を落とす洗顔フォーム", "ピーリング"
            )
        )

    def test_wash_or_foam_named_cleanser_is_excluded_even_without_keratin_word(self):
        self.assertFalse(
            app._is_rakuten_item_valid_for_category("さっぱり洗顔ウォッシュ", "ピーリング")
        )
        self.assertFalse(
            app._is_rakuten_item_valid_for_category("マイルドクレンジングフォーム", "ピーリング")
        )

    def test_genuine_peeling_product_is_still_accepted(self):
        self.assertTrue(
            app._is_rakuten_item_valid_for_category("薬用ピーリングジェル", "ピーリング")
        )

    def test_genuine_aha_bha_pha_exfoliant_is_still_accepted(self):
        self.assertTrue(
            app._is_rakuten_item_valid_for_category("サリチル酸BHA配合 角質ケアローション", "ピーリング")
        )
        self.assertTrue(
            app._is_rakuten_item_valid_for_category("グリコール酸ピール美容液", "ピーリング")
        )

    def test_enzyme_cleansing_powder_is_still_accepted(self):
        # 「酵素洗顔」は過去に誤除外の実害があったため正当な週1ピーリング商品として
        # 扱う既存方針(_CANDIDATE_CATEGORY_FORBIDDENのコメント参照)を壊さないこと。
        self.assertTrue(
            app._is_rakuten_item_valid_for_category("酵素洗顔パウダー", "ピーリング")
        )


class CandidateWrongForCategoryPeelingTests(unittest.TestCase):
    """is_candidate_wrong_for_category()(Gemini候補経路)のテスト。"""

    def test_gentle_wash_is_wrong_for_peeling(self):
        self.assertTrue(
            app.is_candidate_wrong_for_category(
                "ピーリング", "雪肌精 クリアウェルネス ジェントル ウォッシュ"
            )
        )

    def test_genuine_peeling_product_is_not_wrong_for_peeling(self):
        self.assertFalse(
            app.is_candidate_wrong_for_category("ピーリング", "薬用ピーリングジェル")
        )


class CategoryValidationConsistencyTests(unittest.TestCase):
    """
    楽天criteria経路(_is_rakuten_item_valid_for_category)とGemini候補経路
    (is_candidate_wrong_for_category)が、同じ商品名に対して一致した判定を
    返すこと(経路によって基準が再び乖離していないこと)。
    """

    _CASES = [
        ("雪肌精 クリアウェルネス ジェントル ウォッシュ", False),
        ("さっぱり洗顔ウォッシュ", False),
        ("薬用ピーリングジェル", True),
        ("グリコール酸ピール美容液", True),
    ]

    def test_both_validators_agree_on_peeling_candidates(self):
        for name, expected_valid in self._CASES:
            with self.subTest(name=name):
                rakuten_valid = app._is_rakuten_item_valid_for_category(name, "ピーリング")
                gemini_valid = not app.is_candidate_wrong_for_category("ピーリング", name)
                self.assertEqual(rakuten_valid, expected_valid, f"rakuten path: {name!r}")
                self.assertEqual(gemini_valid, expected_valid, f"gemini path: {name!r}")
                self.assertEqual(rakuten_valid, gemini_valid, f"paths disagree for {name!r}")


class SelectBestMarketCandidatePeelingIntegrationTests(unittest.TestCase):
    """
    select_best_market_candidate()(1位選定そのもの)がカテゴリ不適合候補を
    最初から候補集合に入れない(表示用top_candidatesだけでなく1位選定にも
    同じ基準が効いていること)ことの統合テスト。
    """

    def setUp(self):
        # 楽天API呼び出しはこのテストの対象外のため、実ネットワーク呼び出しを
        # 避けて決定的にする(このdevの環境変数にRAKUTEN_APP_ID等が設定されている
        # ため、パッチしないと実APIへ通信してしまう)。
        self._orig_search = app.search_rakuten_for_step
        app.search_rakuten_for_step = lambda step, improvement_plan: []

    def tearDown(self):
        app.search_rakuten_for_step = self._orig_search

    def test_wash_type_candidate_never_becomes_peeling_winner(self):
        step = {
            "category": "ピーリング",
            "purpose": "毛穴ケア",
            "ingredient_focus": "",
            "product_candidates": [
                {"brand": "コーセー", "name": "雪肌精 クリアウェルネス ジェントル ウォッシュ"},
                {"brand": "テストブランド", "name": "薬用ピーリングジェル AHA配合"},
            ],
        }
        result = app.select_best_market_candidate(
            step,
            db_products=[],
            user_data={"oil": "normal", "sens": "low", "exp": "middle"},
            budget_value=3000,
            verified_products=[],
        )
        self.assertIsNotNone(result)
        self.assertNotIn("ジェントル ウォッシュ", result.get("name", ""))
        self.assertIn("ピーリングジェル", result.get("name", ""))

    def test_top_candidates_and_winner_share_the_same_category_filter(self):
        """
        preserve_ranked_top_candidates()の表示用フィルタと、
        select_best_market_candidate()の1位選定が同じ基準を使うため、
        _top_candidatesにウォッシュ系候補が最初から含まれないこと。
        """
        step = {
            "category": "ピーリング",
            "purpose": "毛穴ケア",
            "ingredient_focus": "",
            "product_candidates": [
                {"brand": "コーセー", "name": "雪肌精 クリアウェルネス ジェントル ウォッシュ"},
                {"brand": "テストブランド", "name": "薬用ピーリングジェル AHA配合"},
                {"brand": "テストブランド2", "name": "グリコール酸ピールローション"},
            ],
        }
        result = app.select_best_market_candidate(
            step,
            db_products=[],
            user_data={"oil": "normal", "sens": "low", "exp": "middle"},
            budget_value=3000,
            verified_products=[],
        )
        self.assertIsNotNone(result)
        top_candidate_names = [c.get("name", "") for c in result.get("_top_candidates", [])]
        self.assertTrue(top_candidate_names)
        for name in top_candidate_names:
            self.assertNotIn("ジェントル ウォッシュ", name)


if __name__ == "__main__":
    unittest.main()
