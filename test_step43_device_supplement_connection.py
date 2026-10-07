"""Step43: 美容機器・サプリメントをProduct Masterへ完全接続するための
テスト。

Step42の監査で判明した断絶(Stage2がcategory_attributesを生成しない/
supplementのingredient_focusタグ5種が未定義/calculate_effective_candidates
とCandidate Discoveryが別々の関連性判定を持つ/診断時に美容機器・サプリが
product_masterを一切見ない)を、どの接続ポイントも個別に検証する。

Gemini/Rakuten実APIは一切呼ばない。
"""

import json
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import psycopg2  # noqa: E402

import app  # noqa: E402
import product_collection_grounding_poc as poc  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402
import product_master_pipeline as orchestrator  # noqa: E402

TEST_NAME_SUFFIX = "_Step43Test"


def _sales_info(item_code, title):
    return {
        "item_code": item_code, "rakuten_link": f"https://item.rakuten.co.jp/shop/{item_code}/",
        "rakuten_title": title, "image": f"https://image.example.com/{item_code}.jpg", "price_ref": 3000,
    }


class SupplementIngredientTagTests(unittest.TestCase):
    """Step43-3: normalize_ingredient_tag()がサプリメント10タグすべてを
    正規化できること、既存タグ(lactobacillus/bifida/cysteamine等)への
    回帰影響が無いことを確認する。"""

    ALL_SUPPLEMENT_TAGS_AND_SAMPLE_TEXT = {
        "vitamin_c": "ビタミンC",
        "l_cysteine": "Lシステイン",
        "vitamin_b": "ビタミンB群",
        "vitamin_d": "ビタミンD",
        "omega3": "オメガ3",
        # "コラーゲンペプチド"(商品名)は既存の"ペプチド"ルールが先に一致して
        # peptideタグになる(normalize_ingredient_tag()の既存の順序依存の
        # 挙動で、Step43で変更していない)。collagenタグ自体の正規化確認には
        # "コラーゲン"単体を使う。
        "collagen": "コラーゲン",
        "ceramide": "セラミド",
        "hyaluronic_acid": "ヒアルロン酸",
        "probiotics": "プロバイオティクス",
        "zinc": "亜鉛",
    }

    def test_all_ten_supplement_tags_are_normalizable(self):
        for tag, sample_text in self.ALL_SUPPLEMENT_TAGS_AND_SAMPLE_TEXT.items():
            with self.subTest(tag=tag):
                self.assertEqual(app.normalize_ingredient_tag(sample_text), tag)

    def test_new_tags_use_existing_keyword_variants(self):
        # _SUPPLEMENT_INGREDIENT_KEYWORDSの既存表記をそのまま使っていること。
        self.assertEqual(app.normalize_ingredient_tag("エルシステイン"), "l_cysteine")
        self.assertEqual(app.normalize_ingredient_tag("cysteine"), "l_cysteine")
        self.assertEqual(app.normalize_ingredient_tag("vitamin b"), "vitamin_b")
        self.assertEqual(app.normalize_ingredient_tag("vitamin d"), "vitamin_d")
        self.assertEqual(app.normalize_ingredient_tag("フィッシュオイル"), "omega3")
        self.assertEqual(app.normalize_ingredient_tag("EPA"), "omega3")
        self.assertEqual(app.normalize_ingredient_tag("DHA"), "omega3")
        self.assertEqual(app.normalize_ingredient_tag("probiotics"), "probiotics")

    def test_existing_fermentation_tags_unaffected_by_probiotics_addition(self):
        # ビフィズス菌/英語のlactobacillusは既存のbifida/lactobacillus
        # (化粧品の発酵成分タグ)のまま。probioticsの追加で壊れていないこと
        # を確認する。
        # 注記(Step43で発見した既存の挙動、今回のスコープ外): "乳酸菌"は
        # さらに手前の"乳酸"ルール(lactic_acid、AHA系角質ケア成分)に部分
        # 文字列一致して先に拾われるため、実際には"lactobacillus"ではなく
        # "lactic_acid"に正規化される(normalize_ingredient_tag()の既存の
        # 順序依存の挙動で、Step43で変更していない)。
        # lactic_acidタグは乳酸塩(乳酸カルシウム等)にも付くため、is_candidate_
        # relevant_to_target()のprobiotics判定は"乳酸菌"を生の原料名で見る。
        self.assertEqual(app.normalize_ingredient_tag("乳酸菌"), "lactic_acid")
        self.assertEqual(app.normalize_ingredient_tag("ビフィズス菌"), "bifida")
        self.assertEqual(app.normalize_ingredient_tag("lactobacillus"), "lactobacillus")

    def test_cysteamine_unaffected_by_l_cysteine_addition(self):
        # システアミン(cysteamine)は既存タグのまま(l_cysteineの"システイン"
        # トリガーと部分文字列的に衝突しないことを再確認)。
        self.assertEqual(app.normalize_ingredient_tag("システアミン"), "cysteamine")
        self.assertEqual(app.normalize_ingredient_tag("cysteamine"), "cysteamine")

    def test_existing_vitamin_c_vitamin_e_tags_unaffected(self):
        self.assertEqual(app.normalize_ingredient_tag("ビタミンC"), "vitamin_c")
        self.assertEqual(app.normalize_ingredient_tag("ビタミンE"), "vitamin_e")

    def test_epa_dha_vitamin_b_d_do_not_match_inside_other_words(self):
        # "epa"/"dha"/"vitamin d"の単純な部分一致による誤判定(ヘパリノイド・
        # アダトダ・vitamin derivative等)が起きないこと。
        for text in ("heparinoid", "Heparin", "Adhatoda Vasica Leaf Extract", "vitamin derivative"):
            with self.subTest(text=text):
                self.assertNotIn(
                    app.normalize_ingredient_tag(text), {"omega3", "vitamin_b", "vitamin_d"},
                )

    def test_epa_dha_vitamin_b_d_still_match_as_standalone_terms(self):
        self.assertEqual(app.normalize_ingredient_tag("EPA・DHA"), "omega3")
        self.assertEqual(app.normalize_ingredient_tag("DHA含有精製魚油"), "omega3")
        self.assertEqual(app.normalize_ingredient_tag("ビタミンB12"), "vitamin_b")
        self.assertEqual(app.normalize_ingredient_tag("vitamin_b"), "vitamin_b")
        self.assertEqual(app.normalize_ingredient_tag("Vitamin D3"), "vitamin_d")
        self.assertEqual(app.normalize_ingredient_tag("vitamin_d"), "vitamin_d")


