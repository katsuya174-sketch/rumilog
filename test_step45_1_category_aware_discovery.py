"""Step45.1: category-aware Discovery / Stage1のテスト。

- 外部Discoveryの検索概念は推薦定義(_DEVICE_DEFAULTS/_SUPPLEMENT_DEFAULTS)から
  作り、内部target(RF/vitamin_c等)をそのまま検索文に入れない
- 本収集Stage1もカテゴリに応じた調査項目(方式/成分)を求める
- 化粧品の文面・挙動は変えない
- 候補0件の原因(no_search_evidence/structured_empty/source_url_mismatch等)を区別できる
- citation URLは完全一致を原則に、書き写し時の空白・括弧・末尾句読点のみ同一視

Gemini/Rakuten実APIは一切呼ばない(Geminiはモック)。
"""

import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import psycopg2  # noqa: E402

import app  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402
import product_master_pipeline as orchestrator  # noqa: E402
from test_product_master_pipeline_orchestrator import (  # noqa: E402
    OrchestratorTestBase, TEST_NAME_SUFFIX, _fake_response, _fake_stage1_response, make_mock_call_gemini,
)

DEVICE_TARGETS = list(app._DEVICE_DEFAULTS.keys())
SUPPLEMENT_TARGETS = [d["ingredient_focus"][0] for d in app._SUPPLEMENT_DEFAULTS.values()]
CIT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ_test_token_0001"


class DiscoverySearchConceptTests(unittest.TestCase):

    def test_rf_is_searched_as_radio_frequency_device(self):
        prompt = pipeline.build_discovery_prompt("美容機器", "RF")
        self.assertIn("RF高周波方式の顔用美容機器", prompt)
        self.assertNotIn("「RF」", prompt)
        self.assertNotIn("成分・特徴を配合", prompt)

    def test_all_six_device_methods_use_recommendation_definitions(self):
        for target in DEVICE_TARGETS:
            with self.subTest(target=target):
                prompt = pipeline.build_discovery_prompt("美容機器", target)
                self.assertIn(app._DEVICE_DEFAULTS[target]["device_function"] + "方式", prompt)
                self.assertIn(app._DEVICE_PURPOSE_LABELS[target], prompt)
                self.assertNotIn(f"「{target}」", prompt)
                self.assertIn("一次情報", prompt)

    def test_vitamin_c_is_searched_as_vitamin_c_supplement(self):
        prompt = pipeline.build_discovery_prompt("サプリメント", "vitamin_c")
        self.assertIn("ビタミンCを含むサプリメント", prompt)
        self.assertNotIn("vitamin_c", prompt)

    def test_all_ten_supplement_tags_use_display_names_not_internal_ids(self):
        for display_name, defaults in app._SUPPLEMENT_DEFAULTS.items():
            target = defaults["ingredient_focus"][0]
            with self.subTest(target=target):
                prompt = pipeline.build_discovery_prompt("サプリメント", target)
                structuring = pipeline.build_discovery_structuring_prompt("サプリメント", target, "本文", [])
                self.assertIn(f"{display_name}を含むサプリメント", prompt)
                self.assertNotIn(target, prompt)
                self.assertNotIn(target, structuring)
                self.assertIn(f"{display_name}を含むサプリメント", structuring)

    def test_cosmetics_discovery_prompts_are_unchanged(self):
        prompt = pipeline.build_discovery_prompt("美容液", "vitamin_c")
        self.assertTrue(prompt.startswith("あなたは化粧品・スキンケア・美容関連製品の情報調査アシスタントです。"))
        self.assertIn("条件: 「vitamin_c」に該当する成分・特徴を配合していることが", prompt)
        structuring = pipeline.build_discovery_structuring_prompt("美容液", "vitamin_c", "本文", [])
        self.assertIn("条件: vitamin_c\n", structuring)

    def test_unknown_device_target_is_not_searched(self):
        with patch.object(pipeline, "call_gemini_for_collection",
                          side_effect=AssertionError("未知targetは検索しないはず")):
            diag = {}
            result = pipeline.discover_candidates_via_gemini("美容機器", "unknown_method", "b", diagnostics=diag)
        self.assertEqual(result, [])
        self.assertEqual(diag["status"], "unknown_target_concept")


