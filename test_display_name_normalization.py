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


class GeminiCleanRakutenProductNamesTopCandidateSyncTests(unittest.TestCase):
    """gemini_clean_rakuten_product_names(): 1位ステップの整形結果が
    step["product"]だけでなくtop_candidates[0]["name"]にも同期される
    こと。why_best(_build_why_best_text)・商品比較表は
    top_candidates[0]を直接参照する(step["product"]とは独立)ため、
    ここが同期していないと、商品カードは整形済みなのに「なぜこの商品が
    1位か」・商品比較表の1位行だけ楽天の生タイトル(販促文・ブランド
    バッジ等込み)のままになる(2026-09、実機診断20260928083119852553で
    確認した不具合の回帰テスト)。"""

    def setUp(self):
        app._rakuten_name_clean_cache.clear()

    def _build_data(self, raw_title):
        step = {
            "product_source": "rakuten_criteria",
            "product": raw_title,
            "brand": "",
            "rakuten_title": raw_title,
            "top_candidates": [
                {"name": raw_title, "brand": "", "source": "rakuten_criteria", "price_ref": 1000},
            ],
        }
        return {"morning": {"steps": [step]}, "night": {"steps": []}, "weekly_care": []}, step

    def test_top_candidate_zero_name_is_synced_with_cleaned_step_product(self):
        raw_title = "59まで！【公式】オルナオーガニック【楽天】乳液「はり対策用」コラーゲン3種+ヒアルロン酸"
        data, step = self._build_data(raw_title)
        with patch("app.call_gemini_with_retry",
                   return_value=_FakeGeminiResponse("1. オルナオーガニック 乳液")):
            app.gemini_clean_rakuten_product_names(data)
        self.assertEqual(step["top_candidates"][0]["name"], step["product"])
        self.assertNotIn("59まで", step["top_candidates"][0]["name"])
        self.assertNotIn("【楽天】", step["top_candidates"][0]["name"])

    def test_top_candidate_zero_name_synced_via_rule_based_fallback(self):
        """Gemini呼び出しが失敗した場合のルールベースfallback整形でも
        同様にtop_candidates[0]["name"]が同期されること。"""
        raw_title = "【スーパーSALE】オルナオーガニック乳液単体オンリー 送料無料"
        data, step = self._build_data(raw_title)
        with patch("app.call_gemini_with_retry", side_effect=Exception("timeout")):
            app.gemini_clean_rakuten_product_names(data)
        self.assertEqual(step["top_candidates"][0]["name"], step["product"])
        self.assertNotIn("スーパーSALE", step["top_candidates"][0]["name"])

    def test_second_candidate_still_cleaned_independently(self):
        """1位のtop_candidates[0]同期を追加しても、2位候補の既存の
        個別整形ロジックには影響しないこと。"""
        raw_title_1 = "59まで！【公式】オルナオーガニック【楽天】乳液"
        raw_title_2 = "【スーパーSALE】競合品乳液単体オンリー 送料無料"
        data, step = self._build_data(raw_title_1)
        step["top_candidates"].append(
            {"name": raw_title_2, "brand": "", "source": "rakuten_criteria", "price_ref": 900}
        )
        with patch(
            "app.call_gemini_with_retry",
            return_value=_FakeGeminiResponse("1. オルナオーガニック 乳液\n2. 競合品 乳液"),
        ):
            app.gemini_clean_rakuten_product_names(data)
        self.assertEqual(step["top_candidates"][0]["name"], "オルナオーガニック 乳液")
        self.assertEqual(step["top_candidates"][1]["name"], "競合品 乳液")

    def test_why_best_uses_synced_name_not_raw_rakuten_title(self):
        """統合確認: 同期後、_build_why_best_textが実際にtop_candidates[0]
        から読むbest_labelも整形済みの名前になること。"""
        raw_title = "59まで！【公式】オルナオーガニック【楽天】乳液「はり対策用」コラーゲン3種+ヒアルロン酸"
        data, step = self._build_data(raw_title)
        step["top_candidates"].append(
            {"name": "競合品", "brand": "", "source": "rakuten_criteria", "price_ref": 900,
             "base_score": 50, "score": 50, "candidate_score_reasons": []}
        )
        step["top_candidates"][0].update({
            "base_score": 90,
            "score": 90,
            "candidate_score_reasons": [
                {"axis": "base", "rule": "ingredient_focus_active_match",
                 "label": "今回重視する成分を主成分として含む",
                 "matched_product_feature": "niacinamide", "matched_user_condition": "niacinamide",
                 "points": 25},
            ],
        })
        with patch("app.call_gemini_with_retry",
                   return_value=_FakeGeminiResponse("1. オルナオーガニック 乳液")):
            app.gemini_clean_rakuten_product_names(data)
        notes = app.build_candidate_comparison_notes(step["top_candidates"], step, {})
        self.assertNotIn("59まで", notes["why_best"])
        self.assertNotIn("【楽天】", notes["why_best"])
        self.assertIn("オルナオーガニック", notes["why_best"])


