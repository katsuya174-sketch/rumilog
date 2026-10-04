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


if __name__ == "__main__":
    unittest.main()
