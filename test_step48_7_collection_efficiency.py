"""Step48.7: 楽天検索キーワードの半角1文字語の扱いと、Discovery予算の事前判定の
テスト。

実Gemini・実楽天・実HTTP・本番DBは使わない。
"""

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402
import product_master_pipeline as orchestrator  # noqa: E402
from test_product_master_pipeline_orchestrator import OrchestratorTestBase  # noqa: E402


class RequestKeywordTests(unittest.TestCase):

    def test_single_halfwidth_tokens_are_removed(self):
        self.assertEqual(app.rakuten_request_keyword("Solaray ソラレー L Cysteine L システイン サプリメント"),
                         "Solaray ソラレー Cysteine システイン サプリメント")
        self.assertEqual(app.rakuten_request_keyword("L Cysteine L システイン"), "Cysteine システイン")

    def test_other_keywords_are_unchanged(self):
        for keyword in ("DHC ビタミンC サプリメント", "ビタミンD 30日分", "コエンザイムQ10", "パナソニック EH SR85",
                        "ヤーマン RF 美顔器", "UV ケア", "ビタミン B6", "ネイチャーメイド スーパーフィッシュオイル"):
            with self.subTest(keyword=keyword):
                self.assertEqual(app.rakuten_request_keyword(keyword), keyword)

    def test_fullwidth_single_character_is_kept(self):
        # 全角1文字は半角2文字相当のため、楽天の最小長制限に当たらない。
        self.assertEqual(app.rakuten_request_keyword("菊 正宗"), "菊 正宗")

    def test_only_single_tokens_is_left_as_is(self):
        self.assertEqual(app.rakuten_request_keyword("L C"), "L C")
        self.assertEqual(app.rakuten_request_keyword(""), "")

    def test_matching_normalization_is_unchanged(self):
        # 名称照合で使うclean_rakuten_keyword()の結果は変えない。
        self.assertEqual(app.clean_rakuten_keyword("L-Cysteine（L-システイン）"), "L Cysteine L システイン")


class FetchUsesRequestKeywordTests(unittest.TestCase):

    def test_requests_have_no_single_halfwidth_tokens(self):
        sent = []

        def fake_get(kind, endpoint, params=None, **kwargs):
            sent.append(params["keyword"])
            res = MagicMock(status_code=200, text="")
            res.json.return_value = {"Items": []}
            return res
        with patch.object(app, "_rakuten_api_get", side_effect=fake_get), \
             patch.object(app, "wait_for_rakuten_rate_limit", return_value=None):
            app.fetch_rakuten_candidates("L-Cysteine 500mg（L-システイン 500mg）", category="サプリメント",
                                         brand="Solaray（ソラレー）")
        self.assertTrue(sent)
        for keyword in sent:
            with self.subTest(keyword=keyword):
                self.assertFalse(any(len(t) == 1 and t.isascii() and t.isalnum() for t in keyword.split()))
        self.assertIn("Cysteine", sent[0])
        self.assertEqual(len(sent), len(set(sent)))  # 正規化後の重複キーワードは送らない


class DiscoveryBudgetTests(OrchestratorTestBase):

    def _run(self, estimates, statuses=("ok",), max_cost=0.50):
        calls = []

        def fake_discover(category, target, batch_id, max_candidates=3, diagnostics=None):
            status = statuses[len(calls)]
            calls.append(status)
            diagnostics.update({"status": status})
            return []
        budget = orchestrator.BatchBudget(self._new_batch_id("s487"), 5, 20, max_cost)
        with patch.object(orchestrator, "conservative_cost_estimate", side_effect=lambda kind: estimates[kind]), \
             patch.object(orchestrator, "_product_master_identity_keys", return_value=set()), \
             patch.object(orchestrator, "_recently_failed_identity_keys", return_value=set()), \
             patch.object(orchestrator, "_gate_failed_identities", return_value={}), \
             patch.object(orchestrator, "_staging_reuse_candidates", return_value=[]), \
             patch.object(app, "load_products", return_value=[]), \
             patch.object(app, "load_verified_products_cache", return_value=[]), \
             patch.object(pipeline, "discover_candidates_via_gemini", side_effect=fake_discover):
            source = orchestrator.make_discovery_candidate_source(budget.batch_id, budget, "execute")
            source("サプリメント", "collagen", 3)
        return calls, budget, source.last_report

    def test_discovery_is_skipped_when_no_product_could_follow(self):
        # Discovery単体(0.05)は入るが、Discovery+1商品分(0.60)は上限0.50を超える。
        calls, budget, report = self._run({orchestrator.DISCOVERY_WITH_ONE_PRODUCT: 0.60, "external_discovery": 0.05})
        self.assertEqual(calls, [])
        self.assertFalse(report["external_discovery_executed"])
        self.assertEqual(budget.stopped_reason, "insufficient_budget_for_next")
        self.assertEqual(budget.last_estimate["kind"], orchestrator.DISCOVERY_WITH_ONE_PRODUCT)

    def test_discovery_runs_when_discovery_and_one_product_fit(self):
        calls, budget, report = self._run({orchestrator.DISCOVERY_WITH_ONE_PRODUCT: 0.20})
        self.assertEqual(calls, ["ok"])
        self.assertTrue(report["external_discovery_executed"])

    def test_retry_also_requires_one_product_budget(self):
        # 初回のpreflightは呼び出し元(candidate_source)が行う。再試行前のpreflight
        # (Discovery+1商品分=0.60)は上限0.50を超えるため再試行しない。
        estimates = iter([0.60])
        budget = orchestrator.BatchBudget(self._new_batch_id("s487-retry"), 5, 20, 0.50)
        discover_calls = []

        def fake_discover(category, target, batch_id, max_candidates=3, diagnostics=None):
            discover_calls.append(1)
            diagnostics.update({"status": "no_search_evidence"})
            return []
        with patch.object(orchestrator, "conservative_cost_estimate", side_effect=lambda kind: next(estimates)), \
             patch.object(pipeline, "discover_candidates_via_gemini", side_effect=fake_discover):
            diag = {}
            orchestrator._gemini_discovery_candidates("サプリメント", "collagen", budget.batch_id, set(), 3,
                                                      diagnostics=diag, budget=budget)
        self.assertEqual(len(discover_calls), 1)
        self.assertEqual([a["status"] for a in diag["attempts"]], ["no_search_evidence", "skipped_budget"])

    def test_combined_estimate_is_sum_of_parts(self):
        with patch.object(orchestrator, "_pessimistic_cost_estimate", return_value=0.0):
            total = orchestrator.conservative_cost_estimate(orchestrator.DISCOVERY_WITH_ONE_PRODUCT)
            parts = (orchestrator.conservative_cost_estimate("external_discovery")
                     + orchestrator.conservative_cost_estimate("product_collection"))
        self.assertAlmostEqual(total, parts)


if __name__ == "__main__":
    unittest.main()
