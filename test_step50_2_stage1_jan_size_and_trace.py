"""Step50.2: Stage1でのJANと内容量の対応取得(案A)と、楽天照合ログの表示修正のテスト。

実Gemini・実楽天・実HTTP・本番DBは使わない。
"""

import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402
from test_step50_1_no_jan_identity import BRAND, CONTEXT, JAN, NAME, OK_TITLE, _item  # noqa: E402


class Stage1PromptTests(unittest.TestCase):

    def test_supplement_prompt_requests_jan_and_package_size(self):
        prompt = pipeline.build_stage1_prompt("B", "N", category="サプリメント")
        self.assertIn("販売規格ごとのJANコードと内容量", prompt)
        self.assertIn("JANコード：内容量", prompt)
        for unit in ("粒数", "カプセル数", "錠数", "重量(g)", "容量(ml)", "日分"):
            self.assertIn(unit, prompt)

    def test_other_categories_are_unchanged(self):
        self.assertNotIn("販売規格ごとのJANコード", pipeline.build_stage1_prompt("B", "N", category="美容機器"))
        self.assertEqual(pipeline.build_stage1_prompt("B", "N", category="美容液"), pipeline.build_stage1_prompt("B", "N"))

    def test_requested_line_format_is_readable_by_existing_parser(self):
        text = "- 4902720163934：30日分(30粒)\n- 4902720133760：15日分(15粒)\n1日1粒"
        self.assertEqual(pipeline.official_variant_specs(text, JAN), {("days", 30.0), ("count", 30.0)})


class TraceLabelTests(unittest.TestCase):

    def run_resolver(self, items, context=CONTEXT):
        out = io.StringIO()
        with patch.object(app, "fetch_rakuten_candidates", return_value=[(150, i) for i in items]), \
             contextlib.redirect_stdout(out):
            result = pipeline.resolve_item_code_for_product(BRAND, NAME, "サプリメント", jan_code=JAN, identity_context=context)
        return result

    def test_labels_distinguish_no_jan_acceptance_and_rejection(self):
        items = [_item("ok:1", OK_TITLE, caption="機能性表示食品"),
                 _item("v:1", "森永乳業 ビヒダス 生きて届くビフィズス菌 15日分 ビフィズス菌 BB536", caption="説明")]
        trace = {"searches": [], "items": [{"stage": "searched_candidate", "item_code": i["itemCode"], "title": i["itemName"],
                                            "_item": i} for i in items], "omitted_items": 0, "cache_hit": False}
        result = self.run_resolver(items)
        summary = pipeline._rakuten_match_summary(trace, result, BRAND, NAME, "サプリメント", JAN, CONTEXT)
        stages = {e["item_code"]: e["resolver_stage"] for e in trace["items"]}
        # Step52: 同じ商品の容量違い(15日分)も受け入れる。
        self.assertEqual(stages, {"ok:1": "eligible_name_without_jan", "v:1": "eligible_name_without_jan"})
        self.assertEqual(result["disambiguated_by"], "name_without_jan+review_selection")
        self.assertEqual(summary["resolver_stage_counts"], {"eligible_name_without_jan": 2})

    def test_jan_match_label_unchanged(self):
        item = _item("j:1", OK_TITLE, caption=f"JAN {JAN}")
        trace = {"searches": [], "items": [{"stage": "searched_candidate", "item_code": "j:1", "title": OK_TITLE, "_item": item}],
                 "omitted_items": 0, "cache_hit": False}
        pipeline._rakuten_match_summary(trace, {"status": "confirmed"}, BRAND, NAME, "サプリメント", JAN, CONTEXT)
        self.assertEqual(trace["items"][0]["resolver_stage"], "eligible")


if __name__ == "__main__":
    unittest.main()