class Stage1PromptTests(unittest.TestCase):

    def test_cosmetics_stage1_prompt_unchanged(self):
        legacy = pipeline.build_stage1_prompt("ブランド", "商品")
        self.assertEqual(pipeline.build_stage1_prompt("ブランド", "商品", category="美容液"), legacy)
        self.assertTrue(legacy.startswith("あなたは化粧品・スキンケア製品の情報調査アシスタントです。"))

    def test_device_stage1_asks_for_method_from_definitions(self):
        prompt = pipeline.build_stage1_prompt("ブランド", "美顔器", category="美容機器")
        self.assertIn("美容機器製品の情報調査アシスタント", prompt)
        for defaults in app._DEVICE_DEFAULTS.values():
            self.assertIn(defaults["device_function"], prompt)
        self.assertIn("禁忌", prompt)
        self.assertNotIn("有効成分とその濃度", prompt)

    def test_supplement_stage1_asks_for_ingredients_and_dosage(self):
        prompt = pipeline.build_stage1_prompt("ブランド", "サプリ", category="サプリメント")
        self.assertIn("含有成分とその含有量", prompt)
        self.assertIn("1日の摂取目安量", prompt)

    def test_run_stage1_collection_passes_category_to_prompt(self):
        seen = []

        def fake(model, contents, config=None, max_retries=2, timeout=60):
            seen.append(contents)
            return _fake_stage1_response()

        with patch.object(pipeline, "call_gemini_for_collection", side_effect=fake), \
             patch.object(pipeline, "record_usage_and_check_limit", return_value={"limit_exceeded": False}):
            pipeline.run_stage1_collection("ブランド", "美顔器", "b", category="美容機器")
        self.assertIn("美容機器製品の情報調査アシスタント", seen[0])

    def test_stage2_method_enum_derived_from_recommendation_definitions(self):
        method = pipeline._CATEGORY_ATTRIBUTES_EXTRACTION_SPECS["美容機器"]["method"]
        self.assertEqual(method.enum, DEVICE_TARGETS + ["unknown"])
        for target, defaults in app._DEVICE_DEFAULTS.items():
            self.assertIn(f"{target}={defaults['device_function']}", method.description)


class DiscoveryDiagnosticsTests(unittest.TestCase):

    def _run(self, search_response, structured):
        def fake(model, contents, config=None, max_retries=2, timeout=60):
            if config is not None and getattr(config, "response_schema", None) is not None:
                return _fake_response(json.dumps({"candidates": structured}))
            return search_response

        diag = {}
        with patch.object(pipeline, "call_gemini_for_collection", side_effect=fake), \
             patch.object(pipeline, "record_usage_and_check_limit", return_value={"limit_exceeded": False}):
            result = pipeline.discover_candidates_via_gemini("サプリメント", "vitamin_c", "b", diagnostics=diag)
        return result, diag

    def _search_ok(self):
        return _fake_stage1_response("検索本文: 公式サイトでビタミンC配合を確認",
                                     citations=[{"uri": CIT, "title": "brand.example.com"}], queries=["q1", "q2"])

    def test_no_grounding_is_no_search_evidence(self):
        result, diag = self._run(_fake_stage1_response("回答", citations=[], queries=[]), [])
        self.assertEqual(result, [])
        self.assertEqual(diag["status"], "no_search_evidence")
        self.assertEqual(diag["search_query_count"], 0)

    def test_structured_empty_is_identified(self):
        result, diag = self._run(self._search_ok(), [])
        self.assertEqual(result, [])
        self.assertEqual(diag["status"], "structured_empty")
        self.assertEqual((diag["search_query_count"], diag["citation_count"]), (2, 1))

    def test_filter_reasons_are_counted(self):
        structured = [
            {"brand": "B", "product_name": "P1", "target_evidence": "x", "source_url": "https://www.brand.example.com/p1"},
            {"brand": "", "product_name": "P2", "target_evidence": "x", "source_url": CIT},
            {"brand": "B", "product_name": "", "target_evidence": "x", "source_url": CIT},
            {"brand": "B", "product_name": "P4", "target_evidence": "x", "source_url": "unknown"},
        ]
        result, diag = self._run(self._search_ok(), structured)
        self.assertEqual(result, [])
        self.assertEqual(diag["status"], "all_filtered")
        self.assertEqual(diag["filtered"], {"source_url_mismatch": 1, "missing_brand": 1,
                                            "missing_product_name": 1, "missing_source_url": 1})

    def test_valid_citation_candidate_passes_and_wrapped_url_is_canonicalized(self):
        structured = [
            {"brand": "B", "product_name": "P1", "target_evidence": "x", "source_url": CIT},
            {"brand": "C", "product_name": "P2", "target_evidence": "x", "source_url": f" <{CIT}>。"},
        ]
        result, diag = self._run(self._search_ok(), structured)
        self.assertEqual(diag["status"], "ok")
        self.assertEqual([c["source_url"] for c in result], [CIT, CIT])

    def test_diagnostics_do_not_store_raw_text(self):
        _, diag = self._run(self._search_ok(), [])
        self.assertNotIn("公式サイト", json.dumps(diag, ensure_ascii=False))
        self.assertEqual(len(diag["text_sha256"]), 12)
        self.assertGreater(diag["text_length"], 0)


