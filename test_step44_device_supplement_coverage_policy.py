"""Step44: 美容機器・サプリメントのCoverage Policy有効化のテスト。

- 美容機器6領域・サプリメント10領域がtarget_count=3で登録されること
- policyが推薦定義(_DEVICE_DEFAULTS/_SUPPLEMENT_DEFAULTS)から生成され、
  方式・タグを二重管理していないこと
- cosmetics 13領域が変わらないこと
- relevance(美容機器=method一致/サプリ=タグ一致)がcoverage計算に反映されること
- work queueにcoverage_gapが生成され、dry-runに副作用が無いこと
- 未知カテゴリは引き続き自動探索しないこと

Gemini/Rakuten実APIは一切呼ばない。
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import psycopg2  # noqa: E402

import app  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402
import product_master_pipeline as orchestrator  # noqa: E402

DEVICE_TARGETS = ["RF", "LED", "EMS", "エレクトロポレーション", "超音波洗浄", "マイクロカレント"]
SUPPLEMENT_TARGETS = [
    "vitamin_c", "l_cysteine", "vitamin_b", "vitamin_d", "omega3",
    "collagen", "ceramide", "hyaluronic_acid", "probiotics", "zinc",
]
COSMETICS_AREAS = [
    ("化粧水", "hyaluronic_acid"), ("美容液", "vitamin_c"), ("化粧水", "ceramide"),
    ("美容液", "peptide"), ("美容液", "retinol"), ("美容液", "niacinamide"),
    ("化粧水", "niacinamide"), ("洗顔", "salicylic_acid"), ("化粧水", "tranexamic_acid"),
    ("化粧水", "amino_acid"), ("洗顔", "glycolic_acid"), ("クリーム", "peptide"),
    ("クレンジング", "centella_extract"),
]

# サプリ10タグそれぞれの代表的な原料名(Stage2のactive_ingredientsに入る形)。
SUPPLEMENT_SAMPLE_INGREDIENT = {
    "vitamin_c": "ビタミンC",
    "l_cysteine": "L-システイン",
    "vitamin_b": "ビタミンB群",
    "vitamin_d": "ビタミンD3",
    "omega3": "DHA",
    "collagen": "コラーゲン",
    "ceramide": "セラミド",
    "hyaluronic_acid": "ヒアルロン酸",
    "probiotics": "乳酸菌",
    "zinc": "亜鉛",
}


def _master_rows_by_category(rows_by_category):
    def fake_query(category, limit=30):
        return [dict(r) for r in rows_by_category.get(category, [])]
    return fake_query


class CoveragePolicyRegistrationTests(unittest.TestCase):

    def test_beauty_device_policy_has_six_method_areas(self):
        policy = app.COVERAGE_POLICIES["beauty_device"]
        self.assertEqual([e["target"] for e in policy], DEVICE_TARGETS)
        self.assertTrue(all(e["category"] == "美容機器" for e in policy))

    def test_supplement_policy_has_ten_tag_areas(self):
        policy = app.COVERAGE_POLICIES["supplement"]
        self.assertEqual([e["target"] for e in policy], SUPPLEMENT_TARGETS)
        self.assertTrue(all(e["category"] == "サプリメント" for e in policy))

    def test_target_count_is_three_for_all_new_areas(self):
        for name in ("beauty_device", "supplement"):
            for entry in app.COVERAGE_POLICIES[name]:
                with self.subTest(policy=name, target=entry["target"]):
                    self.assertEqual(entry["target_count"], 3)

    def test_cosmetics_policy_unchanged(self):
        self.assertEqual(
            [(e["category"], e["target"]) for e in app.COVERAGE_POLICIES["cosmetics"]],
            COSMETICS_AREAS,
        )
        self.assertTrue(all("target_count" not in e for e in app.COVERAGE_POLICIES["cosmetics"]))

    def test_policies_are_derived_from_recommendation_definitions(self):
        # 二重管理していないこと: 推薦定義が変わればpolicyも追従する。
        self.assertEqual(
            [e["target"] for e in app.build_beauty_device_coverage_policy()],
            list(app._DEVICE_DEFAULTS.keys()),
        )
        self.assertEqual(
            [e["target"] for e in app.build_supplement_coverage_policy()],
            [d["ingredient_focus"][0] for d in app._SUPPLEMENT_DEFAULTS.values()],
        )
        extra_devices = dict(app._DEVICE_DEFAULTS, 新方式={"product": "x"})
        with patch.object(app, "_DEVICE_DEFAULTS", extra_devices):
            self.assertIn("新方式", [e["target"] for e in app.build_beauty_device_coverage_policy()])
        extra_supps = dict(app._SUPPLEMENT_DEFAULTS, 鉄={"ingredient_focus": ["iron"]})
        with patch.object(app, "_SUPPLEMENT_DEFAULTS", extra_supps):
            self.assertIn("iron", [e["target"] for e in app.build_supplement_coverage_policy()])

    def test_supplement_policy_deduplicates_shared_tags(self):
        dup = dict(app._SUPPLEMENT_DEFAULTS, 別名ビタミンC={"ingredient_focus": ["vitamin_c"]})
        with patch.object(app, "_SUPPLEMENT_DEFAULTS", dup):
            targets = [e["target"] for e in app.build_supplement_coverage_policy()]
        self.assertEqual(targets.count("vitamin_c"), 1)

    def test_policy_targets_are_valid_recommendation_values(self):
        # 美容機器のtargetは診断側device_type、Stage2 methodのenumとも一致する。
        method_enum = pipeline._CATEGORY_ATTRIBUTES_EXTRACTION_SPECS["美容機器"]["method"].enum
        for target in DEVICE_TARGETS:
            with self.subTest(target=target):
                self.assertIn(target, method_enum)
        # サプリのtargetは選定ロジックのキーワード辞書にも存在する。
        for target in SUPPLEMENT_TARGETS:
            with self.subTest(target=target):
                self.assertIn(target, app._SUPPLEMENT_INGREDIENT_KEYWORDS)


class CoverageReportTests(unittest.TestCase):

    def _report(self, policy_name, rows_by_category):
        with patch.object(app, "query_product_master_candidates",
                          side_effect=_master_rows_by_category(rows_by_category)):
            return app.get_coverage_report(policy_name, db_products=[], verified_products=[])

    def test_empty_product_master_gives_zero_effective_and_shortage_three(self):
        for name, n in (("beauty_device", 6), ("supplement", 10)):
            report = self._report(name, {})
            with self.subTest(policy=name):
                self.assertEqual(len(report), n)
                for area in report:
                    self.assertEqual(area["effective_count"], 0)
                    self.assertEqual(area["shortage_count"], 3)
                    self.assertFalse(area["sufficient"])

    def test_beauty_device_relevance_counts_only_matching_method(self):
        rows = [
            {"brand": "B", "name": f"RF機{i}", "category": "美容機器",
             "category_attributes": {"method": "RF"}}
            for i in range(3)
        ] + [
            {"brand": "B", "name": "method無し", "category": "美容機器", "category_attributes": {}},
        ]
        report = {a["target"]: a for a in self._report("beauty_device", {"美容機器": rows})}
        self.assertEqual(report["RF"]["effective_count"], 3)
        self.assertTrue(report["RF"]["sufficient"])
        for target in DEVICE_TARGETS[1:]:
            with self.subTest(target=target):
                self.assertEqual(report[target]["effective_count"], 0)

    def test_supplement_relevance_for_all_ten_tags(self):
        rows = [
            {"brand": "S", "name": f"{tag}サプリ{i}", "category": "サプリメント",
             "active_ingredients": [sample]}
            for tag, sample in SUPPLEMENT_SAMPLE_INGREDIENT.items()
            for i in range(3)
        ]
        report = self._report("supplement", {"サプリメント": rows})
        for area in report:
            with self.subTest(target=area["target"]):
                self.assertEqual(area["effective_count"], 3)
                self.assertTrue(area["sufficient"])


class WorkQueueAndDryRunTests(unittest.TestCase):

    def _patched_queue(self, policy_name):
        with patch.object(app, "query_product_master_candidates", return_value=[]), \
             patch.object(app, "get_stale_product_master_candidates", return_value=[]), \
             patch.object(app, "get_needs_review_staging_items", return_value=[]):
            return app.generate_product_master_work_queue(coverage_policy_name=policy_name)

    def test_work_queue_has_coverage_gap_for_every_new_area(self):
        for name, category, targets in (
            ("beauty_device", "美容機器", DEVICE_TARGETS),
            ("supplement", "サプリメント", SUPPLEMENT_TARGETS),
        ):
            queue = self._patched_queue(name)
            gaps = [i for i in queue if i["type"] == "coverage_gap"]
            with self.subTest(policy=name):
                self.assertEqual([g["target"] for g in gaps], targets)
                self.assertTrue(all(g["category"] == category for g in gaps))
                self.assertTrue(all(g["shortage_count"] == 3 and g["priority"] == "high" for g in gaps))

    def _table_counts(self):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            counts = {}
            for table in ("product_master", "product_collection_staging", "product_collection_usage"):
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                counts[table] = cur.fetchone()[0]
            return counts
        finally:
            conn.close()

    def test_dry_run_has_no_side_effects(self):
        pipeline.init_product_collection_tables()
        app.init_product_master_table()
        before = self._table_counts()
        forbidden = AssertionError("dry-runでは呼ばれないはず")
        for name in ("beauty_device", "supplement"):
            with self.subTest(policy=name), \
                 patch.object(app, "query_product_master_candidates", return_value=[]), \
                 patch.object(app, "get_stale_product_master_candidates", return_value=[]), \
                 patch.object(app, "get_needs_review_staging_items", return_value=[]), \
                 patch.object(pipeline, "call_gemini_for_collection", side_effect=forbidden), \
                 patch.object(pipeline, "collect_one_product", side_effect=forbidden), \
                 patch.object(pipeline, "reflect_staging_to_product_master", side_effect=forbidden), \
                 patch.object(orchestrator, "_gemini_discovery_candidates", side_effect=forbidden), \
                 patch.object(orchestrator, "_resolve_item_code_safely", side_effect=forbidden), \
                 patch.object(app, "fetch_rakuten_candidates", side_effect=forbidden), \
                 patch.object(app, "fetch_rakuten_item_by_item_code", side_effect=forbidden), \
                 patch.object(app, "upsert_product_master", side_effect=forbidden):
                result = orchestrator.run_batch(coverage_policy_name=name, mode="dry_run")
            self.assertEqual(result["mode"], "dry_run")
            self.assertEqual(result["work_items"], len(app.COVERAGE_POLICIES[name]))
            self.assertNotIn("coverage_after", result)
            for action in result["actions"]:
                self.assertIn(action["action"], {"would_collect", "skipped_duplicate"})
        self.assertEqual(self._table_counts(), before)

    def test_unknown_category_still_not_explored(self):
        self.assertFalse(orchestrator._category_has_registered_policy("ヘアオイル"))
        budget = orchestrator.BatchBudget("step44-unknown", 10, 20, 0.50)
        with patch.object(orchestrator, "_staging_reuse_candidates",
                          side_effect=AssertionError("未登録カテゴリでは探索しないはず")), \
             patch.object(orchestrator, "_db_reuse_candidates",
                          side_effect=AssertionError("未登録カテゴリでは探索しないはず")):
            source = orchestrator.make_discovery_candidate_source("step44-unknown", budget, "execute")
            self.assertEqual(source("ヘアオイル", "anything", 3), [])
        self.assertEqual(app.get_coverage_report("hair_care"), [])


if __name__ == "__main__":
    unittest.main()
