"""Product Master半自動オーケストレーター(product_master_pipeline.py)の
E2Eテスト(Step40)。

Gemini/Rakutenの実APIは一切呼ばない。call_gemini_for_collection()と
pipeline.verify_and_resolve_item_code()だけをmockし、それ以外(Stage1/2の
構造化・citation検証・identity/dedup・conflict判定・reflect)は実際の
ローカルテストDB(rumilog_test)に対して本物のロジックを通す。
"""

import json
import os
import time
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import psycopg2  # noqa: E402

import app  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402
import product_master_pipeline as orchestrator  # noqa: E402

TEST_BATCH_PREFIX = "orch-test-"
TEST_NAME_SUFFIX = "_OrchTest"


def _fake_response(text):
    response = MagicMock()
    response.text = text
    response.usage_metadata.prompt_token_count = 100
    response.usage_metadata.candidates_token_count = 100
    response.usage_metadata.tool_use_prompt_token_count = None
    response.usage_metadata.total_token_count = 200
    response.candidates = [MagicMock(grounding_metadata=None)]
    return response


def _fake_stage1_response(text="検索結果に基づく回答", citations=None, queries=None):
    citations = citations if citations is not None else [
        {"uri": "https://official.example.com/x", "title": "official.example.com"}
    ]
    queries = queries if queries is not None else ["query"]
    response = _fake_response(text)
    chunks = []
    for c in citations:
        web = MagicMock()
        web.uri = c["uri"]
        web.title = c.get("title", "")
        web.domain = c.get("domain", "")
        chunks.append(MagicMock(web=web))
    response.candidates = [MagicMock(grounding_metadata=MagicMock(
        web_search_queries=queries, grounding_chunks=chunks,
    ))]
    return response


def make_mock_call_gemini(candidate_configs, discovery_configs=None):
    """candidate_configs: {"brand name": {"stage1": {...}, "stage2_payload": {...}}}
    promptの文字列に"brand name"が含まれるかでどの候補の呼び出しかを判定し、
    config(response_schemaの有無)でstage1/stage2を判定する。

    discovery_configs(Step41): {(category, target): {"search": {...},
    "candidates": [...]}}。Candidate Discovery(③Gemini Grounding探索)の
    build_discovery_prompt/build_discovery_structuring_promptが両方
    含む"カテゴリ: {category}"を手がかりに判定する(brand/nameを含まない
    探索専用のプロンプト形なので、既存のcandidate_configs判定とは別に
    先に判定する)。
    """
    discovery_configs = discovery_configs or {}

    def _mock(model, contents, config=None, max_retries=2, timeout=60):
        is_stage2 = config is not None and getattr(config, "response_schema", None) is not None
        for (category, target), cfg in discovery_configs.items():
            if f"カテゴリ: {category}" in contents and target in contents:
                if is_stage2:
                    return _fake_response(json.dumps({"candidates": cfg.get("candidates", [])}))
                search_cfg = cfg.get("search", {})
                return _fake_stage1_response(
                    text=search_cfg.get("text", "検索結果に基づく回答"),
                    citations=search_cfg.get("citations"),
                    queries=search_cfg.get("queries"),
                )

        matched = None
        for key, cfg in candidate_configs.items():
            if key in contents:
                matched = cfg
                break
        if matched is None:
            return _fake_stage1_response("該当なし", citations=[], queries=[])
        if is_stage2:
            return _fake_response(json.dumps(matched.get("stage2_payload", {})))
        stage1_cfg = matched.get("stage1", {})
        return _fake_stage1_response(
            text=stage1_cfg.get("text", "検索結果に基づく回答"),
            citations=stage1_cfg.get("citations"),
            queries=stage1_cfg.get("queries"),
        )
    return _mock


