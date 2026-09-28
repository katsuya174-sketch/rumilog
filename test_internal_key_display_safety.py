"""
内部識別子(内部キー)がユーザー向け文章・Gemini入力へ漏出しないことの
回帰テスト。

診断20260927050949108625・20260928022558595699の実機監査で、
salicylic_acid / dipotassium_glycyrrhizate / hyaluronic / oil_control /
pores / sensitive_ok=yes / normal / ceramide×barrier / niacinamide×azelaic
等の内部キーがそのままユーザー向け文章(recommend_reason/why_best)や
Gemini入力プロンプトへ混入していたことが判明した。

方針:
- candidate_score_reasons等の内部トレース値(raw)は書き換えない。
- ユーザー表示/Gemini入力の境界(_safe_display_label/_safe_display_labels)
  でだけ安全な日本語ラベルへ変換する。
- 変換できない内部キーはrawへフォールバックせず、表示から除外する。
"""

import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402
from test_candidate_comparison_notes import _candidate, _reason  # noqa: E402


class SafeDisplayLabelUnitTests(unittest.TestCase):
    """_safe_display_label()単体のテスト。"""

    def test_translates_known_internal_ingredient_keys(self):
        # 実機で漏出が確認された内部キー。
        self.assertEqual(app._safe_display_label("salicylic_acid"), "サリチル酸")
        self.assertEqual(app._safe_display_label("dipotassium_glycyrrhizate"), "グリチルリチン酸2K")
        self.assertEqual(app._safe_display_label("enzyme"), "酵素")

    def test_translates_ingredient_key_aliases_via_normalize_ingredient_tag(self):
        # "hyaluronic"(内部alias) → normalize_ingredient_tag() →
        # "hyaluronic_acid" → ingredient_map。
        self.assertEqual(app._safe_display_label("hyaluronic"), "ヒアルロン酸")
        # "azelaic"(synergy family省略形) → "azelaic_acid" → ingredient_map。
        self.assertEqual(app._safe_display_label("azelaic"), "アゼライン酸")

    def test_translates_concern_tags_via_existing_label_assets(self):
        # oil_control/poresは実機監査で実際に漏出したconcernタグ。
        # (_CONCERN_LABEL_MAPまたはMAIN_FUNCTION_MAPいずれか、既存の
        # 表示ラベル資産で翻訳されていればよい。二重管理を避けるため
        # 優先順位の詳細はapp.py側の実装に委ねる。)
        self.assertIsNotNone(app._safe_display_label("oil_control"))
        self.assertIsNotNone(app._safe_display_label("pores"))
        self.assertNotEqual(app._safe_display_label("oil_control"), "oil_control")
        self.assertNotEqual(app._safe_display_label("pores"), "pores")

    def test_translates_main_functions_via_main_function_map(self):
        from constants import MAIN_FUNCTION_MAP
        self.assertEqual(app._safe_display_label("uv_protection"), MAIN_FUNCTION_MAP.get("uv_protection"))

    def test_translates_synergy_family_labels(self):
        self.assertEqual(app._safe_display_label("aha_bha"), app._NIGHT_IRRITANT_GROUP_LABELS.get("aha_bha"))
        self.assertEqual(app._safe_display_label("strong_vitamin_c"), app._NIGHT_IRRITANT_GROUP_LABELS.get("strong_vitamin_c"))

    def test_translates_skin_type_keys(self):
        self.assertEqual(app._safe_display_label("normal"), "普通肌")
        self.assertEqual(app._safe_display_label("oily"), "脂性肌")

    def test_translates_score_condition_literals(self):
        # apply_common_score_rules()系が_record()へ渡す「key=value」形式の
        # 内部条件文字列(実際にapp.py中で使われている値)。
        self.assertEqual(app._safe_display_label("sensitive_ok=yes"), "敏感肌向け")
        self.assertEqual(app._safe_display_label("sens=high"), "敏感肌")

    def test_unknown_internal_key_is_dropped_not_shown_raw(self):
        """既存のどの表示ラベル資産にも無い内部キーはNoneを返す(rawへ
        フォールバックしない)。"""
        self.assertIsNone(app._safe_display_label("totally_unknown_internal_key_xyz"))

    def test_already_japanese_text_passes_through_unchanged(self):
        """自由記述の日本語文章(内部キーの形をしていない)はそのまま返す。"""
        self.assertEqual(app._safe_display_label("今回重視する成分"), "今回重視する成分")

    def test_empty_and_none_return_none(self):
        self.assertIsNone(app._safe_display_label(""))
        self.assertIsNone(app._safe_display_label(None))

    def test_safe_display_labels_filters_and_dedupes(self):
        result = app._safe_display_labels(["salicylic_acid", "unknown_xyz", "salicylic_acid", "enzyme"])
        self.assertEqual(result, ["サリチル酸", "酵素"])

    def test_safe_display_labels_respects_limit(self):
        result = app._safe_display_labels(["salicylic_acid", "enzyme", "niacinamide"], limit=2)
        self.assertEqual(len(result), 2)


