"""
Phase 3「AIによる商品事前収集」(50商品パイロット)の実装テスト。
実際のGemini APIは一切呼ばない(pipeline.call_gemini_for_collectionをモックする)。
50商品の実API収集はこのテストでも実行しない。

必須検証項目(合意事項):
- Stage1検索証拠なし→拒否
- 架空URL(Stage1の実citationに存在しないURL)→拒否
- Stage2の推測値(Stage1に無い情報)→拒否される設計の確認
- 既存product_masterと矛盾→needs_review(自動上書きしない)
- cost limit到達→バッチ停止
- dry-run→実際には更新しない
"""

import json
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import psycopg2  # noqa: E402

import app  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402


def _fake_response(text, prompt_tokens=100, output_tokens=100, grounding_sources=None, grounding_queries=None):
    response = MagicMock()
    response.text = text
    response.usage_metadata.prompt_token_count = prompt_tokens
    response.usage_metadata.candidates_token_count = output_tokens
    response.usage_metadata.tool_use_prompt_token_count = None
    response.usage_metadata.total_token_count = prompt_tokens + output_tokens

    if grounding_sources is None and grounding_queries is None:
        response.candidates = [MagicMock(grounding_metadata=None)]
    else:
        chunks = []
        for s in (grounding_sources or []):
            web = MagicMock()
            web.uri = s["uri"]
            web.title = s.get("title", "")
            web.domain = s.get("domain", "")
            chunks.append(MagicMock(web=web))
        response.candidates = [MagicMock(grounding_metadata=MagicMock(
            web_search_queries=grounding_queries or [],
            grounding_chunks=chunks,
        ))]
    return response


class DbCleanupMixin:
    TEST_BATCH_PREFIX = "test-batch-"

    def _cleanup(self):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM product_field_sources WHERE staging_id IN "
                "(SELECT staging_id FROM product_collection_staging WHERE batch_id LIKE %s)",
                (f"{self.TEST_BATCH_PREFIX}%",),
            )
            cur.execute("DELETE FROM product_collection_staging WHERE batch_id LIKE %s", (f"{self.TEST_BATCH_PREFIX}%",))
            cur.execute("DELETE FROM product_collection_usage WHERE batch_id LIKE %s", (f"{self.TEST_BATCH_PREFIX}%",))
            cur.execute("DELETE FROM product_master WHERE name LIKE %s", ("%_PMCollectionTest%",))
            conn.commit()
        finally:
            conn.close()


class InitTablesTests(unittest.TestCase):
    def test_tables_are_created_idempotently(self):
        pipeline.init_product_collection_tables()
        pipeline.init_product_collection_tables()  # 2回実行してもエラーにならない


class SelectPilotProductsTests(unittest.TestCase):
    def test_prioritizes_incomplete_entries(self):
        products = [
            {"brand": "A", "name": "完全商品", "category": "美容液",
             "active_ingredients": ["niacinamide"], "ingredient_strength": {"niacinamide": "high"},
             "formulation": ["liposome"]},
            {"brand": "B", "name": "不完全商品", "category": "美容液",
             "active_ingredients": ["niacinamide"], "ingredient_strength": {}, "formulation": []},
        ]
        selected = pipeline.select_pilot_products(target_count=10, existing_products=products)
        names = [p["name"] for p in selected]
        self.assertIn("不完全商品", names)
        self.assertNotIn("完全商品", names)

    def test_balances_across_category_and_ingredient_buckets(self):
        products = []
        for i in range(5):
            products.append({"brand": f"A{i}", "name": f"美容液{i}", "category": "美容液",
                              "active_ingredients": ["niacinamide"], "ingredient_strength": {}, "formulation": []})
        for i in range(5):
            products.append({"brand": f"B{i}", "name": f"化粧水{i}", "category": "化粧水",
                              "active_ingredients": ["vitamin_c"], "ingredient_strength": {}, "formulation": []})

        selected = pipeline.select_pilot_products(target_count=4, existing_products=products)
        categories = [p["category"] for p in selected]
        self.assertIn("美容液", categories)
        self.assertIn("化粧水", categories)

    def test_does_not_exceed_target_count(self):
        products = [
            {"brand": f"A{i}", "name": f"商品{i}", "category": "美容液",
             "active_ingredients": [], "ingredient_strength": {}, "formulation": []}
            for i in range(20)
        ]
        selected = pipeline.select_pilot_products(target_count=5, existing_products=products)
        self.assertLessEqual(len(selected), 5)

    def test_empty_product_list_returns_empty(self):
        self.assertEqual(pipeline.select_pilot_products(target_count=10, existing_products=[]), [])


class Stage2PromptExtractionCompletenessTests(unittest.TestCase):
    """Stage1で明示され裏付けられた成分は、濃度が不明でも漏らさず
    抽出するようプロンプトで明示的に指示していること(過小抽出対策)。"""

    def test_prompt_instructs_not_to_omit_named_ingredients(self):
        prompt = pipeline.build_stage2_prompt("Brand", "Product", "text", [])
        self.assertIn("が不明であっても", prompt)
        self.assertIn("active_ingredientsに必ず含める", prompt)

    def test_prompt_forbids_unknown_placeholder_items(self):
        prompt = pipeline.build_stage2_prompt("Brand", "Product", "text", [])
        self.assertIn("ingredient: unknown", prompt)


