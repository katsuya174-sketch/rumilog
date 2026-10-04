"""Phase 2+3統合検証(Step1: カバレッジ, Step2: 候補品質) — 読み取り専用。
product_master・診断データへの書き込みは一切行わない。楽天/Gemini等の実APIは
一切呼ばない(score_product等、既存の採点ロジックをそのまま再利用するのみ)。
一回限りの分析スクリプト。"""

import json
import os
from collections import Counter, defaultdict

import psycopg2

import app  # noqa: F401 (DATABASE_URL等の.env読み込みトリガー)

DATA_SOURCE = "ai_precollected"


def fetch_master_rows():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        f"SELECT {', '.join(app._PRODUCT_MASTER_ROW_COLUMNS)} "
        "FROM product_master WHERE data_source = %s",
        (DATA_SOURCE,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


# ============================================================
# 診断入力パターン(実際の診断フローの悩み入力を想定した代表例)
# 新規ヒューリスティック・閾値変更は行わず、既存のscore_product()/
# apply_common_score_rules()/_is_relevant_scored_candidate()を
# そのまま使う。
# ============================================================
PATTERNS = [
    {
        "label": "化粧水/乾燥バリア/セラミド",
        "step": {"category": "化粧水", "purpose": "乾燥・バリア機能の低下が気になる", "ingredient_focus": "セラミド"},
        "user_data": {"skin_type": "dry", "oil": "dry", "sens": "normal", "pregnant": False, "exp": "none"},
        "budget_value": 3000,
    },
    {
        "label": "化粧水/乾燥/ヒアルロン酸",
        "step": {"category": "化粧水", "purpose": "とにかく乾燥・潤い不足が気になる", "ingredient_focus": "ヒアルロン酸"},
        "user_data": {"skin_type": "dry", "oil": "dry", "sens": "normal", "pregnant": False, "exp": "none"},
        "budget_value": 2500,
    },
    {
        "label": "美容液/くすみ美白/ビタミンC",
        "step": {"category": "美容液", "purpose": "くすみ・美白が気になる", "ingredient_focus": "ビタミンC"},
        "user_data": {"skin_type": "normal", "oil": "normal", "sens": "normal", "pregnant": False, "exp": "none"},
        "budget_value": 4000,
    },
    {
        "label": "美容液/エイジング/ペプチド",
        "step": {"category": "美容液", "purpose": "ハリ不足・エイジングが気になる", "ingredient_focus": "ペプチド"},
        "user_data": {"skin_type": "normal", "oil": "normal", "sens": "normal", "pregnant": False, "exp": "none"},
        "budget_value": 5000,
    },
    {
        "label": "洗顔/毛穴ニキビ/サリチル酸",
        "step": {"category": "洗顔", "purpose": "毛穴の詰まり・ニキビが気になる", "ingredient_focus": "サリチル酸"},
        "user_data": {"skin_type": "oily", "oil": "oily", "sens": "normal", "pregnant": False, "exp": "none"},
        "budget_value": 2000,
    },
    {
        "label": "ピーリング/角質ケア/AHA",
        "step": {"category": "ピーリング", "purpose": "角質・肌のざらつきが気になる", "ingredient_focus": "AHA"},
        "user_data": {"skin_type": "normal", "oil": "normal", "sens": "normal", "pregnant": False, "exp": "intermediate"},
        "budget_value": 3500,
    },
    {
        "label": "クレンジング/毛穴皮脂/(focus無し)",
        "step": {"category": "クレンジング", "purpose": "メイク落とし・皮脂汚れが気になる", "ingredient_focus": ""},
        "user_data": {"skin_type": "oily", "oil": "oily", "sens": "normal", "pregnant": False, "exp": "none"},
        "budget_value": 2000,
    },
    {
        "label": "クレンジング/敏感肌低刺激/シカ",
        "step": {"category": "クレンジング", "purpose": "敏感肌で刺激が気になる", "ingredient_focus": "シカ"},
        "user_data": {"skin_type": "sensitive", "oil": "normal", "sens": "high", "pregnant": False, "exp": "none"},
        "budget_value": 2500,
    },
    {
        "label": "クリーム/乾燥エイジング/セラミド",
        "step": {"category": "クリーム", "purpose": "乾燥・ハリ不足が気になる", "ingredient_focus": "セラミド"},
        "user_data": {"skin_type": "dry", "oil": "dry", "sens": "normal", "pregnant": False, "exp": "none"},
        "budget_value": 4000,
    },
    {
        "label": "乳液/乾燥/(focus無し)",
        "step": {"category": "乳液", "purpose": "乾燥が気になる", "ingredient_focus": ""},
        "user_data": {"skin_type": "dry", "oil": "dry", "sens": "normal", "pregnant": False, "exp": "none"},
        "budget_value": 2500,
    },
    {
        "label": "パック/乾燥集中ケア/(focus無し)",
        "step": {"category": "パック", "purpose": "乾燥の集中ケアがしたい", "ingredient_focus": ""},
        "user_data": {"skin_type": "dry", "oil": "dry", "sens": "normal", "pregnant": False, "exp": "none"},
        "budget_value": 2000,
    },
    {
        "label": "日焼け止め/UV防御/(focus無し)",
        "step": {"category": "日焼け止め", "purpose": "紫外線対策をしたい", "ingredient_focus": ""},
        "user_data": {"skin_type": "normal", "oil": "normal", "sens": "normal", "pregnant": False, "exp": "none"},
        "budget_value": 2000,
    },
]


def run_pattern(pattern):
    step = pattern["step"]
    user_data = pattern["user_data"]
    budget_value = pattern["budget_value"]
    category = step["category"]
    ingredient_tag = app.normalize_ingredient_tag(step.get("ingredient_focus", "") or "")

    candidates = app.query_product_master_candidates(category, limit=50)
    candidates = [c for c in candidates if c.get("_source") == "product_master"]
    # 今回の分析対象はai_precollectedのみ(診断実績由来diagnosis_time等が
    # 混在していないことを確認: 今はproduct_masterは全件ai_precollectedのはず)
    scored = []
    for c in candidates:
        reasons = []
        score = app.score_product(c, step, user_data, budget_value, reasons=reasons)
        hard_excluded = score <= -9000
        relevant = app._is_relevant_scored_candidate(score, reasons, ingredient_tag)
        scored.append({
            "brand": c.get("brand"), "name": c.get("name"),
            "score": score, "hard_excluded": hard_excluded,
            "relevant": relevant, "reasons": reasons,
        })

    scored_sorted = sorted(scored, key=lambda x: x["score"], reverse=True)
    relevant_count = sum(1 for s in scored if s["relevant"])
    sufficient = relevant_count >= app.PRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT

    return {
        "label": pattern["label"],
        "category": category,
        "ingredient_focus_raw": step.get("ingredient_focus", ""),
        "ingredient_tag_normalized": ingredient_tag,
        "total_master_candidates_in_category": len(candidates),
        "hard_excluded_count": sum(1 for s in scored if s["hard_excluded"]),
        "effective_candidates": sum(1 for s in scored if not s["hard_excluded"]),
        "relevant_count": relevant_count,
        "sufficient": sufficient,
        "top5": scored_sorted[:5],
    }


def main():
    rows = fetch_master_rows()
    products = [app._product_master_row_to_product(r) for r in rows]
    print(f"=== product_master(data_source={DATA_SOURCE}) 総件数: {len(products)} ===\n")

    # ---- 追加確認: category分布 ----
    cat_counter = Counter(p.get("category") for p in products)
    print("--- category分布 ---")
    for cat, n in sorted(cat_counter.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {n}件")

    # ---- 追加確認: item_code保有率 ----
    with_item_code = sum(1 for p in products if str(p.get("item_code") or "").strip())
    print(f"\n--- item_code保有率 ---")
    print(f"  item_code有: {with_item_code}/{len(products)}件 "
          f"({with_item_code / len(products) * 100:.1f}%)")

    # ---- Step1: カバレッジ + Step2: 候補品質 ----
    print("\n" + "=" * 70)
    print("Step1(カバレッジ) / Step2(候補品質) パターン別結果")
    print("=" * 70)

    results = []
    sufficient_count = 0
    top_pick_counter = Counter()
    for pattern in PATTERNS:
        r = run_pattern(pattern)
        results.append(r)
        if r["sufficient"]:
            sufficient_count += 1
        if r["top5"]:
            top_pick_counter[(r["top5"][0]["brand"], r["top5"][0]["name"])] += 1

        print(f"\n### {r['label']}")
        print(f"  category={r['category']!r} ingredient_focus(raw)={r['ingredient_focus_raw']!r} "
              f"-> normalized_tag={r['ingredient_tag_normalized']!r}")
        print(f"  category一致候補総数: {r['total_master_candidates_in_category']}件")
        print(f"  hard_exclude: {r['hard_excluded_count']}件 / "
              f"有効候補(非除外): {r['effective_candidates']}件")
        print(f"  active/support match等で関連性確認できた候補: {r['relevant_count']}件 "
              f"(閾値{app.PRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT}件) "
              f"-> sufficient={r['sufficient']}")
        print("  上位候補(score降順、上位5件):")
        for i, s in enumerate(r["top5"], 1):
            rule_names = [x.get("rule") for x in s["reasons"]]
            key_rules = [rn for rn in rule_names if rn in (
                "ingredient_focus_active_match", "ingredient_focus_support_match",
                "ingredient_focus_missing_penalty", "common_concern_match",
                "common_sensitive_ok_yes", "common_sensitive_ok_no_penalty",
                "moisturizer_keyword_match", "pack_keyword_match", "peeling_keyword_match",
                "product_category_base_fit",
            )]
            print(f"    {i}. [{s['score']:>4}pt] {s['brand']} / {s['name']} "
                  f"hard_excluded={s['hard_excluded']} relevant={s['relevant']} "
                  f"主な採点理由={key_rules}")

    print("\n" + "=" * 70)
    print("--- 追加確認: sufficientになったケース数/全ケース数 ---")
    print(f"  {sufficient_count}/{len(PATTERNS)}パターン")

    print("\n--- 追加確認: 同一商品ばかりが複数条件で上位になる偏り ---")
    for (brand, name), n in top_pick_counter.most_common():
        if n > 1:
            print(f"  [{n}パターンで1位] {brand} / {name}")
    if not any(n > 1 for n in top_pick_counter.values()):
        print("  複数パターンで1位を占有する商品は無し")

    # ---- 用語体系(ボキャブラリ)不一致の直接検証 ----
    # score_product/apply_common_score_rulesの ingredient_focus_active_match
    # 判定は「ingredient_tag(normalize_ingredient_tagが返すASCIIタグ、
    # 例: vitamin_c/ceramide/centella_extract) が product_actives に
    # 文字列として完全一致で含まれるか」で行う(app.py内 apply_common_score_rules
    # 1行目付近: `if ingredient_tag in product_actives`)。
    # product_masterのai_precollected側のactive_ingredientsが実際に
    # ASCIIタグ化されているか、生の日本語成分名のままかをサンプルで確認する。
    print("\n" + "=" * 70)
    print("--- 構造的確認: active_ingredientsの用語体系(生データかASCIIタグ化済みか) ---")
    import re as _re
    ascii_tag_like = 0
    total_nonempty = 0
    for p in products:
        actives = p.get("active_ingredients") or []
        for a in actives:
            total_nonempty += 1
            if isinstance(a, str) and _re.fullmatch(r"[a-z0-9_]+", a):
                ascii_tag_like += 1
    print(f"  全active_ingredients要素数: {total_nonempty}")
    print(f"  ASCIIタグ風(英数字+アンダースコアのみ)の要素数: {ascii_tag_like} "
          f"({(ascii_tag_like / total_nonempty * 100) if total_nonempty else 0:.1f}%)")
    print(f"  サンプル(先頭10件)の生データ:")
    shown = 0
    for p in products:
        actives = p.get("active_ingredients") or []
        if actives:
            print(f"    {p.get('brand')} / {p.get('name')}: {actives}")
            shown += 1
        if shown >= 10:
            break


if __name__ == "__main__":
    main()
