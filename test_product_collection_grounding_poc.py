"""
product_collection_grounding_poc.py(Phase 3本実装前の最小技術検証)の
単体テスト。実際のGemini APIは一切呼ばない(モックしたレスポンスのみ使用)。
"""

import unittest
from unittest.mock import MagicMock

from google.genai import types

import product_collection_grounding_poc as poc


class BuildProductCollectionPromptTests(unittest.TestCase):
    def test_includes_brand_and_product_name(self):
        prompt = poc.build_product_collection_prompt("COSRX", "アドバンスドスネイルムチンエッセンス96")
        self.assertIn("COSRX", prompt)
        self.assertIn("アドバンスドスネイルムチンエッセンス96", prompt)

    def test_instructs_no_guessing_and_unknown_fallback(self):
        prompt = poc.build_product_collection_prompt("Brand", "Product")
        self.assertIn("unknown", prompt)
        self.assertIn("推測", prompt)

    def test_mentions_official_source_priority(self):
        prompt = poc.build_product_collection_prompt("Brand", "Product")
        self.assertIn("公式", prompt)

    def test_mentions_formulation_feature_other_detail_requirement(self):
        prompt = poc.build_product_collection_prompt("Brand", "Product")
        self.assertIn("other", prompt)
        self.assertIn("other_detail", prompt)


class BuildProductCollectionResponseSchemaTests(unittest.TestCase):
    def test_schema_is_valid_types_schema_instance(self):
        schema = poc.build_product_collection_response_schema()
        self.assertIsInstance(schema, types.Schema)

    def test_top_level_required_fields_present(self):
        schema = poc.build_product_collection_response_schema()
        self.assertEqual(
            set(schema.required),
            {"brand", "product_name", "jan_code", "active_ingredients",
             "formulation_features", "official_source_confirmed"},
        )

    def test_formulation_feature_enum_matches_agreed_vocabulary(self):
        schema = poc.build_product_collection_response_schema()
        feature_schema = schema.properties["formulation_features"].items.properties["feature"]
        self.assertEqual(
            feature_schema.enum,
            ["liposome", "encapsulated", "nano", "sustained_release", "stabilized", "derivative", "other", "unknown"],
        )

    def test_ingredient_item_requires_confidence_and_source_url(self):
        schema = poc.build_product_collection_response_schema()
        ingredient_schema = schema.properties["active_ingredients"].items
        self.assertEqual(
            set(ingredient_schema.required),
            {"ingredient", "concentration", "confidence", "source_url"},
        )

    def test_formulation_item_requires_other_detail_field(self):
        schema = poc.build_product_collection_response_schema()
        formulation_schema = schema.properties["formulation_features"].items
        self.assertIn("other_detail", formulation_schema.required)


class BuildProductCollectionConfigTests(unittest.TestCase):
    def test_config_includes_google_search_tool(self):
        config = poc.build_product_collection_config()
        self.assertEqual(len(config.tools), 1)
        self.assertIsNotNone(config.tools[0].google_search)

    def test_config_requests_json_response(self):
        config = poc.build_product_collection_config()
        self.assertEqual(config.response_mime_type, "application/json")
        self.assertIsNotNone(config.response_schema)


class ExtractUsageSummaryTests(unittest.TestCase):
    def test_extracts_all_token_counts(self):
        response = MagicMock()
        response.usage_metadata.prompt_token_count = 100
        response.usage_metadata.candidates_token_count = 50
        response.usage_metadata.tool_use_prompt_token_count = 20
        response.usage_metadata.total_token_count = 170

        summary = poc.extract_usage_summary(response)

        self.assertEqual(summary["prompt_token_count"], 100)
        self.assertEqual(summary["candidates_token_count"], 50)
        self.assertEqual(summary["tool_use_prompt_token_count"], 20)
        self.assertEqual(summary["total_token_count"], 170)

    def test_missing_usage_metadata_returns_empty_dict(self):
        response = MagicMock()
        response.usage_metadata = None
        self.assertEqual(poc.extract_usage_summary(response), {})


class ExtractGroundingSummaryTests(unittest.TestCase):
    def test_extracts_search_queries_and_sources(self):
        response = MagicMock()
        web_chunk = MagicMock()
        web_chunk.web.uri = "https://example.com/product"
        web_chunk.web.title = "公式サイト"
        web_chunk.web.domain = "example.com"
        response.candidates = [MagicMock(
            grounding_metadata=MagicMock(
                web_search_queries=["COSRX スネイルムチン 成分"],
                grounding_chunks=[web_chunk],
            )
        )]

        summary = poc.extract_grounding_summary(response)

        self.assertEqual(summary["web_search_queries"], ["COSRX スネイルムチン 成分"])
        self.assertEqual(len(summary["sources"]), 1)
        self.assertEqual(summary["sources"][0]["uri"], "https://example.com/product")

    def test_missing_grounding_metadata_returns_empty_summary(self):
        response = MagicMock()
        response.candidates = [MagicMock(grounding_metadata=None)]
        summary = poc.extract_grounding_summary(response)
        self.assertEqual(summary, {"web_search_queries": [], "sources": []})

    def test_no_candidates_returns_empty_summary_without_crashing(self):
        response = MagicMock()
        response.candidates = []
        summary = poc.extract_grounding_summary(response)
        self.assertEqual(summary, {"web_search_queries": [], "sources": []})


if __name__ == "__main__":
    unittest.main()
