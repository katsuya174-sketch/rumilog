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
        # build_candidate_comparison_table()は実売候補(rakuten_criteria/
        # verified_cache)のみを対象にするため、sourceを明示する。
        top_candidates = [
            {"name": "1位商品", "price_ref": 1000, "score": 100, "source": "rakuten_criteria"},
            {
                "name": "【｜9/19 00〜9/30 59】【期間限定】The Ordinary N10+Z1フェイスセラム",
                "price_ref": 900,
                "score": 90,
                "source": "rakuten_criteria",
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

    def test_accepts_brand_containing_symbol_when_present_verbatim_in_raw_title(self):
        # 診断20260927050949108625の実データ調査で判明: brand="肌〇"は
        # Geminiの創作でも伏せ字でもなく、売り手自身が記載した実在ブランド名
        # 「肌〇(はだまる)」(U+3007、フリガナ付きで元タイトルに明記)だった。
        # 記号を含むという理由だけでbrandを一律rejectする実装を一度導入したが、
        # この実データにより誤検知(実在ブランドの過剰リジェクト)と判明し撤回した
        # (経緯はapp.py側の_validate_inferred_brand_and_nameのdocstring参照)。
        # 元タイトルとの文字重なりが高い記号入りbrandは正しく採用されること。
        raw = "肌〇 ( はだまる ) ナチュラルフェイスソープ60g 敏感肌 低刺激 洗顔石鹸"
        brand, name = app._validate_inferred_brand_and_name(raw, "肌〇", "ナチュラルフェイスソープ")
        self.assertEqual(brand, "肌〇")
        self.assertEqual(name, "ナチュラルフェイスソープ")

    def test_rejects_brand_that_is_generic_category_name(self):
        raw = "洗顔 低刺激 敏感肌用"
        brand, name = app._validate_inferred_brand_and_name(raw, "洗顔", "低刺激フェイスソープ")
        self.assertEqual(brand, "")

    def test_rejects_brand_not_present_in_raw_title(self):
        """brandが元タイトルに全く含まれない(=作文の疑いが強い)場合は
        brandのみ不採用にし、nameは正常なら残すこと。"""
        raw = "ANLAN 超音波洗浄機 毛穴ケア"
        brand, name = app._validate_inferred_brand_and_name(raw, "全く無関係なブランド名", "超音波洗浄機")
        self.assertEqual(brand, "")
        self.assertEqual(name, "超音波洗浄機")

    def test_accepts_brand_present_in_raw_title(self):
        raw = "肌ラボ 極潤ヒアルロン液 化粧水"
        brand, name = app._validate_inferred_brand_and_name(raw, "肌ラボ", "極潤ヒアルロン液")
        self.assertEqual(brand, "肌ラボ")
        self.assertEqual(name, "極潤ヒアルロン液")


class InferBrandFromTitleGeneralKnowledgeInferenceTests(unittest.TestCase):
    """infer_brand_from_title(): 診断20260927050949108625の調査で判明した、
    この関数固有の設計上の限界を記録する回帰・特性テスト。

    この関数は「タイトルからの抽出」ではなく「Geminiの一般知識による推測」を
    意図した設計(docstring: "商品名だけからGeminiでブランド名を推測する")。
    呼び出し元のattach_affiliate_links_to_stepは、step["rakuten_title"]
    (楽天の生タイトル)が保存されていればそれを優先して渡すよう修正済み
    (BrandInferSourceTextPrefersRakutenTitleTests参照)。ただしそれでも
    real Rakuten商品にマッチしなかったstep(product_source="ai"/"ai_virtual"、
    rakuten_titleが無い)では、従来どおり短い商品名のみでの一般知識推測に
    フォールバックする。

    そのため、_validate_inferred_brand_and_name()のような「元テキストとの
    文字重なりが低ければreject」という検証は、この関数の意図された使い方
    (テキストに無い一般知識での補完)そのものを壊してしまうため適用できない。
    実データ(brand="肌〇" = 実在ブランド「肌丸(はだまる)」)でも、記号を
    含むという理由だけでの一律rejectが実在ブランドを誤って落とすことが
    判明した(詳細はapp.py _validate_inferred_brand_and_name docstring参照)。

    現状、この関数が返す値がGeminiの正しい一般知識なのか、もっともらしい
    架空のでっち上げなのかを、既存データだけで安全に判別する一般的な方法は
    無い(製品ブランドの正解データベースが存在しない、元テキストとの表記
    揺れ・言語違いを安全に吸収できる汎用的な照合ロジックも無い)ため、
    追加の検証ロジックは実装しない。空文字・40文字超・既存カテゴリ総称語
    という既存の最小限の検証のみを維持する。"""

    def setUp(self):
        app._BRAND_NAME_CACHE.clear()

    def test_plausible_fabricated_brand_name_still_passes_current_validation(self):
        """既存検証の限界を示す特性テスト: 元タイトルに存在しない、もっともらしい
        架空ブランド名でもGemini応答が空文字・40文字以内・非汎用カテゴリ名で
        あれば現状は採用される。安全な追加検証が無いことを明示するための
        テストであり、これは既知の制約であって隠れたバグではない。"""
        with patch("app.call_gemini_with_retry", return_value=_FakeGeminiResponse("ナチュールピュアラボ")), \
             patch("app.save_brand_to_cache"):
            brand = app.infer_brand_from_title("ナチュラルフェイス ソープ", category="洗顔")
        self.assertEqual(brand, "ナチュールピュアラボ")

    def test_accepts_normal_brand_name(self):
        with patch("app.call_gemini_with_retry", return_value=_FakeGeminiResponse("ファンケル")), \
             patch("app.save_brand_to_cache"):
            brand = app.infer_brand_from_title("マイルドクレンジングオイル", category="クレンジング")
        self.assertEqual(brand, "ファンケル")

    def test_accepts_real_brand_name_containing_symbol_character(self):
        # 実データ回帰: 記号を含む実在ブランド名(肌〇=肌丸/はだまる)を、
        # 記号を理由に誤ってrejectしないこと。
        with patch("app.call_gemini_with_retry", return_value=_FakeGeminiResponse("肌〇")), \
             patch("app.save_brand_to_cache"):
            brand = app.infer_brand_from_title("ナチュラルフェイス ソープ", category="洗顔")
        self.assertEqual(brand, "肌〇")


class BrandInferSourceTextPrefersRakutenTitleTests(unittest.TestCase):
    """attach_affiliate_links_to_step(): brand補完のためのinfer_brand_from_title()
    呼び出しに、短い整形済み商品名ではなくstep["rakuten_title"](楽天の生
    タイトル)が利用可能ならそちらを優先して渡すこと(診断20260927050949108625
    の回帰テスト)。ネットワークI/O(fetch_rakuten_item等)を避けるため、
    step側に実画像+リンクを与えてbrand補完直後に早期returnする経路を使う。"""

    def test_uses_rakuten_title_when_available(self):
        step = {
            "category": "洗顔",
            "product": "ナチュラルフェイス ソープ",
            "brand": "",
            "rakuten_title": "肌〇 ( はだまる ) ナチュラルフェイス ソープ60g 敏感肌 低刺激",
            "image": "https://example.com/real.jpg",
            "rakuten_link": "https://example.com/item",
            "product_source": "rakuten_criteria",
        }
        with patch("app.infer_brand_from_title", return_value="肌〇") as mock_infer:
            app.attach_affiliate_links_to_step(step, [])
        mock_infer.assert_called_once_with(
            "肌〇 ( はだまる ) ナチュラルフェイス ソープ60g 敏感肌 低刺激", "洗顔",
        )
        self.assertEqual(step["brand"], "肌〇")

    def test_falls_back_to_product_name_when_no_rakuten_title(self):
        # ai/ai_virtual由来等、実在の楽天商品にマッチしなかったstepでは
        # 従来どおり短い商品名のみで推測する。
        step = {
            "category": "洗顔",
            "product": "低刺激泡洗顔フォーム",
            "brand": "",
            "image": "https://example.com/real.jpg",
            "rakuten_link": "https://example.com/item",
            "product_source": "ai_virtual",
        }
        with patch("app.infer_brand_from_title", return_value="") as mock_infer:
            app.attach_affiliate_links_to_step(step, [])
        mock_infer.assert_called_once_with("低刺激泡洗顔フォーム", "洗顔")


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
