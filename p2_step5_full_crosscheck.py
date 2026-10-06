"""P2 Step5 要件6/7: 既存52商品について、現実的なimprovement_planを与えられる
全ingredient_focus経路(9category x 26tag=234通りのうち、category内に候補が
1件以上存在するもの)を横断比較し、補正前後の1位変更を列挙・分類する。
読み取り専用、DB変更・実API呼び出しなし。score_product/score_improvement/
score_routine_balance/get_dynamic_score_weights/get_routine_score_weight/
enrich_product_metadata_from_ingredientsはそのまま再利用し、新しいスコア
ロジックは作らない(+15/-15の再現のみ、app.py側の実装と同一)。
"""
import copy
import os

import psycopg2
import app

BUDGET_VALUE = 3000
NEUTRAL_USER_DATA = {"skin_type": "normal", "oil": "normal", "sens": "normal", "pregnant": False, "exp": "none"}

CATEGORIES = ["クレンジング", "洗顔", "化粧水", "美容液", "乳液", "クリーム", "パック", "ピーリング", "日焼け止め"]
COMMON_INGREDIENT_TAGS = [
    "retinol", "retinal", "retinoid", "vitamin_c", "niacinamide", "azelaic_acid",
    "tranexamic_acid", "peptide", "ceramide", "hyaluronic_acid", "centella_extract",
    "panthenol", "aha", "bha", "salicylic_acid", "glycolic_acid", "lactic_acid", "pha",
    "arbutin", "kojic_acid", "glutathione", "squalane", "glycerin", "amino_acid",
    "collagen", "enzyme",
]

# tag -> 実際のnormalize_ingredient_tag()で同じtagに正規化される成分名の
# 日本語/英語表記(事前にapp.normalize_ingredient_tag()で1件ずつ検証済み)。
# step.ingredient_focusにはこちらを使う(purposeの悩み文言とは別)。
TAG_TO_INGREDIENT_TEXT = {
    "retinol": "レチノール", "retinal": "レチナール", "retinoid": "レチノイド",
    "vitamin_c": "ビタミンC", "niacinamide": "ナイアシンアミド", "azelaic_acid": "アゼライン酸",
    "tranexamic_acid": "トラネキサム酸", "peptide": "ペプチド", "ceramide": "セラミド",
    "hyaluronic_acid": "ヒアルロン酸", "centella_extract": "ツボクサ", "panthenol": "パンテノール",
    "aha": "AHA", "bha": "BHA", "salicylic_acid": "サリチル酸", "glycolic_acid": "グリコール酸",
    "lactic_acid": "乳酸", "pha": "PHA", "arbutin": "アルブチン", "kojic_acid": "コウジ酸",
    "glutathione": "グルタチオン", "squalane": "スクワラン", "glycerin": "グリセリン",
    "amino_acid": "アミノ酸", "collagen": "コラーゲン", "enzyme": "酵素",
}

# tag -> 現実的なpurpose/improvement_plan文言(_RAKUTEN_INGREDIENT_METADATAの
# concernsをそのまま日本語化して使う。推測で無関係な文言を作らない)。
TAG_TO_PURPOSE_TEXT = {
    "retinol": "ハリ不足・毛穴・エイジングが気になる", "retinal": "ハリ不足・毛穴・エイジングが気になる",
    "retinoid": "ハリ不足・毛穴・エイジングが気になる",
    "vitamin_c": "くすみ・美白・色素沈着が気になる", "niacinamide": "毛穴・くすみ・美白・皮脂が気になる",
    "azelaic_acid": "ニキビ・赤み・くすみが気になる", "tranexamic_acid": "くすみ・美白・色素沈着が気になる",
    "peptide": "ハリ不足・エイジングが気になる", "ceramide": "乾燥・バリア機能の低下が気になる",
    "hyaluronic_acid": "乾燥・バリア機能の低下が気になる", "centella_extract": "赤み・バリア・ニキビが気になる",
    "panthenol": "赤み・バリア・乾燥が気になる", "aha": "角質・くすみ・毛穴が気になる",
    "bha": "毛穴・ニキビ・角質・皮脂が気になる", "salicylic_acid": "毛穴・ニキビ・皮脂が気になる",
    "glycolic_acid": "角質・くすみ・毛穴が気になる", "lactic_acid": "角質・くすみ・乾燥が気になる",
    "pha": "角質・くすみが気になる", "arbutin": "くすみ・美白・色素沈着が気になる",
    "kojic_acid": "くすみ・美白・色素沈着が気になる", "glutathione": "くすみ・美白が気になる",
    "squalane": "乾燥・バリア機能の低下が気になる", "glycerin": "乾燥が気になる",
    "amino_acid": "乾燥・バリア機能の低下が気になる", "collagen": "ハリ不足・乾燥・エイジングが気になる",
    "enzyme": "毛穴・角質・くすみが気になる",
}


