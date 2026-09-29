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


def test_selection_reason_does_not_cite_review_advantage_without_concern_match():
    """肌悩み適合が決定的でない場合、レビュー評価の優位を選定理由として
    書かないこと(2026-09、実機診断で「価格が抑えられている」等の商流上の
    指標だけを理由にしている、という指摘を繰り返し受けたための修正)。"""
    scored_items = [
        (50, make_item("RF美顔器A", 8000, 100, 4.9, "A")),
        (50, make_item("RF美顔器B", 8000, 100, 3.0, "B")),
    ]
    best_item, reason = app_module.select_best_beauty_device_candidate(
        scored_items, "RF", {"sensitivity": "low"}, budget_value=0
    )
    assert best_item["itemCode"] == "A"
    assert reason == app_module._DEVICE_SELECTION_REASON_FALLBACK
    assert "レビュー評価" not in reason


def test_selection_reason_does_not_cite_price_advantage_without_concern_match():
    """肌悩み適合が決定的でない場合、価格の優位を選定理由として書かない
    こと(前回指摘: 価格が抑えられてただけでは選定理由にしない)。"""
    scored_items = [
        (50, make_item("RF美顔器A", 5000, 100, 4.0, "A")),
        (10, make_item("RF美顔器B", 9000, 100, 4.0, "B")),
    ]
    best_item, reason = app_module.select_best_beauty_device_candidate(
        scored_items, "RF", {"sensitivity": "low"}, budget_value=0
    )
    assert best_item["itemCode"] == "A"
    assert reason == app_module._DEVICE_SELECTION_REASON_FALLBACK
    assert "価格" not in reason


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


def test_device_selection_reason_does_not_cite_feature_mention_without_concern_match():
    """肌悩み適合が決定的でない場合、「商品説明に記載がある」だけを選定
    理由として書かないこと(前回指摘: 商品説明にその改善項目が記載されて
    いただけでは不十分。ただ選ばれればいいというものではない)。"""
    scored_items = [
        (50, make_item("毛穴ケアRF美顔器", 8000, 200, 4.9, "A", caption="毛穴の目立ちが気になる方に")),
        (40, make_item("シンプルRF美顔器", 7000, 50, 3.5, "B")),
    ]
    best_item, reason = app_module.select_best_beauty_device_candidate(
        scored_items, "RF", {"sensitivity": "low"}, budget_value=10000,
    )
    assert best_item["itemCode"] == "A"
    assert reason == app_module._DEVICE_SELECTION_REASON_FALLBACK
    assert "記載を確認できます" not in reason
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


def test_review_and_price_not_cited_when_concern_data_absent():
    """concernsが未選択で肌悩み適合の判定材料自体が無い場合、レビュー・価格を
    選定理由として書かず中立的なフォールバックになること。"""
    scored_items = [
        (50, make_item("毛穴ケアRF美顔器A", 8000, 300, 4.9, "A", caption="毛穴ケアに")),
        (40, make_item("毛穴ケアRF美顔器B", 9000, 50, 3.0, "B", caption="毛穴ケアに")),
    ]
    best_item, reason = app_module.select_best_beauty_device_candidate(
        scored_items, "RF", {"sensitivity": "low"}, budget_value=0,
    )
    assert best_item["itemCode"] == "A"
    assert reason == app_module._DEVICE_SELECTION_REASON_FALLBACK
    assert "レビュー評価" not in reason
    assert "レビュー件数" not in reason


def test_extraction_does_not_affect_ranking_winner():
    """診断で優先対象となった肌悩み(concerns)が未選択の場合、特徴抽出そのもの
    は勝者選定(_fit_score/_sort_key)に影響しないこと。レビュー・価格で劣る
    が特徴語(=今回優先していない訴求)が多いだけの候補が不当に繰り上がら
    ないこと(肌悩み適合ボーナスは診断が実際に優先した悩みとの一致でのみ
    加点される。ボーナスがランキングを動かすケースは
    test_concern_match_changes_ranking_outcome を参照)。"""
    scored_items = [
        (50, make_item("シンプルRF美顔器A", 8000, 300, 4.9, "A")),
        (40, make_item("毛穴・ハリ・乾燥・皮脂・赤み・ニキビ・キメ・くすみ全部入りRF美顔器B", 8000, 10, 2.0, "B",
                        caption="毛穴 ハリ 乾燥 皮脂 赤み ニキビ キメ くすみ")),
    ]
    best_item, reason = app_module.select_best_beauty_device_candidate(
        scored_items, "RF", {"sensitivity": "low"}, budget_value=0,
    )
    assert best_item["itemCode"] == "A"


