"""Step47.6: 楽天precheckでのJAN利用と、実楽天API呼び出しの共通計測のテスト。

実Gemini・実楽天・実HTTP・本番DBは使わない。
"""

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import psycopg2  # noqa: E402

import app  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402
import product_master_pipeline as orchestrator  # noqa: E402
from test_product_master_pipeline_orchestrator import OrchestratorTestBase, TEST_NAME_SUFFIX  # noqa: E402

JAN_102 = "4987035691914"
BRAND, NAME = "ネイチャーメイド（大塚製薬）", "ネイチャーメイド スーパーフィッシュオイル"


def _item(code, title, caption="", price=1880, shop="shop", reviews=1):
    return {"itemCode": code, "itemName": title, "itemCaption": caption, "itemPrice": price,
            "itemUrl": f"https://item.rakuten.co.jp/{code.replace(':', '/')}/", "shopName": shop,
            "reviewCount": reviews, "reviewAverage": 4.0, "mediumImageUrls": [{"imageUrl": "https://img.example/x.jpg"}]}


# Step47.5の実データ(product_id 78)相当: JAN一致の90粒単品と、名前が似ているがJAN不一致の候補。
CORRECT_90 = _item("irisplaza-r:10166330",
                   "【1個】ネイチャーメイド スーパーフィッシュオイル（90粒） ネイチャーメイド 大塚製薬 サプリメント",
                   caption=f"JANコード：{JAN_102} 1日1粒を目安に", shop="アイリスオーヤマ公式 楽天市場店")
CORRECT_90_OTHER_SHOP = _item("akarie:10171576", "ネイチャーメイド スーパーフィッシュオイル 90粒",
                              caption=f"JAN {JAN_102}", price=1890, shop="健幸館")
SIMILAR_OTHER_JAN = _item("other:200", "ネイチャーメイド スーパーフィッシュオイル 徳用180粒",
                          caption="JANコード：4987035999999", price=3200, shop="別ショップ", reviews=500)


def resolve(items, jan=JAN_102):
    with patch.object(app, "fetch_rakuten_candidates", return_value=items):
        return pipeline.resolve_item_code_for_product(BRAND, NAME, "サプリメント", jan_code=jan)


class JanSelectionTests(unittest.TestCase):

    def test_jan_match_is_selected_over_higher_scored_mismatch(self):
        result = resolve([(300, SIMILAR_OTHER_JAN), (100, CORRECT_90)])
        self.assertEqual((result["status"], result["disambiguated_by"]), ("confirmed", "jan_code"))
        self.assertEqual(result["item"]["itemCode"], "irisplaza-r:10166330")

    def test_only_jan_mismatch_candidates_is_safe_not_found(self):
        result = resolve([(300, SIMILAR_OTHER_JAN)])
        self.assertEqual((result["status"], result["reason"]), ("not_found", "jan_mismatch"))

    def test_multiple_jan_matches_use_merchant_tiebreak_among_them_only(self):
        result = resolve([(500, SIMILAR_OTHER_JAN), (247, CORRECT_90), (202, CORRECT_90_OTHER_SHOP)])
        self.assertEqual(result["disambiguated_by"], "jan_code+merchant_tiebreak")
        self.assertEqual({p["item_code"] for p in result["tiebreak_pool"]},
                         {"irisplaza-r:10166330", "akarie:10171576"})
        self.assertNotEqual(result["item"]["itemCode"], "other:200")

    def test_without_jan_existing_name_matching_is_used(self):
        self.assertEqual(resolve([(100, CORRECT_90)], jan=None)["status"], "confirmed")
        self.assertEqual(resolve([(100, CORRECT_90)], jan="unknown")["status"], "confirmed")

    def test_jan_normalization(self):
        self.assertEqual(pipeline.normalize_jan_code("４９８７０３５６９１９１４"), JAN_102)
        self.assertEqual(pipeline.normalize_jan_code("498-7035-691914"), JAN_102)
        self.assertEqual(pipeline.normalize_jan_code("unknown"), "")
        self.assertEqual(pipeline.normalize_jan_code("12345"), "")
        self.assertFalse(pipeline.rakuten_item_mentions_jan(_item("x:1", "t", caption="149870356919145"), JAN_102))

    def test_existing_safety_filters_still_apply_with_jan(self):
        rental = _item("r:1", "ネイチャーメイド スーパーフィッシュオイル 1ヶ月レンタル", caption=f"JAN {JAN_102}")
        bundle = _item("b:1", "ネイチャーメイド スーパーフィッシュオイル 90粒 3個セット", caption=f"JAN {JAN_102}")
        self.assertEqual(resolve([(100, rental)])["reason"], "only_non_new_sale_listings")
        self.assertEqual(resolve([(100, bundle)])["status"], "not_found")