class Stage1NoSearchEvidenceRejectionTests(unittest.TestCase):
    """Stage1検索証拠なし→拒否。"""

    def test_no_grounding_metadata_is_rejected(self):
        fake_response = _fake_response("成分は...(一般的な知識による回答)")
        with patch.object(pipeline, "call_gemini_for_collection", return_value=fake_response), \
             patch.object(pipeline, "record_usage_and_check_limit", return_value={"limit_exceeded": False}):
            result = pipeline.run_stage1_collection("Brand", "Product", "batch-1")
        self.assertEqual(result["status"], "no_search_evidence")

    def test_empty_queries_and_sources_is_rejected(self):
        fake_response = _fake_response("text", grounding_sources=[], grounding_queries=[])
        with patch.object(pipeline, "call_gemini_for_collection", return_value=fake_response), \
             patch.object(pipeline, "record_usage_and_check_limit", return_value={"limit_exceeded": False}):
            result = pipeline.run_stage1_collection("Brand", "Product", "batch-1")
        self.assertEqual(result["status"], "no_search_evidence")

    def test_real_search_evidence_is_accepted(self):
        fake_response = _fake_response(
            "検索結果に基づく回答",
            grounding_sources=[{"uri": "https://real-source.example.com/product", "title": "公式"}],
            grounding_queries=["Brand Product ingredients"],
        )
        with patch.object(pipeline, "call_gemini_for_collection", return_value=fake_response), \
             patch.object(pipeline, "record_usage_and_check_limit", return_value={"limit_exceeded": False}):
            result = pipeline.run_stage1_collection("Brand", "Product", "batch-1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["citations"]), 1)

    def test_api_exception_is_handled_as_error_not_crash(self):
        with patch.object(pipeline, "call_gemini_for_collection", side_effect=RuntimeError("timeout")):
            result = pipeline.run_stage1_collection("Brand", "Product", "batch-1")
        self.assertEqual(result["status"], "error")

    def test_stage2_is_skipped_when_stage1_has_no_evidence(self):
        stage1_result = {"status": "no_search_evidence", "raw_text": "", "citations": []}
        result = pipeline.run_stage2_structuring("Brand", "Product", stage1_result, "batch-1")
        self.assertEqual(result["status"], "skipped")


class SanitizeNoPlaceholderIngredientTests(unittest.TestCase):
    """成分不明時はactive_ingredients=[]に統一し、{"ingredient":"unknown"}
    というプレースホルダー項目は作らない(合意事項)。"""

    def test_unknown_ingredient_placeholder_is_dropped(self):
        payload = {
            "active_ingredients": [
                {"ingredient": "unknown", "concentration": "unknown", "confidence": "unknown",
                 "source_url": "https://real.example.com/a"},
            ],
            "formulation_features": [],
            "jan_code": "unknown",
        }
        sanitized = pipeline.sanitize_stage2_payload(payload, {"https://real.example.com/a"}, "text")
        self.assertEqual(sanitized["active_ingredients"], [])

    def test_real_ingredient_with_unknown_concentration_is_kept(self):
        # 成分名自体は明記されているが濃度だけ不明な場合は保持する(過小抽出対策)。
        payload = {
            "active_ingredients": [
                {"ingredient": "テトラヘキシルデカン酸アスコルビル", "concentration": "unknown",
                 "confidence": "high", "source_url": "https://real.example.com/a"},
            ],
            "formulation_features": [],
            "jan_code": "unknown",
        }
        sanitized = pipeline.sanitize_stage2_payload(payload, {"https://real.example.com/a"}, "text")
        self.assertEqual(len(sanitized["active_ingredients"]), 1)
        self.assertEqual(sanitized["active_ingredients"][0]["concentration"], "unknown")


class OfficialSourceConfirmedDeterministicTests(unittest.TestCase):
    """official_source_confirmedはモデルの自己申告を使わず、Stage1の実
    citationから決定論的に判定する(合意事項)。"""

    def test_brand_domain_match_returns_true(self):
        result = pipeline.is_official_source_confirmed(
            "LANEIGE", [{"title": "laneige.com", "uri": "https://laneige.com/x"}],
        )
        self.assertTrue(result)

    def test_no_matching_domain_returns_false(self):
        result = pipeline.is_official_source_confirmed(
            "LANEIGE", [{"title": "cosme.net", "uri": "https://cosme.net/x"}],
        )
        self.assertFalse(result)

    def test_no_citations_returns_false(self):
        self.assertFalse(pipeline.is_official_source_confirmed("LANEIGE", []))

    def test_no_brand_returns_false(self):
        self.assertFalse(pipeline.is_official_source_confirmed("", [{"title": "laneige.com"}]))

    def test_sanitize_overrides_model_self_reported_true_with_false(self):
        payload = {
            "active_ingredients": [], "formulation_features": [], "jan_code": "unknown",
            "official_source_confirmed": True,  # モデルの自己申告(信用しない)
        }
        sanitized = pipeline.sanitize_stage2_payload(
            payload, set(), "text", brand="Brand", citations=[{"title": "unrelated-site.com"}],
        )
        self.assertFalse(sanitized["official_source_confirmed"])

    def test_sanitize_sets_true_when_brand_domain_actually_present(self):
        payload = {
            "active_ingredients": [], "formulation_features": [], "jan_code": "unknown",
            "official_source_confirmed": False,  # モデルの自己申告(無視され、実citationで再判定)
        }
        sanitized = pipeline.sanitize_stage2_payload(
            payload, set(), "text", brand="LANEIGE", citations=[{"title": "laneige.com"}],
        )
        self.assertTrue(sanitized["official_source_confirmed"])

    def test_run_stage2_structuring_applies_deterministic_official_source_check(self):
        stage1_result = {
            "status": "ok", "raw_text": "text",
            "citations": [{"uri": "https://real.example.com/a", "title": "cosme.net"}],
        }
        fake_payload = {
            "active_ingredients": [], "formulation_features": [], "jan_code": "unknown",
            "official_source_confirmed": True,  # モデルの自己申告(非公式サイトなのに自己申告はtrue)
        }
        fake_response = _fake_response(json.dumps(fake_payload))
        with patch.object(pipeline, "call_gemini_for_collection", return_value=fake_response), \
             patch.object(pipeline, "record_usage_and_check_limit", return_value={"limit_exceeded": False}):
            result = pipeline.run_stage2_structuring("Brand", "Product", stage1_result, "batch-1")
        self.assertFalse(result["payload"]["official_source_confirmed"])


class FakeUrlRejectionTests(unittest.TestCase):
    """架空URL(Stage1の実citationに存在しないURL)→拒否。"""

    def test_sanitize_drops_ingredient_with_unverified_source_url(self):
        payload = {
            "active_ingredients": [
                {"ingredient": "niacinamide", "concentration": "5%", "confidence": "high",
                 "source_url": "https://fake-generated-url.example.com/made-up"},
            ],
            "formulation_features": [],
            "jan_code": "unknown",
        }
        valid_urls = {"https://real-source.example.com/product"}
        sanitized = pipeline.sanitize_stage2_payload(payload, valid_urls, "stage1 text")
        self.assertEqual(sanitized["active_ingredients"], [])

    def test_sanitize_keeps_ingredient_with_verified_source_url(self):
        payload = {
            "active_ingredients": [
                {"ingredient": "niacinamide", "concentration": "5%", "confidence": "high",
                 "source_url": "https://real-source.example.com/product"},
            ],
            "formulation_features": [],
            "jan_code": "unknown",
        }
        valid_urls = {"https://real-source.example.com/product"}
        sanitized = pipeline.sanitize_stage2_payload(payload, valid_urls, "stage1 text")
        self.assertEqual(len(sanitized["active_ingredients"]), 1)

    def test_sanitize_drops_formulation_feature_with_unverified_source_url(self):
        payload = {
            "active_ingredients": [],
            "formulation_features": [
                {"feature": "liposome", "other_detail": "unknown", "confidence": "high",
                 "source_url": "https://fake-generated-url.example.com/made-up"},
            ],
            "jan_code": "unknown",
        }
        sanitized = pipeline.sanitize_stage2_payload(payload, {"https://real.example.com"}, "text")
        self.assertEqual(sanitized["formulation_features"], [])

    def test_sanitize_forces_unverified_jan_code_to_unknown(self):
        payload = {"active_ingredients": [], "formulation_features": [], "jan_code": "4900000000000"}
        sanitized = pipeline.sanitize_stage2_payload(payload, set(), "この製品のJANコードは不明です")
        self.assertEqual(sanitized["jan_code"], "unknown")

    def test_sanitize_keeps_jan_code_present_verbatim_in_stage1_text(self):
        payload = {"active_ingredients": [], "formulation_features": [], "jan_code": "4900000000000"}
        sanitized = pipeline.sanitize_stage2_payload(payload, set(), "JANコード: 4900000000000 です")
        self.assertEqual(sanitized["jan_code"], "4900000000000")


class Stage2GuessingRejectionTests(unittest.TestCase):
    """Stage2推測値→拒否される設計の確認(sanitizeが必ず適用されること)。"""

    def test_run_stage2_structuring_applies_sanitization(self):
        stage1_result = {
            "status": "ok", "raw_text": "ナイアシンアミド配合",
            "citations": [{"uri": "https://real.example.com/a", "title": "公式"}],
        }
        fake_payload = {
            "brand": "Brand", "product_name": "Product", "jan_code": "unknown",
            "active_ingredients": [
                {"ingredient": "niacinamide", "concentration": "5%", "confidence": "high",
                 "source_url": "https://real.example.com/a"},
                {"ingredient": "retinol", "concentration": "1%", "confidence": "high",
                 "source_url": "https://totally-made-up.example.com/x"},
            ],
            "formulation_features": [],
            "official_source_confirmed": True,
        }
        fake_response = _fake_response(json.dumps(fake_payload, ensure_ascii=False))
        with patch.object(pipeline, "call_gemini_for_collection", return_value=fake_response), \
             patch.object(pipeline, "record_usage_and_check_limit", return_value={"limit_exceeded": False}):
            result = pipeline.run_stage2_structuring("Brand", "Product", stage1_result, "batch-1")

        self.assertEqual(result["status"], "ok")
        ingredients = result["payload"]["active_ingredients"]
        self.assertEqual(len(ingredients), 1)
        self.assertEqual(ingredients[0]["ingredient"], "niacinamide")


class ConflictDetectionTests(unittest.TestCase):
    """既存product_masterと矛盾→needs_review(自動上書きしない)。"""

    def test_no_existing_product_is_new(self):
        result = pipeline.detect_conflicts({"jan_code": "123", "active_ingredients": []}, None)
        self.assertEqual(result["status"], "new")

    def test_matching_values_are_not_conflicts(self):
        staged = {"jan_code": "4900000000000", "active_ingredients": [
            {"ingredient": "niacinamide", "concentration": "5%"},
        ]}
        existing = {"jan_code": "4900000000000", "ingredient_strength": {"niacinamide": "5%"}}
        result = pipeline.detect_conflicts(staged, existing)
        self.assertEqual(result["status"], "none")

    def test_new_info_not_present_in_existing_is_not_a_conflict(self):
        staged = {"jan_code": "unknown", "active_ingredients": [
            {"ingredient": "vitamin_c", "concentration": "10%"},
        ]}
        existing = {"jan_code": "", "ingredient_strength": {}}
        result = pipeline.detect_conflicts(staged, existing)
        self.assertEqual(result["status"], "none")

    def test_conflicting_jan_code_triggers_needs_review(self):
        staged = {"jan_code": "9999999999999", "active_ingredients": []}
        existing = {"jan_code": "4900000000000", "ingredient_strength": {}}
        result = pipeline.detect_conflicts(staged, existing)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["conflicts"][0]["field"], "jan_code")

    def test_conflicting_ingredient_concentration_triggers_needs_review(self):
        staged = {"jan_code": "unknown", "active_ingredients": [
            {"ingredient": "niacinamide", "concentration": "2%"},
        ]}
        existing = {"jan_code": "", "ingredient_strength": {"niacinamide": "10%"}}
        result = pipeline.detect_conflicts(staged, existing)
        self.assertEqual(result["status"], "needs_review")

    def test_formulation_features_disjoint_from_existing_triggers_needs_review(self):
        staged = {"jan_code": "unknown", "active_ingredients": [],
                  "formulation_features": [{"feature": "nano"}]}
        existing = {"jan_code": "", "ingredient_strength": {}, "formulation": ["liposome"]}
        result = pipeline.detect_conflicts(staged, existing)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["conflicts"][0]["field"], "formulation_features")

    def test_formulation_features_overlapping_existing_is_not_a_conflict(self):
        staged = {"jan_code": "unknown", "active_ingredients": [],
                  "formulation_features": [{"feature": "liposome"}]}
        existing = {"jan_code": "", "ingredient_strength": {}, "formulation": ["liposome"]}
        result = pipeline.detect_conflicts(staged, existing)
        self.assertEqual(result["status"], "none")

    def test_formulation_features_empty_staged_is_not_a_conflict(self):
        # 今回formulation_featuresが何も確認できなかった場合、既存の確認内容と
        # 比較する材料が無いため矛盾としない(情報不足として扱う)。
        staged = {"jan_code": "unknown", "active_ingredients": [], "formulation_features": []}
        existing = {"jan_code": "", "ingredient_strength": {}, "formulation": ["liposome"]}
        result = pipeline.detect_conflicts(staged, existing)
        self.assertEqual(result["status"], "none")

    def test_formulation_features_ignores_old_uncontrolled_vocabulary(self):
        # 既存のformulationが旧語彙("oil_formula"等)のみの場合、今回の統制
        # 語彙とは無関係のため比較対象にせず矛盾としない。
        staged = {"jan_code": "unknown", "active_ingredients": [],
                  "formulation_features": [{"feature": "nano"}]}
        existing = {"jan_code": "", "ingredient_strength": {}, "formulation": ["oil_formula", "low_irritation"]}
        result = pipeline.detect_conflicts(staged, existing)
        self.assertEqual(result["status"], "none")