# === 肌悩み適合ボーナス(_fit_score反映、前回承認分) ===

def test_user_priority_feature_labels_bridges_form_concerns_to_device_labels():
    """get_user_concern_tags()(スキンケア側score_productと同じ信号源)を
    _extract_device_appeal_features()と同じラベル空間へ変換できること。"""
    labels = app_module._user_priority_feature_labels({"concerns": ["pores", "dryness"]})
    assert labels == {"毛穴ケア", "乾燥対策", "バリア強化"}


def test_user_priority_feature_labels_empty_when_no_concerns():
    assert app_module._user_priority_feature_labels({}) == set()
    assert app_module._user_priority_feature_labels({"concerns": []}) == set()
    assert app_module._user_priority_feature_labels(None) == set()


def test_no_description_item_earns_no_concern_bonus_source():
    """商品説明が空の場合、特徴が抽出されず(=加点対象0)であること。"""
    item = make_item("", 8000, 200, 4.0, "EMPTY", caption="")
    assert app_module._extract_device_appeal_features(item) == []


def test_unrelated_marketing_terms_do_not_earn_concern_bonus_source():
    """「小顔」等は_DEVICE_FEATURE_KEYWORDSに含まれないため、診断で優先した
    肌悩みがあっても一致せず加点対象にならないこと。"""
    item = make_item("小顔RF美顔器B", 8000, 200, 4.0, "B", caption="小顔効果 全身美肌")
    priority_labels = app_module._user_priority_feature_labels({"concerns": ["pores"]})
    features = app_module._extract_device_appeal_features(item)
    assert features == []
    assert (set(features) & priority_labels) == set()


def test_concern_match_changes_ranking_outcome():
    """他の評価軸(score/review/price)が完全に同一でも、肌悩み適合ボーナスの
    有無だけでランキングの勝者が変わること。"""
    matched_item = make_item(
        "毛穴乾燥ケアRF美顔器AAA", 8000, 100, 4.0, "AAA", caption="毛穴 乾燥",
    )
    unmatched_item = make_item(
        "シンプルRF美顔器ZZZ", 8000, 100, 4.0, "ZZZ",
    )
    scored_items = [(50, matched_item), (50, unmatched_item)]

    # concern未選択なら他条件が同一のため、concernボーナスなしのタイブレークで
    # ZZZが勝つ(=AAAがボーナスなしでは勝てないことの確認)。
    winner_without_bonus, _ = app_module.select_best_beauty_device_candidate(
        scored_items, "RF", {"sensitivity": "low"}, budget_value=0,
    )
    assert winner_without_bonus["itemCode"] == "ZZZ"

    # concernを選択すると、AAAが2特徴一致(+16)で逆転して勝つ。
    winner_with_bonus, reason = app_module.select_best_beauty_device_candidate(
        scored_items, "RF", {"sensitivity": "low", "concerns": ["pores", "dryness"]},
        budget_value=0,
    )
    assert winner_with_bonus["itemCode"] == "AAA"


def test_concern_match_caps_at_two_features_and_sixteen_points():
    """3特徴一致でも加点は最大2特徴・+16点で頭打ちになること。頭打ちがなければ
    3特徴一致のTHREEが勝つはずのレビュー件数差を用いて検証する。"""
    user_data = {"sensitivity": "low", "concerns": ["pores", "dryness", "redness"]}
    two_match_item = make_item(
        "2特徴一致RF美顔器TWO", 8000, 200, 4.0, "TWO", caption="毛穴 乾燥",
    )
    three_match_item = make_item(
        "3特徴一致RF美顔器THREE", 8000, 100, 4.0, "THREE", caption="毛穴 乾燥 赤み",
    )
    scored_items = [(50, two_match_item), (50, three_match_item)]
    best_item, _ = app_module.select_best_beauty_device_candidate(
        scored_items, "RF", user_data, budget_value=0,
    )
    assert best_item["itemCode"] == "TWO"