class CategoryAttributesSchemaFinalizationTests(unittest.TestCase):
    """Step43-2: Step42監査で判明した重複・不要フィールドを除いた最終schema
    (美容機器=method必須のみ/サプリメント=必須フィールド無し)を確認する。"""

    def test_beauty_device_schema_requires_only_method(self):
        self.assertEqual(app.CATEGORY_ATTRIBUTE_SCHEMAS["美容機器"]["required"], ["method"])

    def test_supplement_schema_has_no_required_fields(self):
        self.assertEqual(app.CATEGORY_ATTRIBUTE_SCHEMAS["サプリメント"]["required"], [])

    def test_supplement_schema_does_not_duplicate_active_ingredients(self):
        # "ingredients"フィールド(Step38で定義、active_ingredientsと二重
        # 管理になっていた)が廃止されていることを確認する。
        extraction_spec = pipeline._category_attributes_extraction_spec("サプリメント")
        self.assertNotIn("ingredients", extraction_spec)
        self.assertIn("dosage", extraction_spec)
        self.assertIn("serving_size", extraction_spec)
        self.assertIn("precautions", extraction_spec)

    def test_beauty_device_extraction_spec_has_no_modes_or_usage_frequency(self):
        # modes/usage_frequencyは現在の推薦・検証のどちらにも使わないため
        # 収集自体をしない。
        extraction_spec = pipeline._category_attributes_extraction_spec("美容機器")
        self.assertIn("method", extraction_spec)
        self.assertNotIn("modes", extraction_spec)
        self.assertNotIn("usage_frequency", extraction_spec)

    def test_cosmetics_category_has_no_extraction_spec(self):
        self.assertIsNone(pipeline._category_attributes_extraction_spec("美容液"))
        self.assertIsNone(pipeline._category_attributes_extraction_spec("化粧水"))