class CostLimitStopTests(unittest.TestCase, DbCleanupMixin):
    """cost limit到達→バッチ停止。"""

    def setUp(self):
        pipeline.init_product_collection_tables()
        self._cleanup()

    def tearDown(self):
        self._cleanup()

    def test_record_usage_flags_limit_exceeded_when_total_reaches_limit(self):
        batch_id = f"{self.TEST_BATCH_PREFIX}cost1"
        with patch.object(pipeline, "PRODUCT_COLLECTION_COST_LIMIT_USD", 0.0001):
            result = pipeline.record_usage_and_check_limit(
                batch_id, "Product", "stage1", "model", {"prompt_token_count": 100000, "candidates_token_count": 100000},
            )
        self.assertTrue(result["limit_exceeded"])

    def test_collect_batch_stops_remaining_products_after_limit_exceeded(self):
        batch_id = f"{self.TEST_BATCH_PREFIX}cost2"
        products = [
            {"brand": "A", "name": "商品1", "category": "美容液"},
            {"brand": "B", "name": "商品2", "category": "美容液"},
            {"brand": "C", "name": "商品3", "category": "美容液"},
        ]
        fake_response = _fake_response(
            "text", grounding_sources=[{"uri": "https://real.example.com/a"}], grounding_queries=["q"],
        )
        with patch.object(pipeline, "call_gemini_for_collection", return_value=fake_response), \
             patch.object(pipeline, "run_stage2_structuring", return_value={"status": "skipped", "payload": None}), \
             patch.object(pipeline, "record_usage_and_check_limit") as mock_usage:
            mock_usage.side_effect = [
                {"limit_exceeded": False},
                {"limit_exceeded": True},
                {"limit_exceeded": False},
            ]
            results = pipeline.collect_batch(products, batch_id)

        self.assertEqual(len(results), 2)  # 3件目は実行されない


