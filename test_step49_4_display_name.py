"""Step49.4: reflect時の表示用商品名から型番の説明注記を除くことのテスト。

実Gemini・実楽天・実HTTP・本番DBは使わない。
"""

import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import psycopg2  # noqa: E402

import app  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402
import product_master_pipeline as orchestrator  # noqa: E402
from test_product_master_pipeline_orchestrator import OrchestratorTestBase, TEST_NAME_SUFFIX  # noqa: E402

CIT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ_s494_"


class ReflectDisplayNameTests(OrchestratorTestBase):

    def _reflect_preview(self, name, category="美容機器"):
        brand = f"BR{TEST_NAME_SUFFIX}"
        payload = {"active_ingredients": [], "formulation_features": [],
                   "category_attributes": {"method": {"value": "EMS", "confidence": "high", "source_url": CIT}}}
        self._insert_staging_row(self._new_batch_id("s494"), brand, name, category, stage2_payload=payload,
                                 citations=[{"uri": CIT, "title": brand}], stage1_raw_text="")
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT staging_id FROM product_collection_staging WHERE product_name = %s", (name,))
            sid = cur.fetchone()[0]
        finally:
            conn.close()
        return pipeline.reflect_staging_to_product_master(sid, dry_run=True)["product"]["name"]

    def test_uncertain_model_annotation_is_removed(self):
        self.assertEqual(self._reflect_preview(f"CARAT LIFT{TEST_NAME_SUFFIX}（型番：RR-AT-02A 等）"),
                         f"CARAT LIFT{TEST_NAME_SUFFIX}")

    def test_single_confirmed_model_is_kept(self):
        self.assertEqual(self._reflect_preview(f"メディリフト プラス{TEST_NAME_SUFFIX}（型番：EPM-18BB）"),
                         f"メディリフト プラス{TEST_NAME_SUFFIX} EPM-18BB")

    def test_names_without_annotation_are_unchanged(self):
        name = f"バイタリフト RF EH-SR85{TEST_NAME_SUFFIX}"
        self.assertEqual(self._reflect_preview(name), name)


class IdentityKeyTests(unittest.TestCase):

    def test_display_name_key_is_included(self):
        keys = orchestrator.identity_keys_for("リファ（ReFa）", "ReFa CARAT LIFT（リファカラットリフト）（型番：RR-AT-02A 等）", "美容機器")
        display_key = app.make_verified_product_key(
            {"brand": "リファ（ReFa）", "name": "ReFa CARAT LIFT（リファカラットリフト）", "category": "美容機器"})
        self.assertIn(display_key, keys)


if __name__ == "__main__":
    unittest.main()