class GeminiNameCleanPromptSeoKeywordGuidanceTests(unittest.TestCase):
    """_GEMINI_NAME_CLEAN_PROMPT_PREFIXに、成分・特徴の列挙とSEOキーワード
    列挙を区別する判断基準が含まれていること。

    実際の出力品質(Geminiが正しく判断するか)はプロンプトエンジニアリング
    の性質上、モックでは検証できない(実際のGemini呼び出しが必要)。ここでは
    プロンプト文言自体が意図通り存在すること・将来の編集で誤って削除
    されないことだけを保証する回帰テスト。2026-09、実機診断
    20260928083119852553で「シムホワイト377 ナイアシンアミド ビタミンC
    誘導体 FGF FGF フラーレン セラミド レチノール 透明感アップ」のような
    成分羅列タイトルがほぼそのまま残っていたことへの対応。"""

    def test_prompt_contains_seo_keyword_vs_formal_name_judgment_criterion(self):
        prompt = app._GEMINI_NAME_CLEAN_PROMPT_PREFIX
        self.assertIn("SEOキーワード", prompt)
        # 「個数」だけによる絶対ルールにしない(正式名称に複数成分が
        # 組み込まれているケースを壊さない)ことを明示した判断基準文言。
        self.assertIn("判断基準は個数そのものではなく", prompt)

    def test_prompt_contains_seo_keyword_example_pair(self):
        prompt = app._GEMINI_NAME_CLEAN_PROMPT_PREFIX
        # 成分列挙を削除すべき例
        self.assertIn("シムホワイト377", prompt)
        # 成分1つが正式名称を構成し保持すべき例
        self.assertIn("アゼライン酸化粧水", prompt)


class RoutineStrategyPromptOverallReasonGuidanceTests(unittest.TestCase):
    """build_analysis_prompt()/build_analysis_prompt_phase2()の
    routine_strategy.overall_policy/reason指示に、「なぜこのルーティン
    にしたのかのトータルの理由」を求める指示が含まれていること。

    修正前は「reason: この肌状態に合う理由」という短い指示のみで、
    実機診断で「週間ルーティンの理由欄の内容が不十分」という指摘を受けた。
    実際の出力品質(Geminiが指示通り書くか)はプロンプトエンジニアリングの
    性質上モックでは検証できないため、指示文言自体が存在すること・将来の
    編集で誤って削除されないことだけを保証する回帰テスト。

    あわせて、ユーザーからの以下の指摘を反映した内容であることも確認する:
    - 「優先順位1位に必ず言及」という硬直したルールにはしない
      (優先度の高い項目を中心に、という柔軟な表現にする)。
    - 個別の頻度設定の詳細理由(use_days_reason/frequency_reason_note側の
      役割)をreasonに重複させない。

    2026-09、実機診断で「具体例を1つ追加しただけでは改善しない」ことが
    確認されたため、reasonを1つの自由記述フィールドのままにせず、
    reason_priority_focus/reason_frequency_designという2つの独立必須
    フィールドに分割した(get_analysis_schema_phase2()参照)。要素の
    どちらかを省略できない構造にすることで、Geminiの遵守を強制する狙い。
    """

    _USER_DATA = {"concerns": [], "age": 30, "budget": 5000, "exp": "beginner", "oil": "oily", "sens": "normal"}

    def test_full_prompt_contains_overall_reason_guidance(self):
        # build_analysis_prompt()/get_analysis_schema()は実際には呼ばれて
        # いない未使用コード(get_analysis_schema_phase2()が実際に使われる
        # スキーマ)だが、将来復活する可能性に備え文言の存在だけ確認する。
        prompt = app.build_analysis_prompt(self._USER_DATA)
        self.assertIn("全体方針を一文で", prompt)
        self.assertIn("優先度の高い改善項目を中心に", prompt)
        # 「優先順位1位に必ず言及」という硬直した表現は含まれないこと
        self.assertNotIn("優先順位1位に必ず言及", prompt)
        self.assertIn("use_days_reason", prompt)

    def test_phase2_prompt_contains_split_reason_field_guidance(self):
        """実際に使われるbuild_analysis_prompt_phase2()の指示。"""
        prompt = app.build_analysis_prompt_phase2(self._USER_DATA, {})
        self.assertIn("全体方針を一文で", prompt)
        self.assertIn("reason_priority_focus:", prompt)
        self.assertIn("reason_frequency_design:", prompt)
        self.assertIn("優先度の高い改善項目を中心に", prompt)
        self.assertIn("use_days_reason", prompt)

    def test_prompt_contains_morning_night_weekly_role_division_guidance(self):
        prompt = app.build_analysis_prompt(self._USER_DATA)
        self.assertIn("互いにどう役割分担しているか", prompt)

    def test_prompt_contains_worked_example_with_target_depth(self):
        prompt = app.build_analysis_prompt(self._USER_DATA)
        self.assertIn("routine_strategy 出力例", prompt)
        self.assertIn("刺激を抑えた最低限のケア", prompt)
        self.assertIn("集中ケアとして頻度を絞る", prompt)

    def test_phase2_prompt_contains_worked_example(self):
        prompt = app.build_analysis_prompt_phase2(self._USER_DATA, {})
        self.assertIn("routine_strategy 出力例", prompt)