class FmtCandidateForGeminiSafetyTests(unittest.TestCase):
    """_fmt_candidate_for_gemini(): Gemini入力プロンプトへ内部キーが
    漏れないこと。"""

    def test_active_ingredients_are_translated_not_raw(self):
        cand = _candidate(
            "テスト商品",
            active_ingredients=["salicylic_acid", "dipotassium_glycyrrhizate", "hyaluronic"],
        )
        text = app._fmt_candidate_for_gemini(cand, 1)
        self.assertNotIn("salicylic_acid", text)
        self.assertNotIn("dipotassium_glycyrrhizate", text)
        self.assertNotIn("hyaluronic", text)
        self.assertIn("サリチル酸", text)
        self.assertIn("グリチルリチン酸2K", text)
        self.assertIn("ヒアルロン酸", text)

    def test_main_functions_are_translated_not_raw(self):
        cand = _candidate("テスト商品", main_functions=["oil_control", "pores"])
        text = app._fmt_candidate_for_gemini(cand, 1)
        self.assertNotIn("oil_control", text)
        self.assertNotIn("pores", text)

    def test_unknown_ingredient_key_is_omitted_not_shown_raw(self):
        cand = _candidate("テスト商品", active_ingredients=["totally_unknown_internal_key_xyz"])
        text = app._fmt_candidate_for_gemini(cand, 1)
        self.assertNotIn("totally_unknown_internal_key_xyz", text)


class BuildSelectionReasonFromScoresSafetyTests(unittest.TestCase):
    """build_selection_reason_from_scores(): gemini_generate_selection_
    reasons()が失敗した場合のルールベースfallback理由生成。静的横断チェック
    (#6)で新たに発見した漏出経路(ingredient_focus/main_functionsが内部
    キーのまま埋め込まれ得た)の回帰テスト。"""

    def test_ingredient_focus_internal_key_is_translated(self):
        product = {"name": "テスト美容液", "brand": "テストブランド",
                   "active_ingredients": ["salicylic_acid"]}
        step = {"category": "美容液", "purpose": "", "ingredient_focus": "salicylic_acid"}
        text = app.build_selection_reason_from_scores(product, step, {"oil": "", "sens": ""})
        self.assertNotIn("salicylic_acid", text)
        self.assertIn("サリチル酸", text)

    def test_unknown_ingredient_focus_key_is_omitted_not_shown_raw(self):
        product = {"name": "テスト美容液", "brand": "テストブランド"}
        step = {"category": "美容液", "purpose": "", "ingredient_focus": "totally_unknown_internal_key_xyz"}
        text = app.build_selection_reason_from_scores(product, step, {"oil": "", "sens": ""})
        self.assertNotIn("totally_unknown_internal_key_xyz", text)

    def test_main_functions_internal_key_is_translated(self):
        product = {"name": "テスト美容液", "brand": "テストブランド",
                   "main_functions": ["oil_control"]}
        step = {"category": "美容液", "purpose": "", "ingredient_focus": ""}
        text = app.build_selection_reason_from_scores(product, step, {"oil": "", "sens": ""})
        self.assertNotIn("oil_control", text)