class DryRunDoesNotUpdateTests(unittest.TestCase, DbCleanupMixin):
    """dry-run→実際には更新しない。"""

    def setUp(self):
        pipeline.init_product_collection_tables()
        app.init_product_master_table()
        self._cleanup()

    def tearDown(self):
        self._cleanup()

    def _insert_staging(self, batch_id, conflict_status="none", stage2_status="ok", payload=None):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO product_collection_staging
                    (batch_id, brand, product_name, category, identity_key,
                     stage1_status, stage2_status, stage2_payload, conflict_status)
                VALUES (%s, %s, %s, %s, %s, 'ok', %s, %s, %s)
                RETURNING staging_id
            """, (
                batch_id, "TestBrand", "TestProduct_PMCollectionTest", "美容液",
                "test-identity-key", stage2_status,
                json.dumps(payload or {"active_ingredients": [], "formulation_features": []}),
                conflict_status,
            ))
            staging_id = cur.fetchone()[0]
            conn.commit()
            return staging_id
        finally:
            conn.close()

    def test_dry_run_does_not_write_to_product_master(self):
        batch_id = f"{self.TEST_BATCH_PREFIX}dryrun1"
        staging_id = self._insert_staging(batch_id)

        result = pipeline.reflect_staging_to_product_master(staging_id, dry_run=True)

        self.assertEqual(result["status"], "would_reflect")
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM product_master WHERE name = %s", ("TestProduct_PMCollectionTest",))
            self.assertEqual(cur.fetchone()[0], 0)
        finally:
            conn.close()

    def test_real_run_writes_to_product_master(self):
        batch_id = f"{self.TEST_BATCH_PREFIX}dryrun2"
        staging_id = self._insert_staging(batch_id)

        result = pipeline.reflect_staging_to_product_master(staging_id, dry_run=False)

        self.assertEqual(result["status"], "reflected")
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM product_master WHERE name = %s", ("TestProduct_PMCollectionTest",))
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute("SELECT data_source FROM product_master WHERE name = %s", ("TestProduct_PMCollectionTest",))
            self.assertEqual(cur.fetchone()[0], "ai_precollected")
        finally:
            conn.close()

    def test_needs_review_staging_is_never_reflected(self):
        batch_id = f"{self.TEST_BATCH_PREFIX}dryrun3"
        staging_id = self._insert_staging(batch_id, conflict_status="needs_review")

        result = pipeline.reflect_staging_to_product_master(staging_id, dry_run=False)

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "needs_review")

    def test_already_reflected_staging_is_not_reflected_again(self):
        batch_id = f"{self.TEST_BATCH_PREFIX}dryrun4"
        staging_id = self._insert_staging(batch_id)
        pipeline.reflect_staging_to_product_master(staging_id, dry_run=False)

        result = pipeline.reflect_staging_to_product_master(staging_id, dry_run=False)
        self.assertEqual(result["status"], "already_reflected")

        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM product_master WHERE name = %s", ("TestProduct_PMCollectionTest",))
            self.assertEqual(cur.fetchone()[0], 1)  # 2回実行しても重複登録されない
        finally:
            conn.close()

    def test_unknown_placeholder_ingredient_is_filtered_out_at_reflect_time(self):
        # この修正より前に収集済みのstaging(ingredient="unknown"が残っている
        # 場合)でも、反映時に安全側で除外されることを確認する(再API実行なし)。
        batch_id = f"{self.TEST_BATCH_PREFIX}dryrun5"
        staging_id = self._insert_staging(batch_id, payload={
            "active_ingredients": [{"ingredient": "unknown", "concentration": "unknown"}],
            "formulation_features": [],
        })

        result = pipeline.reflect_staging_to_product_master(staging_id, dry_run=True)

        self.assertEqual(result["product"]["active_ingredients"], [])

    def test_reflect_reports_insert_action_for_new_identity(self):
        batch_id = f"{self.TEST_BATCH_PREFIX}dryrun6"
        staging_id = self._insert_staging(batch_id)

        result = pipeline.reflect_staging_to_product_master(staging_id, dry_run=True)

        self.assertEqual(result["action"], "insert")
        self.assertIsNone(result["existing_product_id"])

    def test_reflect_reports_update_action_for_existing_identity(self):
        batch_id = f"{self.TEST_BATCH_PREFIX}dryrun7"
        existing_id = app.upsert_product_master({
            "brand": "TestBrand", "name": "TestProduct_PMCollectionTest", "category": "美容液",
        })
        staging_id = self._insert_staging(batch_id)

        result = pipeline.reflect_staging_to_product_master(staging_id, dry_run=True)

        self.assertEqual(result["action"], "update")
        self.assertEqual(result["existing_product_id"], existing_id)

    def test_dry_run_preview_includes_normalized_ingredient_tags(self):
        # Phase2+3統合検証で判明した構造的不整合の修正: 生の原文成分名のみでも
        # 既存のnormalize_ingredient_tag()を再利用した統制タグがdry-run結果に
        # 含まれ、原文のactive_ingredientsは変更されないことを確認する。
        batch_id = f"{self.TEST_BATCH_PREFIX}dryrun8"
        staging_id = self._insert_staging(batch_id, payload={
            "active_ingredients": [
                {"ingredient": "ツボクサエキス", "concentration": "unknown"},
                {"ingredient": "ダマスクバラ花エキス", "concentration": "unknown"},
            ],
            "formulation_features": [],
        })

        result = pipeline.reflect_staging_to_product_master(staging_id, dry_run=True)

        self.assertEqual(
            result["product"]["active_ingredients"],
            ["ツボクサエキス", "ダマスクバラ花エキス"],
        )
        self.assertEqual(result["product"]["active_ingredient_tags"], ["centella_extract"])

    def test_real_run_persists_active_ingredient_tags_column(self):
        batch_id = f"{self.TEST_BATCH_PREFIX}dryrun9"
        staging_id = self._insert_staging(batch_id, payload={
            "active_ingredients": [{"ingredient": "ツボクサエキス", "concentration": "unknown"}],
            "formulation_features": [],
        })

        pipeline.reflect_staging_to_product_master(staging_id, dry_run=False)

        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT active_ingredient_tags FROM product_master WHERE name = %s",
                ("TestProduct_PMCollectionTest",),
            )
            self.assertEqual(cur.fetchone()[0], ["centella_extract"])
        finally:
            conn.close()


class StagingAndFieldSourcesWriteTests(unittest.TestCase, DbCleanupMixin):
    def setUp(self):
        pipeline.init_product_collection_tables()
        self._cleanup()

    def tearDown(self):
        self._cleanup()

    def test_write_staging_record_persists_stage1_and_stage2_data(self):
        batch_id = f"{self.TEST_BATCH_PREFIX}staging1"
        stage1_result = {
            "status": "ok", "raw_text": "text",
            "citations": [{"uri": "https://real.example.com/a", "title": "t"}],
            "search_queries": ["q"],
        }
        stage2_result = {
            "status": "ok",
            "payload": {
                "active_ingredients": [
                    {"ingredient": "niacinamide", "concentration": "5%", "confidence": "high",
                     "source_url": "https://real.example.com/a"},
                ],
                "formulation_features": [],
            },
        }
        conflict_result = {"status": "new", "conflicts": []}

        staging_id = pipeline.write_staging_record(
            batch_id, "Brand", "Product", "美容液", stage1_result, stage2_result, conflict_result,
        )
        self.assertIsNotNone(staging_id)

        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM product_field_sources WHERE staging_id = %s", (staging_id,))
            self.assertEqual(cur.fetchone()[0], 1)
        finally:
            conn.close()


class UsageRecordingAndNotificationTests(unittest.TestCase, DbCleanupMixin):
    def setUp(self):
        pipeline.init_product_collection_tables()
        self._cleanup()
        pipeline._80_PERCENT_NOTIFIED_BATCHES.clear()

    def tearDown(self):
        self._cleanup()

    def test_usage_is_recorded_in_db(self):
        batch_id = f"{self.TEST_BATCH_PREFIX}usage1"
        pipeline.record_usage_and_check_limit(
            batch_id, "Product", "stage1", "model", {"prompt_token_count": 100, "candidates_token_count": 50},
        )
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM product_collection_usage WHERE batch_id = %s", (batch_id,))
            self.assertEqual(cur.fetchone()[0], 1)
        finally:
            conn.close()

    def test_80_percent_threshold_sends_admin_email_once(self):
        batch_id = f"{self.TEST_BATCH_PREFIX}usage2"
        with patch.object(pipeline, "PRODUCT_COLLECTION_COST_LIMIT_USD", 0.0001), \
             patch.object(app, "send_admin_email") as mock_email:
            pipeline.record_usage_and_check_limit(
                batch_id, "Product", "stage1", "model", {"prompt_token_count": 100000, "candidates_token_count": 100000},
            )
            pipeline.record_usage_and_check_limit(
                batch_id, "Product2", "stage1", "model", {"prompt_token_count": 100000, "candidates_token_count": 100000},
            )
        mock_email.assert_called_once()  # 同じbatch_idでは1回だけ通知


class PricingConstantsTests(unittest.TestCase):
    """2026-10時点の公式料金に基づく最新化(根拠はコードコメント参照)。"""

    def test_token_pricing_matches_latest_official_rates(self):
        self.assertEqual(pipeline._ESTIMATED_INPUT_COST_PER_MILLION_TOKENS, 0.25)
        self.assertEqual(pipeline._ESTIMATED_OUTPUT_COST_PER_MILLION_TOKENS, 1.50)

    def test_grounding_query_pricing_matches_gemini_3x_rate(self):
        self.assertEqual(pipeline._ESTIMATED_COST_PER_GROUNDED_SEARCH_QUERY, 0.014)

    def test_cost_limit_remains_five_dollars_by_default(self):
        self.assertEqual(pipeline.PRODUCT_COLLECTION_COST_LIMIT_USD, 5.0)


class PilotSelectionLoggingTests(unittest.TestCase):
    """50商品の事前目視確認は不要だが、選定結果をbatchログとして
    再現可能な形で保存する(合意事項)。"""

    def setUp(self):
        self.batch_id = "test-batch-selection-log-001"
        self.log_path = os.path.join(pipeline.BATCH_LOG_DIR, f"{self.batch_id}_selection.json")

    def tearDown(self):
        if os.path.exists(self.log_path):
            os.remove(self.log_path)

    def test_log_pilot_selection_writes_reproducible_json_file(self):
        selected = [
            {"brand": "A", "name": "商品1", "category": "美容液", "active_ingredients": ["niacinamide"]},
            {"brand": "B", "name": "商品2", "category": "化粧水", "active_ingredients": ["vitamin_c"]},
        ]
        log_path = pipeline.log_pilot_selection(self.batch_id, selected)

        self.assertTrue(os.path.exists(log_path))
        with open(log_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["batch_id"], self.batch_id)
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["products"][0]["name"], "商品1")

    def test_rerunning_same_batch_id_overwrites_log_reproducibly(self):
        pipeline.log_pilot_selection(self.batch_id, [{"brand": "A", "name": "商品1", "category": "美容液"}])
        pipeline.log_pilot_selection(self.batch_id, [{"brand": "B", "name": "商品2", "category": "化粧水"}])

        with open(self.log_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["products"][0]["name"], "商品2")

    def test_select_and_log_pilot_products_returns_same_list_as_logged(self):
        products = [
            {"brand": "A", "name": "不完全商品", "category": "美容液",
             "active_ingredients": ["niacinamide"], "ingredient_strength": {}, "formulation": []},
        ]
        selected = pipeline.select_and_log_pilot_products(
            self.batch_id, target_count=10, existing_products=products,
        )
        with open(self.log_path, encoding="utf-8") as f:
            logged = json.load(f)
        self.assertEqual(len(selected), logged["count"])
        self.assertEqual(selected[0]["name"], logged["products"][0]["name"])


class DiagnosisQuotaIsolationTests(unittest.TestCase):
    """
    インシデント修正の検証: Phase 3(Stage1/Stage2)が診断用call_gemini_
    with_retry()を一切使わず、診断用gemini_usageカウンタに一切影響しない
    ことを明示的に確認する(合意事項の最重要項目)。
    """

    def _current_gemini_usage_count(self):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            usage_key = app.get_gemini_usage_key()
            cur.execute("SELECT request_count FROM gemini_usage WHERE usage_key = %s", (usage_key,))
            row = cur.fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

    def test_call_gemini_for_collection_never_calls_diagnosis_wrapper(self):
        with patch.object(app, "call_gemini_with_retry") as mock_diagnosis_call, \
             patch.object(app.client.models, "generate_content", return_value=_fake_response("{}")):
            pipeline.call_gemini_for_collection("some-model", "prompt")
        mock_diagnosis_call.assert_not_called()

    def test_call_gemini_for_collection_never_calls_increment_gemini_usage(self):
        with patch.object(app, "increment_gemini_usage") as mock_increment, \
             patch.object(app.client.models, "generate_content", return_value=_fake_response("{}")):
            pipeline.call_gemini_for_collection("some-model", "prompt")
        mock_increment.assert_not_called()

    def test_stage1_collection_does_not_change_real_diagnosis_usage_count(self):
        before = self._current_gemini_usage_count()
        fake_response = _fake_response(
            "text", grounding_sources=[{"uri": "https://real.example.com/a"}], grounding_queries=["q"],
        )
        with patch.object(pipeline, "call_gemini_for_collection", return_value=fake_response), \
             patch.object(pipeline, "record_usage_and_check_limit", return_value={"limit_exceeded": False}):
            pipeline.run_stage1_collection("Brand", "Product", "batch-quota-test")
        after = self._current_gemini_usage_count()
        self.assertEqual(before, after)

    def test_stage2_structuring_does_not_change_real_diagnosis_usage_count(self):
        before = self._current_gemini_usage_count()
        stage1_result = {
            "status": "ok", "raw_text": "text",
            "citations": [{"uri": "https://real.example.com/a", "title": "t"}],
        }
        fake_payload = {"active_ingredients": [], "formulation_features": [], "jan_code": "unknown"}
        fake_response = _fake_response(json.dumps(fake_payload))
        with patch.object(pipeline, "call_gemini_for_collection", return_value=fake_response), \
             patch.object(pipeline, "record_usage_and_check_limit", return_value={"limit_exceeded": False}):
            pipeline.run_stage2_structuring("Brand", "Product", stage1_result, "batch-quota-test")
        after = self._current_gemini_usage_count()
        self.assertEqual(before, after)

    def test_run_stage1_collection_uses_dedicated_function_not_diagnosis_wrapper(self):
        fake_response = _fake_response(
            "text", grounding_sources=[{"uri": "https://real.example.com/a"}], grounding_queries=["q"],
        )
        with patch.object(app, "call_gemini_with_retry") as mock_diagnosis_call, \
             patch.object(pipeline, "call_gemini_for_collection", return_value=fake_response) as mock_collection_call, \
             patch.object(pipeline, "record_usage_and_check_limit", return_value={"limit_exceeded": False}):
            pipeline.run_stage1_collection("Brand", "Product", "batch-quota-test")
        mock_diagnosis_call.assert_not_called()
        mock_collection_call.assert_called_once()


class BatchResumeTests(unittest.TestCase, DbCleanupMixin):
    """
    再開処理の冪等性: 完了済み商品は再実行しない、Stage1成功・Stage2未完了
    はStage1を再実行せずStage2から再開する、失敗済みStage1は再試行しない。
    """

    def setUp(self):
        pipeline.init_product_collection_tables()
        self._cleanup()
        self.batch_id = f"{self.TEST_BATCH_PREFIX}resume"
        os.makedirs(pipeline.BATCH_LOG_DIR, exist_ok=True)
        self.log_path = os.path.join(pipeline.BATCH_LOG_DIR, f"{self.batch_id}_selection.json")

    def tearDown(self):
        self._cleanup()
        if os.path.exists(self.log_path):
            os.remove(self.log_path)

    def _insert_staging(self, brand, name, category, stage1_status, stage2_status, stage1_text="text", citations=None):
        identity_key = app._normalize_product_master_identity_key(brand, name, category)
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO product_collection_staging
                    (batch_id, brand, product_name, category, identity_key,
                     stage1_status, stage1_raw_text, stage1_citations, stage1_search_queries, stage2_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING staging_id
            """, (
                self.batch_id, brand, name, category, identity_key,
                stage1_status, stage1_text, json.dumps(citations or []), json.dumps([]), stage2_status,
            ))
            staging_id = cur.fetchone()[0]
            conn.commit()
            return staging_id
        finally:
            conn.close()

    def test_not_started_product_runs_full_pipeline(self):
        with patch.object(pipeline, "collect_one_product", return_value={"staging_id": 1, "limit_exceeded": False}) as mock_collect:
            result = pipeline.resume_one_product("A", "商品1", "美容液", self.batch_id)
        self.assertEqual(result["action"], "full_run")
        mock_collect.assert_called_once()

    def test_complete_product_is_skipped_without_any_api_call(self):
        self._insert_staging("A", "商品2", "美容液", "ok", "ok")
        with patch.object(pipeline, "run_stage1_collection") as mock_stage1, \
             patch.object(pipeline, "run_stage2_structuring") as mock_stage2:
            result = pipeline.resume_one_product("A", "商品2", "美容液", self.batch_id)
        self.assertEqual(result["action"], "skip_complete")
        mock_stage1.assert_not_called()
        mock_stage2.assert_not_called()

    def test_stage1_ok_stage2_incomplete_resumes_stage2_only(self):
        staging_id = self._insert_staging(
            "A", "商品3", "美容液", "ok", "error",
            stage1_text="検索結果本文", citations=[{"uri": "https://real.example.com/a", "title": "t"}],
        )
        fake_payload = {"active_ingredients": [], "formulation_features": [], "jan_code": "unknown"}
        with patch.object(pipeline, "run_stage1_collection") as mock_stage1, \
             patch.object(pipeline, "run_stage2_structuring",
                           return_value={"status": "ok", "payload": fake_payload, "usage_result": {"limit_exceeded": False}}) as mock_stage2:
            result = pipeline.resume_one_product("A", "商品3", "美容液", self.batch_id)

        mock_stage1.assert_not_called()  # Stage1は再実行しない
        mock_stage2.assert_called_once()
        self.assertEqual(result["action"], "resume_stage2")
        self.assertEqual(result["staging_id"], staging_id)

        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT stage2_status FROM product_collection_staging WHERE staging_id = %s", (staging_id,))
            self.assertEqual(cur.fetchone()[0], "ok")
            cur.execute("SELECT COUNT(*) FROM product_collection_staging WHERE batch_id = %s", (self.batch_id,))
            self.assertEqual(cur.fetchone()[0], 1)  # 新規行は作られず既存行が更新された
        finally:
            conn.close()

    def test_stage1_failed_is_not_retried(self):
        self._insert_staging("A", "商品4", "美容液", "no_search_evidence", None)
        with patch.object(pipeline, "run_stage1_collection") as mock_stage1, \
             patch.object(pipeline, "run_stage2_structuring") as mock_stage2:
            result = pipeline.resume_one_product("A", "商品4", "美容液", self.batch_id)
        self.assertEqual(result["action"], "skip_failed")
        mock_stage1.assert_not_called()
        mock_stage2.assert_not_called()

    def test_get_batch_resume_status_counts_correctly(self):
        self._insert_staging("A", "完了商品", "美容液", "ok", "ok")
        self._insert_staging("B", "途中商品", "美容液", "ok", "error")
        self._insert_staging("C", "失敗商品", "美容液", "no_search_evidence", None)
        pipeline.log_pilot_selection(self.batch_id, [
            {"brand": "A", "name": "完了商品", "category": "美容液"},
            {"brand": "B", "name": "途中商品", "category": "美容液"},
            {"brand": "C", "name": "失敗商品", "category": "美容液"},
            {"brand": "D", "name": "未実行商品", "category": "美容液"},
        ])

        status = pipeline.get_batch_resume_status(self.batch_id)

        self.assertEqual(status["total_selected"], 4)
        self.assertEqual(status["complete"], 1)
        self.assertEqual(status["partial_stage1_only"], 1)
        self.assertEqual(status["failed_stage1"], 1)
        self.assertEqual(status["not_started"], 1)

    def test_resume_batch_stops_on_limit_exceeded(self):
        pipeline.log_pilot_selection(self.batch_id, [
            {"brand": "A", "name": "商品A", "category": "美容液"},
            {"brand": "B", "name": "商品B", "category": "美容液"},
        ])
        with patch.object(pipeline, "resume_one_product") as mock_resume:
            mock_resume.side_effect = [
                {"action": "full_run", "limit_exceeded": True},
                {"action": "full_run", "limit_exceeded": False},
            ]
            results = pipeline.resume_batch(self.batch_id)
        self.assertEqual(len(results), 1)  # 2件目は実行されない