def test_concern_match_does_not_double_count_synonyms():
    """同じ意味の同義語を商品説明内に何度書いていても1特徴・+8点のみである
    こと。重複加点されていればSYNが勝つはずのレビュー件数差を用いて検証する。"""
    user_data = {"sensitivity": "low", "concerns": ["dryness"]}
    synonym_heavy_item = make_item(
        "乾燥ケアRF美顔器SYN", 8000, 100, 4.0, "SYN", caption="乾燥 保湿 うるおい 潤い",
    )
    single_mention_item = make_item(
        "乾燥ケアRF美顔器SINGLE", 8000, 150, 4.0, "SINGLE", caption="乾燥",
    )
    scored_items = [(50, synonym_heavy_item), (50, single_mention_item)]
    best_item, _ = app_module.select_best_beauty_device_candidate(
        scored_items, "RF", user_data, budget_value=0,
    )
    assert best_item["itemCode"] == "SINGLE"


def test_concern_match_tied_falls_back_to_neutral_reason():
    """肌悩み一致数が候補間で同じ(決定的でない)場合、肌悩み適合を理由に
    書かないのはもちろん、レビュー・価格も選定理由として書かず中立的な
    フォールバックになること(前回指摘: 商流上の指標を主理由にしない)。"""
    user_data = {"sensitivity": "low", "concerns": ["pores"]}
    item_a = make_item("毛穴ケアRF美顔器A", 8000, 300, 4.9, "A", caption="毛穴ケアに")
    item_b = make_item("毛穴ケアRF美顔器B", 9000, 50, 3.0, "B", caption="毛穴ケアに")
    scored_items = [(50, item_a), (40, item_b)]
    best_item, reason = app_module.select_best_beauty_device_candidate(
        scored_items, "RF", user_data, budget_value=0,
    )
    assert best_item["itemCode"] == "A"
    assert reason == app_module._DEVICE_SELECTION_REASON_FALLBACK
    assert "肌悩みとの一致項目が多かったため優先しました" not in reason
    assert "レビュー評価" not in reason
    assert "レビュー件数" not in reason


def test_device_selection_reason_leads_with_concern_match_when_decisive():
    """肌悩み適合ボーナスが順位差に決定的に寄与した場合、その説明が実際に
    一致した特徴名とともにレビュー・価格より先に述べられること。"""
    user_data = {"sensitivity": "low", "concerns": ["pores", "dryness"]}
    matched_item = make_item(
        "毛穴乾燥ケアRF美顔器A", 6000, 50, 3.0, "A", caption="毛穴 乾燥ケアに",
    )
    unmatched_item = make_item(
        "レビュー高評価RF美顔器B", 8000, 100, 3.5, "B",
    )
    scored_items = [(50, matched_item), (50, unmatched_item)]
    best_item, reason = app_module.select_best_beauty_device_candidate(
        scored_items, "RF", user_data, budget_value=0,
    )
    assert best_item["itemCode"] == "A"
    assert "毛穴ケア" in reason
    assert "乾燥対策" in reason
    assert "肌悩みとの一致項目が多かったため優先しました" in reason
    assert "効果がある" not in reason
    assert "改善します" not in reason
    concern_idx = reason.index("肌悩みとの一致項目が多かったため優先しました")
    addend_idx = reason.index("加えて")
    assert concern_idx < addend_idx
    assert "価格が候補内で最も抑えられている" in reason


# =========================================================
# ①② 選定ロジックの辞書式順序(lexicographic tuple)化
# 「肌悩み適合を主軸、レビュー・価格・検索適合度は補助/タイブレーカー」という
# 優先関係そのものを構造化し、重み付き合計(旧設計)では埋もれていた肌悩み
# 適合の優位性が確実にランキングを支配することを確認する回帰テスト。
# =========================================================

def test_concern_match_wins_over_large_review_and_relevance_advantage():
    """旧来の重み付き合計(レビュー最大20点+検索適合最大20点=最大40点)なら
    肌悩み適合(最大16点)を上回って逆転していたはずの大きなレビュー・検索適合
    差があっても、新しい辞書式順序では肌悩み適合が確実に優先されること。
    (この回帰テストは、_sort_keyを旧来の重み付き合計に戻すと失敗する。)"""
    user_data = {"sensitivity": "low", "concerns": ["pores", "dryness"]}
    matched_item = make_item(
        "毛穴乾燥ケアRF美顔器A", 8000, 10, 3.0, "A", caption="毛穴 乾燥ケアに",
    )
    unmatched_item = make_item(
        "レビュー圧倒的RF美顔器B", 8000, 1000, 5.0, "B",
    )
    # scoreの差(score_rakuten_item由来、相対正規化で最大20点分)もBに有利。
    scored_items = [(30, matched_item), (50, unmatched_item)]
    best_item, reason = app_module.select_best_beauty_device_candidate(
        scored_items, "RF", user_data, budget_value=0,
    )
    assert best_item["itemCode"] == "A"
    assert "肌悩みとの一致項目が多かったため優先しました" in reason