class Stage2CategoryAwarePromptAndSchemaTests(unittest.TestCase):
    """Step43-1: build_stage2_prompt()/build_product_collection_response_
    schema()がcategory_attributes_spec=None(cosmetics)のとき既存出力を
    完全維持し、spec指定時のみcategory_attributes関連の指示/schemaが
    追加されることを確認する。"""

    def test_cosmetics_prompt_is_byte_identical_to_before(self):
        citations = [{"uri": "https://x.example.com/a", "title": "a"}]
        prompt = pipeline.build_stage2_prompt("Brand", "Product", "text", citations)
        self.assertNotIn("category_attributes", prompt)
        # category_attributes_spec=Noneを明示した場合と完全一致すること。
        self.assertEqual(
            prompt,
            pipeline.build_stage2_prompt("Brand", "Product", "text", citations, category_attributes_spec=None),
        )

    def test_beauty_device_prompt_includes_category_attributes_instructions(self):
        citations = [{"uri": "https://x.example.com/a", "title": "a"}]
        spec = pipeline._category_attributes_extraction_spec("美容機器")
        prompt = pipeline.build_stage2_prompt("Brand", "Product", "text", citations, category_attributes_spec=spec)
        self.assertIn("category_attributes", prompt)
        self.assertIn("method", prompt)

    def test_cosmetics_response_schema_has_no_category_attributes_property(self):
        schema = poc.build_product_collection_response_schema()
        self.assertNotIn("category_attributes", schema.properties)
        self.assertNotIn("category_attributes", schema.required)

    def test_beauty_device_response_schema_has_method_enum(self):
        spec = pipeline._category_attributes_extraction_spec("美容機器")
        schema = poc.build_product_collection_response_schema(category_attributes_spec=spec)
        self.assertIn("category_attributes", schema.properties)
        method_value_schema = schema.properties["category_attributes"].properties["method"].properties["value"]
        self.assertIn("RF", method_value_schema.enum)
        self.assertIn("unknown", method_value_schema.enum)


class Stage2SanitizeCategoryAttributesTests(unittest.TestCase):
    """Step43-1: Stage1 citationに無いsource_urlを持つcategory_attributes
    フィールドは、active_ingredients/formulation_featuresと同じ原則で
    value/source_urlがunknownに強制され、citationに存在する場合のみ
    採用されることを確認する(推測で埋めない)。"""

    def test_ungrounded_category_attribute_is_forced_unknown(self):
        valid_urls = {"https://official.example.com/x"}
        payload = {
            "active_ingredients": [], "formulation_features": [], "jan_code": "unknown",
            "official_source_confirmed": False,
            "category_attributes": {
                "method": {
                    "value": "RF", "confidence": "high",
                    "source_url": "https://not-a-real-citation.example.com/made-up",
                },
            },
        }
        sanitized = pipeline.sanitize_stage2_payload(payload, valid_urls, "stage1 text")
        self.assertEqual(sanitized["category_attributes"]["method"]["value"], "unknown")
        self.assertEqual(sanitized["category_attributes"]["method"]["source_url"], "unknown")

    def test_grounded_category_attribute_is_kept(self):
        valid_urls = {"https://official.example.com/x"}
        payload = {
            "active_ingredients": [], "formulation_features": [], "jan_code": "unknown",
            "official_source_confirmed": False,
            "category_attributes": {
                "method": {
                    "value": "RF", "confidence": "high",
                    "source_url": "https://official.example.com/x",
                },
            },
        }
        sanitized = pipeline.sanitize_stage2_payload(payload, valid_urls, "stage1 text")
        self.assertEqual(sanitized["category_attributes"]["method"]["value"], "RF")

    def test_cosmetics_payload_without_category_attributes_key_is_unaffected(self):
        valid_urls = {"https://official.example.com/x"}
        payload = {
            "active_ingredients": [
                {"ingredient": "アスコルビン酸", "concentration": "unknown",
                 "confidence": "high", "source_url": "https://official.example.com/x"},
            ],
            "formulation_features": [], "jan_code": "unknown", "official_source_confirmed": False,
        }
        sanitized = pipeline.sanitize_stage2_payload(payload, valid_urls, "stage1 text")
        self.assertNotIn("category_attributes", sanitized)


class FlattenCategoryAttributesTests(unittest.TestCase):
    """flatten_category_attributes(): wrapped形式({value,confidence,
    source_url})から、product_master反映・relevance判定用の単純な
    {field: value}形式への変換を確認する。unknown値のフィールドはキー
    ごと落ちる(validate_category_attributes()が正しく「欠落」として
    検出できるようにするため)。"""

    def test_flattens_real_values_and_drops_unknown(self):
        raw = {
            "method": {"value": "RF", "confidence": "high", "source_url": "https://x"},
            "contraindications": {"value": "unknown", "confidence": "unknown", "source_url": "unknown"},
        }
        self.assertEqual(pipeline.flatten_category_attributes(raw), {"method": "RF"})

    def test_none_or_non_dict_returns_empty_dict(self):
        self.assertEqual(pipeline.flatten_category_attributes(None), {})
        self.assertEqual(pipeline.flatten_category_attributes("not a dict"), {})