def _rakuten_item(item_code="shop1:1001", item_name="テスト美容液", price=2000, url="https://item.rakuten.co.jp/shop1/1001/",
                   images=None, caption="", shop_name="shop1"):
    return {
        "itemCode": item_code,
        "itemName": item_name,
        "itemPrice": price,
        "itemUrl": url,
        "itemCaption": caption,
        "shopName": shop_name,
        "mediumImageUrls": images if images is not None else [{"imageUrl": "https://img.example/1.jpg"}],
    }


class ResolveItemCodeForProductTests(unittest.TestCase):
    """Step3-B: brand+product_nameでの楽天候補検索→確信を持てる場合のみ
    1件に絞る照合ロジック。実楽天APIは呼ばない(app.fetch_rakuten_candidates
    をモックする)。"""

    def test_zero_candidates_returns_not_found(self):
        with patch.object(app, "fetch_rakuten_candidates", return_value=[]):
            result = pipeline.resolve_item_code_for_product("TestBrand", "テスト美容液", "美容液")
        self.assertEqual(result["status"], "not_found")

    def test_429_exhausted_search_is_handled_gracefully_as_not_found(self):
        # fetch_rakuten_candidates自体の429retry/cooldownは既存実装側の責務。
        # 持続的な429で最終的に[]が返るケースでも、ここで例外にならず
        # not_foundとして安全に扱われることを確認する。
        with patch.object(app, "fetch_rakuten_candidates", return_value=[]) as mock_fetch:
            result = pipeline.resolve_item_code_for_product("TestBrand", "テスト美容液", "美容液")
        mock_fetch.assert_called_once()
        self.assertEqual(result["status"], "not_found")

    def test_wrong_product_title_is_rejected_as_not_found(self):
        # 別商品(ブランド・商品名ともに一致しないタイトル)が万一候補に
        # 混入しても、is_same_verified_rakuten_product()の再確認で弾かれる。
        wrong_item = _rakuten_item(item_name="全く別のブランドの日焼け止めクリーム")
        with patch.object(app, "fetch_rakuten_candidates", return_value=[(50, wrong_item)]):
            result = pipeline.resolve_item_code_for_product("TestBrand", "テスト美容液", "美容液")
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result.get("reason"), "title_mismatch")

    def test_set_product_is_excluded_as_not_found(self):
        set_item = _rakuten_item(item_name="TestBrand テスト美容液 スペシャルセット 3点セット")
        with patch.object(app, "fetch_rakuten_candidates", return_value=[(50, set_item)]):
            result = pipeline.resolve_item_code_for_product("TestBrand", "テスト美容液", "美容液")
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result.get("reason"), "only_set_items")

    def test_single_matching_candidate_is_confirmed(self):
        item = _rakuten_item(item_name="TestBrand テスト美容液 30mL")
        with patch.object(app, "fetch_rakuten_candidates", return_value=[(50, item)]):
            result = pipeline.resolve_item_code_for_product("TestBrand", "テスト美容液", "美容液")
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["item"]["itemCode"], "shop1:1001")

    def test_multiple_same_product_candidates_without_jan_use_merchant_tiebreak(self):
        # 同一商品(タイトル一致・セット除外通過済み)が複数店舗に残った場合、
        # 別商品の可能性は無い前提のため、既存タイブレーク基準で代表1件に
        # 自動確定する(以前のambiguous仕様から変更)。
        item1 = _rakuten_item(item_code="shop1:1001", item_name="TestBrand テスト美容液 30mL")
        item2 = _rakuten_item(item_code="shop2:2002", item_name="TestBrand テスト美容液 詰め替え用")
        with patch.object(app, "fetch_rakuten_candidates", return_value=[(50, item1), (45, item2)]):
            result = pipeline.resolve_item_code_for_product("TestBrand", "テスト美容液", "美容液")
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["disambiguated_by"], "merchant_tiebreak")
        self.assertEqual(result["item"]["itemCode"], "shop1:1001")  # スコアが高い方(既存タイブレーク基準)

    def test_set_item_mixed_in_is_excluded_before_merchant_tiebreak(self):
        # セット商品がスコア最高でも、タイブレーク対象には含めない
        # (セット除外後の単品候補だけでタイブレークする)。
        single_a = _rakuten_item(item_code="shop1:1001", item_name="TestBrand テスト美容液 30mL")
        set_item = _rakuten_item(item_code="shop9:9999", item_name="TestBrand テスト美容液 3点セット")
        single_b = _rakuten_item(item_code="shop2:2002", item_name="TestBrand テスト美容液 詰め替え用")
        with patch.object(
            app, "fetch_rakuten_candidates",
            return_value=[(50, single_a), (55, set_item), (45, single_b)],
        ):
            result = pipeline.resolve_item_code_for_product("TestBrand", "テスト美容液", "美容液")
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["disambiguated_by"], "merchant_tiebreak")
        self.assertNotEqual(result["item"]["itemCode"], "shop9:9999")
        self.assertEqual(result["item"]["itemCode"], "shop1:1001")

    def test_jan_disambiguates_multiple_candidates(self):
        item1 = _rakuten_item(item_code="shop1:1001", item_name="TestBrand テスト美容液 30mL", caption="JAN:4912345678901")
        item2 = _rakuten_item(item_code="shop2:2002", item_name="TestBrand テスト美容液 詰め替え用")
        with patch.object(app, "fetch_rakuten_candidates", return_value=[(50, item1), (45, item2)]):
            result = pipeline.resolve_item_code_for_product(
                "TestBrand", "テスト美容液", "美容液", jan_code="4912345678901",
            )
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["item"]["itemCode"], "shop1:1001")
        self.assertEqual(result["disambiguated_by"], "jan_code")

    def test_jan_matching_more_than_one_candidate_falls_back_to_merchant_tiebreak(self):
        # JANで一意に決まらない場合のみ店舗タイブレークに落ちる。
        item1 = _rakuten_item(item_code="shop1:1001", item_name="TestBrand テスト美容液", caption="JAN:4912345678901")
        item2 = _rakuten_item(item_code="shop2:2002", item_name="TestBrand テスト美容液", caption="JAN:4912345678901")
        with patch.object(app, "fetch_rakuten_candidates", return_value=[(50, item1), (45, item2)]):
            result = pipeline.resolve_item_code_for_product(
                "TestBrand", "テスト美容液", "美容液", jan_code="4912345678901",
            )
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["disambiguated_by"], "merchant_tiebreak")

    def test_missing_jan_does_not_drop_candidates(self):
        # JAN記載なしの候補を、JAN不明であることを理由に落とさない。
        item = _rakuten_item(item_name="TestBrand テスト美容液 30mL", caption="")
        with patch.object(app, "fetch_rakuten_candidates", return_value=[(50, item)]):
            result = pipeline.resolve_item_code_for_product("TestBrand", "テスト美容液", "美容液", jan_code=None)
        self.assertEqual(result["status"], "confirmed")