class AssembleRoutineStrategyReasonTests(unittest.TestCase):
    """assemble_routine_strategy_reason(): reason_priority_focus/
    reason_frequency_designの2つの独立フィールドを、Android側の既存
    フィールド(routine_strategy.reason、単一文字列)へ自然に連結する。"""

    def test_both_parts_present_are_joined_with_space(self):
        rs = {"reason_priority_focus": "優先度説明。", "reason_frequency_design": "頻度説明。"}
        app.assemble_routine_strategy_reason(rs)
        self.assertEqual(rs["reason"], "優先度説明。 頻度説明。")
        self.assertNotIn("reason_priority_focus", rs)
        self.assertNotIn("reason_frequency_design", rs)

    def test_missing_frequency_part_still_produces_valid_reason(self):
        rs = {"reason_priority_focus": "優先度説明のみ。", "reason_frequency_design": ""}
        app.assemble_routine_strategy_reason(rs)
        self.assertEqual(rs["reason"], "優先度説明のみ。")

    def test_none_routine_strategy_does_not_raise(self):
        app.assemble_routine_strategy_reason(None)

    def test_non_dict_routine_strategy_does_not_raise(self):
        app.assemble_routine_strategy_reason("not a dict")

    def test_other_routine_strategy_fields_are_preserved(self):
        rs = {"reason_priority_focus": "a", "reason_frequency_design": "b", "overall_policy": "x", "morning_order": ["1"]}
        app.assemble_routine_strategy_reason(rs)
        self.assertEqual(rs["overall_policy"], "x")
        self.assertEqual(rs["morning_order"], ["1"])


def _real_candidate(name, price_ref, item_code, score=90, brand="テストブランド"):
    """商品比較表テスト用: 実売(rakuten_criteria)候補を1件作る。
    brandはデフォルトで非空にしている(空文字だとpreserve_ranked_top_candidates
    がinfer_brand_from_title()経由で実際にGemini APIを呼んでしまうため)。"""
    return {
        "name": name, "brand": brand, "source": "rakuten_criteria",
        "price_ref": price_ref, "item_code": item_code,
        "rakuten_link": f"https://item.rakuten.co.jp/{item_code}/",
        "score": score, "base_score": score, "improve_score": 0, "routine_score": 0,
        "active_ingredients": [], "main_functions": [],
    }


