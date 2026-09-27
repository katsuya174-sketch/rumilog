"""
ユーザー向け商品名表示の統一正規化(normalize_display_product_name)の
テスト。

2026-09の品質監査(診断ID 20260926152914212101)で発見した実データを
fixtureとして使用し、以下を確認する:
- ランキング訴求・「業界初」・プレゼント訴求・内部注記(「※ブランド名
  補完」等)が除去されること。
- ブランド名・正式商品名・型番・成分濃度表記等は誤って削除しないこと
  (検索・affiliate用の元データには一切影響しないこと)。
- 美容機器/サプリメントで、販促文言除去後もなお非常に長い場合は
  カテゴリの安全な総称名へフォールバックすること。
- candidate_comparison_table(2位・3位候補)にも同じ正規化が適用される
  こと。
- 通常商品カード(finalize_step_display_fields)にも適用され、Geminiが
  自発的に書いた内部注記が除去されること。
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402


class _FakeGeminiResponse:
    def __init__(self, text):
        self.text = text


class NormalizeDisplayProductNameTests(unittest.TestCase):
    """実データを含むfixtureでの正規化結果を確認する。"""

    def test_removes_ranking_badge_and_gift_wording_and_industry_first(self):
        # 2026-09-27診断(ID 20260926152914212101)の実データ。
        raw = (
            "【美人百花毛穴ケア美顔器部門NO1】業界初完全防水 "
            "ロイヤルウォーターピーリング IPX7 1台6役 イオン導入 美顔器 "
            "超音波ピーリング 毛穴吸引 EMS イオン導出 赤青光エステ "
            "毛穴洗浄 黒ずみ 角栓角質 スタンド充電 プレゼント ANLAN"
        )
        cleaned = app.normalize_display_product_name(raw)
        self.assertNotIn("部門NO1", cleaned)
        self.assertNotIn("業界初", cleaned)
        self.assertNotIn("プレゼント", cleaned)
        # ブランド名(ANLAN)は残ること
        self.assertIn("ANLAN", cleaned)

    def test_removes_internal_brand_completion_annotation(self):
        # 同診断の週ケアstep実データ。
        raw = "タカミ スキンピール（※ブランド名補完）"
        cleaned = app.normalize_display_product_name(raw)
        self.assertEqual(cleaned, "タカミ スキンピール")
        self.assertNotIn("※", cleaned)
        self.assertNotIn("ブランド名補完", cleaned)

    def test_removes_date_time_range_promo_marker(self):
        # 過去に確認済みの実データパターン。
        raw = "【★ 9/19 00~9/24 59】【】乳液 ヒト型セラミド4.5%配合"
        cleaned = app.normalize_display_product_name(raw)
        self.assertNotIn("9/19", cleaned)
        self.assertNotIn("9/24", cleaned)
        self.assertIn("乳液", cleaned)
        self.assertIn("ヒト型セラミド", cleaned)

    def test_does_not_strip_legitimate_brand_and_concentration_names(self):
        """正式商品名・成分濃度表記・型番は誤って削除しないこと。"""
        cases = [
            "COSRX アドバンスドスネイルムチン96 パワーエッセンス",
            "The Ordinary ナイアシンアミド10% + 亜鉛1%",
            "ドクターケイ ABC-Gリペアセラム",
            "VT シカデイリースージングマスク",
        ]
        for raw in cases:
            cleaned = app.normalize_display_product_name(raw)
            self.assertEqual(cleaned, raw, f"raw={raw!r}")

    def test_led_model_designation_is_not_mistaken_for_feature_list_marker(self):
        """検討して不採用にした「大文字トークンを機能列挙の開始位置と
        みなす」構造的抽出手法の反例(実データ)。"LED"はこの商品の
        正式名称・型番の一部であり、販促スパムの開始位置ではない。
        normalize_display_product_name()は既知パターンのみを除去する
        安全な方式のため、"LED"を含む正式名称部分を誤って削除しない
        ことを確認する(将来的に安易なマーカーベース抽出を追加した
        場合の回帰防止)。"""
        raw = (
            "7in1 LED美顔器 ニキビ対策 ハリ 7色光IPL 軽量 7in1 リフトアップ "
            "美容マスク マスク 美肌 光エステ 小顔 コードレス ツヤ 透明肌 "
            "ホームエステ 毛穴 乾燥肌 弾力 スキンケア 家庭用美顔器 家電 "
            "美容グッズ プレゼント"
        )
        cleaned = app.normalize_display_product_name(raw)
        self.assertIn("LED美顔器", cleaned)
        self.assertNotIn("プレゼント", cleaned)

    def test_never_returns_empty_string_for_non_empty_input(self):
        """クリーニングで全部消えてしまっても、空文字は返さず
        元の文字列へ段階的にフォールバックすること(商品名を消さない)。"""
        raw = "プレゼント 業界初"
        cleaned = app.normalize_display_product_name(raw)
        self.assertNotEqual(cleaned, "")

    def test_empty_input_returns_empty_string(self):
        self.assertEqual(app.normalize_display_product_name(""), "")
        self.assertEqual(app.normalize_display_product_name(None), "")


class BeautyDeviceDisplayNameFallbackTests(unittest.TestCase):
    """販促文言除去後もなお非常に長い商品名は、カテゴリの安全な
    総称名へフォールバックすることを確認する(assign_one_stepの
    美容機器/サプリメント分岐、実際の商品検索フローを直接は呼べない
    ため、フォールバック判定のしきい値定数と_DEVICE_DEFAULTS/
    _SUPPLEMENT_DEFAULTSの対応関係を確認する)。"""

    def test_fallback_length_threshold_is_generous_for_normal_names(self):
        normal_names = [
            "ANLAN ロイヤルウォーターピーリング",
            "ドクターシーラボ VC100エッセンスローションEX",
            "こんにゃくセラミド(グルコシルセラミドサプリメント)EX3600",
        ]
        for name in normal_names:
            self.assertLessEqual(
                len(name), app.DISPLAY_NAME_FALLBACK_LENGTH,
                f"正常な商品名がフォールバックしきい値を超えている: {name!r}",
            )

    def test_fallback_length_threshold_catches_real_spam_title(self):
        # 実際に観測した美容機器の販促スパムタイトル(正規化後もなお長い)。
        raw = (
            "【美人百花毛穴ケア美顔器部門NO1】業界初完全防水 "
            "ロイヤルウォーターピーリング IPX7 1台6役 イオン導入 美顔器 "
            "超音波ピーリング 毛穴吸引 EMS イオン導出 赤青光エステ "
            "毛穴洗浄 黒ずみ 角栓角質 スタンド充電 プレゼント ANLAN"
        )
        cleaned = app.normalize_display_product_name(raw)
        self.assertGreater(len(cleaned), app.DISPLAY_NAME_FALLBACK_LENGTH)

    def test_device_defaults_provide_generic_fallback_name(self):
        self.assertIn("product", app._DEVICE_DEFAULTS.get("超音波洗浄", {}))

    def test_supplement_defaults_provide_generic_fallback_name(self):
        self.assertIn("product", app._SUPPLEMENT_DEFAULTS.get("ビタミンB群", {}))


class CandidateComparisonTableNameCleaningTests(unittest.TestCase):
    """商品比較表(2位・3位候補)の商品名にも正規化が適用されること
    (勝者だけでなく比較候補の表示品質も統一する)。"""

    def test_non_winner_candidate_names_are_cleaned(self):
        top_candidates = [
            {"name": "1位商品", "price_ref": 1000, "score": 100},
            {
                "name": "【｜9/19 00〜9/30 59】【期間限定】The Ordinary N10+Z1フェイスセラム",
                "price_ref": 900,
                "score": 90,
            },
        ]
        table = app.build_candidate_comparison_table(top_candidates)
        self.assertNotIn("9/19", table[1]["name"])
        self.assertNotIn("期間限定", table[1]["name"])
        self.assertIn("The Ordinary", table[1]["name"])


class DeviceContextSentenceDoublePeriodTests(unittest.TestCase):
    """device_selection_reason生成時の二重句点バグの回帰テスト。"""

    def test_no_double_period_when_reason_already_ends_with_period(self):
        sentence = app._device_context_sentence(
            category_purpose="",
            category_reason="毛穴の黒ずみと皮脂詰まりを効率的にケアするため。",
            device_type="超音波洗浄",
        )
        self.assertNotIn("。。", sentence)
        self.assertTrue(sentence.endswith("。"))

    def test_single_period_added_when_reason_has_no_trailing_punctuation(self):
        sentence = app._device_context_sentence(
            category_purpose="",
            category_reason="赤みと炎症の鎮静をサポートするため",
            device_type="LED",
        )
        self.assertTrue(sentence.endswith("ため。"))
        self.assertNotIn("。。", sentence)

    def test_does_not_duplicate_other_sentence_ending_punctuation(self):
        sentence = app._device_context_sentence(
            category_purpose="",
            category_reason="毛穴ケアに効果的です！",
            device_type="RF",
        )
        self.assertNotIn("！。", sentence)
        self.assertNotIn("。。", sentence)


class ValidateInferredBrandAndNameTests(unittest.TestCase):
    """_validate_inferred_brand_and_name(): Gemini出力を無条件に信用せず、
    ユーザー表示に使う前の検証ロジック単体のテスト(Gemini呼び出し不要)。"""

    def test_accepts_plausible_extraction(self):
        raw = "【美人百花毛穴ケア美顔器部門NO1】業界初完全防水 ロイヤルウォーターピーリング IPX7 ANLAN"
        brand, name = app._validate_inferred_brand_and_name(raw, "ANLAN", "ロイヤルウォーターピーリング")
        self.assertEqual(brand, "ANLAN")
        self.assertEqual(name, "ロイヤルウォーターピーリング")

    def test_rejects_empty_name(self):
        brand, name = app._validate_inferred_brand_and_name("何かのタイトル", "ANLAN", "")
        self.assertEqual((brand, name), ("", ""))

    def test_rejects_name_longer_than_raw_title(self):
        """商品名のはずが元タイトルより長い = 抽出になっていない。"""
        raw = "短いタイトル"
        brand, name = app._validate_inferred_brand_and_name(raw, "", "短いタイトルよりずっと長い作文された商品名がここに来る")
        self.assertEqual((brand, name), ("", ""))

    def test_rejects_extremely_long_name(self):
        raw = "x" * 200
        long_name = "あ" * (app.DISPLAY_NAME_FALLBACK_LENGTH * 3)
        brand, name = app._validate_inferred_brand_and_name(raw, "", long_name)
        self.assertEqual((brand, name), ("", ""))

    def test_rejects_hallucinated_name_not_related_to_raw_title(self):
        """Geminiが元データに存在しない内容を作文した疑いが強い場合
        (文字集合の重なりが極端に低い)は不採用にすること。"""
        raw = "ANLAN 超音波洗浄機 毛穴ケア"
        brand, name = app._validate_inferred_brand_and_name(raw, "ANLAN", "完全に無関係などこかのブランドの全く別の商品名テキスト")
        self.assertEqual((brand, name), ("", ""))

    def test_rejects_name_that_still_contains_promo_wording(self):
        """normalize_display_product_name()適用後に大きく縮む(=まだ
        販促文言が残っていた)場合は不採用にすること。"""
        raw = "業界初 プレゼント ANLAN 洗浄機"
        brand, name = app._validate_inferred_brand_and_name(raw, "ANLAN", "業界初 プレゼント ANLAN 洗浄機")
        self.assertEqual((brand, name), ("", ""))

    def test_rejects_overly_long_brand(self):
        raw = "テスト商品 " + ("あ" * 50)
        brand, name = app._validate_inferred_brand_and_name(raw, "あ" * 50, "テスト商品")
        self.assertEqual(brand, "")
        self.assertEqual(name, "テスト商品")


class InferBrandAndCleanNameFromTitleTests(unittest.TestCase):
    """infer_brand_and_clean_name_from_title(): 既存のinfer_brand_from_title()
    と同じ呼び出しパターンを再利用し、ブランド名+正式商品名を同時取得する
    新規関数のテスト(新規の外部API・新規の呼び出し経路の追加ではなく、
    既存のGemini呼び出しパターンの拡張であることを実際の挙動で確認)。"""

    def setUp(self):
        # プロセス内メモリキャッシュを毎回クリーンな状態にする(DB非依存)。
        app._BRAND_NAME_CACHE.clear()

    def test_cache_hit_returns_without_calling_gemini(self):
        raw = "キャッシュ済みタイトル テスト"
        cache_key = f"pname_v1:{app.normalize_product_name(raw)}"
        app._BRAND_NAME_CACHE[cache_key] = app.json.dumps({"brand": "TestBrand", "name": "テスト商品"}, ensure_ascii=False)
        with patch("app.call_gemini_with_retry") as mock_call:
            brand, name = app.infer_brand_and_clean_name_from_title(raw)
        mock_call.assert_not_called()
        self.assertEqual((brand, name), ("TestBrand", "テスト商品"))

    def test_negative_cache_hit_returns_empty_without_calling_gemini(self):
        raw = "不明タイトル テスト"
        cache_key = f"pname_v1:{app.normalize_product_name(raw)}"
        app._BRAND_NAME_CACHE[cache_key] = app._BRAND_CACHE_NO_BRAND_SENTINEL
        with patch("app.call_gemini_with_retry") as mock_call:
            brand, name = app.infer_brand_and_clean_name_from_title(raw)
        mock_call.assert_not_called()
        self.assertEqual((brand, name), ("", ""))

    def test_successful_gemini_response_is_parsed_and_validated(self):
        raw = "【美人百花毛穴ケア美顔器部門NO1】業界初完全防水 ロイヤルウォーターピーリング IPX7 ANLAN"
        with patch("app.call_gemini_with_retry", return_value=_FakeGeminiResponse("ANLAN|||ロイヤルウォーターピーリング")), \
             patch("app.save_brand_to_cache") as mock_save:
            brand, name = app.infer_brand_and_clean_name_from_title(raw, category="美容機器")
        self.assertEqual(brand, "ANLAN")
        self.assertEqual(name, "ロイヤルウォーターピーリング")
        mock_save.assert_called_once()

    def test_malformed_gemini_response_without_delimiter_falls_back_to_empty(self):
        raw = "何らかの商品タイトル"
        with patch("app.call_gemini_with_retry", return_value=_FakeGeminiResponse("よくわかりません")), \
             patch("app.save_brand_to_cache"):
            brand, name = app.infer_brand_and_clean_name_from_title(raw)
        self.assertEqual((brand, name), ("", ""))

    def test_gemini_exception_falls_back_to_empty_without_crashing(self):
        raw = "何らかの商品タイトル"
        with patch("app.call_gemini_with_retry", side_effect=Exception("timeout")):
            brand, name = app.infer_brand_and_clean_name_from_title(raw)
        self.assertEqual((brand, name), ("", ""))

    def test_gemini_returns_low_confidence_empty_fields(self):
        """Geminiが確信を持てず空欄で返した場合、安全に空文字になること。"""
        raw = "判別困難な商品タイトル"
        with patch("app.call_gemini_with_retry", return_value=_FakeGeminiResponse("|||")), \
             patch("app.save_brand_to_cache"):
            brand, name = app.infer_brand_and_clean_name_from_title(raw)
        self.assertEqual((brand, name), ("", ""))

    def test_empty_raw_title_returns_empty_without_calling_gemini(self):
        with patch("app.call_gemini_with_retry") as mock_call:
            brand, name = app.infer_brand_and_clean_name_from_title("")
        mock_call.assert_not_called()
        self.assertEqual((brand, name), ("", ""))


class BeautyDeviceNameExtractionIntegrationTests(unittest.TestCase):
    """assign_one_step相当の統合ロジック: Gemini抽出成功時にブランド名の
    二重表示が起きないこと(ProductTitleTextはbrand+productを単純連結
    するだけで重複除去しないため、Flask側でclean_brand_and_product_name
    による重複除去が必須)。"""

    def test_brand_and_name_are_deduped_when_name_already_contains_brand(self):
        combined_brand, deduped_name = app.clean_brand_and_product_name("ANLAN", "ANLAN ロイヤルウォーターピーリング")
        self.assertEqual(deduped_name, "ロイヤルウォーターピーリング")
        display_text = (combined_brand + " " + deduped_name).strip()
        self.assertEqual(display_text.count("ANLAN"), 1)


if __name__ == "__main__":
    unittest.main()