class VerifyAndResolveItemCodeTests(unittest.TestCase, DbCleanupMixin):
    """resolve確定後のfetch_rakuten_item_by_item_code再確認とDB更新。
    成分・formulation等は変更しないことを確認する(実楽天APIは呼ばない)。"""

    TEST_SUFFIX = "_ItemCodeTest"

    def setUp(self):
        app.init_product_master_table()
        self._cleanup_item_code_rows()

    def tearDown(self):
        self._cleanup_item_code_rows()

    def _cleanup_item_code_rows(self):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM product_master WHERE name LIKE %s", (f"%{self.TEST_SUFFIX}%",))
            conn.commit()
        finally:
            conn.close()

    def _insert_product(self, suffix):
        return app.upsert_product_master({
            "brand": "TestBrand", "name": f"テスト美容液{suffix}", "category": "美容液",
            "active_ingredients": ["ツボクサエキス"], "formulation": ["nano"],
            "jan_code": "4912345678901",
        }, data_source="ai_precollected")

    def test_title_mismatch_resolution_never_calls_fetch_by_item_code(self):
        # 明らかな別商品(タイトル不一致)は選択対象外のままfetch_rakuten_item_by_item_codeも呼ばれない。
        product_id = self._insert_product(self.TEST_SUFFIX + "1")
        wrong_item = _rakuten_item(item_name="全く別のブランドの日焼け止めクリーム")
        with patch.object(app, "fetch_rakuten_candidates", return_value=[(50, wrong_item)]), \
             patch.object(app, "fetch_rakuten_item_by_item_code") as mock_verify:
            result = pipeline.verify_and_resolve_item_code(
                product_id, "TestBrand", f"テスト美容液{self.TEST_SUFFIX}1", "美容液",
            )
        mock_verify.assert_not_called()
        self.assertEqual(result["status"], "not_found")

    def test_merchant_tiebreak_confirmation_then_fetch_failure_does_not_update_db(self):
        # 同一商品・複数店舗でタイブレークにより1件に確定した後、
        # itemCode再検証に失敗した場合はDBを更新しない。
        product_id = self._insert_product(self.TEST_SUFFIX + "1b")
        item1 = _rakuten_item(item_code="shop1:1001", item_name=f"TestBrand テスト美容液{self.TEST_SUFFIX}1b 30mL")
        item2 = _rakuten_item(item_code="shop2:2002", item_name=f"TestBrand テスト美容液{self.TEST_SUFFIX}1b 詰め替え")
        with patch.object(app, "fetch_rakuten_candidates", return_value=[(50, item1), (45, item2)]), \
             patch.object(app, "fetch_rakuten_item_by_item_code",
                           return_value={"ok": False, "http_status": 404, "rakuten_error": "not found", "item": None}) as mock_verify:
            result = pipeline.verify_and_resolve_item_code(
                product_id, "TestBrand", f"テスト美容液{self.TEST_SUFFIX}1b", "美容液",
            )
        mock_verify.assert_called_once_with("shop1:1001")  # タイブレークで選ばれた代表1件のみ再検証
        self.assertEqual(result["status"], "verification_failed")

        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT item_code FROM product_master WHERE product_id = %s", (product_id,))
            self.assertEqual(cur.fetchone()[0], "")
        finally:
            conn.close()

    def test_fetch_failure_does_not_update_db(self):
        product_id = self._insert_product(self.TEST_SUFFIX + "2")
        item = _rakuten_item(item_name=f"TestBrand テスト美容液{self.TEST_SUFFIX}2 30mL")
        with patch.object(app, "fetch_rakuten_candidates", return_value=[(50, item)]), \
             patch.object(app, "fetch_rakuten_item_by_item_code",
                           return_value={"ok": False, "http_status": 404, "rakuten_error": "not found", "item": None}):
            result = pipeline.verify_and_resolve_item_code(
                product_id, "TestBrand", f"テスト美容液{self.TEST_SUFFIX}2", "美容液",
            )
        self.assertEqual(result["status"], "verification_failed")

        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT item_code FROM product_master WHERE product_id = %s", (product_id,))
            self.assertEqual(cur.fetchone()[0], "")
        finally:
            conn.close()

    def test_successful_verification_updates_item_code_price_url_image_only(self):
        product_id = self._insert_product(self.TEST_SUFFIX + "3")
        item = _rakuten_item(item_name=f"TestBrand テスト美容液{self.TEST_SUFFIX}3 30mL")
        verified_item = {
            "itemPrice": 2480, "itemUrl": "https://item.rakuten.co.jp/shop1/1001/",
            "mediumImageUrls": [{"imageUrl": "https://img.example/verified.jpg"}],
        }
        with patch.object(app, "fetch_rakuten_candidates", return_value=[(50, item)]), \
             patch.object(app, "fetch_rakuten_item_by_item_code",
                           return_value={"ok": True, "http_status": 200, "rakuten_error": None, "item": verified_item}):
            result = pipeline.verify_and_resolve_item_code(
                product_id, "TestBrand", f"テスト美容液{self.TEST_SUFFIX}3", "美容液",
            )
        self.assertEqual(result["status"], "resolved")

        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT item_code, price_ref, last_known_rakuten_link, last_known_image, "
                "active_ingredients, formulation FROM product_master WHERE product_id = %s",
                (product_id,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "shop1:1001")
        self.assertEqual(row[1], 2480)
        self.assertEqual(row[2], "https://item.rakuten.co.jp/shop1/1001/")
        self.assertEqual(row[3], "https://img.example/verified.jpg")
        self.assertEqual(row[4], ["ツボクサエキス"])  # 成分は不変
        self.assertEqual(row[5], ["nano"])  # formulationも不変

    def test_merchant_tiebreak_success_leaves_ingredients_formulation_jan_unchanged(self):
        product_id = self._insert_product(self.TEST_SUFFIX + "3b")
        item1 = _rakuten_item(item_code="shop1:1001", item_name=f"TestBrand テスト美容液{self.TEST_SUFFIX}3b 30mL")
        item2 = _rakuten_item(item_code="shop2:2002", item_name=f"TestBrand テスト美容液{self.TEST_SUFFIX}3b 詰め替え")
        verified_item = {
            "itemPrice": 1980, "itemUrl": "https://item.rakuten.co.jp/shop1/1001/",
            "mediumImageUrls": [{"imageUrl": "https://img.example/verified.jpg"}],
        }
        with patch.object(app, "fetch_rakuten_candidates", return_value=[(50, item1), (45, item2)]), \
             patch.object(app, "fetch_rakuten_item_by_item_code",
                           return_value={"ok": True, "http_status": 200, "rakuten_error": None, "item": verified_item}):
            result = pipeline.verify_and_resolve_item_code(
                product_id, "TestBrand", f"テスト美容液{self.TEST_SUFFIX}3b", "美容液",
            )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["disambiguated_by"], "merchant_tiebreak")

        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT item_code, active_ingredients, formulation, jan_code "
                "FROM product_master WHERE product_id = %s", (product_id,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "shop1:1001")
        self.assertEqual(row[1], ["ツボクサエキス"])  # 成分不変
        self.assertEqual(row[2], ["nano"])  # formulation不変
        self.assertEqual(row[3], "4912345678901")  # JAN不変

    def test_not_found_resolution_never_calls_fetch_by_item_code(self):
        product_id = self._insert_product(self.TEST_SUFFIX + "4")
        with patch.object(app, "fetch_rakuten_candidates", return_value=[]), \
             patch.object(app, "fetch_rakuten_item_by_item_code") as mock_verify:
            result = pipeline.verify_and_resolve_item_code(
                product_id, "TestBrand", f"テスト美容液{self.TEST_SUFFIX}4", "美容液",
            )
        mock_verify.assert_not_called()
        self.assertEqual(result["status"], "not_found")


class SelectItemCodePilotCandidatesTests(unittest.TestCase):
    """実APIパイロット対象選定(読み取り専用、楽天APIは呼ばない)。"""

    def test_returns_only_rows_with_jan_code(self):
        candidates = pipeline.select_item_code_pilot_candidates(count=5)
        for c in candidates:
            self.assertTrue(c["jan_code"])

    def test_returns_at_most_requested_count(self):
        candidates = pipeline.select_item_code_pilot_candidates(count=3)
        self.assertLessEqual(len(candidates), 3)

    def test_prefers_category_diversity(self):
        candidates = pipeline.select_item_code_pilot_candidates(count=5)
        categories = [c["category"] for c in candidates]
        self.assertEqual(len(categories), len(set(categories)))


if __name__ == "__main__":
    unittest.main()