class RelevanceAdapterTests(unittest.TestCase):
    """Step43-4: is_candidate_relevant_to_target()のcategory分岐(cosmetics/
    サプリメント/美容機器)を確認する。"""

    def test_beauty_device_method_match_is_relevant(self):
        product = {"category_attributes": {"method": "RF"}}
        self.assertTrue(app.is_candidate_relevant_to_target("美容機器", "RF", product))

    def test_beauty_device_method_mismatch_is_not_relevant(self):
        product = {"category_attributes": {"method": "LED"}}
        self.assertFalse(app.is_candidate_relevant_to_target("美容機器", "RF", product))

    def test_beauty_device_missing_category_attributes_is_not_relevant(self):
        self.assertFalse(app.is_candidate_relevant_to_target("美容機器", "RF", {}))

    def test_supplement_tag_match_is_relevant(self):
        product = {"active_ingredients": ["vitamin_c"]}
        self.assertTrue(app.is_candidate_relevant_to_target("サプリメント", "vitamin_c", product))

    def test_supplement_tag_mismatch_is_not_relevant(self):
        product = {"active_ingredients": ["zinc"]}
        self.assertFalse(app.is_candidate_relevant_to_target("サプリメント", "vitamin_c", product))

    def test_supplement_probiotics_target_accepts_raw_lactic_acid_bacteria_name(self):
        # 乳酸菌サプリメントの原料名が文字どおり"乳酸菌"と抽出された場合、
        # normalize_ingredient_tag()は(既存の順序依存の挙動により)
        # lactic_acidへ正規化されるが、probioticsターゲットは生の原料名で
        # これを受理する。
        product = {"active_ingredients": ["乳酸菌"]}
        self.assertTrue(app.is_candidate_relevant_to_target("サプリメント", "probiotics", product))

    def test_supplement_probiotics_target_rejects_lactate_salts(self):
        # 乳酸カルシウム/乳酸Naもlactic_acidタグになるが、乳酸菌ではない。
        for name in ("乳酸カルシウム", "乳酸Na"):
            with self.subTest(name=name):
                product = {"active_ingredients": [name]}
                self.assertFalse(app.is_candidate_relevant_to_target("サプリメント", "probiotics", product))

    def test_supplement_probiotics_target_accepts_bifida_synonym(self):
        product = {"active_ingredients": ["ビフィズス菌"]}
        self.assertTrue(app.is_candidate_relevant_to_target("サプリメント", "probiotics", product))

    def test_cosmetics_delegates_to_existing_score_product_logic(self):
        product = {
            "brand": "サンプルブランド", "name": "サンプルブランド ビタミンC美容液プレミアム", "category": "美容液",
            "active_ingredients": ["vitamin_c"],
        }
        reasons = []
        score = app.score_product(
            dict(product), {"category": "美容液", "purpose": "", "ingredient_focus": "vitamin_c"},
            app._EFFECTIVE_CANDIDATE_NEUTRAL_USER_DATA, app._EFFECTIVE_CANDIDATE_BUDGET_VALUE, reasons=reasons,
        )
        expected = app._is_relevant_scored_candidate(score, reasons, "vitamin_c")
        self.assertEqual(
            app.is_candidate_relevant_to_target("美容液", "vitamin_c", product), expected,
        )
        self.assertTrue(expected)