class OrchestratorTestBase(unittest.TestCase):
    def setUp(self):
        pipeline.init_product_collection_tables()
        app.init_product_master_table()
        self._cleanup()

    def tearDown(self):
        self._cleanup()

    def _cleanup(self):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM product_field_sources WHERE staging_id IN "
                "(SELECT staging_id FROM product_collection_staging WHERE batch_id LIKE %s)",
                (f"{TEST_BATCH_PREFIX}%",),
            )
            cur.execute("DELETE FROM product_collection_staging WHERE batch_id LIKE %s", (f"{TEST_BATCH_PREFIX}%",))
            cur.execute("DELETE FROM product_collection_usage WHERE batch_id LIKE %s", (f"{TEST_BATCH_PREFIX}%",))
            cur.execute("DELETE FROM product_master WHERE name LIKE %s", (f"%{TEST_NAME_SUFFIX}%",))
            conn.commit()
        finally:
            conn.close()

    def _new_batch_id(self, suffix):
        return f"{TEST_BATCH_PREFIX}{suffix}-{int(time.time() * 1000)}"

    def _count_staging(self, batch_id):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM product_collection_staging WHERE batch_id = %s", (batch_id,))
            return cur.fetchone()[0]
        finally:
            conn.close()

    def _product_master_row(self, brand, name, category):
        identity_key = app._normalize_product_master_identity_key(brand, name, category)
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT product_id, active_ingredients, jan_code FROM product_master WHERE identity_key = %s",
                (identity_key,),
            )
            return cur.fetchone()
        finally:
            conn.close()

    def _insert_staging_row(self, batch_id, brand, name, category, stage2_payload,
                             citations=None, stage1_status="ok", stage2_status="ok",
                             conflict_status="none", reflected_at=None, created_at=None):
        """Candidate Discovery(Step41)の①staging再利用/②最近失敗した候補
        除外のテスト用に、product_collection_stagingへ直接行を挿入する
        (test_product_master.pyの_insert_stagingと同じ既存パターン)。"""
        identity_key = app._normalize_product_master_identity_key(brand, name, category)
        citations = citations if citations is not None else []
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO product_collection_staging
                    (batch_id, brand, product_name, category, identity_key,
                     stage1_status, stage1_citations, stage2_status, stage2_payload,
                     conflict_status, conflict_detail, reflected_at, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '[]', %s,
                        COALESCE(%s, CURRENT_TIMESTAMP))
                """,
                (batch_id, brand, name, category, identity_key, stage1_status,
                 json.dumps(citations), stage2_status, json.dumps(stage2_payload),
                 conflict_status, reflected_at, created_at),
            )
            conn.commit()
        finally:
            conn.close()


class DryRunNoSideEffectsTests(OrchestratorTestBase):
    """Step40-5: dry-runは既定で、Gemini/Rakuten実呼び出し・reflect・DB更新
    を一切行わないこと(副作用ゼロ)を確認する。"""

    def test_dry_run_default_mode(self):
        batch_id = self._new_batch_id("dryrun-default")
        with patch.object(app, "generate_product_master_work_queue", return_value=[
            {"type": "coverage_gap", "category": "美容液", "target": "vitamin_c", "shortage_count": 1,
             "priority": "high", "reason": "test"},
        ]):
            def candidate_source(category, target, limit):
                return [{"brand": f"ブランド{TEST_NAME_SUFFIX}", "name": f"商品{TEST_NAME_SUFFIX}", "category": category}]

            with patch.object(pipeline, "call_gemini_for_collection") as mock_gemini:
                result = orchestrator.run_batch(
                    mode="dry_run", candidate_source=candidate_source, batch_id=batch_id,
                )
        self.assertEqual(result["mode"], "dry_run")
        self.assertEqual(len(result["actions"]), 1)
        self.assertEqual(result["actions"][0]["action"], "would_collect")
        mock_gemini.assert_not_called()
        self.assertEqual(self._count_staging(batch_id), 0)
        self.assertIsNone(self._product_master_row(f"ブランド{TEST_NAME_SUFFIX}", f"商品{TEST_NAME_SUFFIX}", "美容液"))

    def test_dry_run_stale_item_no_side_effects(self):
        batch_id = self._new_batch_id("dryrun-stale")
        with patch.object(app, "generate_product_master_work_queue", return_value=[
            {"type": "stale_reverification", "category": "美容液",
             "brand": f"ブランド{TEST_NAME_SUFFIX}", "name": f"商品{TEST_NAME_SUFFIX}", "product_id": 1,
             "priority": "low", "reason": "test"},
        ]), patch.object(app, "get_stale_product_master_candidates", return_value=[
            {"_product_master_id": 1, "brand": f"ブランド{TEST_NAME_SUFFIX}", "name": f"商品{TEST_NAME_SUFFIX}",
             "category": "美容液"},
        ]):
            with patch.object(pipeline, "call_gemini_for_collection") as mock_gemini:
                result = orchestrator.run_batch(mode="dry_run", batch_id=batch_id)
        self.assertEqual(result["actions"][0]["action"], "would_reverify")
        mock_gemini.assert_not_called()
        self.assertEqual(self._count_staging(batch_id), 0)

    def test_empty_work_queue_produces_zero_actions(self):
        # 実際に production DB が13/13 sufficient(work_items=0)であることは
        # 手動のdry-run実行(本Stepの報告内で記載)で別途確認済み。ここでは
        # ローカルのテストDB状態に依存せず、「work_items=0ならactions=0」
        # という構造を直接検証する(モックしたwork queueで決定的に確認)。
        with patch.object(app, "generate_product_master_work_queue", return_value=[]):
            result = orchestrator.run_batch()
        self.assertEqual(result["mode"], "dry_run")
        self.assertEqual(result["work_items"], 0)
        self.assertEqual(result["actions"], [])


class CoverageGapHappyPathTests(OrchestratorTestBase):
    """Step40-1/6: coverage不足→候補→Stage1/2→validator→reflect→
    coverage改善のE2Eを確認する(cosmetics)。"""

    def test_coverage_gap_reflects_new_product_and_improves_coverage(self):
        brand, name, category = f"ブランドA{TEST_NAME_SUFFIX}", f"商品A{TEST_NAME_SUFFIX}", "美容液"
        batch_id = self._new_batch_id("happy")

        before = app.calculate_effective_candidates(category, "vitamin_c")

        mock_gemini = make_mock_call_gemini({
            f"{brand} {name}": {
                "stage1": {"text": "アスコルビン酸を配合しています。",
                           "citations": [{"uri": "https://official.example.com/x", "title": brand}]},
                "stage2_payload": {
                    "brand": brand, "product_name": name, "jan_code": "unknown",
                    "active_ingredients": [
                        {"ingredient": "アスコルビン酸", "concentration": "unknown",
                         "confidence": "high", "source_url": "https://official.example.com/x"},
                    ],
                    "formulation_features": [], "official_source_confirmed": True,
                },
            },
        })

        def candidate_source(cat, target, limit):
            return [{"brand": brand, "name": name, "category": cat}]

        with patch.object(app, "generate_product_master_work_queue", return_value=[
            {"type": "coverage_gap", "category": category, "target": "vitamin_c", "shortage_count": 1,
             "priority": "high", "reason": "test"},
        ]), patch.object(pipeline, "call_gemini_for_collection", side_effect=mock_gemini), \
             patch.object(pipeline, "verify_and_resolve_item_code", return_value={"status": "not_found"}):
            result = orchestrator.run_batch(mode="execute", candidate_source=candidate_source, batch_id=batch_id)

        reflected = [a for a in result["actions"] if a["action"] == "reflected"]
        self.assertEqual(len(reflected), 1)
        row = self._product_master_row(brand, name, category)
        self.assertIsNotNone(row)

        after = app.calculate_effective_candidates(category, "vitamin_c")
        self.assertEqual(after, before + 1)


class CitationInsufficientTests(OrchestratorTestBase):
    """Step40-4: citation不足(stage1はokだがcitationsが0件)の場合は
    reflectしないことを確認する。"""

    def test_zero_citations_is_not_reflected(self):
        brand, name, category = f"ブランドB{TEST_NAME_SUFFIX}", f"商品B{TEST_NAME_SUFFIX}", "美容液"
        batch_id = self._new_batch_id("nocitation")

        mock_gemini = make_mock_call_gemini({
            f"{brand} {name}": {
                # queriesはあるがsources(citations)が無い -> stage1_status="ok"かつcitations=0
                "stage1": {"text": "検索したが詳細不明", "citations": [], "queries": ["q"]},
                "stage2_payload": {"active_ingredients": [], "formulation_features": [], "official_source_confirmed": False},
            },
        })

        def candidate_source(cat, target, limit):
            return [{"brand": brand, "name": name, "category": cat}]

        with patch.object(app, "generate_product_master_work_queue", return_value=[
            {"type": "coverage_gap", "category": category, "target": "vitamin_c", "shortage_count": 1,
             "priority": "high", "reason": "test"},
        ]), patch.object(pipeline, "call_gemini_for_collection", side_effect=mock_gemini):
            result = orchestrator.run_batch(mode="execute", candidate_source=candidate_source, batch_id=batch_id)

        self.assertEqual(len(result["actions"]), 1)
        self.assertEqual(result["actions"][0]["action"], "not_reflected")
        self.assertEqual(result["actions"][0]["reason"], "no_citations")
        self.assertIsNone(self._product_master_row(brand, name, category))


class VariantUncertainTests(OrchestratorTestBase):
    """Step40-4: citationはあるがofficial_source_confirmed=False(variant
    不明相当)の場合はreflectしないことを確認する。"""

    def test_official_source_not_confirmed_is_not_reflected(self):
        brand, name, category = f"ブランドC{TEST_NAME_SUFFIX}", f"商品C{TEST_NAME_SUFFIX}", "美容液"
        batch_id = self._new_batch_id("variant")

        mock_gemini = make_mock_call_gemini({
            f"{brand} {name}": {
                "stage1": {"text": "第三者サイトでの言及のみ"},
                "stage2_payload": {
                    "active_ingredients": [
                        {"ingredient": "アスコルビン酸", "concentration": "unknown",
                         "confidence": "low", "source_url": "https://official.example.com/x"},
                    ],
                    "formulation_features": [], "official_source_confirmed": False,
                },
            },
        })

        def candidate_source(cat, target, limit):
            return [{"brand": brand, "name": name, "category": cat}]

        with patch.object(app, "generate_product_master_work_queue", return_value=[
            {"type": "coverage_gap", "category": category, "target": "vitamin_c", "shortage_count": 1,
             "priority": "high", "reason": "test"},
        ]), patch.object(pipeline, "call_gemini_for_collection", side_effect=mock_gemini):
            result = orchestrator.run_batch(mode="execute", candidate_source=candidate_source, batch_id=batch_id)

        self.assertEqual(result["actions"][0]["action"], "not_reflected")
        self.assertEqual(result["actions"][0]["reason"], "official_source_not_confirmed_variant_uncertain")
        self.assertIsNone(self._product_master_row(brand, name, category))


class IdentityDuplicateTests(OrchestratorTestBase):
    """Step40-4: identity重複が既にproduct_masterに存在する候補はskipされ、
    Geminiが呼ばれないことを確認する。"""

    def test_duplicate_candidate_is_skipped_without_gemini_call(self):
        brand, name, category = f"ブランドD{TEST_NAME_SUFFIX}", f"商品D{TEST_NAME_SUFFIX}", "美容液"
        app.upsert_product_master({
            "brand": brand, "name": name, "category": category,
            "active_ingredients": ["vitamin_c"], "verified_at": time.time(),
        }, data_source="ai_precollected")

        batch_id = self._new_batch_id("dup")

        def candidate_source(cat, target, limit):
            return [{"brand": brand, "name": name, "category": cat}]

        with patch.object(app, "generate_product_master_work_queue", return_value=[
            {"type": "coverage_gap", "category": category, "target": "vitamin_c", "shortage_count": 1,
             "priority": "high", "reason": "test"},
        ]), patch.object(pipeline, "call_gemini_for_collection") as mock_gemini:
            result = orchestrator.run_batch(mode="execute", candidate_source=candidate_source, batch_id=batch_id)

        self.assertEqual(result["actions"][0]["action"], "skipped_duplicate")
        mock_gemini.assert_not_called()
        self.assertEqual(self._count_staging(batch_id), 0)


class CategoryValidatorFailureTests(OrchestratorTestBase):
    """Step40-3/6: beauty_deviceの候補でcategory_attributesの必須フィールド
    が欠落している場合、reflectしない(cosmetics専用にしていないことの確認)。"""

    def test_beauty_device_missing_required_attributes_is_not_reflected(self):
        # Step43: 現在の推薦が実際に使うのはmethodのみ(modes/usage_frequency
        # は根拠も使途も無いため廃止済み)。methodが根拠不十分(citationに無い
        # URLでsanitizeにより落とされる)場合、必須フィールド欠落として
        # reflectされないことを確認する。
        brand, name, category = f"デバイスブランド{TEST_NAME_SUFFIX}", f"デバイス商品{TEST_NAME_SUFFIX}", "美容機器"
        batch_id = self._new_batch_id("device-fail")

        mock_gemini = make_mock_call_gemini({
            f"{brand} {name}": {
                "stage1": {"text": "RF方式の美容機器",
                           "citations": [{"uri": "https://official.example.com/x", "title": brand}]},
                "stage2_payload": {
                    "active_ingredients": [], "formulation_features": [],
                    "official_source_confirmed": True,
                    "category_attributes": {
                        "method": {
                            "value": "RF", "confidence": "high",
                            "source_url": "https://fake-generated-url.example.com/made-up",  # citationに無いURL
                        },
                        "contraindications": {
                            "value": "unknown", "confidence": "unknown", "source_url": "unknown",
                        },
                    },
                },
            },
        })

        def candidate_source(cat, target, limit):
            return [{"brand": brand, "name": name, "category": cat}]

        item = {"category": category, "target": "method", "shortage_count": 1}
        budget = orchestrator.BatchBudget(batch_id, 5, 20, 0.50)
        with patch.object(pipeline, "call_gemini_for_collection", side_effect=mock_gemini):
            actions = orchestrator.process_coverage_gap_item(
                item, "execute", batch_id, budget, candidate_source, 3, set(),
            )

        self.assertEqual(actions[0]["action"], "not_reflected")
        self.assertFalse(actions[0]["category_validator"]["valid"])
        self.assertIn("method", actions[0]["category_validator"]["missing_fields"])
        self.assertIsNone(self._product_master_row(brand, name, category))


class CategoryDelegationSupplementTests(OrchestratorTestBase):
    """Step40-3: supplementの候補がcategory_attributesの必須フィールドを
    すべて満たす場合はcosmetics同様に正しくreflectされること(同じ
    processorがカテゴリ非依存に動くことの確認)。"""

    def test_supplement_with_complete_attributes_is_reflected(self):
        # Step43: サプリメントに必須category_attributesフィールドは無い
        # (成分名・濃度はactive_ingredientsがsource of truth)。dosage/
        # serving_size/precautionsは補助情報として正しくproduct_masterへ
        # 反映されることを確認する(citation根拠ありのwrapped形式)。
        brand, name, category = f"サプリブランド{TEST_NAME_SUFFIX}", f"サプリ商品{TEST_NAME_SUFFIX}", "サプリメント"
        batch_id = self._new_batch_id("supplement-ok")
        source_url = "https://official.example.com/x"

        mock_gemini = make_mock_call_gemini({
            f"{brand} {name}": {
                "stage1": {"text": "ビタミンC含有のサプリメント。1日2粒を目安に摂取。",
                           "citations": [{"uri": source_url, "title": brand}]},
                "stage2_payload": {
                    "active_ingredients": [
                        {"ingredient": "アスコルビン酸", "concentration": "unknown",
                         "confidence": "high", "source_url": source_url},
                    ],
                    "formulation_features": [], "official_source_confirmed": True,
                    "category_attributes": {
                        "dosage": {"value": "1日500mg", "confidence": "high", "source_url": source_url},
                        "serving_size": {"value": "1日2粒", "confidence": "high", "source_url": source_url},
                        "precautions": {"value": "unknown", "confidence": "unknown", "source_url": "unknown"},
                    },
                },
            },
        })

        def candidate_source(cat, target, limit):
            return [{"brand": brand, "name": name, "category": cat}]

        item = {"category": category, "target": "vitamin_c", "shortage_count": 1}
        budget = orchestrator.BatchBudget(batch_id, 5, 20, 0.50)
        with patch.object(pipeline, "call_gemini_for_collection", side_effect=mock_gemini), \
             patch.object(pipeline, "verify_and_resolve_item_code", return_value={"status": "not_found"}):
            actions = orchestrator.process_coverage_gap_item(
                item, "execute", batch_id, budget, candidate_source, 3, set(),
            )

        self.assertEqual(actions[0]["action"], "reflected")
        row = self._product_master_row(brand, name, category)
        self.assertIsNotNone(row)
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT category_attributes FROM product_master WHERE product_id = %s", (row[0],))
            category_attributes = cur.fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(category_attributes.get("dosage"), "1日500mg")
        self.assertEqual(category_attributes.get("serving_size"), "1日2粒")
        self.assertNotIn("precautions", category_attributes)

    def test_unset_policy_category_never_enters_queue(self):
        # COVERAGE_POLICIESに未登録のpolicyは、実際のget_coverage_report()
        # 経由ではcoverage_gap itemが一切生成されない(架空の基準で収集しない)。
        self.assertEqual(app.get_coverage_report("not_a_real_policy"), [])


class ConsecutiveFailureStopTests(OrchestratorTestBase):
    """Step40-4: 同一coverage領域で候補が3回連続失敗したら、その領域だけ
    STOPし、4件目以降は試行しないことを確認する。"""

    def test_three_consecutive_failures_stops_area_before_fourth_candidate(self):
        category = "美容液"
        batch_id = self._new_batch_id("threefail")
        candidates = [
            {"brand": f"失敗A{TEST_NAME_SUFFIX}", "name": f"商品A{TEST_NAME_SUFFIX}", "category": category},
            {"brand": f"失敗B{TEST_NAME_SUFFIX}", "name": f"商品B{TEST_NAME_SUFFIX}", "category": category},
            {"brand": f"失敗C{TEST_NAME_SUFFIX}", "name": f"商品C{TEST_NAME_SUFFIX}", "category": category},
            {"brand": f"成功D{TEST_NAME_SUFFIX}", "name": f"商品D{TEST_NAME_SUFFIX}", "category": category},
        ]

        def candidate_source(cat, target, limit):
            return candidates

        # 全候補について、Stage1自体が検索証拠なし(no_search_evidence)で
        # 失敗する(=matched configを持たない名前にして既定のno-evidence応答)。
        with patch.object(pipeline, "call_gemini_for_collection",
                           side_effect=make_mock_call_gemini({})):
            item = {"category": category, "target": "vitamin_c", "shortage_count": 4}
            budget = orchestrator.BatchBudget(batch_id, 10, 20, 0.50)
            actions = orchestrator.process_coverage_gap_item(
                item, "execute", batch_id, budget, candidate_source, 3, set(),
            )

        action_types = [a["action"] for a in actions]
        self.assertEqual(action_types.count("not_reflected"), 3)
        self.assertIn("area_stop", action_types)
        # 4件目(成功D)は試行されていない
        for a in actions:
            self.assertNotIn("成功D", a.get("brand", ""))


class BatchBudgetStopTests(OrchestratorTestBase):
    """Step40-4: バッチ全体の商品数上限に達したら、残りを実行せずSTOPする
    ことを確認する。"""

    def test_max_products_per_batch_stops_remaining_candidates(self):
        category = "美容液"
        batch_id = self._new_batch_id("budget")
        brand1, name1 = f"予算A{TEST_NAME_SUFFIX}", f"商品A{TEST_NAME_SUFFIX}"
        brand2, name2 = f"予算B{TEST_NAME_SUFFIX}", f"商品B{TEST_NAME_SUFFIX}"

        mock_gemini = make_mock_call_gemini({
            f"{brand1} {name1}": {
                "stage1": {"text": "アスコルビン酸配合",
                           "citations": [{"uri": "https://official.example.com/x", "title": brand1}]},
                "stage2_payload": {
                    "active_ingredients": [
                        {"ingredient": "アスコルビン酸", "concentration": "unknown",
                         "confidence": "high", "source_url": "https://official.example.com/x"},
                    ],
                    "formulation_features": [], "official_source_confirmed": True,
                },
            },
            f"{brand2} {name2}": {
                "stage1": {"text": "アスコルビン酸配合",
                           "citations": [{"uri": "https://official.example.com/x", "title": brand2}]},
                "stage2_payload": {
                    "active_ingredients": [
                        {"ingredient": "アスコルビン酸", "concentration": "unknown",
                         "confidence": "high", "source_url": "https://official.example.com/x"},
                    ],
                    "formulation_features": [], "official_source_confirmed": True,
                },
            },
        })

        def candidate_source(cat, target, limit):
            return [
                {"brand": brand1, "name": name1, "category": cat},
                {"brand": brand2, "name": name2, "category": cat},
            ]

        with patch.object(pipeline, "call_gemini_for_collection", side_effect=mock_gemini), \
             patch.object(pipeline, "verify_and_resolve_item_code", return_value={"status": "not_found"}):
            item = {"category": category, "target": "vitamin_c", "shortage_count": 2}
            budget = orchestrator.BatchBudget(batch_id, 1, 20, 0.50)  # 商品数上限=1
            actions = orchestrator.process_coverage_gap_item(
                item, "execute", batch_id, budget, candidate_source, 3, set(),
            )

        action_types = [a["action"] for a in actions]
        self.assertEqual(action_types.count("reflected"), 1)
        self.assertIn("batch_stop", action_types)
        self.assertIsNone(self._product_master_row(brand2, name2, category))


class NeedsReviewReportOnlyTests(OrchestratorTestBase):
    """Step40-2: needs_review work itemは自動解決・自動reflectせず、
    報告対象として残るだけであることを確認する。"""

    def test_needs_review_item_is_only_reported(self):
        item = {
            "type": "needs_review", "staging_id": 1, "batch_id": "b",
            "brand": "テストブランド", "name": "テスト商品", "category": "美容液",
            "identity_key": "key1", "conflict_detail": [],
        }
        actions = orchestrator.process_needs_review_item(item)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "reported_only")


class StaleReverificationTests(OrchestratorTestBase):
    """Step40-2: stale_reverificationが既存商品の再調査を行い、確認済み
    タグを根拠なく削除しないことを確認する(実DB)。"""

    def test_stale_item_reverifies_and_updates_verified_at(self):
        brand, name, category = f"ステイルA{TEST_NAME_SUFFIX}", f"商品A{TEST_NAME_SUFFIX}", "美容液"
        old_ts = time.time() - (200 * 24 * 60 * 60)
        app.upsert_product_master({
            "brand": brand, "name": name, "category": category,
            "active_ingredients": ["vitamin_c"], "verified_at": old_ts,
        }, data_source="ai_precollected")
        row = self._product_master_row(brand, name, category)
        product_id = row[0]

        batch_id = self._new_batch_id("stale-ok")
        mock_gemini = make_mock_call_gemini({
            f"{brand} {name}": {
                "stage1": {"text": "アスコルビン酸配合を再確認",
                           "citations": [{"uri": "https://official.example.com/x", "title": brand}]},
                "stage2_payload": {
                    "active_ingredients": [
                        {"ingredient": "アスコルビン酸", "concentration": "unknown",
                         "confidence": "high", "source_url": "https://official.example.com/x"},
                    ],
                    "formulation_features": [], "official_source_confirmed": True,
                },
            },
        })

        item = {
            "type": "stale_reverification", "category": category, "product_id": product_id,
            "brand": brand, "name": name, "priority": "low", "reason": "test",
        }
        with patch.object(app, "generate_product_master_work_queue", return_value=[item]), \
             patch.object(pipeline, "call_gemini_for_collection", side_effect=mock_gemini), \
             patch.object(pipeline, "verify_and_resolve_item_code", return_value={"status": "not_found"}):
            result = orchestrator.run_batch(mode="execute", batch_id=batch_id)

        self.assertEqual(result["actions"][0]["action"], "reflected")

    def test_stale_item_does_not_drop_confirmed_tags_when_new_extraction_is_narrower(self):
        brand, name, category = f"ステイルB{TEST_NAME_SUFFIX}", f"商品B{TEST_NAME_SUFFIX}", "美容液"
        old_ts = time.time() - (200 * 24 * 60 * 60)
        # 既存はvitamin_c+peptideの2タグを確認済み
        app.upsert_product_master({
            "brand": brand, "name": name, "category": category,
            "active_ingredients": ["アスコルビン酸", "ペプチド"], "verified_at": old_ts,
        }, data_source="ai_precollected")
        row = self._product_master_row(brand, name, category)
        product_id, before_actives, _ = row

        batch_id = self._new_batch_id("stale-narrow")
        # 新しい抽出結果はvitamin_cのみ(peptideへの言及が無い=タグが減る)
        mock_gemini = make_mock_call_gemini({
            f"{brand} {name}": {
                "stage1": {"text": "アスコルビン酸配合を再確認(ペプチドへの言及なし)",
                           "citations": [{"uri": "https://official.example.com/x", "title": brand}]},
                "stage2_payload": {
                    "active_ingredients": [
                        {"ingredient": "アスコルビン酸", "concentration": "unknown",
                         "confidence": "high", "source_url": "https://official.example.com/x"},
                    ],
                    "formulation_features": [], "official_source_confirmed": True,
                },
            },
        })

        item = {
            "type": "stale_reverification", "category": category, "product_id": product_id,
            "brand": brand, "name": name, "priority": "low", "reason": "test",
        }
        with patch.object(app, "generate_product_master_work_queue", return_value=[item]), \
             patch.object(pipeline, "call_gemini_for_collection", side_effect=mock_gemini):
            result = orchestrator.run_batch(mode="execute", batch_id=batch_id)

        self.assertEqual(result["actions"][0]["action"], "not_reflected")
        self.assertEqual(result["actions"][0]["reason"], "would_lose_confirmed_ingredient_tags")
        self.assertIn("peptide", result["actions"][0]["lost_tags"])

        after_row = self._product_master_row(brand, name, category)
        self.assertEqual(after_row[1], before_actives)  # 既存のactive_ingredientsは不変

    def test_needs_review_conflict_during_stale_reverification(self):
        brand, name, category = f"ステイルC{TEST_NAME_SUFFIX}", f"商品C{TEST_NAME_SUFFIX}", "美容液"
        old_ts = time.time() - (200 * 24 * 60 * 60)
        app.upsert_product_master({
            "brand": brand, "name": name, "category": category,
            "active_ingredients": ["アスコルビン酸"], "formulation": ["stabilized"],
            "verified_at": old_ts,
        }, data_source="ai_precollected")
        row = self._product_master_row(brand, name, category)
        product_id = row[0]

        batch_id = self._new_batch_id("stale-conflict")
        # 新しい抽出結果が既存と矛盾するformulation_feature(統制語彙同士で
        # 重ならない)を報告 -> detect_conflicts()がneeds_reviewを返す
        # (既存ロジックそのまま、新しい照合ロジックは作っていない)。
        mock_gemini = make_mock_call_gemini({
            f"{brand} {name}": {
                "stage1": {"text": "カプセル化技術を採用したアスコルビン酸配合",
                           "citations": [{"uri": "https://official.example.com/x", "title": brand}]},
                "stage2_payload": {
                    "active_ingredients": [
                        {"ingredient": "アスコルビン酸", "concentration": "unknown",
                         "confidence": "high", "source_url": "https://official.example.com/x"},
                    ],
                    "formulation_features": [
                        {"feature": "encapsulated", "other_detail": "unknown",
                         "confidence": "high", "source_url": "https://official.example.com/x"},
                    ],
                    "official_source_confirmed": True,
                },
            },
        })

        item = {
            "type": "stale_reverification", "category": category, "product_id": product_id,
            "brand": brand, "name": name, "priority": "low", "reason": "test",
        }
        with patch.object(app, "generate_product_master_work_queue", return_value=[item]), \
             patch.object(pipeline, "call_gemini_for_collection", side_effect=mock_gemini):
            result = orchestrator.run_batch(mode="execute", batch_id=batch_id)

        self.assertEqual(result["actions"][0]["action"], "needs_review")


class CandidateDiscoveryStagingReuseTests(OrchestratorTestBase):
    """Step41 探索順序①: 既存staging/sourceの未反映候補を再利用し、
    Discovery API(Gemini)を呼ばないことを確認する。"""

    def test_staging_candidate_reused_without_discovery_api_call(self):
        category, target = "美容液", "vitamin_c"
        batch_id = self._new_batch_id("stagingreuse")
        brand, name = f"ステ探索{TEST_NAME_SUFFIX}", f"商品ステ探索{TEST_NAME_SUFFIX}"
        self._insert_staging_row(
            batch_id, brand, name, category,
            stage2_payload={
                "active_ingredients": [{"ingredient": "アスコルビン酸", "concentration": "unknown"}],
                "official_source_confirmed": True,
            },
            citations=[{"uri": "https://official.example.com/staged", "title": brand}],
        )

        with patch.object(pipeline, "call_gemini_for_collection",
                           side_effect=AssertionError("staging再利用時はDiscovery APIを呼ばないはず")):
            budget = orchestrator.BatchBudget(batch_id, 10, 20, 0.50)
            candidate_source = orchestrator.make_discovery_candidate_source(batch_id, budget, "execute")
            result = candidate_source(category, target, 3)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["brand"], brand)
        self.assertEqual(result[0]["name"], name)
        self.assertEqual(result[0]["discovery_source"], "staging_reuse")
        self.assertTrue(result[0]["discovery_evidence"])

    def test_staging_candidate_with_unrelated_target_is_not_reused(self):
        category = "美容液"
        batch_id = self._new_batch_id("stagingreuse-unrelated")
        brand, name = f"ステ無関係{TEST_NAME_SUFFIX}", f"商品ステ無関係{TEST_NAME_SUFFIX}"
        self._insert_staging_row(
            batch_id, brand, name, category,
            stage2_payload={
                "active_ingredients": [{"ingredient": "セラミド", "concentration": "unknown"}],
                "official_source_confirmed": True,
            },
            citations=[{"uri": "https://official.example.com/staged2", "title": brand}],
        )

        budget = orchestrator.BatchBudget(batch_id, 10, 20, 0.50)
        result = orchestrator._staging_reuse_candidates(category, "vitamin_c", set(), 3)
        names = [c["name"] for c in result]
        self.assertNotIn(name, names)


class CandidateDiscoveryDbReuseTests(OrchestratorTestBase):
    """Step41 探索順序②: 既存DB(load_products/verified_products_cache)内の
    まだproduct_masterへ反映されていない利用可能候補を再利用する。"""

    def test_db_product_relevant_to_target_is_reused(self):
        category, target = "美容液", "vitamin_c"
        brand, name = f"DB探索{TEST_NAME_SUFFIX}", f"商品DB探索{TEST_NAME_SUFFIX}"
        fake_db_product = {
            "brand": brand, "name": name, "category": category,
            # load_products()/verified_products_cacheは既に統制タグ形式
            # (normalize_ingredient_tag()の出力そのもの)でactive_ingredients
            # を保持する(products.jsonの実データと同じ形式)。
            "active_ingredients": ["vitamin_c"], "price_ref": 2000,
        }
        with patch.object(app, "load_products", return_value=[fake_db_product]), \
             patch.object(app, "load_verified_products_cache", return_value=[]):
            result = orchestrator._db_reuse_candidates(category, target, set(), 3)

        names = [c["name"] for c in result]
        self.assertIn(name, names)
        match = next(c for c in result if c["name"] == name)
        self.assertEqual(match["discovery_source"], "db_reuse")
        self.assertTrue(match["discovery_evidence"])

    def test_db_product_already_in_excluded_keys_is_not_reused(self):
        category, target = "美容液", "vitamin_c"
        brand, name = f"DB重複{TEST_NAME_SUFFIX}", f"商品DB重複{TEST_NAME_SUFFIX}"
        fake_db_product = {
            "brand": brand, "name": name, "category": category,
            "active_ingredients": ["vitamin_c"], "price_ref": 2000,
        }
        key = app.make_verified_product_key(fake_db_product)
        with patch.object(app, "load_products", return_value=[fake_db_product]), \
             patch.object(app, "load_verified_products_cache", return_value=[]):
            result = orchestrator._db_reuse_candidates(category, target, {key}, 3)

        self.assertEqual(result, [])


class CandidateDiscoveryGeminiTierTests(OrchestratorTestBase):
    """Step41 探索順序③: staging/DBで埋まらない場合のみGemini Groundingで
    探索し、見つかった候補はcitation検証済みのdiscovery evidenceを持つ。"""

    def test_gemini_discovery_returns_evidence_backed_candidates(self):
        category, target = "美容液", "vitamin_c"
        batch_id = self._new_batch_id("geminidisc")
        brand, name = f"発見{TEST_NAME_SUFFIX}", f"商品発見{TEST_NAME_SUFFIX}"
        mock_gemini = make_mock_call_gemini({}, discovery_configs={
            (category, target): {
                "search": {"text": f"{brand}の{name}はビタミンC配合",
                            "citations": [{"uri": "https://official.example.com/found", "title": "found"}]},
                "candidates": [
                    {"brand": brand, "product_name": name, "target_evidence": "ビタミンC配合を確認",
                     "source_url": "https://official.example.com/found"},
                ],
            },
        })
        with patch.object(pipeline, "call_gemini_for_collection", side_effect=mock_gemini):
            result = orchestrator._gemini_discovery_candidates(category, target, batch_id, set(), 3)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["brand"], brand)
        self.assertEqual(result[0]["name"], name)
        self.assertEqual(result[0]["discovery_source"], "gemini_grounding")
        self.assertEqual(result[0]["discovery_evidence"], ["https://official.example.com/found"])

    def test_gemini_discovery_candidate_missing_source_url_is_dropped(self):
        category, target = "美容液", "vitamin_c"
        batch_id = self._new_batch_id("geminidisc-nourl")
        with patch.object(pipeline, "discover_candidates_via_gemini", return_value=[
            {"brand": "欠落", "product_name": "欠落商品", "target_evidence": "unknown", "source_url": ""},
        ]):
            result = orchestrator._gemini_discovery_candidates(category, target, batch_id, set(), 3)
        self.assertEqual(result, [])

    def test_gemini_discovery_candidate_already_excluded_is_dropped(self):
        category, target = "美容液", "vitamin_c"
        batch_id = self._new_batch_id("geminidisc-dup")
        brand, name = f"既存{TEST_NAME_SUFFIX}", f"商品既存{TEST_NAME_SUFFIX}"
        key = app.make_verified_product_key({"brand": brand, "name": name, "category": category})
        with patch.object(pipeline, "discover_candidates_via_gemini", return_value=[
            {"brand": brand, "product_name": name, "target_evidence": "x",
             "source_url": "https://official.example.com/x"},
        ]):
            result = orchestrator._gemini_discovery_candidates(category, target, batch_id, {key}, 3)
        self.assertEqual(result, [])


class CandidateDiscoveryExclusionTests(OrchestratorTestBase):
    """Step41: identity重複商品・最近失敗した同一候補をStage1実行前に除外
    することを確認する。"""

    def test_identity_duplicate_excluded_from_staging_reuse(self):
        category, target = "美容液", "vitamin_c"
        batch_id = self._new_batch_id("dupexclude")
        brand, name = f"重複{TEST_NAME_SUFFIX}", f"商品重複{TEST_NAME_SUFFIX}"
        self._insert_staging_row(
            batch_id, brand, name, category,
            stage2_payload={"active_ingredients": [{"ingredient": "アスコルビン酸"}],
                             "official_source_confirmed": True},
            citations=[{"uri": "https://official.example.com/dup", "title": brand}],
        )
        key = app.make_verified_product_key({"brand": brand, "name": name, "category": category})
        result = orchestrator._staging_reuse_candidates(category, target, {key}, 3)
        self.assertEqual(result, [])

    def test_recently_failed_identity_is_detected(self):
        batch_id = self._new_batch_id("recentfail")
        brand, name, category = f"失敗{TEST_NAME_SUFFIX}", f"商品失敗{TEST_NAME_SUFFIX}", "美容液"
        self._insert_staging_row(
            batch_id, brand, name, category,
            stage2_payload={}, stage2_status="failed", conflict_status="none",
        )
        identity_key = app._normalize_product_master_identity_key(brand, name, category)
        failed_keys = orchestrator._recently_failed_identity_keys(hours=24)
        self.assertIn(identity_key, failed_keys)

    def test_old_failed_identity_outside_lookback_is_not_detected(self):
        batch_id = self._new_batch_id("oldfail")
        brand, name, category = f"古い失敗{TEST_NAME_SUFFIX}", f"商品古い失敗{TEST_NAME_SUFFIX}", "美容液"
        old_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 48 * 60 * 60))
        self._insert_staging_row(
            batch_id, brand, name, category,
            stage2_payload={}, stage2_status="failed", conflict_status="none",
            created_at=old_ts,
        )
        identity_key = app._normalize_product_master_identity_key(brand, name, category)
        failed_keys = orchestrator._recently_failed_identity_keys(hours=24)
        self.assertNotIn(identity_key, failed_keys)

    def test_recently_failed_identity_excluded_from_gemini_tier(self):
        category, target = "美容液", "vitamin_c"
        batch_id = self._new_batch_id("recentfail-gemini")
        brand, name = f"失敗再探索{TEST_NAME_SUFFIX}", f"商品失敗再探索{TEST_NAME_SUFFIX}"
        self._insert_staging_row(
            batch_id, brand, name, category,
            stage2_payload={}, stage2_status="failed", conflict_status="none",
        )

        budget = orchestrator.BatchBudget(batch_id, 10, 20, 0.50)
        with patch.object(orchestrator, "_staging_reuse_candidates", return_value=[]), \
             patch.object(orchestrator, "_db_reuse_candidates", return_value=[]), \
             patch.object(orchestrator, "_existing_identity_keys", return_value=set()), \
             patch.object(pipeline, "discover_candidates_via_gemini", return_value=[
                 {"brand": brand, "product_name": name, "target_evidence": "x",
                  "source_url": "https://official.example.com/x"},
             ]):
            candidate_source = orchestrator.make_discovery_candidate_source(batch_id, budget, "execute")
            result = candidate_source(category, target, 3)

        self.assertEqual(result, [])


class CandidateDiscoveryCapAndEvidenceTests(OrchestratorTestBase):
    """Step41 暴走防止: shortage_countに関わらず1領域最大
    MAX_DISCOVERY_CANDIDATES_PER_AREA件、discovery evidenceの無い候補は
    破棄することを確認する。"""

    def test_candidates_capped_at_max_per_area_even_if_more_available(self):
        category, target = "美容液", "vitamin_c"
        batch_id = self._new_batch_id("cap")
        fake_candidates = [
            {"brand": f"候補{i}{TEST_NAME_SUFFIX}", "name": f"商品{i}{TEST_NAME_SUFFIX}", "category": category,
             "discovery_source": "staging_reuse", "discovery_evidence": ["https://x"], "target_evidence": "x"}
            for i in range(5)
        ]
        budget = orchestrator.BatchBudget(batch_id, 10, 20, 0.50)
        with patch.object(orchestrator, "_staging_reuse_candidates", return_value=fake_candidates), \
             patch.object(orchestrator, "_db_reuse_candidates", side_effect=AssertionError("tier1だけで埋まるはず")), \
             patch.object(pipeline, "discover_candidates_via_gemini",
                           side_effect=AssertionError("tier1だけで埋まるはず")), \
             patch.object(orchestrator, "_existing_identity_keys", return_value=set()):
            candidate_source = orchestrator.make_discovery_candidate_source(batch_id, budget, "execute")
            result = candidate_source(category, target, 10)

        self.assertEqual(len(result), orchestrator.MAX_DISCOVERY_CANDIDATES_PER_AREA)

    def test_candidate_without_discovery_evidence_is_dropped(self):
        category, target = "美容液", "vitamin_c"
        batch_id = self._new_batch_id("noevidence")
        no_evidence_candidate = {
            "brand": f"根拠無{TEST_NAME_SUFFIX}", "name": f"商品根拠無{TEST_NAME_SUFFIX}", "category": category,
            "discovery_source": "staging_reuse", "discovery_evidence": [],
        }
        budget = orchestrator.BatchBudget(batch_id, 10, 20, 0.50)
        with patch.object(orchestrator, "_staging_reuse_candidates", return_value=[no_evidence_candidate]), \
             patch.object(orchestrator, "_db_reuse_candidates", return_value=[]), \
             patch.object(orchestrator, "_existing_identity_keys", return_value=set()):
            candidate_source = orchestrator.make_discovery_candidate_source(batch_id, budget, "execute")
            result = candidate_source(category, target, 3)

        self.assertEqual(result, [])


class CandidateDiscoveryPolicyGuardTests(OrchestratorTestBase):
    """Step41: coverage policyが未定義のカテゴリでは勝手な基準で探索を
    開始しないことを確認する(美容機器/サプリメントはStep44で登録済みの
    ため、未登録カテゴリで確認する)。"""

    def _assert_never_explores(self, category):
        batch_id = self._new_batch_id("nopolicy")
        budget = orchestrator.BatchBudget(batch_id, 10, 20, 0.50)
        with patch.object(orchestrator, "_staging_reuse_candidates",
                           side_effect=AssertionError("policy未定義カテゴリでは探索しないはず")), \
             patch.object(orchestrator, "_db_reuse_candidates",
                           side_effect=AssertionError("policy未定義カテゴリでは探索しないはず")), \
             patch.object(pipeline, "call_gemini_for_collection",
                           side_effect=AssertionError("policy未定義カテゴリでは探索しないはず")):
            candidate_source = orchestrator.make_discovery_candidate_source(batch_id, budget, "execute")
            result = candidate_source(category, "anything", 3)
        self.assertEqual(result, [])

    def test_unknown_category_without_policy_never_explores(self):
        self.assertFalse(orchestrator._category_has_registered_policy("ヘアオイル"))
        self._assert_never_explores("ヘアオイル")

    def test_unknown_cosmetics_like_category_without_policy_never_explores(self):
        # cosmetics policyに無いカテゴリ(乳液)も同様に探索しない。
        self._assert_never_explores("乳液")

    def test_category_policy_registration_lookup(self):
        self.assertTrue(orchestrator._category_has_registered_policy("美容液"))
        self.assertTrue(orchestrator._category_has_registered_policy("美容機器"))
        self.assertTrue(orchestrator._category_has_registered_policy("サプリメント"))
        self.assertFalse(orchestrator._category_has_registered_policy("ヘアオイル"))


class CandidateDiscoveryBudgetAndModeGateTests(OrchestratorTestBase):
    """Step41 暴走防止/安全ゲート: API・費用上限に達したらGemini探索を
    スキップし、dry-runでは探索自体に副作用(Gemini呼び出し)が無いことを
    確認する。"""

    def test_gemini_tier_skipped_when_budget_exhausted(self):
        category, target = "美容液", "vitamin_c"
        batch_id = self._new_batch_id("budgetexhausted")
        budget = orchestrator.BatchBudget(batch_id, 0, 20, 0.50)  # max_products=0 -> can_continue()は常にFalse
        with patch.object(orchestrator, "_staging_reuse_candidates", return_value=[]), \
             patch.object(orchestrator, "_db_reuse_candidates", return_value=[]), \
             patch.object(orchestrator, "_existing_identity_keys", return_value=set()), \
             patch.object(pipeline, "discover_candidates_via_gemini",
                           side_effect=AssertionError("budget超過時はGemini探索しないはず")):
            candidate_source = orchestrator.make_discovery_candidate_source(batch_id, budget, "execute")
            result = candidate_source(category, target, 3)
        self.assertEqual(result, [])
        self.assertFalse(budget.can_continue())

    def test_dry_run_mode_never_invokes_gemini_tier(self):
        category, target = "美容液", "vitamin_c"
        batch_id = self._new_batch_id("dryrundisc")
        budget = orchestrator.BatchBudget(batch_id, 10, 20, 0.50)
        with patch.object(orchestrator, "_staging_reuse_candidates", return_value=[]), \
             patch.object(orchestrator, "_db_reuse_candidates", return_value=[]), \
             patch.object(orchestrator, "_existing_identity_keys", return_value=set()), \
             patch.object(pipeline, "discover_candidates_via_gemini",
                           side_effect=AssertionError("dry_runではGemini探索しないはず")):
            candidate_source = orchestrator.make_discovery_candidate_source(batch_id, budget, "dry_run")
            result = candidate_source(category, target, 3)
        self.assertEqual(result, [])


class CandidateDiscoveryStep40WiringTests(OrchestratorTestBase):
    """Step41とStep40の接続: coverage_gap work item -> 候補自動発見
    (candidate_source未指定=既定のtiered discovery) -> process_coverage_gap_
    item -> Stage1/2/validator/reflectの既存経路、というE2Eを確認する。"""

    def test_coverage_gap_discovers_via_gemini_and_flows_into_reflect(self):
        category, target = "美容液", "vitamin_c"
        batch_id = self._new_batch_id("wiring")
        brand, name = f"配線{TEST_NAME_SUFFIX}", f"商品配線{TEST_NAME_SUFFIX}"

        mock_gemini = make_mock_call_gemini(
            candidate_configs={
                f"{brand} {name}": {
                    "stage1": {"text": "アスコルビン酸配合を確認",
                               "citations": [{"uri": "https://official.example.com/collect", "title": brand}]},
                    "stage2_payload": {
                        "active_ingredients": [
                            {"ingredient": "アスコルビン酸", "concentration": "unknown",
                             "confidence": "high", "source_url": "https://official.example.com/collect"},
                        ],
                        "formulation_features": [], "official_source_confirmed": True,
                    },
                },
            },
            discovery_configs={
                (category, target): {
                    "search": {"text": f"{brand}の{name}はビタミンC配合",
                                "citations": [{"uri": "https://official.example.com/found", "title": "found"}]},
                    "candidates": [
                        {"brand": brand, "product_name": name, "target_evidence": "ビタミンC配合を確認",
                         "source_url": "https://official.example.com/found"},
                    ],
                },
            },
        )

        item = {
            "type": "coverage_gap", "category": category, "target": target,
            "shortage_count": 1, "priority": "high", "reason": "test",
        }
        with patch.object(app, "generate_product_master_work_queue", return_value=[item]), \
             patch.object(pipeline, "call_gemini_for_collection", side_effect=mock_gemini), \
             patch.object(pipeline, "verify_and_resolve_item_code", return_value={"status": "not_found"}):
            # candidate_sourceを明示指定しない -> Step41の既定tiered discoveryを使う
            result = orchestrator.run_batch(mode="execute", batch_id=batch_id)

        action_types = [a["action"] for a in result["actions"]]
        self.assertIn("reflected", action_types)
        self.assertIsNotNone(self._product_master_row(brand, name, category))


if __name__ == "__main__":
    unittest.main()