class GeminiSelectionReasonAllowedIngredientsSafetyTests(unittest.TestCase):
    """gemini_generate_selection_reasons(): Geminiプロンプトの「言及可能
    成分」欄に内部キーが漏れないこと(実際のGemini呼び出しはモックする)。"""

    def test_allowed_ingredients_prompt_has_no_raw_internal_keys(self):
        captured_prompt = {}

        def _fake_call_gemini_with_retry(client, model, contents, **kwargs):
            captured_prompt["text"] = contents
            class _R:
                text = "[]"
            return _R()

        data = {
            "morning": {"steps": []},
            "night": {"steps": [
                {
                    "product": "テスト美容液",
                    "product_source": "rakuten_criteria",
                    "category": "美容液",
                    "top_candidates": [
                        _candidate("テスト美容液", active_ingredients=["salicylic_acid", "hyaluronic"]),
                    ],
                },
            ]},
            "weekly_care": [],
        }
        from unittest.mock import patch
        with patch("app.call_gemini_with_retry", side_effect=_fake_call_gemini_with_retry):
            app.gemini_generate_selection_reasons(data, {})

        prompt_text = captured_prompt.get("text", "")
        self.assertNotIn("salicylic_acid", prompt_text)
        self.assertNotIn("hyaluronic", prompt_text)


class BuildWhyBestTextSafetyTests(unittest.TestCase):
    """_build_why_best_text()/build_candidate_comparison_notes(): why_bestへ
    内部キーが漏れないこと(candidate_score_reasonsのmatched_product_feature
    経由)。"""

    def test_internal_feature_key_is_translated_in_why_best(self):
        candidates = [
            _candidate("商品A", base_score=80, candidate_score_reasons=[
                _reason("common_concern_match", "今回の悩みタグに一致する",
                        feature="oil_control", condition="oil_control", points=8),
            ]),
            _candidate("競合B", base_score=50, candidate_score_reasons=[]),
        ]
        result = app.build_candidate_comparison_notes(candidates, {}, {})
        self.assertNotIn("oil_control", result["why_best"])

    def test_unknown_internal_feature_key_is_omitted_not_shown_raw(self):
        """翻訳できない内部キーはwhy_bestから除外され、labelのみが残ること。"""
        candidates = [
            _candidate("商品A", base_score=80, candidate_score_reasons=[
                _reason("some_rule", "今回の目的に合う特徴を持つ",
                        feature="totally_unknown_internal_key_xyz",
                        condition="totally_unknown_internal_key_xyz", points=8),
            ]),
            _candidate("競合B", base_score=50, candidate_score_reasons=[]),
        ]
        result = app.build_candidate_comparison_notes(candidates, {}, {})
        self.assertNotIn("totally_unknown_internal_key_xyz", result["why_best"])
        self.assertIn("今回の目的に合う特徴を持つ", result["why_best"])

    def test_sensitive_ok_literal_condition_is_translated(self):
        candidates = [
            _candidate("商品A", base_score=80, candidate_score_reasons=[
                _reason("common_sensitive_ok_yes", "敏感肌向けとして確認されている",
                        feature="sensitive_ok=yes", condition="sens=high", points=12),
            ]),
            _candidate("競合B", base_score=50, candidate_score_reasons=[]),
        ]
        result = app.build_candidate_comparison_notes(candidates, {}, {})
        self.assertNotIn("sensitive_ok=yes", result["why_best"])

    def test_synergy_single_family_pair_is_translated(self):
        candidates = [
            _candidate("商品A", base_score=80, candidate_score_reasons=[
                _reason("routine_synergy_bonus", "他ステップの成分と相乗効果が期待できる組み合わせ",
                        feature="ceramide×barrier", condition="routine_context.synergy_rules", points=10),
            ]),
            _candidate("競合B", base_score=50, candidate_score_reasons=[]),
        ]
        result = app.build_candidate_comparison_notes(candidates, {}, {})
        self.assertNotIn("ceramide×barrier", result["why_best"])
        self.assertIn("セラミド", result["why_best"])
        self.assertIn("バリア", result["why_best"])

    def test_synergy_multiple_family_pairs_are_both_translated(self):
        """診断20260928022558595699の実データ回帰:
        「ceramide×barrier・niacinamide×azelaic」のように、同じrule
        (routine_synergy_bonus)が複数回マッチして_aggregate_reasons_by_rule
        でfeatureリストとして蓄積されるケース。"""
        candidates = [
            _candidate("商品A", base_score=80, candidate_score_reasons=[
                _reason("routine_synergy_bonus", "他ステップの成分と相乗効果が期待できる組み合わせ",
                        feature="ceramide×barrier", condition="routine_context.synergy_rules", points=10),
                _reason("routine_synergy_bonus", "他ステップの成分と相乗効果が期待できる組み合わせ",
                        feature="niacinamide×azelaic", condition="routine_context.synergy_rules", points=8),
            ]),
            _candidate("競合B", base_score=50, candidate_score_reasons=[]),
        ]
        result = app.build_candidate_comparison_notes(candidates, {}, {})
        self.assertNotIn("ceramide×barrier", result["why_best"])
        self.assertNotIn("niacinamide×azelaic", result["why_best"])
        self.assertIn("ナイアシンアミド", result["why_best"])
        self.assertIn("アゼライン酸", result["why_best"])