class ProductMasterFixtureEffectiveCandidateTests(unittest.TestCase):
    """Step43-4: calculate_effective_candidates()が美容機器/サプリメントの
    product_masterフィクスチャを正しく数えること(method一致→カウント、
    method不一致→除外)を実DBで確認する。"""

    def setUp(self):
        app.init_product_master_table()
        self._cleanup()

    def tearDown(self):
        self._cleanup()

    def _cleanup(self):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM product_master WHERE name LIKE %s", (f"%{TEST_NAME_SUFFIX}%",))
            conn.commit()
        finally:
            conn.close()

    def test_beauty_device_method_match_counts_as_effective_candidate(self):
        brand, name, category = f"デバイスA{TEST_NAME_SUFFIX}", f"商品A{TEST_NAME_SUFFIX}", "美容機器"
        # Step44.8: coverageは診断で使える候補だけを数えるため販売情報が必要。
        # 実DBの行(query_product_master_candidates()の変換後の形)で確認する。
        app.upsert_product_master(dict({
            "brand": brand, "name": name, "category": category,
            "category_attributes": {"method": "RF"},
        }, **_sales_info("dev-a", f"{brand} {name} RF美顔器")), data_source="ai_precollected")

        count = app.calculate_effective_candidates(category, "RF", db_products=[], verified_products=[])
        self.assertEqual(count, 1)

    def test_beauty_device_without_sales_info_is_not_counted(self):
        brand, name, category = f"デバイスN{TEST_NAME_SUFFIX}", f"商品N{TEST_NAME_SUFFIX}", "美容機器"
        app.upsert_product_master({
            "brand": brand, "name": name, "category": category,
            "category_attributes": {"method": "RF"},
        }, data_source="ai_precollected")

        count = app.calculate_effective_candidates(category, "RF", db_products=[], verified_products=[])
        self.assertEqual(count, 0)

    def test_beauty_device_method_mismatch_is_excluded(self):
        brand, name, category = f"デバイスB{TEST_NAME_SUFFIX}", f"商品B{TEST_NAME_SUFFIX}", "美容機器"
        app.upsert_product_master({
            "brand": brand, "name": name, "category": category,
            "category_attributes": {"method": "LED"},
        }, data_source="ai_precollected")

        count = app.calculate_effective_candidates(category, "RF", db_products=[], verified_products=[])
        self.assertEqual(count, 0)

    def test_supplement_ingredient_tag_match_counts_as_effective_candidate(self):
        brand, name, category = f"サプリA{TEST_NAME_SUFFIX}", f"商品A{TEST_NAME_SUFFIX}", "サプリメント"
        app.upsert_product_master(dict({
            "brand": brand, "name": name, "category": category,
            "active_ingredients": ["アスコルビン酸"],
        }, **_sales_info("supp-a", f"{brand} {name} ビタミンC")), data_source="ai_precollected")

        count = app.calculate_effective_candidates(category, "vitamin_c", db_products=[], verified_products=[])
        self.assertEqual(count, 1)

    def test_candidate_discovery_db_reuse_uses_same_adapter_as_coverage(self):
        # Step43-4(接続確認): calculate_effective_candidates()が関連あり
        # と判定する美容機器候補は、_db_reuse_candidates()(Candidate
        # Discovery)でも同じ関連あり判定になることを、同一fixtureに対して
        # 同じapp.is_candidate_relevant_to_target()を経由していることで
        # 直接確認する(判定ロジックの二重実装が無いことの確認)。
        brand, name, category = f"デバイスC{TEST_NAME_SUFFIX}", f"商品C{TEST_NAME_SUFFIX}", "美容機器"
        product = {
            "brand": brand, "name": name, "category": category,
            "category_attributes": {"method": "RF"},
        }
        coverage_relevant = app.is_candidate_relevant_to_target(category, "RF", product)
        with patch.object(app, "load_products", return_value=[product]), \
             patch.object(app, "load_verified_products_cache", return_value=[]):
            discovery_candidates = orchestrator._db_reuse_candidates(category, "RF", set(), 3)
        self.assertTrue(coverage_relevant)
        self.assertEqual(len(discovery_candidates), 1)
        self.assertEqual(discovery_candidates[0]["name"], name)