def test_safety_penalty_outranks_review_advantage():
    """敏感肌×刺激的方式の強度語ペナルティは、レビュー・検索適合の優位より
    優先して勝敗を決めること(安全性は肌悩み適合の次に優先される軸)。"""
    user_data = {"sensitivity": "high"}
    safe_item = make_item("やさしいRF美顔器A", 8000, 10, 3.0, "A", caption="やさしい使い心地")
    intense_item = make_item(
        "業務用RF美顔器B", 8000, 1000, 5.0, "B", caption="業務用の高出力設計",
    )
    scored_items = [(30, safe_item), (50, intense_item)]
    best_item, reason = app_module.select_best_beauty_device_candidate(
        scored_items, "RF", user_data, budget_value=0,
    )
    assert best_item["itemCode"] == "A"
    assert "業務用" in reason or "高出力" in reason


# =========================================================
# サプリメント選定(select_best_supplement_candidate /
# _build_supplement_selection_reason): 美容機器と同じ辞書式順序の考え方を
# 「成分(ingredient_focus)一致」を主軸として適用する。
# =========================================================

def make_supplement_item(name, price, review_count, review_avg, code, caption=""):
    return {
        "itemName": name,
        "itemCaption": caption,
        "itemPrice": price,
        "reviewCount": review_count,
        "reviewAverage": review_avg,
        "itemCode": code,
    }


def test_supplement_winner_and_reason_are_returned_as_tuple():
    scored_items = [
        (50, make_supplement_item("ビタミンCサプリA", 2000, 100, 4.5, "A", caption="ビタミンC配合")),
    ]
    best_item, reason = app_module.select_best_supplement_candidate(
        scored_items, "ビタミンC", ["vitamin_c"], {}, budget_value=0,
    )
    assert best_item["itemCode"] == "A"
    assert isinstance(reason, str) and reason


def test_supplement_selection_empty_pool_returns_none_and_empty_reason():
    best_item, reason = app_module.select_best_supplement_candidate(
        [], "ビタミンC", ["vitamin_c"], {}, budget_value=0,
    )
    assert best_item is None
    assert reason == ""


def test_supplement_ingredient_match_wins_over_large_review_advantage():
    """旧来の重み付き合計(成分一致+25点 vs レビュー最大20点)ではレビュー差が
    大きいと逆転しうる組み合わせでも、辞書式順序では成分一致が確実に優先
    されること。"""
    matched_item = make_supplement_item(
        "ビタミンCサプリA", 2000, 10, 3.0, "A", caption="ビタミンC配合",
    )
    unmatched_item = make_supplement_item(
        "無関係サプリB", 2000, 1000, 5.0, "B", caption="コラーゲン配合",
    )
    scored_items = [(30, matched_item), (50, unmatched_item)]
    best_item, reason = app_module.select_best_supplement_candidate(
        scored_items, "ビタミンC", ["vitamin_c"], {}, budget_value=0,
    )
    assert best_item["itemCode"] == "A"
    assert "対象成分" in reason
    assert "ビタミンC" in reason


def test_supplement_selection_reason_falls_back_to_neutral_when_ingredient_tied():
    """成分一致が同点(決定的でない)場合、成分一致を優位と偽らないのは
    もちろん、レビューも選定理由として書かず中立的なフォールバックに
    なること(前回指摘: 商流上の指標を主理由にしない。サプリメントには
    美容機器のsafety_partsに相当する軸が無いため、成分一致が決定的で
    ない場合は常に中立フォールバックになる)。"""
    item_a = make_supplement_item("ビタミンCサプリA", 2000, 100, 4.9, "A", caption="ビタミンC配合")
    item_b = make_supplement_item("ビタミンCサプリB", 2000, 100, 3.0, "B", caption="ビタミンC配合")
    scored_items = [(50, item_a), (50, item_b)]
    best_item, reason = app_module.select_best_supplement_candidate(
        scored_items, "ビタミンC", ["vitamin_c"], {}, budget_value=0,
    )
    assert best_item["itemCode"] == "A"
    assert reason == app_module._SUPPLEMENT_SELECTION_REASON_FALLBACK
    assert "対象成分" not in reason
    assert "レビュー評価" not in reason


