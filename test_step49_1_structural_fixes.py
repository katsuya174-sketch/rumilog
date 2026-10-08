"""Step49.1: Product Master収集の構造問題(法人格表記・確認順序・コラーゲンペプチド・
JAN/ブランド重複・型番注記・transient staging)の修正テスト。

実Gemini・実楽天・実HTTP・本番DBは使わない。
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import psycopg2  # noqa: E402

import app  # noqa: E402
import citation_verification as cv  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402
import product_master_pipeline as orchestrator  # noqa: E402
from test_product_master_pipeline_orchestrator import OrchestratorTestBase, TEST_NAME_SUFFIX  # noqa: E402

CIT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ_s491_"
FORBIDDEN = AssertionError("呼ばれないはず")


def _jan(body12):
    digits = [int(c) for c in body12]
    total = sum(d * (3 if i % 2 else 1) for i, d in enumerate(digits))
    return body12 + str((10 - total % 10) % 10)


JAN_A = _jan("490000049101")
JAN_B = _jan("490000049102")


def _site(domain, og):
    return {"uri": CIT + domain, "title": domain, "page_verification": {
        "fetch_status": "ok", "status": "not_confirmed", "final_domain": domain, "canonical_domain": domain,
        "site_identity": {"og_site_name": og, "jsonld_names": [], "title_site_name": None}}}


class CorporateDesignatorTests(unittest.TestCase):

    def official(self, brand, og, domain="kobayashi.co.jp"):
        return pipeline.is_official_source_confirmed(brand, [_site(domain, og)])

    def test_legal_name_with_tagline_confirms_brand(self):
        self.assertTrue(self.official("小林製薬", "小林製薬株式会社 -あったらいいなをカタチにする-"))
        self.assertTrue(self.official("小林製薬", "株式会社小林製薬"))
        self.assertTrue(self.official("小林製薬", "小林製薬(株)"))

    def test_partial_brand_is_still_not_confirmed(self):
        # 「小林」は「小林製薬」の一部であり、法人格表記の扱いで緩めない。
        self.assertFalse(self.official("小林", "小林製薬株式会社"))
        self.assertFalse(self.official("製薬", "小林製薬株式会社"))

    def test_maker_only_identity_is_still_not_confirmed(self):
        self.assertFalse(self.official("森永ビヒダス（森永乳業）", "森永乳業ウェブサイト", "morinagamilk.co.jp"))
        self.assertFalse(self.official("iMUSE（イミューズ）", "キリン", "imuse-p.jp"))

    def test_retail_and_comparison_sites_are_not_confirmed(self):
        self.assertFalse(self.official("小林製薬", "Yahoo!ショッピング", "yahoo.co.jp"))
        kakaku = _site("kakaku.com", "価格.com")
        kakaku["page_verification"]["site_identity"]["title_site_name"] = "小林製薬 亜鉛 価格比較"
        self.assertFalse(pipeline.is_official_source_confirmed("小林製薬", [kakaku]))


class CheckOrderTests(unittest.TestCase):

    def test_most_cited_domain_is_checked_first(self):
        cits = [{"uri": CIT + "1", "title": "yahoo.co.jp"}, {"uri": CIT + "2", "title": "shop.example.com"},
                {"uri": CIT + "3", "title": "brand.example.jp"}, {"uri": CIT + "4", "title": "brand.example.jp"},
                {"uri": CIT + "5", "title": "brand.example.jp"}, {"uri": CIT + "6", "title": "shop.example.com"}]
        order = [c["title"] for c in pipeline._page_verification_order(cits)]
        self.assertEqual(order, ["brand.example.jp", "shop.example.com", "yahoo.co.jp"])

    def test_attach_uses_order_and_keeps_five_domain_limit(self):
        cits = [{"uri": f"{CIT}s{i}", "title": f"site{i}.example.com"} for i in range(7)]
        cits += [{"uri": CIT + "o1", "title": "official.example.jp"}, {"uri": CIT + "o2", "title": "official.example.jp"}]
        fetched = []

        def fetch(url):
            fetched.append(url)
            return {"status": cv.UNVERIFIABLE, "reason": "http_403", "final_url": ""}
        with patch.object(cv, "fetch_html", side_effect=fetch):
            pipeline.attach_citation_page_verification("ブランドX", cits)
        self.assertEqual(fetched[0], CIT + "o1")
        self.assertEqual(len(fetched), pipeline.MAX_PAGE_VERIFICATION_DOMAINS)


class CollagenNormalizationTests(unittest.TestCase):

    def test_collagen_peptide_is_collagen(self):
        for name in ("コラーゲンペプチド", "豚コラーゲンペプチド", "コラーゲントリペプチド", "Collagen Peptide"):
            with self.subTest(name=name):
                self.assertEqual(app.normalize_ingredient_tag(name), "collagen")

    def test_other_peptides_unchanged(self):
        for name in ("ペプチド", "パルミトイルトリペプチド-1", "アセチルヘキサペプチド-8", "sh-oligopeptide-1"):
            with self.subTest(name=name):
                self.assertEqual(app.normalize_ingredient_tag(name), "peptide")
        self.assertEqual(app.normalize_ingredient_tag("加水分解コラーゲン"), "collagen")

    def test_supplement_primary_tag(self):
        payload = {"active_ingredients": [{"ingredient": "コラーゲンペプチド"}, {"ingredient": "ビタミンC"}],
                   "category_attributes": {"primary_ingredients": {"value": "コラーゲンペプチド、ビタミンC"}}}
        self.assertEqual(pipeline.supplement_primary_tags_for_payload("X", payload), ["collagen", "vitamin_c"])


class JanAndNameTests(unittest.TestCase):

    def test_extract_jan_codes_requires_valid_check_digit(self):
        text = f"JAN: {JAN_A} / 誤り: {JAN_A[:-1]}{(int(JAN_A[-1]) + 1) % 10} / 14桁: 1{JAN_A}"
        self.assertEqual(pipeline.extract_jan_codes(text), {JAN_A})
        self.assertEqual(pipeline.extract_jan_codes("４９８７０３５６９１９１４"), {"4987035691914"})

    def test_brand_prefix_stripping(self):
        self.assertEqual(pipeline.brand_stripped_product_name("ネイチャーメイド（大塚製薬）", "ネイチャーメイド スーパーフィッシュオイル"),
                         "スーパーフィッシュオイル")
        self.assertIsNone(pipeline.brand_stripped_product_name("ネイチャーメイド（大塚製薬）", "スーパーフィッシュオイル"))
        self.assertIsNone(pipeline.brand_stripped_product_name("DHC", "DHC"))
        self.assertIsNone(pipeline.brand_stripped_product_name("DHC", "DHCA"))  # 区切り無しの別語は除かない

    def test_identity_keys_match_across_brand_prefix(self):
        a = orchestrator.identity_keys_for("ネイチャーメイド（大塚製薬）", "ネイチャーメイド スーパーフィッシュオイル", "サプリメント")
        b = orchestrator.identity_keys_for("ネイチャーメイド（大塚製薬）", "スーパーフィッシュオイル", "サプリメント")
        self.assertTrue(a & b)
        other = orchestrator.identity_keys_for("ネイチャーメイド（大塚製薬）", "スーパーマルチビタミン", "サプリメント")
        self.assertFalse(a & other)
        self.assertFalse(a & orchestrator.identity_keys_for("別ブランド", "スーパーフィッシュオイル", "サプリメント"))


class ModelAnnotationTests(unittest.TestCase):

    def test_single_model_is_kept_inline(self):
        self.assertEqual(pipeline.split_discovery_product_name("メディリフト プラス（型番：EPM-18BB）"),
                         ("メディリフト プラス EPM-18BB", ["EPM-18BB"]))

    def test_uncertain_models_become_hints_only(self):
        self.assertEqual(pipeline.split_discovery_product_name("ReFa CARAT LIFT（リファカラットリフト）（型番：RR-AT-02A 等）"),
                         ("ReFa CARAT LIFT（リファカラットリフト）", ["RR-AT-02A"]))
        self.assertEqual(pipeline.split_discovery_product_name("RF美顔器 フォトプラス（型番 HRF-10T など）"),
                         ("RF美顔器 フォトプラス", ["HRF-10T"]))
        self.assertEqual(pipeline.split_discovery_product_name("X（型番：A-100、A-200）"), ("X", ["A-100", "A-200"]))

    def test_names_without_model_annotation_are_unchanged(self):
        for name in ("SHAPE POINTER（シェイプポインター）", "バイタリフト RF EH-SR85", "ビタミンC（ハードカプセル）"):
            self.assertEqual(pipeline.split_discovery_product_name(name), (name, []))

    def test_model_hint_still_used_for_device_duplicate_guard(self):
        guard = orchestrator.DeviceDuplicateGuard("美容機器", [{"brand": "ReFa", "name": "ReFa CARAT LIFT RR-AT-02A"}])
        clean, hints = pipeline.split_discovery_product_name("ReFa CARAT LIFT（型番：RR-AT-02A 等）")
        self.assertEqual(guard.reason("ReFa", " ".join([clean, *hints])), "duplicate_model")


class FlowTests(OrchestratorTestBase):

    def _pm(self, name, jan, category="サプリメント", brand=None):
        brand = brand or f"ブランド{TEST_NAME_SUFFIX}"
        app.upsert_product_master({"brand": brand, "name": name, "category": category, "active_ingredients": ["亜鉛"],
                                   "jan_code": jan}, data_source="ai_precollected")

    def _run(self, candidates, collect, max_consecutive_failures=1):
        budget = orchestrator.BatchBudget(self._new_batch_id("s491"), 5, 20, 0.50)
        with patch.object(orchestrator, "conservative_cost_estimate", return_value=0.01), \
             patch.object(pipeline, "collect_one_product", side_effect=collect), \
             patch.object(pipeline, "call_gemini_for_collection", side_effect=FORBIDDEN), \
             patch.object(pipeline.citation_verification, "fetch_html", side_effect=FORBIDDEN), \
             patch.object(pipeline, "resolve_item_code_for_product", side_effect=FORBIDDEN), \
             patch.object(pipeline, "reflect_staging_to_product_master", side_effect=FORBIDDEN):
            return orchestrator.process_coverage_gap_item(
                {"category": "サプリメント", "target": "zinc", "shortage_count": 1}, "execute", budget.batch_id, budget,
                lambda c, t, n: [dict(x, category=c) for x in candidates], max_consecutive_failures,
                orchestrator._product_master_identity_keys())

    def test_stage1_jan_duplicate_skips_stage2_and_is_not_a_failure(self):
        self._pm(f"亜鉛A{TEST_NAME_SUFFIX}", JAN_A)
        calls = []

        def collect(brand, name, category, batch_id, existing_product=None, existing_jans=None):
            calls.append(name)
            self.assertIn(JAN_A, existing_jans)
            if name.startswith("新亜鉛X"):
                return {"staging_id": None, "stage1_status": "ok", "stage2_status": "skipped",
                        "conflict_status": "skipped", "limit_exceeded": False,
                        "duplicate_jan": {"jan": JAN_A, "product_id": existing_jans[JAN_A]}}
            raise StopIteration("2件目まで進んだ")
        cands = [{"brand": f"別表記{TEST_NAME_SUFFIX}", "name": f"新亜鉛X{TEST_NAME_SUFFIX}"},
                 {"brand": f"別表記{TEST_NAME_SUFFIX}", "name": f"新亜鉛Y{TEST_NAME_SUFFIX}"}]
        with self.assertRaises(StopIteration):  # 連続失敗上限1でも2件目へ進む
            self._run(cands, collect)
        self.assertEqual(len(calls), 2)

    def test_collect_one_product_skips_page_check_and_stage2_on_jan_duplicate(self):
        stage1 = {"status": "ok", "raw_text": f"JANコード：{JAN_A}", "citations": [{"uri": CIT, "title": "x"}],
                  "search_queries": ["q"]}
        with patch.object(pipeline, "run_stage1_collection", return_value=stage1), \
             patch.object(pipeline, "attach_citation_page_verification", side_effect=FORBIDDEN), \
             patch.object(pipeline, "run_stage2_structuring", side_effect=FORBIDDEN), \
             patch.object(pipeline, "write_staging_record", return_value=7) as write:
            result = pipeline.collect_one_product("B", "N", "サプリメント", "b", existing_jans={JAN_A: 99})
        self.assertEqual(result["duplicate_jan"], {"jan": JAN_A, "product_id": 99})
        self.assertEqual(write.call_args[0][5]["status"], "skipped")

    def test_staging_reuse_with_existing_jan_is_skipped_with_marker(self):
        self._pm(f"亜鉛B{TEST_NAME_SUFFIX}", JAN_B)
        brand, name = f"別表記{TEST_NAME_SUFFIX}", f"亜鉛B再{TEST_NAME_SUFFIX}"
        self._insert_staging_row(self._new_batch_id("s491-st"), brand, name, "サプリメント",
                                 stage2_payload={"jan_code": JAN_B, "active_ingredients": [], "formulation_features": []},
                                 citations=[{"uri": CIT, "title": "x"}], stage1_raw_text="")
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT staging_id FROM product_collection_staging WHERE product_name = %s", (name,))
            sid = cur.fetchone()[0]
        finally:
            conn.close()
        actions = self._run([{"brand": brand, "name": name, "discovery_source": "staging_reuse", "staging_id": sid}],
                            collect=FORBIDDEN)
        self.assertEqual(actions[0]["action"], "skipped_duplicate_jan")
        self.assertEqual(actions[0]["gate_failure_marker"]["gate"], "duplicate_jan")

    def test_brand_prefixed_name_is_treated_as_existing(self):
        brand = f"ネイチャー{TEST_NAME_SUFFIX}"
        self._pm(f"ネイチャー{TEST_NAME_SUFFIX} フィッシュオイル", None, brand=brand)
        actions = self._run([{"brand": brand, "name": "フィッシュオイル"}], collect=FORBIDDEN)
        self.assertEqual(actions[0]["action"], "skipped_duplicate")


class TransientStagingOnceTests(OrchestratorTestBase):

    def test_transient_staging_is_offered_once_per_batch(self):
        brand, name = f"BRtr{TEST_NAME_SUFFIX}", f"ビタミンCサプリtr{TEST_NAME_SUFFIX}"
        transient = {"uri": CIT + "t", "title": "shop.example.com", "page_verification": {
            "fetch_status": "unverifiable", "status": "unverifiable", "reason": "timeout"}}
        payload = {"active_ingredients": [{"ingredient": "ビタミンC", "source_url": CIT + "t"}],
                   "formulation_features": [], "category_attributes": {"primary_ingredients": {
                       "value": "ビタミンC", "confidence": "high", "source_url": CIT + "t"}}}
        self._insert_staging_row(self._new_batch_id("s491-tr"), brand, name, "サプリメント",
                                 stage2_payload=payload, citations=[transient], stage1_raw_text="")
        budget = orchestrator.BatchBudget(self._new_batch_id("s491-trb"), 5, 20, 0.50)
        with patch.object(app, "load_products", return_value=[]), \
             patch.object(app, "load_verified_products_cache", return_value=[]):
            source = orchestrator.make_discovery_candidate_source(budget.batch_id, budget, "dry_run")
            first = [c["name"] for c in source("サプリメント", "vitamin_c", 3)]
            second = [c["name"] for c in source("サプリメント", "vitamin_c", 3)]
            reasons = [e["reason"] for e in source.last_report["excluded"] if e.get("name") == name]
        self.assertIn(name, first)
        self.assertNotIn(name, second)
        self.assertEqual(reasons, ["transient_already_offered_this_batch"])


if __name__ == "__main__":
    unittest.main()