class CitationUrlMatchTests(unittest.TestCase):

    def test_exact_and_safe_normalized_matches_only(self):
        urls = {CIT}
        self.assertEqual(pipeline.match_citation_url(CIT, urls), CIT)
        for variant in (f" {CIT} ", f"<{CIT}>", f"「{CIT}」", f"{CIT}。", f"({CIT}),"):
            with self.subTest(variant=variant):
                self.assertEqual(pipeline.match_citation_url(variant, urls), CIT)
        for bad in (CIT[:-1], CIT + "x", "https://vertexaisearch.cloud.google.com/", "unknown", "", None):
            with self.subTest(bad=bad):
                self.assertIsNone(pipeline.match_citation_url(bad, urls))

    def test_stage2_sanitize_uses_same_matching(self):
        payload = {
            "active_ingredients": [
                {"ingredient": "アスコルビン酸", "source_url": f"{CIT}。"},
                {"ingredient": "ナイアシンアミド", "source_url": "https://other.example.com/"},
            ],
            "formulation_features": [],
            "category_attributes": {"method": {"value": "RF", "confidence": "high", "source_url": f" {CIT}"}},
        }
        out = pipeline.sanitize_stage2_payload(payload, {CIT}, "本文", brand="B", citations=[{"uri": CIT, "title": "x"}])
        self.assertEqual([i["ingredient"] for i in out["active_ingredients"]], ["アスコルビン酸"])
        self.assertEqual(out["active_ingredients"][0]["source_url"], CIT)
        self.assertEqual(out["category_attributes"]["method"]["source_url"], CIT)


class OrchestratorDiscoveryReportTests(OrchestratorTestBase):

    def test_execute_reports_external_diagnostics_and_exclusion_reasons(self):
        dup = {"brand": f"重複{TEST_NAME_SUFFIX}", "product_name": f"P重複{TEST_NAME_SUFFIX}", "source_url": CIT}
        failed = {"brand": f"失敗{TEST_NAME_SUFFIX}", "product_name": f"P失敗{TEST_NAME_SUFFIX}", "source_url": CIT}
        key = lambda c: app.make_verified_product_key({"brand": c["brand"], "name": c["product_name"], "category": "美容機器"})  # noqa: E731

        def fake_discover(category, target, batch_id, max_candidates=3, diagnostics=None):
            diagnostics.update({"status": "ok", "structured_count": 2, "filtered": {}})
            return [dup, failed]

        budget = orchestrator.BatchBudget(self._new_batch_id("diag"), 5, 20, 0.50)
        with patch.object(orchestrator, "_product_master_identity_keys", return_value={key(dup)}), \
             patch.object(orchestrator, "_recently_failed_identity_keys", return_value={key(failed)}), \
             patch.object(app, "load_products", return_value=[]), \
             patch.object(app, "load_verified_products_cache", return_value=[]), \
             patch.object(pipeline, "discover_candidates_via_gemini", side_effect=fake_discover):
            source = orchestrator.make_discovery_candidate_source("b", budget, "execute")
            actions = orchestrator.process_coverage_gap_item(
                {"category": "美容機器", "target": "RF", "shortage_count": 1},
                "execute", "b", budget, source, 3, set(),
            )
        self.assertEqual(actions[0]["action"], "external_discovery_result")
        self.assertEqual(actions[0]["diagnostics"]["status"], "ok")
        self.assertEqual(actions[0]["excluded_by_reason"], {"duplicate": 1, "recent_failure": 1})


