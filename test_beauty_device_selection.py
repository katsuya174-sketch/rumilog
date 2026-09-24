"""
美容機器(select_best_beauty_device_candidate / _build_device_selection_reason)の
テスト。

対応する依頼内容:
- 案B(候補プールの上位を保持し、比較理由を新規フィールドとして返す)の実装確認
- 2位以下を保持しても選ばれる1位商品が従来と変わらないこと
- 楽天API呼び出しは一切増えないこと(この関数はfetch_rakuten_candidatesを
  一切呼ばない=引数のscored_itemsのみを使うことで担保する)
- 製品選択理由が実際の候補データと一致し、優位でない項目を優位と書かないこと
- データ不足(候補1件のみ)でも捏造せず安全にフォールバックすること
- category-level reason(enrich_beauty_devices)と製品選択理由が別物であること
"""
import app as app_module


def make_item(name, price, review_count, review_avg, code, caption=""):
    return {
        "itemName": name,
        "itemCaption": caption,
        "itemPrice": price,
        "reviewCount": review_count,
        "reviewAverage": review_avg,
        "itemCode": code,
    }


def test_winner_unchanged_when_runner_ups_are_kept():
    """2位・3位を保持しても、勝者(1位)は従来と同じ1件になること。"""
    scored_items = [
        (50, make_item("RF美顔器A", 8000, 120, 4.5, "A")),
        (40, make_item("RF美顔器B", 7000, 300, 4.8, "B")),
        (30, make_item("RF美顔器C", 6000, 10, 3.0, "C")),
    ]
    user_data = {"sensitivity": "low"}
    best_item, reason = app_module.select_best_beauty_device_candidate(
        scored_items, "RF", user_data, budget_value=10000
    )
    assert best_item["itemCode"] == "A"
    assert isinstance(reason, str) and reason


