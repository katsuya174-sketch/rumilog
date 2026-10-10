"""Step48.5: サプリDiscoveryで医薬品・医薬部外品と判明した候補をStage1前に除外する
(negative gate専用)ことのテスト。

実Gemini・実楽天・実HTTP・本番DBは使わない。
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import psycopg2  # noqa: E402

import app  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402
import product_master_pipeline as orchestrator  # noqa: E402
from test_product_master_pipeline_orchestrator import OrchestratorTestBase, TEST_NAME_SUFFIX  # noqa: E402

CIT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ_s485_"
FORBIDDEN = AssertionError("early reject後に呼ばれないはず")


def _precheck_only_resolver(calls):
    """Step49.3: Stage1前の楽天事前確認(JANなし)だけを許可し、商品名を記録する。
    検索0件(除外しない)を返す。reflect前の確認(JANあり等)は呼ばれないはず。"""
    def resolve(brand, product_name, category, jan_code=None):
        if jan_code is not None:
            raise AssertionError("reflect前の楽天確認には進まないはず")
        calls.append(product_name)
        return {"status": "not_found", "initial_candidate_count": 0}
    return resolve


def _cand(name, evidence="L-システイン配合", brand="ブランド"):
    return {"brand": f"{brand}{TEST_NAME_SUFFIX}", "product_name": f"{name}{TEST_NAME_SUFFIX}",
            "target_evidence": evidence, "source_url": CIT + name}


def regulated(evidence, text="", category="サプリメント", name="商品X"):
    return pipeline.discovery_regulated_product_class(category, _cand(name, evidence), text)


class RegulatedClassTests(unittest.TestCase):

    def test_drug_labels_in_evidence_are_excluded(self):
        for label in ("第1類医薬品", "第2類医薬品", "指定第2類医薬品", "第3類医薬品", "要指導医薬品", "第３類医薬品"):
            with self.subTest(label=label):
                self.assertEqual(regulated(f"L-システイン240mg配合の{label}"), "drug")

    def test_quasi_drug_is_excluded(self):
        self.assertEqual(regulated("ビタミンC誘導体配合の医薬部外品"), "quasi_drug")

    def test_label_in_discovery_text_sentence_naming_the_candidate(self):
        text = f"1. 商品X{TEST_NAME_SUFFIX}（第3類医薬品）はL-システインを含みます。\n2. 商品Yはサプリメントです。"
        self.assertEqual(regulated("L-システイン配合", text), "drug")
        # 他の候補の文の区分表示は流用しない。
        self.assertIsNone(regulated("L-システイン配合", text, name="商品Y"))

    def test_precaution_only_is_not_excluded(self):
        self.assertIsNone(regulated("医薬品を服用中の方は医師に相談してください。L-システイン配合"))

    def test_food_classes_and_unknown_are_not_excluded(self):
        for evidence in ("機能性表示食品。L-システイン配合", "栄養機能食品(ビタミンC)", "unknown", ""):
            with self.subTest(evidence=evidence):
                self.assertIsNone(regulated(evidence))

    def test_other_categories_are_never_excluded(self):
        for category in ("美容機器", "美容液"):
            self.assertIsNone(regulated("第3類医薬品", category=category))


class DiscoverCandidatesTaggingTests(unittest.TestCase):

    def _discover(self, candidates, text, max_candidates=3):
        search = {"status": "ok", "raw_text": text, "citations": [{"uri": CIT, "title": "x"}], "search_queries": ["q"]}
        structured = {"status": "ok", "payload": {"candidates": candidates}, "structured_count": len(candidates)}
        diag = {}
        with patch.object(pipeline, "run_discovery_search", return_value=search), \
             patch.object(pipeline, "run_discovery_structuring", return_value=structured):
            result = pipeline.discover_candidates_via_gemini("サプリメント", "l_cysteine", "b",
                                                            max_candidates=max_candidates, diagnostics=diag)
        return result, diag

    def test_regulated_candidates_do_not_use_up_normal_slots(self):
        cands = [_cand("薬A", "第3類医薬品"), _cand("サプリB"), _cand("サプリC"), _cand("サプリD")]
        result, diag = self._discover(cands, "")
        self.assertEqual([c.get("discovery_regulated_product_class") for c in result], ["drug", None, None, None])
        self.assertEqual((diag["returned"], diag["regulated_excluded"], diag["truncated"]), (3, 1, 0))

    def test_device_discovery_is_unchanged(self):
        search = {"status": "ok", "raw_text": "第3類医薬品", "citations": [{"uri": CIT, "title": "x"}], "search_queries": ["q"]}
        cands = [_cand("機器A", "第3類医薬品"), _cand("機器B"), _cand("機器C"), _cand("機器D")]
        structured = {"status": "ok", "payload": {"candidates": cands}, "structured_count": 4}
        diag = {}
        with patch.object(pipeline, "run_discovery_search", return_value=search), \
             patch.object(pipeline, "run_discovery_structuring", return_value=structured):
            result = pipeline.discover_candidates_via_gemini("美容機器", "RF", "b", diagnostics=diag)
        self.assertEqual(result, cands[:3])
        self.assertNotIn("regulated_excluded", diag)


class EarlyRejectFlowTests(OrchestratorTestBase):

    def _run(self, discovered, collect, max_consecutive_failures=3):
        budget = orchestrator.BatchBudget(self._new_batch_id("s485"), 5, 20, 0.50)
        self.rakuten_prechecked = []

        # 検索・構造化だけを差し替え、候補の区分判定(discover_candidates_via_gemini)は実コードを通す。
        search = {"status": "ok", "raw_text": "", "citations": [{"uri": CIT, "title": "x"}], "search_queries": ["q"]}
        structured = {"status": "ok", "payload": {"candidates": [dict(c) for c in discovered]},
                      "structured_count": len(discovered)}
        patches = [patch.object(orchestrator, "_product_master_identity_keys", return_value=set()),
                   patch.object(orchestrator, "_recently_failed_identity_keys", return_value=set()),
                   patch.object(orchestrator, "_gate_failed_identities", return_value={}),
                   patch.object(orchestrator, "_staging_reuse_candidates", return_value=[]),
                   patch.object(app, "load_products", return_value=[]),
                   patch.object(app, "load_verified_products_cache", return_value=[]),
                   patch.object(orchestrator, "conservative_cost_estimate", return_value=0.01),
                   patch.object(pipeline, "run_discovery_search", return_value=search),
                   patch.object(pipeline, "run_discovery_structuring", return_value=structured),
                   patch.object(pipeline, "collect_one_product", side_effect=collect),
                   patch.object(pipeline, "call_gemini_for_collection", side_effect=FORBIDDEN),
                   patch.object(pipeline.citation_verification, "fetch_html", side_effect=FORBIDDEN),
                   patch.object(pipeline, "resolve_item_code_for_product",
                                side_effect=_precheck_only_resolver(self.rakuten_prechecked)),
                   patch.object(pipeline, "reflect_staging_to_product_master", side_effect=FORBIDDEN)]
        for p in patches:
            p.start()
        try:
            source = orchestrator.make_discovery_candidate_source(budget.batch_id, budget, "execute")
            actions = orchestrator.process_coverage_gap_item(
                {"category": "サプリメント", "target": "l_cysteine", "shortage_count": 1}, "execute",
                budget.batch_id, budget, source, max_consecutive_failures, set())
        finally:
            for p in reversed(patches):
                p.stop()
        return actions, source.last_report

    def _staging_without_evidence(self, name):
        brand = f"ブランド{TEST_NAME_SUFFIX}"
        self._insert_staging_row(self._new_batch_id("s485-st"), brand, name, "サプリメント",
                                 stage2_payload={"active_ingredients": [{"ingredient": "L-システイン", "source_url": CIT}],
                                                 "formulation_features": [], "category_attributes": {}},
                                 citations=[{"uri": CIT, "title": "cosme.net"}], stage1_raw_text="L-システイン配合。")
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT staging_id FROM product_collection_staging WHERE product_name = %s", (name,))
            return cur.fetchone()[0]
        finally:
            conn.close()

    def test_regulated_candidates_are_skipped_and_next_normal_candidate_reaches_final_gate(self):
        normal = _cand("サプリN")
        sid = self._staging_without_evidence(normal["product_name"])
        collected = []

        def collect(brand, name, category, batch_id, existing_product=None, existing_jans=None):
            collected.append(name)
            return {"staging_id": sid, "stage1_status": "ok", "stage2_status": "ok",
                    "conflict_status": "new", "limit_exceeded": False}
        discovered = [_cand("薬A", "第3類医薬品"), _cand("薬B", "指定第2類医薬品"), _cand("薬C", "医薬部外品"), normal]
        # 連続失敗上限1でも、early rejectは失敗に数えないため正常候補まで進む。
        actions, report = self._run(discovered, collect, max_consecutive_failures=1)

        self.assertEqual(collected, [normal["product_name"]])  # 医薬品候補はStage1(収集)へ進まない
        self.assertEqual(self.rakuten_prechecked, [normal["product_name"]])  # 楽天事前確認にも進まない
        processed = [a for a in actions if a["action"] not in ("external_discovery_result",)]
        self.assertEqual([a["action"] for a in processed], ["not_reflected"])
        # Step48.2/48.3の最終eligibilityゲートは維持される。
        self.assertEqual(processed[0]["reason"], "supplement_not_eligible")
        excluded = [e for e in report["excluded"] if e["reason"] == "discovery_regulated_product"]
        self.assertEqual(sorted(e["product_classification"] for e in excluded), ["drug", "drug", "quasi_drug"])
        summary = next(a for a in actions if a["action"] == "external_discovery_result")
        self.assertEqual(summary["excluded_by_reason"]["discovery_regulated_product"], 3)

    def test_all_regulated_means_no_collection_and_no_area_stop(self):
        actions, _ = self._run([_cand("薬A", "第3類医薬品"), _cand("薬B", "第2類医薬品")],
                               collect=FORBIDDEN, max_consecutive_failures=1)
        self.assertEqual([a["action"] for a in actions], ["external_discovery_result"])

    def test_precaution_only_candidate_goes_to_stage1(self):
        cand = _cand("サプリP", "医薬品を服用中の方は医師に相談。L-システイン配合")
        sid = self._staging_without_evidence(cand["product_name"])
        collected = []

        def collect(brand, name, category, batch_id, existing_product=None, existing_jans=None):
            collected.append(name)
            return {"staging_id": sid, "stage1_status": "ok", "stage2_status": "ok",
                    "conflict_status": "new", "limit_exceeded": False}
        self._run([cand], collect)
        self.assertEqual(collected, [cand["product_name"]])


if __name__ == "__main__":
    unittest.main()