class CategoryStage1EndToEndTests(OrchestratorTestBase):
    """美容機器/サプリの本収集Stage1→Stage2→validator→reflectが、カテゴリに
    応じたStage1プロンプトで通ること(Geminiはモック)。"""

    def _run(self, category, target, brand, name, stage1_text, stage2_payload):
        batch_id = self._new_batch_id(f"s451-{target}")
        mock = make_mock_call_gemini({f"{brand} {name}": {
            "stage1": {"text": stage1_text, "citations": [{"uri": CIT, "title": brand}]},
            "stage2_payload": stage2_payload,
        }})
        prompts = []

        def spy(model, contents, config=None, max_retries=2, timeout=60):
            prompts.append(contents)
            return mock(model, contents, config=config, max_retries=max_retries, timeout=timeout)

        budget = orchestrator.BatchBudget(batch_id, 5, 20, 0.50)
        with patch.object(pipeline, "call_gemini_for_collection", side_effect=spy), \
             patch.object(pipeline, "verify_and_resolve_item_code", return_value={"status": "not_found"}):
            actions = orchestrator.process_coverage_gap_item(
                {"category": category, "target": target, "shortage_count": 1}, "execute", batch_id, budget,
                lambda c, t, n: [{"brand": brand, "name": name, "category": c}], 3, set(),
            )
        return actions, prompts

    def _master(self, brand, name, category):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT category_attributes, active_ingredients, active_ingredient_tags FROM product_master "
                        "WHERE identity_key = %s",
                        (app._normalize_product_master_identity_key(brand, name, category),))
            return cur.fetchone()
        finally:
            conn.close()

    def test_device_stage1_method_extracted_and_validator_passes(self):
        brand, name = f"デバイス{TEST_NAME_SUFFIX}", f"RF美顔器{TEST_NAME_SUFFIX}"
        actions, prompts = self._run("美容機器", "RF", brand, name,
                                     "公式サイトによると、RF高周波方式を採用した美顔器。",
                                     {"brand": brand, "product_name": name, "jan_code": "unknown",
                                      "active_ingredients": [], "formulation_features": [],
                                      "official_source_confirmed": True,
                                      "category_attributes": {
                                          "method": {"value": "RF", "confidence": "high", "source_url": CIT},
                                          "contraindications": {"value": "unknown", "confidence": "unknown",
                                                                "source_url": "unknown"},
                                      }})
        self.assertIn("美容機器製品の情報調査アシスタント", prompts[0])
        self.assertEqual(actions[0]["action"], "reflected")
        attrs, _, _ = self._master(brand, name, "美容機器")
        self.assertEqual(attrs, {"method": "RF"})
        self.assertTrue(app.is_candidate_relevant_to_target("美容機器", "RF", {"category_attributes": attrs}))

    def test_device_without_grounded_method_is_not_reflected(self):
        brand, name = f"デバイス無{TEST_NAME_SUFFIX}", f"美顔器無{TEST_NAME_SUFFIX}"
        actions, _ = self._run("美容機器", "RF", brand, name, "方式は不明。",
                               {"brand": brand, "product_name": name, "jan_code": "unknown",
                                "active_ingredients": [], "formulation_features": [],
                                "official_source_confirmed": True,
                                "category_attributes": {
                                    "method": {"value": "RF", "confidence": "high",
                                               "source_url": "https://not-a-citation.example.com/"},
                                    "contraindications": {"value": "unknown", "confidence": "unknown",
                                                          "source_url": "unknown"},
                                }})
        self.assertNotEqual(actions[0]["action"], "reflected")
        self.assertIsNone(self._master(brand, name, "美容機器"))

    def test_supplement_stage1_active_ingredient_relevance_passes(self):
        brand, name = f"サプリ{TEST_NAME_SUFFIX}", f"ビタミンCサプリ{TEST_NAME_SUFFIX}"
        actions, prompts = self._run("サプリメント", "vitamin_c", brand, name,
                                     "公式表示: ビタミンC(アスコルビン酸)1000mg。1日2粒。",
                                     {"brand": brand, "product_name": name, "jan_code": "unknown",
                                      "active_ingredients": [{"ingredient": "アスコルビン酸", "concentration": "1000mg",
                                                              "confidence": "high", "source_url": CIT}],
                                      "formulation_features": [], "official_source_confirmed": True,
                                      "category_attributes": {
                                          "dosage": {"value": "1日2粒", "confidence": "high", "source_url": CIT},
                                          "serving_size": {"value": "unknown", "confidence": "unknown",
                                                           "source_url": "unknown"},
                                          "precautions": {"value": "unknown", "confidence": "unknown",
                                                          "source_url": "unknown"},
                                      }})
        self.assertIn("含有成分とその含有量", prompts[0])
        self.assertEqual(actions[0]["action"], "reflected")
        _, actives, tags = self._master(brand, name, "サプリメント")
        self.assertIn("vitamin_c", tags)
        self.assertTrue(app.is_candidate_relevant_to_target("サプリメント", "vitamin_c",
                                                            {"active_ingredients": list(actives) + list(tags)}))


if __name__ == "__main__":
    unittest.main()