def test_no_rakuten_api_call_is_made_inside_selection():
    """select_best_beauty_device_candidate()はfetch_rakuten_candidates等の
    楽天API呼び出しを一切行わず、渡されたscored_itemsのみで完結すること
    (2位・3位を保持しても追加の楽天API呼び出しが発生しないことの根拠)。"""
    called = {"count": 0}

    def _fail_if_called(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("select_best_beauty_device_candidate should never call Rakuten API")

    original = app_module.fetch_rakuten_candidates
    app_module.fetch_rakuten_candidates = _fail_if_called
    try:
        scored_items = [
            (50, make_item("RF美顔器A", 8000, 120, 4.5, "A")),
            (40, make_item("RF美顔器B", 7000, 300, 4.8, "B")),
        ]
        best_item, reason = app_module.select_best_beauty_device_candidate(
            scored_items, "RF", {"sensitivity": "low"}, budget_value=10000
        )
        assert best_item is not None
        assert called["count"] == 0
    finally:
        app_module.fetch_rakuten_candidates = original


def test_selection_reason_reflects_actual_review_average_advantage():
    """勝者のレビュー評価が他候補より本当に高い場合のみ、その事実を含めること。"""
    scored_items = [
        (50, make_item("RF美顔器A", 8000, 100, 4.9, "A")),
        (50, make_item("RF美顔器B", 8000, 100, 3.0, "B")),
    ]
    best_item, reason = app_module.select_best_beauty_device_candidate(
        scored_items, "RF", {"sensitivity": "low"}, budget_value=0
    )
    assert best_item["itemCode"] == "A"
    assert "4.9" in reason
    assert "レビュー評価" in reason


def test_selection_reason_reflects_actual_price_advantage():
    """勝者の価格が他候補より本当に安い場合のみ、その事実を含めること。"""
    scored_items = [
        (50, make_item("RF美顔器A", 5000, 100, 4.0, "A")),
        (10, make_item("RF美顔器B", 9000, 100, 4.0, "B")),
    ]
    best_item, reason = app_module.select_best_beauty_device_candidate(
        scored_items, "RF", {"sensitivity": "low"}, budget_value=0
    )
    assert best_item["itemCode"] == "A"
    assert "価格" in reason


def test_selection_reason_does_not_claim_advantage_on_tie():
    """全評価項目が同点の場合、優位性を主張せず中立的なフォールバック文言になること
    (同点の項目を優位と表現しない)。"""
    scored_items = [
        (50, make_item("RF美顔器A", 8000, 100, 4.0, "A")),
        (50, make_item("RF美顔器B", 8000, 100, 4.0, "B")),
    ]
    best_item, reason = app_module.select_best_beauty_device_candidate(
        scored_items, "RF", {"sensitivity": "low"}, budget_value=0
    )
    assert reason == app_module._DEVICE_SELECTION_REASON_FALLBACK
    assert "他候補より" not in reason


def test_selection_reason_never_fabricates_feature_not_in_data():
    """楽天のitemName/itemCaptionに存在しない機能・性能の語(例: 防水)を
    自由生成しないこと。"""
    scored_items = [
        (50, make_item("RF美顔器A", 8000, 200, 4.9, "A", caption="毎日のケアに")),
        (40, make_item("RF美顔器B", 9000, 50, 3.5, "B", caption="持ち運びに便利")),
    ]
    best_item, reason = app_module.select_best_beauty_device_candidate(
        scored_items, "RF", {"sensitivity": "low"}, budget_value=0
    )
    for forbidden in ("防水", "コードレス", "持ち運び", "軽量"):
        assert forbidden not in reason


def test_selection_reason_fallback_when_only_one_candidate():
    """候補が1件しかない場合は比較できないため、優位性を主張しない
    中立的なフォールバック文言になること(データ不足時の安全なフォールバック)。"""
    scored_items = [(50, make_item("RF美顔器A", 8000, 100, 4.0, "A"))]
    best_item, reason = app_module.select_best_beauty_device_candidate(
        scored_items, "RF", {"sensitivity": "low"}, budget_value=0
    )
    assert best_item["itemCode"] == "A"
    assert reason == app_module._DEVICE_SELECTION_REASON_FALLBACK


def test_selection_reason_empty_pool_returns_none_and_empty_reason():
    best_item, reason = app_module.select_best_beauty_device_candidate(
        [], "RF", {"sensitivity": "low"}, budget_value=0
    )
    assert best_item is None
    assert reason == ""


def test_sensitivity_intensity_penalty_reflected_in_reason():
    """敏感肌ユーザーで、勝者に「業務用」「高出力」等の強度語が無く、
    次点候補にはある場合のみ、その事実を理由に含めること。"""
    scored_items = [
        (50, make_item("RF美顔器A", 8000, 100, 4.0, "A", caption="やさしい使い心地")),
        (50, make_item("RF美顔器B", 8000, 100, 4.0, "B", caption="業務用の高出力設計")),
    ]
    best_item, reason = app_module.select_best_beauty_device_candidate(
        scored_items, "RF", {"sensitivity": "high"}, budget_value=0
    )
    assert best_item["itemCode"] == "A"
    assert "業務用" in reason or "高出力" in reason


def test_category_level_reason_and_selection_reason_are_independent_fields():
    """enrich_beauty_devices()が持つcategory-level reason(なぜこの方式を
    選んだか)と、select_best_beauty_device_candidate()が返す
    device_selection_reason(なぜこの製品を選んだか)は別フィールドとして
    共存し、一方が他方を上書きしないこと。"""
    data = {"beauty_devices": [{"device_type": "RF", "reason": "毛穴・たるみ改善に有効なため"}]}
    enriched = app_module.enrich_beauty_devices(data, {})
    step = enriched["beauty_devices"][0]
    assert step["reason"] == "毛穴・たるみ改善に有効なため"

    scored_items = [
        (50, make_item("RF美顔器A", 5000, 100, 4.5, "A")),
        (10, make_item("RF美顔器B", 9000, 50, 3.0, "B")),
    ]
    best_item, selection_reason = app_module.select_best_beauty_device_candidate(
        scored_items, "RF", {"sensitivity": "low"}, budget_value=0
    )
    step["device_selection_reason"] = selection_reason

    assert step["reason"] == "毛穴・たるみ改善に有効なため"
    assert step["device_selection_reason"] != step["reason"]
    assert step["device_selection_reason"]


# =========================================================
# 美容機器の商品固有特徴抽出(_extract_device_appeal_features)
# ランキング(_fit_score)には未反映。説明文のみで使用する。
# =========================================================

def test_extracts_feature_from_item_caption():
    item = make_item("RF美顔器X", 8000, 100, 4.0, "X", caption="毛穴の黒ずみが気になる方に")
    features = app_module._extract_device_appeal_features(item)
    assert "毛穴ケア" in features


def test_extracts_feature_from_item_name():
    item = make_item("ハリ・たるみ改善 RF美顔器Y", 8000, 100, 4.0, "Y")
    features = app_module._extract_device_appeal_features(item)
    assert "ハリ補給" in features


def test_synonyms_normalize_to_same_label():
    """「うるおい」「潤い」「保湿」はいずれも同じラベル(乾燥対策)へ正規化されること。"""
    for word in ["うるおい", "潤い", "保湿", "乾燥"]:
        item = make_item("美顔器", 8000, 100, 4.0, "Z", caption=f"{word}ケアに")
        features = app_module._extract_device_appeal_features(item)
        assert features == ["乾燥対策"], f"{word!r} が正規化されていない: {features}"


def test_does_not_fabricate_feature_not_in_description():
    item = make_item("シンプル美顔器", 8000, 100, 4.0, "W", caption="毎日のケアに")
    features = app_module._extract_device_appeal_features(item)
    assert features == []


def test_unrelated_beauty_marketing_terms_are_not_extracted():
    """「小顔」等、現在の肌診断項目に対応しない訴求語は抽出対象にしないこと。"""
    item = make_item("小顔美顔器プレミアム", 8000, 100, 4.0, "V", caption="小顔効果・美肌効果")
    features = app_module._extract_device_appeal_features(item)
    assert features == []


def test_extraction_is_deterministic_for_same_input():
    item = make_item("毛穴・くすみケア RF美顔器", 8000, 100, 4.0, "U", caption="ハリも意識した処方")
    results = {tuple(app_module._extract_device_appeal_features(item)) for _ in range(20)}
    assert len(results) == 1


def test_device_selection_reason_includes_confirmed_feature_wording():
    """特徴を含める場合、断定的な効果表現ではなく「記載を確認できます」という
    範囲を超えない表現になること。"""
    scored_items = [
        (50, make_item("毛穴ケアRF美顔器", 8000, 200, 4.9, "A", caption="毛穴の目立ちが気になる方に")),
        (40, make_item("シンプルRF美顔器", 7000, 50, 3.5, "B")),
    ]
    best_item, reason = app_module.select_best_beauty_device_candidate(
        scored_items, "RF", {"sensitivity": "low"}, budget_value=10000,
    )
    assert best_item["itemCode"] == "A"
    assert "毛穴ケア" in reason
    assert "記載を確認できます" in reason
    assert "効果がある" not in reason
    assert "改善します" not in reason


def test_device_selection_reason_cites_category_context_without_overriding_it():
    """category_purpose/category_reasonを渡した場合、既存のcategory-level
    reasonをそのまま引用し、新たな判定を行わないこと。"""
    scored_items = [
        (50, make_item("RF美顔器A", 8000, 100, 4.0, "A")),
        (40, make_item("RF美顔器B", 7000, 50, 3.0, "B")),
    ]
    best_item, reason = app_module.select_best_beauty_device_candidate(
        scored_items, "RF", {"sensitivity": "low"}, budget_value=0,
        category_purpose="ハリ・たるみの引き締めケア",
        category_reason="たるみ・ハリ不足の改善に有効なため",
    )
    assert "たるみ・ハリ不足の改善に有効なため" in reason


def test_review_and_price_logic_unchanged_when_features_present():
    """特徴抽出を追加しても、既存のレビュー/価格比較ロジック自体(_fit_scoreの
    ランキング)は変更されていないこと(勝者・比較文の同時共存を確認)。"""
    scored_items = [
        (50, make_item("毛穴ケアRF美顔器A", 8000, 300, 4.9, "A", caption="毛穴ケアに")),
        (40, make_item("毛穴ケアRF美顔器B", 9000, 50, 3.0, "B", caption="毛穴ケアに")),
    ]
    best_item, reason = app_module.select_best_beauty_device_candidate(
        scored_items, "RF", {"sensitivity": "low"}, budget_value=0,
    )
    assert best_item["itemCode"] == "A"
    assert "レビュー評価" in reason
    assert "レビュー件数" in reason


def test_extraction_does_not_affect_ranking_winner():
    """特徴抽出は勝者選定(_fit_score/_sort_key)に一切影響しないこと。
    レビュー・価格で劣るが特徴語が多い候補が不当に繰り上がらないこと。"""
    scored_items = [
        (50, make_item("シンプルRF美顔器A", 8000, 300, 4.9, "A")),
        (40, make_item("毛穴・ハリ・乾燥・皮脂・赤み・ニキビ・キメ・くすみ全部入りRF美顔器B", 8000, 10, 2.0, "B",
                        caption="毛穴 ハリ 乾燥 皮脂 赤み ニキビ キメ くすみ")),
    ]
    best_item, reason = app_module.select_best_beauty_device_candidate(
        scored_items, "RF", {"sensitivity": "low"}, budget_value=0,
    )
    assert best_item["itemCode"] == "A"