class DiffsFromBestSafetyTests(unittest.TestCase):
    """build_candidate_comparison_notes()のdiffs(「1位との違い」)へ
    内部キーが漏れないこと。"""

    def test_unique_active_ingredient_diff_is_translated(self):
        candidates = [
            _candidate("商品A", base_score=90, active_ingredients=["salicylic_acid"]),
            _candidate("商品B", base_score=70, active_ingredients=[]),
        ]
        result = app.build_candidate_comparison_notes(candidates, {}, {})
        diff_text = result["diffs"][0]["text"]
        self.assertNotIn("salicylic_acid", diff_text)
        self.assertIn("サリチル酸", diff_text)

    def test_unique_main_function_diff_is_translated(self):
        candidates = [
            _candidate("商品A", base_score=90, active_ingredients=[], main_functions=["oil_control"]),
            _candidate("商品B", base_score=70, active_ingredients=[], main_functions=[]),
        ]
        result = app.build_candidate_comparison_notes(candidates, {}, {})
        diff_text = result["diffs"][0]["text"]
        self.assertNotIn("oil_control", diff_text)

    def test_unknown_diff_ingredient_falls_back_gracefully(self):
        """翻訳できない成分・機能しか無い場合でも、raw文字列を出さず
        「スコア内訳がわずかに劣る」等の既存フォールバックへ落ちること。"""
        candidates = [
            _candidate("商品A", base_score=90, active_ingredients=["totally_unknown_xyz"], main_functions=["totally_unknown_func_xyz"]),
            _candidate("商品B", base_score=90, active_ingredients=[], main_functions=[]),
        ]
        result = app.build_candidate_comparison_notes(candidates, {}, {})
        diff_text = result["diffs"][0]["text"]
        self.assertNotIn("totally_unknown_xyz", diff_text)
        self.assertNotIn("totally_unknown_func_xyz", diff_text)