def score_all(category, tag, products_by_category, apply_correction):
    purpose_text = TAG_TO_PURPOSE_TEXT[tag]
    ingredient_text = TAG_TO_INGREDIENT_TEXT[tag]
    step = {"category": category, "purpose": purpose_text, "ingredient_focus": ingredient_text}
    improvement_plan = {"summary": purpose_text}
    ingredient_tag = app.normalize_ingredient_tag(ingredient_text)
    assert ingredient_tag == tag, f"normalize mismatch: {ingredient_text!r} -> {ingredient_tag!r}, expected {tag!r}"

    base_weight, improve_weight = app.get_dynamic_score_weights(step, NEUTRAL_USER_DATA)
    routine_weight = app.get_routine_score_weight(step)

    results = []
    for c in products_by_category.get(category, []):
        product = copy.deepcopy(c)
        app.enrich_product_metadata_from_ingredients(product)

        base_reasons = []
        base_score = app.score_product(product, step, NEUTRAL_USER_DATA, BUDGET_VALUE, reasons=base_reasons)
        if base_score <= -9000:
            continue

        improve_score = app.score_improvement(product, improvement_plan, None)
        routine_score = app.score_routine_balance(step, product, None, reasons=[])

        final_score = base_score * base_weight + improve_score * improve_weight + routine_score * routine_weight
        tag_holder = bool(ingredient_tag) and ingredient_tag in (product.get("active_ingredients") or [])

        if apply_correction and ingredient_tag:
            final_score += 15 if tag_holder else -15

        results.append({"brand": product.get("brand"), "name": product.get("name"),
                         "final_score": final_score, "tag_holder": tag_holder})

    if not results:
        return None
    results.sort(key=lambda r: r["final_score"], reverse=True)
    return results[0]


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(f"SELECT {', '.join(app._PRODUCT_MASTER_ROW_COLUMNS)} FROM product_master")
    rows = cur.fetchall()
    conn.close()
    products = [app._product_master_row_to_product(r) for r in rows]
    products_by_category = {}
    for p in products:
        products_by_category.setdefault(p.get("category"), []).append(p)

    total_cases = 0
    changed = []
    unchanged_count = 0
    for category in CATEGORIES:
        if not products_by_category.get(category):
            continue
        for tag in COMMON_INGREDIENT_TAGS:
            total_cases += 1
            before = score_all(category, tag, products_by_category, apply_correction=False)
            after = score_all(category, tag, products_by_category, apply_correction=True)
            if before is None or after is None:
                continue
            if before["name"] != after["name"]:
                changed.append((category, tag, before, after))
            else:
                unchanged_count += 1

    print(f"横断検証総ケース数(category内に候補1件以上存在する組み合わせ): {total_cases}")
    print(f"1位変更なし: {unchanged_count}")
    print(f"1位変更あり: {len(changed)}\n")

    for category, tag, before, after in changed:
        classification = "正しいtag保持への修正" if after["tag_holder"] and not before["tag_holder"] else "要確認(新たな逆転の可能性)"
        print(f"[{category} x {tag}] {before['brand']}/{before['name']}(tag_holder={before['tag_holder']}) "
              f"-> {after['brand']}/{after['name']}(tag_holder={after['tag_holder']})  分類: {classification}")


if __name__ == "__main__":
    main()