class SearchRakutenForStepListingDedupTests(unittest.TestCase):
    """search_rakuten_for_step()内のcollect()が、出品単位(item_code優先、
    無ければrakuten_link、それも無ければ商品名正規化)で重複除去すること。

    2026-09、実機診断で「商品比較欄が変わらない」ことが確認された根本
    原因: 前回実装した商品比較表専用の別ショップ判定(preserve_ranked_
    top_candidates側)は、search_rakuten_for_step()のcollect()が商品名
    だけで既に別ショップ出品を1件に潰した"後"のデータを受け取っていた
    ため、そもそも別ショップ出品がスコアリング段階まで届いていなかった。
    この回帰テストはより手前のcollect()自体が出品単位で残すことを確認する。"""

    def _fake_search(self, results_by_keyword_substr):
        def _search(keyword, category):
            for substr, results in results_by_keyword_substr.items():
                if substr in keyword:
                    return results
            return []
        return _search

    def test_same_product_name_different_item_code_both_kept(self):
        results = [
            {"name": "商品A", "rakuten_title": "商品A", "item_code": "shop1:item1",
             "rakuten_link": "https://item.rakuten.co.jp/shop1/item1/", "price_ref": 1000},
            {"name": "商品A", "rakuten_title": "商品A", "item_code": "shop2:item2",
             "rakuten_link": "https://item.rakuten.co.jp/shop2/item2/", "price_ref": 1200},
        ]
        step = {"category": "化粧水", "purpose": "保湿", "ingredient_focus": ""}
        with patch("app._rakuten_criteria_search_single", side_effect=self._fake_search({"化粧水": results})):
            all_results = app.search_rakuten_for_step(step, {})
        self.assertEqual(len(all_results), 2)
        prices = sorted(r["price_ref"] for r in all_results)
        self.assertEqual(prices, [1000, 1200])

    def test_same_item_code_returned_twice_is_deduped(self):
        """同一出品(item_codeが同じ)がQ1・Q2両方の検索結果に含まれていても
        1件にまとめること(重複除去自体は維持される)。"""
        same_item = {"name": "商品A", "rakuten_title": "商品A", "item_code": "shop1:item1",
                      "rakuten_link": "https://item.rakuten.co.jp/shop1/item1/", "price_ref": 1000}
        step = {"category": "化粧水", "purpose": "保湿", "ingredient_focus": "ナイアシンアミド"}
        with patch("app._rakuten_criteria_search_single", return_value=[dict(same_item)]):
            all_results = app.search_rakuten_for_step(step, {})
        self.assertEqual(len(all_results), 1)

    def test_falls_back_to_rakuten_link_when_item_code_missing(self):
        results = [
            {"name": "商品A", "rakuten_title": "商品A", "item_code": "",
             "rakuten_link": "https://item.rakuten.co.jp/shop1/item1/", "price_ref": 1000},
            {"name": "商品A", "rakuten_title": "商品A", "item_code": "",
             "rakuten_link": "https://item.rakuten.co.jp/shop2/item2/", "price_ref": 1200},
        ]
        step = {"category": "化粧水", "purpose": "保湿", "ingredient_focus": ""}
        with patch("app._rakuten_criteria_search_single", side_effect=self._fake_search({"化粧水": results})):
            all_results = app.search_rakuten_for_step(step, {})
        self.assertEqual(len(all_results), 2)

    def test_falls_back_to_normalized_name_when_no_identifier_available(self):
        """item_code/rakuten_linkのどちらも取得できない場合のみ、従来通り
        商品名の正規化で重複除去する(安全側のフォールバック)。"""
        results = [
            {"name": "商品A", "rakuten_title": "商品A", "item_code": "", "rakuten_link": "", "price_ref": 1000},
            {"name": "商品A", "rakuten_title": "商品A", "item_code": "", "rakuten_link": "", "price_ref": 1200},
        ]
        step = {"category": "化粧水", "purpose": "保湿", "ingredient_focus": ""}
        with patch("app._rakuten_criteria_search_single", side_effect=self._fake_search({"化粧水": results})):
            all_results = app.search_rakuten_for_step(step, {})
        self.assertEqual(len(all_results), 1)