class LongRakutenTitleTruncationOrderTests(unittest.TestCase):
    """診断20260927050949108625の実データ回帰:
    「normalize→truncate」の順序で、販促文言が先頭にある長いタイトルでも
    商品名本体が失われないこと。"""

    def test_normalize_before_truncate_preserves_product_name(self):
        # 部門NO1/業界初/ランキング/ポイント/あす楽/期間限定/買いまわり/
        # エントリーで/楽天SALE等、_WHY_BEST_PROMO_PATTERNS(clean_display_
        # product_nameには無く、normalize_display_product_nameだけが持つ
        # 強力な販促文言除去パターン)に該当する語だけで60文字超を構成し、
        # 商品名本体("オルナオーガニック乳液はり対策用")は末尾に置く。
        raw_title = (
            "毛穴ケア美顔器部門NO1業界初完全防水ランキング1位獲得記念"
            "エントリーでポイント10倍買いまわり対象期間限定あす楽対応可能"
            "楽天スーパーSALE開催中オルナオーガニック乳液はり対策用"
        )
        # 旧実装(clean_display_product_name→60文字): 軽量クリーニングだけ
        # では上記の販促パターンを除去できず、60文字の枠が販促文言だけで
        # 埋まり、商品名本体が一文字も残らない(実障害の再現)。
        old_order_result = app.clean_display_product_name(raw_title)[:60]
        # 新実装(normalize_display_product_name→60文字): 強力な販促文言
        # 除去を先に適用してから60文字上限を掛けるため、商品名本体が残る。
        new_order_result = app.normalize_display_product_name(raw_title)[:60]
        self.assertIn("オルナオーガニック", new_order_result)
        self.assertNotIn("オルナオーガニック", old_order_result)

    def test_brand_inference_receives_improved_name_downstream(self):
        """preserve_ranked_top_candidates()内のinfer_brand_from_title()呼び
        出し(app.py:17700付近)は、このname生成順序修正の副次効果として、
        壊れた60文字タイトルではなく商品名本体が残ったnameを受け取るように
        なることの回帰確認(専用のコード変更ではなく、name生成の一次修正が
        そのままこの下流の呼び出しにも伝播することを確認する)。"""
        raw_title = (
            "毛穴ケア美顔器部門NO1業界初完全防水ランキング1位獲得記念"
            "エントリーでポイント10倍買いまわり対象期間限定あす楽対応可能"
            "楽天スーパーSALE開催中オルナオーガニック乳液はり対策用"
        )
        old_name = app.clean_display_product_name(raw_title)[:60]
        new_name = app.normalize_display_product_name(raw_title)[:60]

        from unittest.mock import patch

        captured = {}

        def _fake_call(client, model, contents, **kwargs):
            captured["prompt"] = contents
            class _R:
                text = "テストブランド"
            return _R()

        app._BRAND_NAME_CACHE.clear()
        with patch("app.call_gemini_with_retry", side_effect=_fake_call), \
             patch("app.save_brand_to_cache"):
            app.infer_brand_from_title(new_name, "美容機器")
        self.assertIn("オルナオーガニック", str(captured["prompt"]))

        app._BRAND_NAME_CACHE.clear()
        with patch("app.call_gemini_with_retry", side_effect=_fake_call), \
             patch("app.save_brand_to_cache"):
            app.infer_brand_from_title(old_name, "美容機器")
        self.assertNotIn("オルナオーガニック", str(captured["prompt"]))


