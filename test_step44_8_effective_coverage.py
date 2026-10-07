"""Step44.8: coverageのeffective_countを「診断時に実際に推薦候補として使える
独立商品数」に一致させるためのテスト。

- 美容機器/サプリ: relevance + 販売情報 + 診断候補生成の除外(単品優先)
- 化粧品: products.json → verified cache → product_masterの先勝ちdedup後に
  実際に使われる商品データで判定し、product_master登録済みidentityだけを数える
- identity/variant重複は1件
- coverage不足 → work queue → Discoveryの不足数が連動
- 実DBの行の形(rakuten_link/image)でも販売情報ありと判定される

Gemini/Rakuten実APIは一切呼ばない。
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import psycopg2  # noqa: E402

import app  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402
import product_master_pipeline as orchestrator  # noqa: E402

SUFFIX = "_Step448Test"


def _sale(row, code=None):
    code = code or f"ic{abs(hash((row['brand'], row['name']))) % 10**8}"
    return dict(row, item_code=code, rakuten_link=f"https://item.rakuten.co.jp/shop/{code}/",
                image=f"https://image.example.com/{code}.jpg",
                rakuten_title=f"{row['brand']} {row['name']}", price_ref=3000)


def _device(i, method="RF", sale=True, name=None):
    row = {"brand": f"デバイス{i}", "name": name or f"美顔器{chr(65 + i)}", "category": "美容機器",
           "category_attributes": {"method": method}}
    return _sale(row) if sale else row


def _supp(i, ingredient="亜鉛", sale=True):
    row = {"brand": f"サプリ{i}", "name": f"サプリ{chr(65 + i)}", "category": "サプリメント",
           "active_ingredients": [ingredient],
           "category_attributes": {"primary_ingredient_tags": [app.normalize_ingredient_tag(ingredient)]}}
    return _sale(row) if sale else row


def _cosme(brand, name, actives, category="化粧水"):
    return {"brand": brand, "name": name, "category": category, "active_ingredients": list(actives)}


def _report(policy, rows_by_category, db_products=(), verified=()):
    with patch.object(app, "query_product_master_candidates",
                      side_effect=lambda c, limit=30: [dict(r) for r in rows_by_category.get(c, [])]):
        return {a["target"]: a for a in app.get_coverage_report(
            policy, db_products=list(db_products), verified_products=list(verified))}


class DeviceAndSupplementEffectiveCountTests(unittest.TestCase):

    def test_device_three_rows_one_without_sales_info(self):
        rows = [_device(0), _device(1), _device(2, sale=False)]
        area = _report("beauty_device", {"美容機器": rows})["RF"]
        self.assertEqual((area["effective_count"], area["shortage_count"], area["sufficient"]), (2, 1, False))

    def test_supplement_three_rows_one_without_sales_info(self):
        rows = [_supp(0), _supp(1), _supp(2, sale=False)]
        area = _report("supplement", {"サプリメント": rows})["zinc"]
        self.assertEqual((area["effective_count"], area["shortage_count"], area["sufficient"]), (2, 1, False))

    def test_three_usable_rows_are_sufficient(self):
        area = _report("beauty_device", {"美容機器": [_device(i) for i in range(3)]})["RF"]
        self.assertEqual((area["effective_count"], area["shortage_count"], area["sufficient"]), (3, 0, True))
        area = _report("supplement", {"サプリメント": [_supp(i) for i in range(3)]})["zinc"]
        self.assertEqual((area["effective_count"], area["sufficient"]), (3, True))

    def test_relevance_mismatch_is_not_counted(self):
        report = _report("beauty_device", {"美容機器": [_device(0, method="LED"), _device(1, method="LED")]})
        self.assertEqual(report["RF"]["effective_count"], 0)
        self.assertEqual(report["LED"]["effective_count"], 2)
        report = _report("supplement", {"サプリメント": [_supp(0, "コラーゲン")]})
        self.assertEqual(report["zinc"]["effective_count"], 0)
        self.assertEqual(report["collagen"]["effective_count"], 1)

    def test_same_product_rows_count_once(self):
        # 同一商品名(ブランド表記違い)の2行は独立候補1件。
        rows = [_device(0, name="RF美顔器プロ"), _sale({"brand": "", "name": "RF美顔器プロ", "category": "美容機器",
                                                       "category_attributes": {"method": "RF"}})]
        self.assertEqual(_report("beauty_device", {"美容機器": rows})["RF"]["effective_count"], 1)

    def test_set_item_is_not_counted_when_single_item_exists(self):
        # 診断時のselect_best_*_candidate()と同じ単品優先(セット販売は候補に残らない)。
        single = _device(0, name="RF美顔器X")
        set_row = _sale({"brand": "デバイスS", "name": "RF美顔器Y 2個セット", "category": "美容機器",
                         "category_attributes": {"method": "RF"}})
        self.assertEqual(_report("beauty_device", {"美容機器": [single, set_row]})["RF"]["effective_count"], 1)


class CosmeticsEffectiveCountTests(unittest.TestCase):

    def test_products_json_version_lacking_target_is_not_counted(self):
        # Step44.7の#21無印良品と同じ形: product_masterには成分があるが、
        # 診断時に先勝ちで使われるproducts.json側には無い。
        pm = _cosme("ブランドA", "化粧水A", ["ヒアルロン酸Na", "hyaluronic_acid"])
        db = _cosme("ブランドA", "化粧水A", ["glycerin"])
        area = _report("cosmetics", {"化粧水": [pm]}, db_products=[db])["hyaluronic_acid"]
        self.assertEqual(area["effective_count"], 0)

    def test_products_json_version_with_target_is_counted(self):
        pm = _cosme("ブランドB", "化粧水B", ["ヒアルロン酸Na", "hyaluronic_acid"])
        db = _cosme("ブランドB", "化粧水B", ["hyaluronic_acid"])
        area = _report("cosmetics", {"化粧水": [pm]}, db_products=[db])["hyaluronic_acid"]
        self.assertEqual(area["effective_count"], 1)

    def test_products_json_only_is_not_counted(self):
        db = [_cosme(f"ブランド{i}", f"化粧水DB{i}", ["hyaluronic_acid"]) for i in range(3)]
        area = _report("cosmetics", {"化粧水": []}, db_products=db)["hyaluronic_acid"]
        self.assertEqual(area["effective_count"], 0)

    def test_diagnosis_category_exclusion_is_reflected(self):
        # 診断時のis_candidate_wrong_for_category()で落ちる商品は数えない
        # (Step44.7のクレアラシルと同じ形。除外ルール自体は変更しない)。
        rows = [_cosme("ブランドC", "薬用洗顔クリーム", ["サリチル酸", "salicylic_acid"], category="洗顔"),
                _cosme("ブランドD", "薬用ウォッシュ", ["サリチル酸", "salicylic_acid"], category="洗顔")]
        area = _report("cosmetics", {"洗顔": rows})["salicylic_acid"]
        self.assertEqual(area["effective_count"], 1)

    def test_same_product_name_variant_counts_once(self):
        rows = [_cosme("ブランドE", "濃密化粧水", ["ヒアルロン酸Na", "hyaluronic_acid"]),
                _cosme("", "濃密化粧水", ["ヒアルロン酸Na", "hyaluronic_acid"])]
        area = _report("cosmetics", {"化粧水": rows})["hyaluronic_acid"]
        self.assertEqual(area["effective_count"], 1)


class ShortageToDiscoveryLinkTests(unittest.TestCase):

    def test_shortage_flows_to_work_queue_and_discovery_plan(self):
        rows = [_device(0), _device(1), _device(2, sale=False)]
        fake_query = lambda c, limit=30: [dict(r) for r in rows] if c == "美容機器" else []  # noqa: E731
        forbidden = AssertionError("dry-runでは呼ばれないはず")
        with patch.object(app, "query_product_master_candidates", side_effect=fake_query), \
             patch.object(app, "get_stale_product_master_candidates", return_value=[]), \
             patch.object(app, "get_needs_review_staging_items", return_value=[]), \
             patch.object(orchestrator, "_product_master_identity_keys", return_value=set()), \
             patch.object(orchestrator, "_recently_failed_identity_keys", return_value=set()), \
             patch.object(orchestrator, "_staging_reuse_candidates", return_value=[]), \
             patch.object(app, "load_products", return_value=[]), \
             patch.object(app, "load_verified_products_cache", return_value=[]), \
             patch.object(pipeline, "discover_candidates_via_gemini", side_effect=forbidden), \
             patch.object(pipeline, "collect_one_product", side_effect=forbidden), \
             patch.object(pipeline, "reflect_staging_to_product_master", side_effect=forbidden), \
             patch.object(app, "fetch_rakuten_candidates", side_effect=forbidden):
            queue = app.generate_product_master_work_queue(coverage_policy_name="beauty_device")
            result = orchestrator.run_batch(coverage_policy_name="beauty_device", mode="dry_run")

        rf = next(i for i in queue if i["type"] == "coverage_gap" and i["target"] == "RF")
        self.assertEqual(rf["shortage_count"], 1)
        plan = next(a for a in result["actions"]
                    if a["action"] == "external_discovery_required" and a["target"] == "RF")
        self.assertEqual(plan["shortage_after_existing"], 1)
        others = [a for a in result["actions"]
                  if a["action"] == "external_discovery_required" and a["target"] != "RF"]
        self.assertTrue(all(a["shortage_after_existing"] == 3 for a in others))


class RealProductMasterRowShapeTests(unittest.TestCase):
    """query_product_master_candidates()が返す実際の行(last_known_rakuten_link
    →rakuten_link等に変換済み)でも、診断経路・coverageが販売情報ありと判定
    すること(Step44.8で判明したバグの回帰テスト)。"""

    def setUp(self):
        app.init_product_master_table()
        self._cleanup()

    def tearDown(self):
        self._cleanup()

    def _cleanup(self):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM product_master WHERE name LIKE %s", (f"%{SUFFIX}%",))
            conn.commit()
        finally:
            conn.close()

    def test_real_row_is_usable_in_diagnosis_and_counted_in_coverage(self):
        brand, name = f"実行形{SUFFIX}", f"EMS美顔器{SUFFIX}"
        app.upsert_product_master(_sale({
            "brand": brand, "name": name, "category": "美容機器",
            "category_attributes": {"method": "EMS"},
        }, code="real-shape-001"), data_source="ai_precollected")

        rows = [r for r in app.query_product_master_candidates("美容機器") if r.get("name") == name]
        self.assertEqual(len(rows), 1)
        self.assertNotIn("last_known_rakuten_link", rows[0])

        candidates = app._product_master_candidates_for_live_style_ranking(
            "美容機器", "EMS", app._DEVICE_DEFAULTS["EMS"]["product"], "", master_rows=rows,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0][1]["itemUrl"], "https://item.rakuten.co.jp/shop/real-shape-001/")
        self.assertEqual(candidates[0][1]["mediumImageUrls"],
                         [{"imageUrl": "https://image.example.com/real-shape-001.jpg"}])
        self.assertGreaterEqual(app.calculate_effective_candidates("美容機器", "EMS", db_products=[], verified_products=[]), 1)

        step = {"category": "美容機器", "product": "EMS美顔器", "device_type": "EMS",
                "brand": "", "product_source": "ai", "purpose": "", "reason": ""}
        with patch.object(app, "infer_brand_from_image", return_value=""), \
             patch.object(app, "accumulate_verified_product", return_value=None), \
             patch.object(app, "fetch_rakuten_item_by_item_code",
                          return_value={"ok": False, "http_status": None, "rakuten_error": "test"}), \
             patch.object(app, "query_product_master_candidates", return_value=rows), \
             patch.object(app, "fetch_rakuten_candidates",
                          side_effect=AssertionError("product_master候補がある場合は楽天ライブ検索しないはず")):
            result = app.attach_affiliate_links_to_step(step, [], user_data={"sens": "normal"}, budget_value=20000)
        self.assertEqual(result["rakuten_link"], "https://item.rakuten.co.jp/shop/real-shape-001/")


if __name__ == "__main__":
    unittest.main()