class RakutenNameJunkScoringScopeTests(unittest.TestCase):
    """_rakuten_name_junk_match_count()(商品比較表の代表名選定専用)と
    _rule_based_clean_rakuten_title()(単独タイトルの汎用クリーニング)の
    役割分離の回帰テスト。容量・数量表記は同一商品と確認できたグループ内
    でのみノイズ扱いし、単独タイトルの整形では除去しない(2026-09、
    ユーザー指摘: 容量違いが商品バリエーションそのものを区別するケースを
    壊さないため)。"""

    def test_junk_match_count_counts_capacity_and_price_appeal_terms(self):
        self.assertEqual(app._rakuten_name_junk_match_count("ピュアメデル 保湿クリーム"), 0)
        self.assertGreater(app._rakuten_name_junk_match_count("ピュアメデル 保湿クリーム 大容量"), 0)
        self.assertGreater(app._rakuten_name_junk_match_count("ピュアメデル 保湿クリーム プチプラ"), 0)
        self.assertGreater(app._rakuten_name_junk_match_count("ピュアメデル 保湿クリーム 200ml"), 0)

    def test_rule_based_title_cleaning_does_not_strip_capacity(self):
        """単独タイトルのルールベース整形は、容量表記を除去しないこと
        (200ml版・400ml版が実際に別商品として並ぶケースを壊さないため)。"""
        cleaned = app._rule_based_clean_rakuten_title("ピュアメデル 保湿クリーム 200ml")
        self.assertIn("200ml", cleaned)

    def test_rule_based_title_cleaning_still_strips_original_junk(self):
        """既存の販促語除去(送料無料等)の挙動は変更していないこと。"""
        cleaned = app._rule_based_clean_rakuten_title("送料無料 ピュアメデル 保湿クリーム")
        self.assertNotIn("送料無料", cleaned)


