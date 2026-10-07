"""Step45.2: 美容機器「エレクトロポレーション」をイオン導入と同一方式として
扱わないことのテスト(6方式は増やさず、他5方式は不変)。

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
    OrchestratorTestBase, TEST_NAME_SUFFIX, _fake_response, make_mock_call_gemini,
)

TARGET = "エレクトロポレーション"
CIT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ_ep_0001"

OTHER_FIVE = {
    "RF": ("RF美顔器", "RF高周波", "RF高周波方式の顔用美容機器（用途: ハリ・たるみの引き締めケア）"),
    "LED": ("LED美顔器", "LED光照射", "LED光照射方式の顔用美容機器（用途: 赤み・炎症の鎮静、ニキビ跡ケア）"),
    "EMS": ("EMS美顔器", "EMS微電流", "EMS微電流方式の顔用美容機器（用途: フェイスラインの引き締め）"),
    "超音波洗浄": ("超音波洗浄機 毛穴 美顔器", "超音波振動", "超音波振動方式の顔用美容機器（用途: 毛穴汚れ・皮脂の毛穴ケア）"),
    "マイクロカレント": ("マイクロカレント美顔器", "微弱電流(マイクロカレント)",
                     "微弱電流(マイクロカレント)方式の顔用美容機器（用途: 軽度のハリ不足のケア）"),
}


def _payload(method_value, source_url=CIT):
    return {
        "brand": "B", "product_name": "P", "jan_code": "unknown",
        "active_ingredients": [], "formulation_features": [], "official_source_confirmed": True,
        "category_attributes": {
            "method": {"value": method_value, "confidence": "high", "source_url": source_url},
            "contraindications": {"value": "unknown", "confidence": "unknown", "source_url": "unknown"},
        },
    }


def _stage2(stage1_text, method_value):
    stage1 = {"status": "ok", "raw_text": stage1_text, "citations": [{"uri": CIT, "title": "b.example.com"}],
              "search_queries": ["q"]}
    with patch.object(pipeline, "call_gemini_for_collection",
                      return_value=_fake_response(json.dumps(_payload(method_value)))), \
         patch.object(pipeline, "record_usage_and_check_limit", return_value={"limit_exceeded": False}):
        result = pipeline.run_stage2_structuring("B", "P", "美容機器", stage1, "b")
    return result["payload"]["category_attributes"]["method"]["value"]


class DefinitionTests(unittest.TestCase):

    def test_electroporation_definition_does_not_mention_iontophoresis(self):
        d = app._DEVICE_DEFAULTS[TARGET]
        self.assertEqual((d["product"], d["device_function"]), ("エレクトロポレーション美顔器", "エレクトロポレーション"))
        self.assertNotIn("イオン導入", json.dumps(d, ensure_ascii=False))
        self.assertNotIn("イオン導入", app._DEVICE_PURPOSE_LABELS[TARGET])

    def test_still_six_methods(self):
        self.assertEqual(list(app._DEVICE_DEFAULTS),
                         ["RF", "LED", "EMS", TARGET, "超音波洗浄", "マイクロカレント"])

    def test_other_five_methods_unchanged(self):
        for target, (product, function, concept) in OTHER_FIVE.items():
            with self.subTest(target=target):
                d = app._DEVICE_DEFAULTS[target]
                self.assertEqual((d["product"], d["device_function"]), (product, function))
                self.assertNotIn("method_evidence_terms", d)
                self.assertEqual(pipeline.discovery_search_concept("美容機器", target), concept)


class PromptTests(unittest.TestCase):

    def test_discovery_concept_is_electroporation_only(self):
        concept = pipeline.discovery_search_concept("美容機器", TARGET)
        self.assertTrue(concept.startswith("エレクトロポレーション方式の顔用美容機器"))
        for prompt in (pipeline.build_discovery_prompt("美容機器", TARGET),
                       pipeline.build_discovery_structuring_prompt("美容機器", TARGET, "本文", [])):
            self.assertNotIn("イオン導入", prompt)

    def test_stage1_and_stage2_definitions_do_not_conflate(self):
        self.assertNotIn("イオン導入", pipeline.build_stage1_prompt("B", "P", category="美容機器"))
        method = pipeline._CATEGORY_ATTRIBUTES_EXTRACTION_SPECS["美容機器"]["method"]
        self.assertNotIn("イオン導入", method.description)
        self.assertIn("エレクトロポレーション=エレクトロポレーション", method.description)


class MethodEvidenceTests(unittest.TestCase):

    def test_electroporation_stated_in_stage1_matches_target(self):
        self.assertEqual(_stage2("公式: エレクトロポレーション技術を採用。", TARGET), TARGET)
        self.assertEqual(_stage2("Official: uses Electroporation technology.", TARGET), TARGET)

    def test_iontophoresis_only_is_not_electroporation(self):
        self.assertEqual(_stage2("公式: イオン導入機能を搭載した美顔器。", TARGET), "unknown")
        self.assertEqual(_stage2("Iontophoresis device.", TARGET), "unknown")

    def test_other_methods_not_subject_to_text_evidence_check(self):
        # 他5方式は従来どおり(citation照合のみ。本文の語チェックは追加しない)。
        self.assertEqual(_stage2("ラジオ波で引き締め。", "RF"), "RF")
        self.assertEqual(_stage2("光でケア。", "LED"), "LED")

    def test_relevance_requires_electroporation_method(self):
        self.assertTrue(app.is_candidate_relevant_to_target("美容機器", TARGET,
                                                            {"category_attributes": {"method": TARGET}}))
        self.assertFalse(app.is_candidate_relevant_to_target("美容機器", TARGET,
                                                             {"category_attributes": {"method": "イオン導入"}}))


class EndToEndTests(OrchestratorTestBase):

    def _run(self, brand, name, stage1_text):
        mock = make_mock_call_gemini({f"{brand} {name}": {
            "stage1": {"text": stage1_text, "citations": [{"uri": CIT, "title": brand}]},
            "stage2_payload": dict(_payload(TARGET), brand=brand, product_name=name),
        }})
        batch_id = self._new_batch_id("s452")
        budget = orchestrator.BatchBudget(batch_id, 5, 20, 0.50)
        with patch.object(pipeline, "call_gemini_for_collection", side_effect=mock), \
             patch.object(pipeline, "verify_and_resolve_item_code", return_value={"status": "not_found"}):
            return orchestrator.process_coverage_gap_item(
                {"category": "美容機器", "target": TARGET, "shortage_count": 1}, "execute", batch_id, budget,
                lambda c, t, n: [{"brand": brand, "name": name, "category": c}], 3, set(),
            )

    def _attrs(self, brand, name):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT category_attributes FROM product_master WHERE identity_key = %s",
                        (app._normalize_product_master_identity_key(brand, name, "美容機器"),))
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def test_electroporation_device_is_reflected(self):
        brand, name = f"EP{TEST_NAME_SUFFIX}", f"EP美顔器{TEST_NAME_SUFFIX}"
        actions = self._run(brand, name, "公式サイト: エレクトロポレーション方式の美顔器。")
        self.assertEqual(actions[0]["action"], "reflected")
        self.assertEqual(self._attrs(brand, name), {"method": TARGET})

    def test_iontophoresis_only_device_is_not_reflected_as_electroporation(self):
        brand, name = f"ION{TEST_NAME_SUFFIX}", f"イオン美顔器{TEST_NAME_SUFFIX}"
        actions = self._run(brand, name, "公式サイト: イオン導入機能付き美顔器。")
        self.assertNotEqual(actions[0]["action"], "reflected")
        self.assertIsNone(self._attrs(brand, name))


if __name__ == "__main__":
    unittest.main()