class VerifyWithResolutionTests(unittest.TestCase):

    def test_preresolved_candidate_without_jan_is_not_saved(self):
        resolution = {"status": "confirmed", "item": SIMILAR_OTHER_JAN}
        with patch.object(app, "fetch_rakuten_item_by_item_code", side_effect=AssertionError("照会しないはず")), \
             patch.object(app, "update_product_master_item_code_fields", side_effect=AssertionError("保存しないはず")):
            result = pipeline.verify_and_resolve_item_code(1, BRAND, NAME, "サプリメント", jan_code=JAN_102,
                                                           resolution=resolution)
        self.assertEqual((result["status"], result["reason"]), ("not_found", "jan_mismatch"))


class PrecheckPassesJanTests(OrchestratorTestBase):

    def test_staged_jan_reaches_resolver_and_wrong_variant_is_not_reflected(self):
        brand, name = f"ネイチャーメイド{TEST_NAME_SUFFIX}", f"スーパーフィッシュオイル{TEST_NAME_SUFFIX}"
        cit = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ_s476"
        self._insert_staging_row(self._new_batch_id("s476"), brand, name, "サプリメント",
                                 stage2_payload={"jan_code": JAN_102,
                                                 "active_ingredients": [{"ingredient": "EPA", "source_url": cit},
                                                                        {"ingredient": "DHA", "source_url": cit}],
                                                 "formulation_features": [],
                                                 "category_attributes": {"primary_ingredients": {
                                                     "value": "EPA、DHA", "confidence": "high", "source_url": cit},
                                                     "product_classification": {
                                                     "value": "supplement", "confidence": "high", "source_url": cit}}},
                                 citations=[{"uri": cit, "title": brand}],
                                 stage1_raw_text="EPA・DHAのサプリメント。")
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT staging_id FROM product_collection_staging WHERE brand = %s", (brand,))
            sid = cur.fetchone()[0]
        finally:
            conn.close()
        mismatch = _item("other:300", f"{brand} {name} 徳用", caption="JAN 4987035999999")
        budget = orchestrator.BatchBudget(self._new_batch_id("s476-exec"), 5, 20, 0.50)
        with patch.object(app, "fetch_rakuten_candidates", return_value=[(300, mismatch)]), \
             patch.object(pipeline, "reflect_staging_to_product_master", side_effect=AssertionError("reflectしないはず")), \
             patch.object(pipeline.citation_verification, "fetch_html", side_effect=AssertionError("no http")):
            actions = orchestrator.process_coverage_gap_item(
                {"category": "サプリメント", "target": "omega3", "shortage_count": 1}, "execute", budget.batch_id, budget,
                lambda c, t, n: [{"brand": brand, "name": name, "category": c,
                                  "discovery_source": "staging_reuse", "staging_id": sid}], 3, set(),
            )
        self.assertEqual(actions[0]["reason"], "rakuten_new_listing_not_found")
        self.assertEqual(actions[0]["rakuten_reason"], "jan_mismatch")


class RakutenMeteringTests(unittest.TestCase):

    def _fake_response(self, payload):
        res = MagicMock()
        res.status_code = 200
        res.json.return_value = payload
        res.text = ""
        return res

    def test_item_code_lookup_is_counted_once_per_real_request(self):
        before = app.rakuten_api_call_snapshot()
        with patch.object(app.requests, "get", return_value=self._fake_response({"Items": [{"itemPrice": 1880}]})), \
             patch.object(app, "wait_for_rakuten_rate_limit", return_value=None):
            result = app.fetch_rakuten_item_by_item_code("irisplaza-r:10166330")
        self.assertTrue(result["ok"])
        self.assertEqual(app.rakuten_api_call_delta(before), {"item_code_lookup": 1})

    def test_mocked_candidate_search_without_http_is_not_counted(self):
        before = app.rakuten_api_call_snapshot()
        resolve([(100, CORRECT_90)])
        self.assertEqual(app.rakuten_api_call_delta(before), {})

    def test_reflect_action_reports_rakuten_calls(self):
        verified = self._fake_response({"Items": [{"itemPrice": 1880, "itemUrl": "https://item.rakuten.co.jp/x/1/"}]})
        with patch.object(app.requests, "get", return_value=verified), \
             patch.object(app, "wait_for_rakuten_rate_limit", return_value=None), \
             patch.object(app, "update_product_master_item_code_fields", return_value=True):
            before = app.rakuten_api_call_snapshot()
            result = pipeline.verify_and_resolve_item_code(1, BRAND, NAME, "サプリメント", jan_code=JAN_102,
                                                           resolution={"status": "confirmed", "item": CORRECT_90})
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(app.rakuten_api_call_delta(before), {"item_code_lookup": 1})


if __name__ == "__main__":
    unittest.main()