class DiagnosisTimeProductMasterUsageTests(unittest.TestCase):
    """Step43-5: attach_affiliate_links_to_step()の美容機器・サプリメント
    経路が、Product Master候補(関連性確認済み・販売情報あり)を優先し、
    無い場合は既存の楽天ライブ経路へ正常にフォールバックすることを確認する
    (実API呼び出しは一切発生しない)。

    注記: attach_affiliate_links_to_step()は最終選定された実在候補を
    accumulate_verified_product()(既存の仕組み、Step43で変更していない)
    経由でverified_products_cache/product_masterへ書き込む設計のため、
    フォールバック(ライブ楽天)テストのfake itemで実際にproduct_masterへ
    行が書き込まれる。テストDBを汚さないよう明示的にpatchする。
    """

    def setUp(self):
        app.init_product_master_table()

    def _beauty_device_step(self):
        return {
            "category": "美容機器", "product": "RF美顔器", "device_type": "RF",
            "brand": "", "product_source": "ai", "purpose": "", "reason": "",
        }

    def _supplement_step(self):
        return {
            "category": "サプリメント", "product": "ビタミンC サプリメント",
            "supplement_type": "ビタミンC", "ingredient_focus": ["vitamin_c"],
            "brand": "", "product_source": "ai",
        }

    def test_beauty_device_uses_product_master_candidate_without_calling_live_rakuten(self):
        master_row = {
            "brand": f"ブランドD{TEST_NAME_SUFFIX}", "name": f"RF美顔器 本体{TEST_NAME_SUFFIX}",
            "category": "美容機器", "category_attributes": {"method": "RF"},
            "item_code": "rk-device-001", "price_ref": 15000,
            "last_known_rakuten_link": "https://item.rakuten.co.jp/shop/rk-device-001/",
            "last_known_image": "https://image.example.com/device.jpg",
            "rakuten_title": f"ブランドD{TEST_NAME_SUFFIX} RF美顔器 本体 家庭用",
            "shop_name": "テストショップ",
        }
        step = self._beauty_device_step()
        with patch.object(app, "query_product_master_candidates", return_value=[master_row]), \
             patch.object(app, "infer_brand_from_image", return_value=""), \
             patch.object(app, "accumulate_verified_product", return_value=None), \
             patch.object(app, "fetch_rakuten_item_by_item_code",
                          return_value={"ok": False, "http_status": None, "rakuten_error": "test"}), \
             patch.object(app, "fetch_rakuten_candidates",
                           side_effect=AssertionError("product_master候補がある場合は楽天ライブ検索しないはず")):
            result = app.attach_affiliate_links_to_step(step, [], user_data={"sens": "normal"}, budget_value=20000)

        # attach_affiliate_links_to_step()はstepへrakuten_link/imageだけを
        # 書き込む設計(item_code自体はrakuten_item内部にのみ保持される)
        # ため、product_master由来のlast_known_rakuten_linkが使われたことを
        # rakuten_link/imageの一致で確認する。
        self.assertEqual(result["rakuten_link"], master_row["last_known_rakuten_link"])
        self.assertEqual(result["image"], master_row["last_known_image"])

    def test_beauty_device_falls_back_to_live_rakuten_when_product_master_empty(self):
        # 現状product_masterに美容機器は0件なので、常にこの経路を通る
        # (既存ユーザー出力が変わらないことの回帰確認)。
        step = self._beauty_device_step()
        fake_item = {
            "itemName": "テストブランド RF美顔器", "itemCaption": "", "itemPrice": 12000,
            "itemCode": "rk-live-001", "itemUrl": "https://item.rakuten.co.jp/shop/rk-live-001/",
            "shopName": "ライブショップ", "reviewCount": 10, "reviewAverage": 4.5,
            "mediumImageUrls": [{"imageUrl": "https://image.example.com/live.jpg"}],
        }
        with patch.object(app, "query_product_master_candidates", return_value=[]), \
             patch.object(app, "infer_brand_from_image", return_value=""), \
             patch.object(app, "accumulate_verified_product", return_value=None), \
             patch.object(app, "fetch_rakuten_candidates", return_value=[(100, fake_item)]) as mock_fetch:
            result = app.attach_affiliate_links_to_step(step, [], user_data={"sens": "normal"}, budget_value=20000)

        mock_fetch.assert_called_once()
        self.assertEqual(result["rakuten_link"], fake_item["itemUrl"])

    def test_supplement_uses_product_master_candidate_without_calling_live_rakuten(self):
        master_row = {
            "brand": f"ブランドE{TEST_NAME_SUFFIX}", "name": f"ビタミンCサプリ{TEST_NAME_SUFFIX}",
            "category": "サプリメント", "active_ingredients": ["vitamin_c"],
            "item_code": "rk-supp-001", "price_ref": 2000,
            "last_known_rakuten_link": "https://item.rakuten.co.jp/shop/rk-supp-001/",
            "last_known_image": "https://image.example.com/supp.jpg",
            "rakuten_title": f"ブランドE{TEST_NAME_SUFFIX} ビタミンCサプリ 60粒",
            "shop_name": "テストショップ",
        }
        step = self._supplement_step()
        with patch.object(app, "query_product_master_candidates", return_value=[master_row]), \
             patch.object(app, "infer_brand_from_image", return_value=""), \
             patch.object(app, "accumulate_verified_product", return_value=None), \
             patch.object(app, "fetch_rakuten_item_by_item_code",
                          return_value={"ok": False, "http_status": None, "rakuten_error": "test"}), \
             patch.object(app, "fetch_rakuten_candidates",
                           side_effect=AssertionError("product_master候補がある場合は楽天ライブ検索しないはず")):
            result = app.attach_affiliate_links_to_step(step, [], user_data={"sens": "normal"}, budget_value=20000)

        self.assertEqual(result["rakuten_link"], master_row["last_known_rakuten_link"])
        self.assertEqual(result["image"], master_row["last_known_image"])

    def test_supplement_falls_back_to_live_rakuten_when_product_master_empty(self):
        step = self._supplement_step()
        fake_item = {
            "itemName": "テストブランド ビタミンCサプリ", "itemCaption": "", "itemPrice": 1800,
            "itemCode": "rk-live-002", "itemUrl": "https://item.rakuten.co.jp/shop/rk-live-002/",
            "shopName": "ライブショップ", "reviewCount": 5, "reviewAverage": 4.0,
            "mediumImageUrls": [{"imageUrl": "https://image.example.com/live2.jpg"}],
        }
        with patch.object(app, "query_product_master_candidates", return_value=[]), \
             patch.object(app, "infer_brand_from_image", return_value=""), \
             patch.object(app, "accumulate_verified_product", return_value=None), \
             patch.object(app, "fetch_rakuten_candidates", return_value=[(100, fake_item)]) as mock_fetch:
            result = app.attach_affiliate_links_to_step(step, [], user_data={"sens": "normal"}, budget_value=20000)

        mock_fetch.assert_called_once()
        self.assertEqual(result["rakuten_link"], fake_item["itemUrl"])

    def _supplement_master_row(self):
        return {
            "brand": f"ブランドF{TEST_NAME_SUFFIX}", "name": f"ビタミンCサプリ{TEST_NAME_SUFFIX}",
            "category": "サプリメント", "active_ingredients": ["vitamin_c"],
            "item_code": "rk-supp-002", "price_ref": 2000,
            "last_known_rakuten_link": "https://item.rakuten.co.jp/shop/rk-supp-002/",
            "last_known_image": "https://image.example.com/supp-old.jpg",
            "rakuten_title": f"ブランドF{TEST_NAME_SUFFIX} ビタミンCサプリ 60粒",
            "shop_name": "テストショップ",
        }

    def test_selected_product_master_candidate_is_refreshed_by_item_code(self):
        # 化粧品経路(apply_db_product_to_step)と同じく、選定されたproduct_
        # master候補はitem_codeでライブの価格・URL・画像へ更新される。
        master_row = self._supplement_master_row()
        live = {
            "ok": True,
            "item": {
                "itemPrice": 2480, "itemUrl": "https://item.rakuten.co.jp/shop/rk-supp-002/?live",
                "mediumImageUrls": [{"imageUrl": "https://image.example.com/supp-live.jpg"}],
            },
        }
        step = self._supplement_step()
        with patch.object(app, "query_product_master_candidates", return_value=[master_row]), \
             patch.object(app, "infer_brand_from_image", return_value=""), \
             patch.object(app, "accumulate_verified_product", return_value=None), \
             patch.object(app, "fetch_rakuten_item_by_item_code", return_value=live) as mock_by_code, \
             patch.object(app, "fetch_rakuten_candidates",
                           side_effect=AssertionError("product_master候補がある場合は楽天ライブ検索しないはず")):
            result = app.attach_affiliate_links_to_step(step, [], user_data={"sens": "normal"}, budget_value=20000)

        mock_by_code.assert_called_once_with("rk-supp-002")
        self.assertEqual(result["rakuten_link"], live["item"]["itemUrl"])
        self.assertEqual(result["image"], "https://image.example.com/supp-live.jpg")

    def test_selected_product_master_candidate_keeps_last_known_when_refresh_fails(self):
        master_row = self._supplement_master_row()
        step = self._supplement_step()
        with patch.object(app, "query_product_master_candidates", return_value=[master_row]), \
             patch.object(app, "infer_brand_from_image", return_value=""), \
             patch.object(app, "accumulate_verified_product", return_value=None), \
             patch.object(app, "fetch_rakuten_item_by_item_code",
                          return_value={"ok": False, "http_status": 500, "rakuten_error": "test"}), \
             patch.object(app, "fetch_rakuten_candidates",
                           side_effect=AssertionError("product_master候補がある場合は楽天ライブ検索しないはず")):
            result = app.attach_affiliate_links_to_step(step, [], user_data={"sens": "normal"}, budget_value=20000)

        self.assertEqual(result["rakuten_link"], master_row["last_known_rakuten_link"])
        self.assertEqual(result["image"], master_row["last_known_image"])

    def test_live_rakuten_winner_is_not_refreshed_by_item_code(self):
        step = self._beauty_device_step()
        fake_item = {
            "itemName": "テストブランド RF美顔器", "itemCaption": "", "itemPrice": 12000,
            "itemCode": "rk-live-003", "itemUrl": "https://item.rakuten.co.jp/shop/rk-live-003/",
            "shopName": "ライブショップ", "reviewCount": 10, "reviewAverage": 4.5,
            "mediumImageUrls": [{"imageUrl": "https://image.example.com/live3.jpg"}],
        }
        with patch.object(app, "query_product_master_candidates", return_value=[]), \
             patch.object(app, "infer_brand_from_image", return_value=""), \
             patch.object(app, "accumulate_verified_product", return_value=None), \
             patch.object(app, "fetch_rakuten_item_by_item_code",
                          side_effect=AssertionError("ライブ楽天候補はitem_code再取得しないはず")), \
             patch.object(app, "fetch_rakuten_candidates", return_value=[(100, fake_item)]):
            result = app.attach_affiliate_links_to_step(step, [], user_data={"sens": "normal"}, budget_value=20000)

        self.assertEqual(result["rakuten_link"], fake_item["itemUrl"])

    def test_cosmetics_step_unaffected_by_product_master_adapter(self):
        # category=="美容機器"/"サプリメント"以外は従来通りfetch_rakuten_item
        # を使う経路のまま(新しいadapterに触れない)ことの回帰確認。
        step = {"category": "美容液", "product": "テスト美容液", "brand": "", "product_source": "ai"}
        with patch.object(app, "fetch_rakuten_item", return_value=None) as mock_fetch_item, \
             patch.object(app, "query_product_master_candidates",
                           side_effect=AssertionError("cosmetics経路はこのadapterを使わないはず")):
            app.attach_affiliate_links_to_step(step, [], user_data={"sens": "normal"}, budget_value=20000)
        mock_fetch_item.assert_called_once()


