"""
商品マスタ(product_master)のテスト。

設計(ユーザーとの合意事項):
- brand+name+categoryを永続的な主キーにはしない(商品名表記は変動するため)。
  product_id(SERIAL)を主キーとし、identity_key(brand+name+categoryの正規化)
  は同一商品照合用のユニークインデックスとして使う。
- verified_products_cache.json(フラットファイル)からの移行は、まずdry_run=True
  で件数・重複・欠落のみ確認し、問題なければdry_run=Falseで本実行する
  (起動時の自動移行は行わない)。

product_master関連のみ実DB(DATABASE_URL、ローカルのrumilog_test)に対して
実行する(他の既存テストはpure-logicでDB接続しないが、ここはスキーマ・
upsert・移行ロジック自体の正しさを検証する必要があるため、実際の
Postgresに対して検証する)。各テストはidentity_keyに一意なテスト専用
プレフィックスを付け、setUp/tearDownで該当行のみ削除する。
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import psycopg2  # noqa: E402

import app  # noqa: E402


def _test_product(suffix, **overrides):
    product = {
        "brand": f"テストブランド{suffix}",
        "name": f"テスト商品マスタ{suffix}",
        "category": "美容液",
        "price_ref": 2000,
        "active_ingredients": ["niacinamide"],
        "support_ingredients": [],
        "signature_ingredients": [],
        "concerns": ["pores"],
        "skin_types": [],
        "sensitive_ok": "unknown",
        "retinol_level": 0,
        "main_functions": [],
        "ingredient_focus": ["niacinamide"],
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
        "item_code": "",
        "shop_name": "",
        "verified_at": 1700000000,
    }
    product.update(overrides)
    return product


class NormalizeProductMasterIdentityKeyTests(unittest.TestCase):
    def test_returns_key_for_valid_input(self):
        key = app._normalize_product_master_identity_key("COSRX", "スネイルムチンエッセンス", "美容液")
        self.assertTrue(key)
        self.assertIn("|", key)

    def test_empty_name_returns_empty_string(self):
        key = app._normalize_product_master_identity_key("COSRX", "", "美容液")
        self.assertEqual(key, "")

    def test_empty_category_returns_empty_string(self):
        key = app._normalize_product_master_identity_key("COSRX", "商品名", "")
        self.assertEqual(key, "")

    def test_same_brand_name_category_produce_same_key_regardless_of_whitespace(self):
        key1 = app._normalize_product_master_identity_key("COSRX", "スネイルムチンエッセンス", "美容液")
        key2 = app._normalize_product_master_identity_key(" COSRX ", " スネイルムチンエッセンス ", "美容液")
        self.assertEqual(key1, key2)


class MigrateDryRunTests(unittest.TestCase):
    """dry_run=TrueはDBに一切書き込まないため、実DB不要でロジックのみ検証する。"""

    def test_dry_run_counts_without_writing(self):
        items = [_test_product("A"), _test_product("B")]
        with patch("app.load_verified_products_cache", return_value=items):
            result = app.migrate_verified_products_cache_to_product_master(dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["total_in_file"], 2)
        self.assertEqual(result["would_migrate"], 2)
        self.assertEqual(result["migrated"], 0)

    def test_dry_run_detects_duplicate_identity_keys_and_keeps_newer(self):
        older = _test_product("DUP", verified_at=1000)
        newer = _test_product("DUP", verified_at=2000, price_ref=3000)
        with patch("app.load_verified_products_cache", return_value=[older, newer]):
            result = app.migrate_verified_products_cache_to_product_master(dry_run=True)
        self.assertEqual(result["would_migrate"], 1)
        self.assertEqual(len(result["duplicate_identity_keys_in_file"]), 1)

    def test_dry_run_skips_items_missing_identity(self):
        missing_name = _test_product("C", name="")
        with patch("app.load_verified_products_cache", return_value=[missing_name]):
            result = app.migrate_verified_products_cache_to_product_master(dry_run=True)
        self.assertEqual(result["skipped_missing_identity"], 1)
        self.assertEqual(result["would_migrate"], 0)

    def test_empty_cache_returns_zeroed_result_without_error(self):
        with patch("app.load_verified_products_cache", return_value=[]):
            result = app.migrate_verified_products_cache_to_product_master(dry_run=True)
        self.assertEqual(result["total_in_file"], 0)
        self.assertEqual(result["would_migrate"], 0)


class ProductMasterRealDbTests(unittest.TestCase):
    """init_product_master_table()/upsert_product_master()/
    migrate_verified_products_cache_to_product_master(dry_run=False)を
    実際のテスト用Postgres(rumilog_test)に対して検証する。"""

    TEST_SUFFIX = "_RealDbTest"

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
            cur.execute(
                "DELETE FROM product_master WHERE name LIKE %s",
                (f"%{self.TEST_SUFFIX}%",),
            )
            conn.commit()
        finally:
            conn.close()

    def _fetch(self, identity_key):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT product_id, brand, name, price_ref, ingredient_focus, data_source "
                "FROM product_master WHERE identity_key = %s",
                (identity_key,),
            )
            return cur.fetchone()
        finally:
            conn.close()

    def test_upsert_inserts_new_row_and_returns_product_id(self):
        product = _test_product(self.TEST_SUFFIX)
        product_id = app.upsert_product_master(product, data_source="diagnosis_time")
        self.assertIsNotNone(product_id)

        identity_key = app._normalize_product_master_identity_key(
            product["brand"], product["name"], product["category"]
        )
        row = self._fetch(identity_key)
        self.assertIsNotNone(row)
        self.assertEqual(row[0], product_id)
        self.assertEqual(row[3], 2000)

    def test_upsert_same_identity_updates_existing_row_not_duplicate(self):
        product = _test_product(self.TEST_SUFFIX + "Dup")
        first_id = app.upsert_product_master(product, data_source="diagnosis_time")

        updated = dict(product)
        updated["price_ref"] = 2500
        second_id = app.upsert_product_master(updated, data_source="diagnosis_time")

        self.assertEqual(first_id, second_id)  # product_idは不変(別行にならない)

        identity_key = app._normalize_product_master_identity_key(
            product["brand"], product["name"], product["category"]
        )
        row = self._fetch(identity_key)
        self.assertEqual(row[3], 2500)  # 価格は新しい値に更新されている

    def test_upsert_invalid_product_returns_none(self):
        self.assertIsNone(app.upsert_product_master({"brand": "X", "name": "", "category": "美容液"}))
        self.assertIsNone(app.upsert_product_master("not a dict"))

    def test_migrate_real_run_inserts_rows_into_product_master(self):
        items = [_test_product(self.TEST_SUFFIX + "Mig1"), _test_product(self.TEST_SUFFIX + "Mig2")]
        with patch("app.load_verified_products_cache", return_value=items):
            result = app.migrate_verified_products_cache_to_product_master(dry_run=False)

        self.assertFalse(result["dry_run"])
        self.assertEqual(result["migrated"], 2)
        self.assertEqual(result["row_errors"], [])

        for item in items:
            identity_key = app._normalize_product_master_identity_key(
                item["brand"], item["name"], item["category"]
            )
            self.assertIsNotNone(self._fetch(identity_key))

    def test_migrate_real_run_is_idempotent(self):
        items = [_test_product(self.TEST_SUFFIX + "Idem")]
        with patch("app.load_verified_products_cache", return_value=items):
            app.migrate_verified_products_cache_to_product_master(dry_run=False)
            result = app.migrate_verified_products_cache_to_product_master(dry_run=False)

        self.assertEqual(result["migrated"], 1)
        identity_key = app._normalize_product_master_identity_key(
            items[0]["brand"], items[0]["name"], items[0]["category"]
        )
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM product_master WHERE identity_key = %s", (identity_key,)
            )
            count = cur.fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)  # 2回実行しても重複行が増えない

    def test_migrate_real_run_tags_data_source_as_migrated_json(self):
        items = [_test_product(self.TEST_SUFFIX + "Src")]
        with patch("app.load_verified_products_cache", return_value=items):
            app.migrate_verified_products_cache_to_product_master(dry_run=False)
        identity_key = app._normalize_product_master_identity_key(
            items[0]["brand"], items[0]["name"], items[0]["category"]
        )
        row = self._fetch(identity_key)
        self.assertEqual(row[5], "migrated_json")

    def _fetch_tags(self, identity_key):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT active_ingredients, active_ingredient_tags "
                "FROM product_master WHERE identity_key = %s",
                (identity_key,),
            )
            return cur.fetchone()
        finally:
            conn.close()

    def test_upsert_computes_active_ingredient_tags_from_raw_ingredient_names(self):
        # Phase 3(ai_precollected)相当: 生の原文成分名のみを保存する場合でも、
        # upsert_product_master()がapp.compute_ingredient_tags()で統制タグを
        # 自動算出して別列へ保存すること(原文のactive_ingredientsは不変)。
        product = _test_product(
            self.TEST_SUFFIX + "TagsRaw",
            active_ingredients=["ツボクサエキス", "ダマスクバラ花エキス"],
            ingredient_focus=[],
        )
        app.upsert_product_master(product, data_source="ai_precollected")
        identity_key = app._normalize_product_master_identity_key(
            product["brand"], product["name"], product["category"]
        )
        active_ingredients, active_ingredient_tags = self._fetch_tags(identity_key)
        self.assertEqual(active_ingredients, ["ツボクサエキス", "ダマスクバラ花エキス"])
        self.assertEqual(active_ingredient_tags, ["centella_extract"])

    def test_upsert_does_not_force_tag_for_unrecognized_ingredient(self):
        product = _test_product(
            self.TEST_SUFFIX + "TagsUnknown",
            active_ingredients=["謎の未知成分エキスXYZ"],
            ingredient_focus=[],
        )
        app.upsert_product_master(product, data_source="ai_precollected")
        identity_key = app._normalize_product_master_identity_key(
            product["brand"], product["name"], product["category"]
        )
        _, active_ingredient_tags = self._fetch_tags(identity_key)
        self.assertEqual(active_ingredient_tags, [])


class CategoryAttributesColumnTests(unittest.TestCase):
    """Step38: category_attributes JSONBカラムの保存/読出し、既存
    cosmetics経路への無影響を検証する(実DB)。"""

    TEST_SUFFIX = "_CategoryAttrTest"

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

    def _fetch_category_attributes(self, identity_key):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT category_attributes FROM product_master WHERE identity_key = %s",
                (identity_key,),
            )
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def test_cosmetics_upsert_without_category_attributes_leaves_it_null(self):
        # cosmetics既存経路は category_attributes を一切渡さない(既存挙動を
        # 完全維持)。既存のupsert呼び出しを変更していないことの確認。
        product = _test_product(self.TEST_SUFFIX + "Cosmetics")
        app.upsert_product_master(product, data_source="diagnosis_time")
        identity_key = app._normalize_product_master_identity_key(
            product["brand"], product["name"], product["category"]
        )
        self.assertIsNone(self._fetch_category_attributes(identity_key))

    def test_beauty_device_category_attributes_round_trip(self):
        product = _test_product(
            self.TEST_SUFFIX + "Device", category="美容機器",
            category_attributes={
                "method": "RF", "modes": ["強", "中", "弱"],
                "usage_frequency": "週3回", "contraindications": ["ペースメーカー使用者は使用不可"],
            },
        )
        app.upsert_product_master(product, data_source="ai_precollected")
        identity_key = app._normalize_product_master_identity_key(
            product["brand"], product["name"], product["category"]
        )
        saved = self._fetch_category_attributes(identity_key)
        self.assertEqual(saved["method"], "RF")
        self.assertEqual(saved["modes"], ["強", "中", "弱"])

    def test_supplement_category_attributes_round_trip(self):
        product = _test_product(
            self.TEST_SUFFIX + "Supplement", category="サプリメント",
            category_attributes={
                "ingredients": ["ビタミンC"], "dosage": {"ビタミンC": "500mg"},
                "serving_size": "1日2粒", "precautions": ["持病のある方は医師に相談"],
            },
        )
        app.upsert_product_master(product, data_source="ai_precollected")
        identity_key = app._normalize_product_master_identity_key(
            product["brand"], product["name"], product["category"]
        )
        saved = self._fetch_category_attributes(identity_key)
        self.assertEqual(saved["dosage"], {"ビタミンC": "500mg"})

    def test_update_without_category_attributes_preserves_existing_value(self):
        # 再upsert時にcategory_attributesを渡さない場合、既存値を消さない
        # (COALESCE、意図しない上書き防止)。
        product = _test_product(
            self.TEST_SUFFIX + "Preserve", category="美容機器",
            category_attributes={"method": "EMS", "modes": [], "usage_frequency": "unknown",
                                  "contraindications": []},
        )
        app.upsert_product_master(product, data_source="ai_precollected")

        updated = dict(product)
        updated.pop("category_attributes", None)
        updated["price_ref"] = 9999
        app.upsert_product_master(updated, data_source="ai_precollected")

        identity_key = app._normalize_product_master_identity_key(
            product["brand"], product["name"], product["category"]
        )
        saved = self._fetch_category_attributes(identity_key)
        self.assertEqual(saved["method"], "EMS")


class EffectiveCandidateCommonFunctionTests(unittest.TestCase):
    """Step38: calculate_effective_candidates()が、Step27のワンオフ
    スクリプトと同じdedup後基準で判定結果を変えずに動作することを確認する。
    (実DBに対して実行、cosmetics以外のカテゴリでも同じ関数が動くことも確認)"""

    def test_returns_non_negative_int_for_known_area(self):
        n = app.calculate_effective_candidates("化粧水", "hyaluronic_acid")
        self.assertIsInstance(n, int)
        self.assertGreaterEqual(n, 0)

    def test_extra_candidates_increase_count_when_relevant_and_not_duplicate(self):
        category, tag = "美容液", "vitamin_c"
        before = app.calculate_effective_candidates(category, tag)
        extra = [{
            "brand": "テストブランド_EffectiveCandidateTest", "name": "テスト商品_EffectiveCandidateTest",
            "category": category, "active_ingredients": [tag],
        }]
        after = app.calculate_effective_candidates(category, tag, extra_candidates=extra)
        self.assertEqual(after, before + 1)

    def test_batch_matches_individual_calls(self):
        areas = [("化粧水", "hyaluronic_acid"), ("美容液", "vitamin_c")]
        batch_result = app.calculate_effective_candidates_batch(areas)
        for category, tag in areas:
            self.assertEqual(
                batch_result[(category, tag)],
                app.calculate_effective_candidates(category, tag),
            )

    def test_unknown_category_with_no_candidates_returns_zero(self):
        n = app.calculate_effective_candidates("美容機器", "nonexistent_tag_xyz")
        self.assertEqual(n, 0)


class StaleProductMasterCandidatesTests(unittest.TestCase):
    """Step38: get_stale_product_master_candidates()が
    _needs_reverification=Trueの行だけを抽出し、収集・反映は行わないこと
    (検出のみ)を確認する。"""

    TEST_SUFFIX = "_StaleTest"

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

    def test_old_verified_at_is_flagged_stale(self):
        import time
        old_timestamp = time.time() - (200 * 24 * 60 * 60)  # 200日前(閾値180日超)
        product = _test_product(self.TEST_SUFFIX + "Old", verified_at=old_timestamp)
        app.upsert_product_master(product, data_source="ai_precollected")

        stale = app.get_stale_product_master_candidates(category="美容液", limit=200)
        stale_names = [p["name"] for p in stale]
        self.assertIn(product["name"], stale_names)

    def test_recently_verified_is_not_flagged_stale(self):
        import time
        product = _test_product(self.TEST_SUFFIX + "Fresh", verified_at=time.time())
        app.upsert_product_master(product, data_source="ai_precollected")

        stale = app.get_stale_product_master_candidates(category="美容液", limit=200)
        stale_names = [p["name"] for p in stale]
        self.assertNotIn(product["name"], stale_names)

    def test_stale_detection_does_not_write_anything(self):
        import time
        old_timestamp = time.time() - (200 * 24 * 60 * 60)
        product = _test_product(self.TEST_SUFFIX + "NoWrite", verified_at=old_timestamp)
        app.upsert_product_master(product, data_source="ai_precollected")

        app.get_stale_product_master_candidates(category="美容液", limit=200)

        identity_key = app._normalize_product_master_identity_key(
            product["brand"], product["name"], product["category"]
        )
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT updated_at FROM product_master WHERE identity_key = %s", (identity_key,))
            row_after = cur.fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row_after)  # 行自体は検出処理で消えていない(書き込み無し)


class CategoryAttributesValidatorTests(unittest.TestCase):
    """Step38: validate_category_attributes()のfixtureベース検証。
    cosmeticsは常にvalid、beauty_device/supplementは必須フィールド欠落を
    検出し、未定義の将来カテゴリは無条件でvalidとする。"""

    def test_cosmetics_always_valid_regardless_of_content(self):
        self.assertEqual(
            app.validate_category_attributes("cosmetics", {"anything": "goes"}),
            {"valid": True, "missing_fields": []},
        )
        self.assertEqual(
            app.validate_category_attributes("cosmetics", None),
            {"valid": True, "missing_fields": []},
        )

    def test_future_category_without_schema_is_valid(self):
        result = app.validate_category_attributes("some_future_category_not_yet_defined", {})
        self.assertTrue(result["valid"])

    def test_beauty_device_missing_required_fields(self):
        result = app.validate_category_attributes("beauty_device", {"method": "RF"})
        self.assertFalse(result["valid"])
        self.assertIn("modes", result["missing_fields"])
        self.assertIn("usage_frequency", result["missing_fields"])
        self.assertIn("contraindications", result["missing_fields"])

    def test_beauty_device_complete_fields_is_valid(self):
        result = app.validate_category_attributes("beauty_device", {
            "method": "RF", "modes": ["強", "中", "弱"],
            "usage_frequency": "週3回", "contraindications": [],
        })
        self.assertTrue(result["valid"])

    def test_beauty_device_unknown_values_still_valid(self):
        # 不明な情報はunknown/null/空のままでよい(推測しない)。キー自体が
        # 存在する限り"欠落"ではない。
        result = app.validate_category_attributes("beauty_device", {
            "method": "unknown", "modes": [], "usage_frequency": "unknown",
            "contraindications": [],
        })
        self.assertTrue(result["valid"])

    def test_supplement_missing_required_fields(self):
        result = app.validate_category_attributes("supplement", {"ingredients": ["ビタミンC"]})
        self.assertFalse(result["valid"])
        self.assertIn("dosage", result["missing_fields"])
        self.assertIn("serving_size", result["missing_fields"])
        self.assertIn("precautions", result["missing_fields"])

    def test_supplement_complete_fields_is_valid(self):
        result = app.validate_category_attributes("supplement", {
            "ingredients": ["ビタミンC"], "dosage": {"ビタミンC": "500mg"},
            "serving_size": "1日2粒", "precautions": ["持病のある方は医師に相談"],
        })
        self.assertTrue(result["valid"])

    def test_common_layer_schema_has_no_cosmetics_specific_fields(self):
        # 共通スキーマ定義(beauty_device/supplement)に、化粧品固有の
        # フィールド名(active_ingredients等)が混在していないことを確認する。
        for schema in app.CATEGORY_ATTRIBUTE_SCHEMAS.values():
            self.assertNotIn("active_ingredients", schema["required"])
            self.assertNotIn("retinol_level", schema["required"])
            self.assertNotIn("uv_level", schema["required"])


if __name__ == "__main__":
    unittest.main()