class ComparisonTableSameProductDifferentShopTests(unittest.TestCase):
    """preserve_ranked_top_candidates()/build_candidate_comparison_table():
    同一商品が複数の楽天ショップから別価格で出品されている場合、推薦用の
    候補(top_candidates)は従来通り1件に重複除去する一方、商品比較表
    (価格・コスパ)専用の候補リスト(_comparison_candidates)では出品
    identifier(item_code/rakuten_link)が異なれば別ショップの出品として
    別行に残すこと。2026-09、実機診断で「商品比較欄が表示されない/
    3位まで出ない」ことが確認された根本原因(推薦用の厳格な重複除去を
    比較表にもそのまま使っていたため、別ショップ出品が1件に潰れていた)
    への対応の回帰テスト。"""

    def test_recommendation_list_still_dedupes_same_product_different_shop(self):
        """推薦用top_candidatesの重複除去基準は変更しないこと。"""
        step = {
            "category": "化粧水", "purpose": "保湿", "product": "", "brand": "",
            "top_candidates": [
                _real_candidate("商品A", 1000, "shop1:item1", score=90),
                _real_candidate("商品A", 1200, "shop2:item2", score=88),
                _real_candidate("商品B", 1500, "shop3:item3", score=60),
            ],
        }
        result = app.finalize_step_data(step, {})
        names = [c["name"] for c in result["top_candidates"]]
        self.assertEqual(names, ["商品A", "商品B"])

    def test_comparison_table_shows_same_product_from_different_shops_separately(self):
        step = {
            "category": "化粧水", "purpose": "保湿", "product": "", "brand": "",
            "top_candidates": [
                _real_candidate("商品A", 1000, "shop1:item1", score=90),
                _real_candidate("商品A", 1200, "shop2:item2", score=88),
                _real_candidate("商品B", 1500, "shop3:item3", score=60),
            ],
        }
        result = app.finalize_step_data(step, {})
        rows = [(r["name"], r["price"]) for r in result["candidate_comparison_table"]]
        self.assertEqual(len(rows), 3)
        self.assertIn(("商品A", 1000), rows)
        self.assertIn(("商品A", 1200), rows)
        self.assertIn(("商品B", 1500), rows)

    def test_comparison_table_dedupes_identical_listing_id_only_once(self):
        """同一出品(item_codeが同じ)が候補プールに重複して入っていても、
        比較表では1件にまとめること(無限に行が増えない)。"""
        step = {
            "category": "化粧水", "purpose": "保湿", "product": "", "brand": "",
            "top_candidates": [
                _real_candidate("商品A", 1000, "shop1:item1", score=90),
                _real_candidate("商品A", 1000, "shop1:item1", score=90),
                _real_candidate("商品B", 1500, "shop3:item3", score=60),
            ],
        }
        result = app.finalize_step_data(step, {})
        rows = [(r["name"], r["price"]) for r in result["candidate_comparison_table"]]
        self.assertEqual(len(rows), 2)

    def test_comparison_table_falls_back_to_name_price_when_no_listing_id(self):
        """item_code/rakuten_linkが無い候補(db由来等)は、保守的にブランド+
        商品名+価格で判定する(同名同価格は1件、同名でも価格が違えば別行)。"""
        step = {
            "category": "化粧水", "purpose": "保湿", "product": "", "brand": "",
            "top_candidates": [
                {"name": "商品A", "brand": "テストブランド", "source": "db", "price_ref": 1000,
                 "score": 90, "base_score": 90, "improve_score": 0, "routine_score": 0,
                 "active_ingredients": [], "main_functions": []},
                {"name": "商品A", "brand": "テストブランド", "source": "db", "price_ref": 1000,
                 "score": 90, "base_score": 90, "improve_score": 0, "routine_score": 0,
                 "active_ingredients": [], "main_functions": []},
                {"name": "商品B", "brand": "テストブランド2", "source": "db", "price_ref": 1500,
                 "score": 60, "base_score": 60, "improve_score": 0, "routine_score": 0,
                 "active_ingredients": [], "main_functions": []},
            ],
        }
        result = app.finalize_step_data(step, {})
        rows = [(r["name"], r["price"]) for r in result["candidate_comparison_table"]]
        self.assertEqual(len(rows), 2)  # 同名同価格の1・2番目は1件にまとめられる

    def test_comparison_rank_one_stays_synced_with_recommendation_after_name_clean(self):
        """商品比較表の1位は、gemini_clean_rakuten_product_names()による
        商品名整形後もstep["product"](推薦用1位)と同じ名前になること
        (_comparison_candidates[0]がtop_candidates[0]と同一dictオブジェクトを
        共有する設計の回帰テスト)。"""
        raw_title = "59まで！【公式】オルナオーガニック【楽天】化粧水"
        step = {
            "category": "化粧水", "purpose": "保湿", "product": raw_title, "brand": "",
            "product_source": "rakuten_criteria", "rakuten_title": raw_title,
            "top_candidates": [
                _real_candidate(raw_title, 1000, "shop1:item1", score=90),
                _real_candidate("競合品", 1500, "shop3:item3", score=60),
            ],
        }
        result = app.finalize_step_data(step, {})
        data = {"morning": {"steps": [result]}, "night": {"steps": []}, "weekly_care": []}
        with patch("app.call_gemini_with_retry",
                   return_value=_FakeGeminiResponse("1. オルナオーガニック 化粧水")):
            data = app.gemini_clean_rakuten_product_names(data)
        step_after = data["morning"]["steps"][0]
        app._refresh_candidate_comparison_after_swap(step_after, {})
        rank1_row = step_after["candidate_comparison_table"][0]
        self.assertNotIn("59まで", rank1_row["name"])
        self.assertEqual(rank1_row["name"], step_after["top_candidates"][0]["name"])

    def test_comparison_rank_two_gets_cleaned_independently(self):
        """比較表専用リストの2位(推薦用top_candidatesには無い、別ショップ
        出品)も、gemini_clean_rakuten_product_names()の対象に含まれること。
        推薦用の重複除去(名前一致)で1件に潰れるよう、2つの候補には
        あえて全く同じ生タイトルを与える(別ショップの出品が同じ楽天生
        タイトルになるのはよくあるケース)。"""
        raw_title = "59まで！【公式】オルナオーガニック【楽天】化粧水"
        step = {
            "category": "化粧水", "purpose": "保湿", "product": raw_title, "brand": "",
            "product_source": "rakuten_criteria", "rakuten_title": raw_title,
            "top_candidates": [
                _real_candidate(raw_title, 1000, "shop1:item1", score=90),
                _real_candidate(raw_title, 1200, "shop2:item2", score=89),
            ],
        }
        result = app.finalize_step_data(step, {})
        # 事前条件: 推薦用は1件に重複除去され、比較表専用は2件残っている
        self.assertEqual(len(result["top_candidates"]), 1)
        self.assertEqual(len(result["candidate_comparison_table"]), 2)

        data = {"morning": {"steps": [result]}, "night": {"steps": []}, "weekly_care": []}
        with patch(
            "app.call_gemini_with_retry",
            return_value=_FakeGeminiResponse("1. オルナオーガニック 化粧水\n2. オルナオーガニック 化粧水"),
        ):
            data = app.gemini_clean_rakuten_product_names(data)
        step_after = data["morning"]["steps"][0]
        app._refresh_candidate_comparison_after_swap(step_after, {})
        rows = step_after["candidate_comparison_table"]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertNotIn("59まで", row["name"])

    def test_refresh_discards_stale_comparison_candidates_when_rank_one_swapped(self):
        """_try_rakuten_fallback_candidateによる1位差し替え後は、古い
        _comparison_candidates(差し替え前の1位を前提に構築)を破棄し、
        新しいtop_candidatesだけで再計算すること(1位のすり替わりを防ぐ)。"""
        step = {
            "category": "化粧水", "purpose": "保湿", "product": "", "brand": "",
            "top_candidates": [
                _real_candidate("商品A", 1000, "shop1:item1", score=90),
                _real_candidate("商品A", 1200, "shop2:item2", score=88),
                _real_candidate("商品C", 1500, "shop3:item3", score=60),
            ],
        }
        result = app.finalize_step_data(step, {})
        self.assertIn("_comparison_candidates", result)

        # 1位差し替え(_try_rakuten_fallback_candidateが行う操作を模す)
        swapped = result["top_candidates"][1]
        result["top_candidates"] = [swapped] + [
            c for c in result["top_candidates"] if c is not swapped
        ]
        app._refresh_candidate_comparison_after_swap(result, {})
        self.assertNotIn("_comparison_candidates", result)
        self.assertEqual(
            result["candidate_comparison_table"][0]["name"],
            result["top_candidates"][0]["name"],
        )

    def test_refresh_keeps_comparison_candidates_when_rank_one_unchanged(self):
        """1位が変わっていない通常のリフレッシュ(商品名整形後の再計算等)
        では、_comparison_candidatesを保持し続けること(比較表の複数ショップ
        表示が失われないこと)。"""
        step = {
            "category": "化粧水", "purpose": "保湿", "product": "", "brand": "",
            "top_candidates": [
                _real_candidate("商品A", 1000, "shop1:item1", score=90),
                _real_candidate("商品A", 1200, "shop2:item2", score=88),
                _real_candidate("商品C", 1500, "shop3:item3", score=60),
            ],
        }
        result = app.finalize_step_data(step, {})
        app._refresh_candidate_comparison_after_swap(result, {})
        self.assertIn("_comparison_candidates", result)
        self.assertEqual(len(result["candidate_comparison_table"]), 3)

    def test_comparison_table_unifies_display_name_for_same_product_different_shops(self):
        """同一商品が複数ショップから出品されている場合、各行のクリーニング
        結果が不揃い(一方は「ブランド 商品名」のみ、もう一方に「大容量」
        「プチプラ」等の販促語が残る)になっても、商品比較表では両行とも
        最も残存ノイズが少ない名前へ統一されること(2026-09、ユーザー
        指摘: 同じ商品なのに表示の詳しさが違って見える不具合)。
        価格・item_code等の出品固有データは変更しないこと。"""
        raw_title_a = "【公式】ピュアメデル 保湿クリーム 大容量 プチプラ"
        raw_title_b = "ピュアメデル 保湿クリーム"
        step = {
            "category": "化粧水", "purpose": "保湿", "product": raw_title_a, "brand": "",
            "product_source": "rakuten_criteria", "rakuten_title": raw_title_a,
            "top_candidates": [
                _real_candidate(raw_title_a, 1000, "shop1:item1", score=90),
                _real_candidate(raw_title_b, 1200, "shop2:item2", score=89),
            ],
        }
        result = app.finalize_step_data(step, {})
        self.assertEqual(len(result["candidate_comparison_table"]), 2)

        data = {"morning": {"steps": [result]}, "night": {"steps": []}, "weekly_care": []}
        with patch(
            "app.call_gemini_with_retry",
            # Geminiの整形結果自体が不揃い(1行目に「大容量」が残存)な
            # ケースを再現する。
            return_value=_FakeGeminiResponse(
                "1. ピュアメデル 保湿クリーム 大容量\n2. ピュアメデル 保湿クリーム"
            ),
        ):
            data = app.gemini_clean_rakuten_product_names(data)
        step_after = data["morning"]["steps"][0]
        app._refresh_candidate_comparison_after_swap(step_after, {})
        rows = step_after["candidate_comparison_table"]
        self.assertEqual(len(rows), 2)
        names = {row["name"] for row in rows}
        self.assertEqual(names, {"ピュアメデル 保湿クリーム"}, "両行とも残存ノイズの少ない方へ統一されること")
        prices = sorted(row["price"] for row in rows)
        self.assertEqual(prices, [1000, 1200], "価格等の出品固有データは変更しないこと")

    def test_comparison_table_not_discarded_when_initial_rank_one_mismatches(self):
        """比較表専用リストと推薦リストの1位が初回構築時に食い違っても
        (識別キーが空になる候補が比較表側の1位に混入する等)、比較表を
        丸ごと破棄せず、1位だけ表示側に揃えて残りの実売候補は活かすこと。

        2026-09、実機診断で「実売候補は5〜6件あるはずなのに商品比較表が
        全く表示されない」ことが報告され、原因はこの1位不一致検知時の
        「安全側でリスト全体を破棄する」設計にあった(候補が少ないのでは
        なく、推薦リストへフォールバックして商品比較専用リストが持つ
        追加の実売候補・別ショップ出品を失っていた)。

        「・-ー」のような記号のみの商品名はbuild_candidate_identity_keys()が
        空集合を返すため推薦リストから除外されるが、item_codeを持つため
        比較表専用リストには残り、たまたま1位に来ると不一致を起こす
        (raw_candidatesの並び順への依存を再現する意図的な構成)。
        """
        step = {
            "category": "化粧水", "purpose": "保湿", "product": "", "brand": "",
            "top_candidates": [
                _real_candidate("・-ー", 999, "weird1", score=95),
                _real_candidate("商品B", 1500, "shop1:item2", score=60),
                _real_candidate("商品B", 1600, "shop2:item2b", score=59),
                _real_candidate("商品C", 2000, "item3", score=55),
            ],
        }
        result = app.finalize_step_data(step, {})
        # 推薦リストからは記号のみの名前が除外され、実在の2商品が残る。
        self.assertEqual(
            [c["name"] for c in result["top_candidates"]], ["商品B", "商品C"]
        )
        # 比較表は「1位不一致だから全部破棄」ではなく、1位を推薦リストの
        # 1位(商品B)へ揃えた上で、商品Bの別ショップ出品(1600円)と商品Cを
        # 失わずに保持すること。
        rows = [(r["name"], r["price"]) for r in result["candidate_comparison_table"]]
        self.assertEqual(rows, [("商品B", 1500), ("商品B", 1600), ("商品C", 2000)])

    def test_comparison_table_skips_virtual_candidates_ranked_above_real_ones(self):
        """build_candidate_comparison_table()は「上位3件に絞ってから実売
        判定」ではなく「全件から実売候補に絞ってから上位3件を取る」順序で
        あること。

        2026-09、実機診断で各stepのcomparison_real_priced(実売候補数)が
        3件以上あるにもかかわらず商品比較表が全く表示されない事例が
        報告された。原因は、ランキング上位3件の中にai_virtual(非実売)
        候補が混ざっていると、実売候補が全体で何件あってもテーブルには
        上位3枠の中の実売分しか反映されず、1件以下になって非表示になる
        ことだった。この回帰テストは、2位・3位がai_virtualでも、
        4位・5位の実売候補がテーブルに繰り上がることを確認する。
        """
        top_candidates = [
            _real_candidate("商品A", 1000, "item1", score=95),
            {"name": "仮想候補B", "brand": "テストブランド", "source": "ai_virtual",
             "price_ref": 0, "score": 90},
            {"name": "仮想候補C", "brand": "テストブランド", "source": "ai_virtual",
             "price_ref": 0, "score": 85},
            _real_candidate("商品D", 1500, "item4", score=80),
            _real_candidate("商品E", 2000, "item5", score=75),
        ]
        rows = app.build_candidate_comparison_table(top_candidates)
        names = [r["name"] for r in rows]
        self.assertEqual(names, ["商品A", "商品D", "商品E"])
        self.assertGreater(len(rows), 1, "実売候補が3件以上あるのにテーブルが1件以下になっている")


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