def test_supplement_selection_reason_does_not_claim_advantage_on_tie():
    item_a = make_supplement_item("ビタミンCサプリA", 2000, 100, 4.0, "A", caption="ビタミンC配合")
    item_b = make_supplement_item("ビタミンCサプリB", 2000, 100, 4.0, "B", caption="ビタミンC配合")
    scored_items = [(50, item_a), (50, item_b)]
    best_item, reason = app_module.select_best_supplement_candidate(
        scored_items, "ビタミンC", ["vitamin_c"], {}, budget_value=0,
    )
    assert reason == app_module._SUPPLEMENT_SELECTION_REASON_FALLBACK
    assert "他候補より" not in reason


def test_supplement_selection_reason_fallback_when_only_one_candidate():
    scored_items = [(50, make_supplement_item("ビタミンCサプリA", 2000, 100, 4.0, "A", caption="ビタミンC配合"))]
    best_item, reason = app_module.select_best_supplement_candidate(
        scored_items, "ビタミンC", ["vitamin_c"], {}, budget_value=0,
    )
    assert best_item["itemCode"] == "A"
    assert reason == app_module._SUPPLEMENT_SELECTION_REASON_FALLBACK


# =========================================================
# ③ デザイン統一: 美容機器・サプリメントにも通常商品と同じ悩みタグ
# (concern_tags)と、定性的な改善期待項目(expected_improvement_areas)を
# 付与する(数値化の根拠がないため、通常商品のtopImpactsのような数値は使わない)。
# =========================================================

def test_enrich_beauty_devices_adds_concern_tags_and_expected_improvement_areas():
    data = {"beauty_devices": [{"device_type": "LED", "reason": "赤み・ニキビ改善に有効なため"}]}
    enriched = app_module.enrich_beauty_devices(data, {})
    step = enriched["beauty_devices"][0]
    assert step["concern_tags"], "LEDのpurposeから悩みタグが抽出されること"
    assert step["expected_improvement_areas"] == ["赤み", "ニキビ"]


def test_enrich_beauty_devices_ems_concern_tags_not_empty():
    """回帰テスト: _DEVICE_PURPOSE_LABELS["EMS"]="フェイスラインの引き締め"が
    _PURPOSE_KEYWORD_LABELSのどのキーワードにも一致せず、EMSだけ
    concern_tagsが空になっていたバグの修正確認。"""
    data = {"beauty_devices": [{"device_type": "EMS", "reason": "フェイスラインのたるみ改善に有効なため"}]}
    enriched = app_module.enrich_beauty_devices(data, {})
    step = enriched["beauty_devices"][0]
    assert step["concern_tags"] == ["ハリ補給"]


def test_enrich_supplements_adds_concern_tags_and_expected_improvement_areas():
    data = {"supplements": [{"supplement_type": "ビタミンC", "reason": "シミ・くすみ改善に有効なため"}]}
    enriched = app_module.enrich_supplements(data, {})
    step = enriched["supplements"][0]
    assert step["concern_tags"], "ビタミンCのpurposeから悩みタグが抽出されること"
    assert step["expected_improvement_areas"] == ["くすみ", "色ムラ"]


def test_enrich_beauty_devices_expected_improvement_areas_covers_all_device_types():
    """未定義のdevice_typeが将来追加された場合に空リストへ安全にフォールバック
    することを含め、既存の全device_typeが定性的な改善期待項目を持つこと。"""
    for dtype in app_module._DEVICE_DEFAULTS:
        assert dtype in app_module._DEVICE_EXPECTED_IMPROVEMENT_AREAS, dtype


def test_enrich_supplements_expected_improvement_areas_covers_all_supplement_types():
    for stype in app_module._SUPPLEMENT_DEFAULTS:
        assert stype in app_module._SUPPLEMENT_EXPECTED_IMPROVEMENT_AREAS, stype