class ComparisonTableRealCandidateOnlyTests(unittest.TestCase):
    """build_candidate_comparison_table(): ai_virtualを価格・コスパ比較から
    除外し、rakuten_criteria/verified_cacheのみを対象にすること。"""

    def test_ai_virtual_only_returns_empty_table(self):
        candidates = [
            _candidate("仮想商品1", source="ai_virtual", price_ref=0),
            _candidate("仮想商品2", source="ai_virtual", price_ref=0),
            _candidate("仮想商品3", source="ai_virtual", price_ref=0),
        ]
        rows = app.build_candidate_comparison_table(candidates)
        self.assertEqual(rows, [])

    def test_single_real_candidate_returns_one_row(self):
        candidates = [
            _candidate("実売商品", source="rakuten_criteria", price_ref=1000),
            _candidate("仮想商品", source="ai_virtual", price_ref=0),
        ]
        rows = app.build_candidate_comparison_table(candidates)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["price"], 1000)

    def test_two_or_more_real_candidates_are_all_shown(self):
        candidates = [
            _candidate("実売商品1", source="rakuten_criteria", price_ref=1000, score=90),
            _candidate("実売商品2", source="verified_cache", price_ref=2000, score=80),
            _candidate("仮想商品", source="ai_virtual", price_ref=0, score=95),
        ]
        rows = app.build_candidate_comparison_table(candidates)
        self.assertEqual(len(rows), 2)
        names = [r["name"] for r in rows]
        self.assertIn("実売商品1", names)
        self.assertIn("実売商品2", names)

    def test_mixed_source_ranks_are_renumbered_from_real_only(self):
        """ai_virtualが1位に混じっていても、表内の順位は実売候補だけで
        1位・2位と振り直されること。"""
        candidates = [
            _candidate("仮想1位", source="ai_virtual", price_ref=0, score=99),
            _candidate("実売2位相当", source="rakuten_criteria", price_ref=1500, score=80),
            _candidate("実売3位相当", source="verified_cache", price_ref=2500, score=70),
        ]
        rows = app.build_candidate_comparison_table(candidates)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["rank"], 1)
        self.assertEqual(rows[1]["rank"], 2)
        self.assertEqual(rows[0]["name"], "実売2位相当")

    def test_verified_cache_source_is_treated_as_real(self):
        candidates = [
            _candidate("実売商品1", source="verified_cache", price_ref=1000, score=90),
            _candidate("実売商品2", source="rakuten_criteria", price_ref=2000, score=80),
        ]
        rows = app.build_candidate_comparison_table(candidates)
        self.assertEqual(len(rows), 2)

    def test_real_candidate_price_is_preserved(self):
        """実売候補の価格伝播ロジック自体は変更していないことの確認。"""
        candidates = [
            _candidate("実売商品1", source="rakuten_criteria", price_ref=1234, score=90),
            _candidate("実売商品2", source="rakuten_criteria", price_ref=5678, score=80),
        ]
        rows = app.build_candidate_comparison_table(candidates)
        prices = {r["name"]: r["price"] for r in rows}
        self.assertEqual(prices["実売商品1"], 1234)
        self.assertEqual(prices["実売商品2"], 5678)

    def test_diff_from_best_kept_when_original_best_is_real(self):
        candidates = [
            _candidate("実売1位", source="rakuten_criteria", price_ref=1000, score=90, active_ingredients=["niacinamide"]),
            _candidate("実売2位", source="rakuten_criteria", price_ref=2000, score=70, active_ingredients=[]),
        ]
        notes = app.build_candidate_comparison_notes(candidates, {}, {})
        rows = app.build_candidate_comparison_table(candidates, notes["diffs"])
        rank2 = next(r for r in rows if r["rank"] == 2)
        self.assertNotEqual(rank2["diff_from_best"], "")

    def test_diff_from_best_blank_when_original_best_is_virtual(self):
        """1位自体がai_virtualの場合、表の1位(実売の中の最上位)は
        why_bestの対象と一致しないため、誤解を招くdiff_from_bestは
        空文字にすること。"""
        candidates = [
            _candidate("仮想1位", source="ai_virtual", price_ref=0, score=95, active_ingredients=["niacinamide"]),
            _candidate("実売候補", source="rakuten_criteria", price_ref=1000, score=70, active_ingredients=[]),
        ]
        notes = app.build_candidate_comparison_notes(candidates, {}, {})
        rows = app.build_candidate_comparison_table(candidates, notes["diffs"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["diff_from_best"], "")


if __name__ == "__main__":
    unittest.main()
