"""
Phase 2: product_masterを診断パイプライン(select_best_market_candidate等)へ
接続する実装のテスト。

設計(ユーザーとの合意事項):
- product_master候補は常にcombined_productsへ追加される(段階導入)。
- 楽天広範囲検索のスキップはPRODUCT_MASTER_SKIP_RAKUTEN_ENABLED(既定False)
  が有効かつ、score_product()ベースで「ハード除外されず関連性も確認できる」
  候補がPRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT件以上のときのみ発動する。
- 最終選定された候補がproduct_master由来の場合のみ、保存済みitem_codeで
  価格・URL・画像をライブ値へリフレッシュする(apply_db_product_to_step)。
- 2段階処方(restrict_high_stim_families_for_fragile_skin)は候補の出自に
  関わらず同じ挙動であること。

楽天APIへの実通信は一切行わない(search_rakuten_for_step/
fetch_rakuten_item_by_item_codeをモックまたは差し替える)。
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import psycopg2  # noqa: E402

import app  # noqa: E402


def _product_master_item(suffix, ingredient_focus="niacinamide", category="美容液"):
    return {
        "brand": f"テストブランドPM{suffix}",
        "name": f"テスト商品マスタPM{suffix}",
        "category": category,
        "price_ref": 2200,
        "active_ingredients": [ingredient_focus] if ingredient_focus else [],
        "support_ingredients": [],
        "signature_ingredients": [],
        "concerns": ["pores"],
        "skin_types": [],
        "sensitive_ok": "unknown",
        "retinol_level": 0,
        "main_functions": [],
        "ingredient_focus": [ingredient_focus] if ingredient_focus else [],
        "ingredient_strength": {},
        "formulation": [],
        "technology": [],
        "texture": "",
        "contraindications": [],
        "uv_level": {},
        "availability_japan": ["rakuten"],
        "image": "",
        "rakuten_link": "",
        "rakuten_title": "",
        "item_code": f"shop:pm-{suffix}",
        "shop_name": "",
        "verified_at": 1700000000,
    }


class SelectBestMarketCandidateProductMasterSufficiencyTests(unittest.TestCase):
    """select_best_market_candidate()のRakutenスキップ判定ロジック。"""

    TEST_CATEGORY = "美容液"
    TEST_SUFFIX = "_SufficiencyTest"

    @classmethod
    def setUpClass(cls):
        app.init_product_master_table()

    def setUp(self):
        self._cleanup()

    def tearDown(self):
        self._cleanup()

    def _cleanup(self):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM product_master WHERE name LIKE %s", (f"%{self.TEST_SUFFIX}%",))
            conn.commit()
        finally:
            conn.close()

    def _insert_master_candidates(self, count, ingredient_focus="niacinamide"):
        for i in range(count):
            app.upsert_product_master(
                _product_master_item(f"{self.TEST_SUFFIX}{i}", ingredient_focus=ingredient_focus, category=self.TEST_CATEGORY),
                data_source="diagnosis_time",
            )

    def _call_select(self, step_ingredient_focus="niacinamide"):
        step = {
            "category": self.TEST_CATEGORY,
            "purpose": "毛穴ケア",
            "ingredient_focus": step_ingredient_focus,
        }
        return app.select_best_market_candidate(
            step,
            db_products=[],
            user_data={"oil": "normal", "sens": "low", "exp": "middle"},
            budget_value=3000,
            verified_products=[],
        )

    def test_flag_disabled_always_calls_rakuten_even_with_sufficient_master_candidates(self):
        self._insert_master_candidates(5)
        with patch.object(app, "PRODUCT_MASTER_SKIP_RAKUTEN_ENABLED", False), \
             patch.object(app, "search_rakuten_for_step", return_value=[]) as mock_search:
            self._call_select()
        mock_search.assert_called_once()

    def test_flag_enabled_with_sufficient_relevant_candidates_skips_rakuten(self):
        self._insert_master_candidates(app.PRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT)
        with patch.object(app, "PRODUCT_MASTER_SKIP_RAKUTEN_ENABLED", True), \
             patch.object(app, "search_rakuten_for_step", return_value=[]) as mock_search:
            self._call_select()
        mock_search.assert_not_called()

    def test_flag_enabled_with_insufficient_candidates_still_calls_rakuten(self):
        self._insert_master_candidates(app.PRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT - 1)
        with patch.object(app, "PRODUCT_MASTER_SKIP_RAKUTEN_ENABLED", True), \
             patch.object(app, "search_rakuten_for_step", return_value=[]) as mock_search:
            self._call_select()
        mock_search.assert_called_once()

    def test_flag_enabled_but_candidates_irrelevant_to_step_focus_still_calls_rakuten(self):
        # ステップが要求するingredient_focusと一致しない候補は「関連性あり」と
        # 数えないため、件数だけ十分でもRakutenスキップは発動しない。
        self._insert_master_candidates(5, ingredient_focus="vitamin_c")
        with patch.object(app, "PRODUCT_MASTER_SKIP_RAKUTEN_ENABLED", True), \
             patch.object(app, "search_rakuten_for_step", return_value=[]) as mock_search:
            self._call_select(step_ingredient_focus="niacinamide")
        mock_search.assert_called_once()

    def test_no_ingredient_focus_specified_treats_non_excluded_candidates_as_relevant(self):
        self._insert_master_candidates(app.PRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT, ingredient_focus="")
        with patch.object(app, "PRODUCT_MASTER_SKIP_RAKUTEN_ENABLED", True), \
             patch.object(app, "search_rakuten_for_step", return_value=[]) as mock_search:
            self._call_select(step_ingredient_focus="")
        mock_search.assert_not_called()

    def test_product_master_candidates_are_included_in_combined_pool(self):
        self._insert_master_candidates(1)
        with patch.object(app, "PRODUCT_MASTER_SKIP_RAKUTEN_ENABLED", False), \
             patch.object(app, "search_rakuten_for_step", return_value=[]):
            result = self._call_select()
        self.assertIsNotNone(result)
        self.assertIn(self.TEST_SUFFIX, result.get("name", ""))


class RawIngredientNameSufficiencyBackfillTests(unittest.TestCase):
    """Phase 2+3統合検証で判明した不整合の回帰テスト: Phase3(ai_precollected)
    のように生の原文成分名(例: ツボクサエキス)しか持たない候補でも、
    active_ingredient_tagsのバックフィルによりingredient_focus一致・
    sufficient判定が機能するようになったことを確認する。"""

    TEST_CATEGORY = "美容液"
    TEST_SUFFIX = "_RawTagBackfillTest"

    @classmethod
    def setUpClass(cls):
        app.init_product_master_table()

    def setUp(self):
        self._cleanup()

    def tearDown(self):
        self._cleanup()

    def _cleanup(self):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM product_master WHERE name LIKE %s", (f"%{self.TEST_SUFFIX}%",))
            conn.commit()
        finally:
            conn.close()

    def _insert_raw_ingredient_candidates(self, count, raw_ingredient="ツボクサエキス"):
        for i in range(count):
            item = _product_master_item(f"{self.TEST_SUFFIX}{i}", ingredient_focus="", category=self.TEST_CATEGORY)
            item["active_ingredients"] = [raw_ingredient]
            item["ingredient_focus"] = []
            app.upsert_product_master(item, data_source="ai_precollected")

    def test_raw_japanese_ingredient_name_now_counts_as_relevant_and_sufficient(self):
        self._insert_raw_ingredient_candidates(app.PRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT)
        step = {"category": self.TEST_CATEGORY, "purpose": "鎮静ケア", "ingredient_focus": "ツボクサ"}
        with patch.object(app, "PRODUCT_MASTER_SKIP_RAKUTEN_ENABLED", True), \
             patch.object(app, "search_rakuten_for_step", return_value=[]) as mock_search:
            app.select_best_market_candidate(
                step, db_products=[], user_data={"oil": "normal", "sens": "low", "exp": "middle"},
                budget_value=3000, verified_products=[],
            )
        mock_search.assert_not_called()

    def test_candidates_returned_with_tag_merged_into_active_ingredients(self):
        self._insert_raw_ingredient_candidates(1)
        candidates = app.query_product_master_candidates(self.TEST_CATEGORY)
        matching = [c for c in candidates if self.TEST_SUFFIX in c.get("name", "")]
        self.assertEqual(len(matching), 1)
        actives = matching[0]["active_ingredients"]
        self.assertIn("ツボクサエキス", actives)
        self.assertIn("centella_extract", actives)


class RefreshSelectedCandidatePriceTests(unittest.TestCase):
    def test_success_overwrites_price_link_image(self):
        product = {"item_code": "shop:123", "price": 1000, "price_ref": 1000, "rakuten_link": "", "image": ""}
        fake_result = {
            "ok": True,
            "item": {
                "itemPrice": 2500,
                "itemUrl": "https://item.rakuten.co.jp/shop/123/",
                "mediumImageUrls": [{"imageUrl": "https://image.rakuten.co.jp/shop/123.jpg"}],
            },
        }
        with patch.object(app, "fetch_rakuten_item_by_item_code", return_value=fake_result):
            updated = app.refresh_selected_candidate_price(product)

        self.assertEqual(updated["price"], 2500)
        self.assertEqual(updated["price_ref"], 2500)
        self.assertEqual(updated["rakuten_link"], "https://item.rakuten.co.jp/shop/123/")
        self.assertEqual(updated["image"], "https://image.rakuten.co.jp/shop/123.jpg")

    def test_failure_falls_back_to_last_known_values(self):
        product = {"item_code": "shop:bad", "price": 1000, "price_ref": 1000, "rakuten_link": "old-link", "image": "old-image"}
        fake_result = {"ok": False, "http_status": 400, "rakuten_error": "boom"}
        with patch.object(app, "fetch_rakuten_item_by_item_code", return_value=fake_result):
            updated = app.refresh_selected_candidate_price(product)

        self.assertEqual(updated["price"], 1000)
        self.assertEqual(updated["rakuten_link"], "old-link")
        self.assertEqual(updated["image"], "old-image")

    def test_missing_item_code_is_noop_without_calling_api(self):
        product = {"item_code": "", "price": 1000}
        with patch.object(app, "fetch_rakuten_item_by_item_code") as mock_fetch:
            updated = app.refresh_selected_candidate_price(product)
        mock_fetch.assert_not_called()
        self.assertEqual(updated["price"], 1000)


class ApplyDbProductToStepRefreshWiringTests(unittest.TestCase):
    """apply_db_product_to_step()が、product_master由来の最終選定候補にのみ
    価格リフレッシュを呼ぶこと(他ソースでは呼ばない)。"""

    def _base_product(self, source):
        return {
            "brand": "テストブランド", "name": "テスト商品", "price_ref": 2000,
            "_source": source, "item_code": "shop:999",
        }

    def test_product_master_source_triggers_refresh(self):
        step = {"category": "美容液"}
        with patch.object(app, "refresh_selected_candidate_price", side_effect=lambda p: p) as mock_refresh:
            app.apply_db_product_to_step(step, self._base_product("product_master"), {"exp": "middle"})
        mock_refresh.assert_called_once()

    def test_db_source_does_not_trigger_refresh(self):
        step = {"category": "美容液"}
        with patch.object(app, "refresh_selected_candidate_price") as mock_refresh:
            app.apply_db_product_to_step(step, self._base_product("db"), {"exp": "middle"})
        mock_refresh.assert_not_called()

    def test_rakuten_criteria_source_does_not_trigger_refresh(self):
        step = {"category": "美容液"}
        with patch.object(app, "refresh_selected_candidate_price") as mock_refresh:
            app.apply_db_product_to_step(step, self._base_product("rakuten_criteria"), {"exp": "middle"})
        mock_refresh.assert_not_called()


class TwoStagePrescriptionUnaffectedByProductMasterOriginTests(unittest.TestCase):
    """2段階処方(restrict_high_stim_families_for_fragile_skin)が、候補の
    出自(_source_hint/_source)に関わらず同じ結果になることの回帰確認。"""

    def _step(self, focus, source="product_master"):
        return {
            "product": f"テスト{focus}", "category": "美容液",
            "ingredient_focus": [focus], "active_ingredients": [focus],
            "ingredient_strength": {}, "use_days": [],
            "_source_hint": "verified_cache", "_source": source,
        }

    def test_two_families_from_product_master_defer_one_same_as_other_sources(self):
        data = {
            "scores": {"hydration": 40, "barrier": 40},
            "night": {"steps": [
                self._step("vitamin_c"), self._step("azelaic_acid"),
            ]},
            "premium_improvement_priority": [],
        }
        # vitamin_c側はingredient_strength未設定だとA区分(高濃度)判定されない
        # ため、明示的にhighを設定してFamily Aとして検出させる。
        data["night"]["steps"][0]["ingredient_strength"] = {"vitamin_c": "high"}

        result = app.restrict_high_stim_families_for_fragile_skin(data)

        self.assertEqual(len(result["night"]["steps"]), 1)
        self.assertEqual(len(result["deferred_steps"]), 1)
        # 出自に関わらず、残った方・保留された方どちらもproduct_master起源のまま
        for s in result["night"]["steps"] + result["deferred_steps"]:
            self.assertEqual(s.get("_source"), "product_master")


class MergeProductMasterWithLiveCandidateTests(unittest.TestCase):
    """_merge_product_master_with_live_candidate(): 成分・濃度・禁忌・
    formulation等はmaster優先、価格・URL・画像・availability等はライブ優先。"""

    def _master_product(self):
        return {
            "brand": "テストブランド", "name": "テスト商品", "category": "美容液",
            "price": 1000, "price_ref": 1000,
            "active_ingredients": ["niacinamide"], "ingredient_strength": {"niacinamide": "high"},
            "contraindications": ["essential_oil_caution"], "formulation": ["liposome"],
            "image": "old-image.jpg", "rakuten_link": "old-link", "item_code": "shop:111",
            "shop_name": "old-shop", "rakuten_title": "古いタイトル",
            "availability_japan": ["rakuten"],
        }

    def _live_product(self, **overrides):
        live = {
            "price": 2500, "price_ref": 2500,
            "image": "new-image.jpg", "rakuten_link": "new-link", "item_code": "shop:111",
            "shop_name": "new-shop", "rakuten_title": "新しいタイトル",
            "availability_japan": ["rakuten", "amazon"],
        }
        live.update(overrides)
        return live

    def test_ingredient_fields_stay_from_master(self):
        merged = app._merge_product_master_with_live_candidate(self._master_product(), self._live_product())
        self.assertEqual(merged["active_ingredients"], ["niacinamide"])
        self.assertEqual(merged["ingredient_strength"], {"niacinamide": "high"})
        self.assertEqual(merged["contraindications"], ["essential_oil_caution"])
        self.assertEqual(merged["formulation"], ["liposome"])

    def test_price_url_image_availability_come_from_live(self):
        merged = app._merge_product_master_with_live_candidate(self._master_product(), self._live_product())
        self.assertEqual(merged["price"], 2500)
        self.assertEqual(merged["price_ref"], 2500)
        self.assertEqual(merged["image"], "new-image.jpg")
        self.assertEqual(merged["rakuten_link"], "new-link")
        self.assertEqual(merged["availability_japan"], ["rakuten", "amazon"])

    def test_zero_live_price_does_not_overwrite_master_price(self):
        merged = app._merge_product_master_with_live_candidate(
            self._master_product(), self._live_product(price=0, price_ref=0)
        )
        self.assertEqual(merged["price"], 1000)

    def test_merged_product_tagged_as_product_master_source(self):
        merged = app._merge_product_master_with_live_candidate(self._master_product(), self._live_product())
        self.assertEqual(merged["_source"], "product_master")
        self.assertEqual(merged["_source_hint"], "verified_cache")
        self.assertTrue(merged["_merged_with_live_candidate"])


class SelectBestMarketCandidateMergesLiveSameItemCodeTests(unittest.TestCase):
    """select_best_market_candidate()内で、product_masterとライブ候補が
    同一item_codeの場合にscore_product()より前でマージされ、予算適合度の
    採点にも最新価格が使われること。"""

    TEST_CATEGORY = "美容液"
    TEST_SUFFIX = "_MergeTest"

    @classmethod
    def setUpClass(cls):
        app.init_product_master_table()

    def setUp(self):
        self._cleanup()

    def tearDown(self):
        self._cleanup()

    def _cleanup(self):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM product_master WHERE name LIKE %s", (f"%{self.TEST_SUFFIX}%",))
            conn.commit()
        finally:
            conn.close()

    def test_live_price_is_reflected_in_scored_candidate_before_selection(self):
        stale_item = _product_master_item(self.TEST_SUFFIX, category=self.TEST_CATEGORY)
        stale_item["price_ref"] = 1000
        app.upsert_product_master(stale_item, data_source="diagnosis_time")

        live_duplicate = {
            "brand": stale_item["brand"], "name": stale_item["name"], "category": self.TEST_CATEGORY,
            "rakuten_title": stale_item["name"], "price": 9800, "price_ref": 9800,
            "item_code": stale_item["item_code"], "rakuten_link": "https://new-live-link/",
            "image": "https://new-live-image/", "availability_japan": ["rakuten"],
            "_source_hint": "rakuten_criteria",
        }
        step = {"category": self.TEST_CATEGORY, "purpose": "毛穴ケア", "ingredient_focus": "niacinamide"}

        with patch.object(app, "PRODUCT_MASTER_SKIP_RAKUTEN_ENABLED", False), \
             patch.object(app, "search_rakuten_for_step", return_value=[live_duplicate]):
            result = app.select_best_market_candidate(
                step, db_products=[], user_data={"oil": "normal", "sens": "low", "exp": "middle"},
                budget_value=3000, verified_products=[],
            )

        self.assertIsNotNone(result)
        self.assertEqual(result.get("price_ref"), 9800)
        # 成分はmaster由来のまま保持されていること
        self.assertEqual(result.get("active_ingredients"), stale_item["active_ingredients"])


class AccumulateVerifiedProductTests(unittest.TestCase):
    """accumulate_verified_product(): 楽天由来+実在確認済み+最終選定された
    場合に、verified_products_cacheとproduct_masterの両方へ保存されること。"""

    def test_real_verified_product_saves_to_both_caches(self):
        fake_verified_product = {"brand": "B", "name": "N", "category": "美容液"}
        with patch.object(app, "build_verified_product_from_step", return_value=fake_verified_product), \
             patch.object(app, "upsert_verified_product_cache") as mock_cache_upsert, \
             patch.object(app, "upsert_product_master") as mock_master_upsert:
            result = app.accumulate_verified_product({"product": "N"}, {"rakuten_title": "N"})

        mock_cache_upsert.assert_called_once_with(fake_verified_product)
        mock_master_upsert.assert_called_once_with(fake_verified_product, data_source="diagnosis_time")
        self.assertEqual(result, fake_verified_product)

    def test_unverified_product_saves_to_neither_cache(self):
        # build_verified_product_from_step()がNone(実在確認失敗)の場合、
        # どちらのキャッシュにも保存しない。
        with patch.object(app, "build_verified_product_from_step", return_value=None), \
             patch.object(app, "upsert_verified_product_cache") as mock_cache_upsert, \
             patch.object(app, "upsert_product_master") as mock_master_upsert:
            result = app.accumulate_verified_product({"product": "N"}, {"rakuten_title": "N"})

        mock_cache_upsert.assert_not_called()
        mock_master_upsert.assert_not_called()
        self.assertIsNone(result)

    def test_exception_is_contained_and_does_not_propagate(self):
        with patch.object(app, "build_verified_product_from_step", side_effect=RuntimeError("boom")):
            result = app.accumulate_verified_product({"product": "N"}, {"rakuten_title": "N"})
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