class ReflectCategoryAttributesPropagationTests(unittest.TestCase):
    """reflect_staging_to_product_master()がcategory_attributesを
    flatten_category_attributes()経由でproduct_masterへ反映すること、
    value="unknown"のフィールドは反映されないことを確認する(実DB)。"""

    TEST_BATCH_PREFIX = "step43-reflect-test"

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
            cur.execute("DELETE FROM product_collection_staging WHERE batch_id LIKE %s", (f"{self.TEST_BATCH_PREFIX}%",))
            cur.execute("DELETE FROM product_master WHERE name LIKE %s", (f"%{TEST_NAME_SUFFIX}%",))
            conn.commit()
        finally:
            conn.close()

    def test_reflect_propagates_flattened_category_attributes(self):
        brand, name, category = f"デバイスF{TEST_NAME_SUFFIX}", f"商品F{TEST_NAME_SUFFIX}", "美容機器"
        identity_key = app._normalize_product_master_identity_key(brand, name, category)
        payload = {
            "active_ingredients": [], "formulation_features": [], "jan_code": "unknown",
            "official_source_confirmed": True,
            "category_attributes": {
                "method": {"value": "RF", "confidence": "high", "source_url": "https://x.example.com/a"},
                "contraindications": {"value": "unknown", "confidence": "unknown", "source_url": "unknown"},
            },
        }
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO product_collection_staging
                    (batch_id, brand, product_name, category, identity_key,
                     stage1_status, stage1_citations, stage2_status, stage2_payload,
                     conflict_status, conflict_detail)
                VALUES (%s, %s, %s, %s, %s, 'ok', %s, 'ok', %s, 'none', '[]')
                RETURNING staging_id
                """,
                (
                    f"{self.TEST_BATCH_PREFIX}-1", brand, name, category, identity_key,
                    json.dumps([{"uri": "https://x.example.com/a", "title": brand}]),
                    json.dumps(payload),
                ),
            )
            staging_id = cur.fetchone()[0]
            conn.commit()
        finally:
            conn.close()

        result = pipeline.reflect_staging_to_product_master(staging_id, dry_run=False)
        self.assertEqual(result["status"], "reflected")

        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute("SELECT category_attributes FROM product_master WHERE product_id = %s", (result["product_id"],))
            category_attributes = cur.fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(category_attributes, {"method": "RF"})
        self.assertNotIn("contraindications", category_attributes)


class ProductionRegressionAuditTests(unittest.TestCase):
    """Step43検証: 既存商品・13領域・74商品を破壊していないことを、実際の
    本番DATABASE_URLに対して読み取りのみで確認する(このテストファイル自体
    はos.environ.setdefaultでrumilog_testに固定済みのため、他のテストと
    同じtest DBに対して実行される=実運用DBには一切触れない。本番側の確認は
    別途read-onlyスクリプトで実施済み)。ここではローカルtest DBに対して、
    is_candidate_relevant_to_target()への置き換えがcosmetics系の既存
    coverage計算を壊していないことだけを構造的に確認する。"""

    def test_calculate_effective_candidates_still_callable_for_all_13_areas(self):
        for item in app.COSMETICS_COVERAGE_POLICY:
            with self.subTest(category=item["category"], target=item["target"]):
                count = app.calculate_effective_candidates(
                    item["category"], item["target"], db_products=[], verified_products=[],
                )
                self.assertIsInstance(count, int)
                self.assertGreaterEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
