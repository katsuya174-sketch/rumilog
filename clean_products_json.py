import json
from copy import deepcopy

INPUT_PATH = "products.json"
OUTPUT_PATH = "products.cleaned.json"
REPORT_PATH = "tag_cleanup_report.json"

ALLOWED_MAIN_FUNCTIONS = {
    "バリア強化",
    "バリア修復",
    "鎮静ケア",
    "保湿",
    "エイジングケア",
    "ハリ改善",
    "紫外線防御",
    "トーンアップ",
    "透明感向上",
    "美白ケア",
    "毛穴改善",
    "ニキビ予防",
    "皮脂抑制",
    "キメ改善",
    "なめらか肌",
    "メイク落とし",
    "日焼け止めオフ",
    "皮脂汚れオフ",
    "黒ずみ予防",
    "毛穴詰まり予防",
    "洗いすぎ防止",
    "うるおいを守って洗う",
}

ALLOWED_INGREDIENT_FOCUS = {
    "毛穴ケア",
    "皮脂コントロール",
    "ニキビケア",
    "鎮静ケア",
    "赤みケア",
    "保湿",
    "バリア修復",
    "美白",
    "くすみ改善",
    "エイジングケア",
    "ハリ改善",
    "角質ケア",
    "ターンオーバー促進",
    "紫外線防御",
}

MAIN_FUNCTION_SYNONYM_MAP = {
    "メイク除去": "メイク落とし",
    "メイク落とし": "メイク落とし",
    "日焼け止め除去": "日焼け止めオフ",
    "日焼け止めオフ": "日焼け止めオフ",
    "皮脂汚れ除去": "皮脂汚れオフ",
    "皮脂汚れオフ": "皮脂汚れオフ",
    "黒ずみ除去": "黒ずみ予防",
    "黒ずみ予防": "黒ずみ予防",
    "毛穴詰まり予防": "毛穴詰まり予防",
    "洗いすぎ防止": "洗いすぎ防止",
    "うるおい保持洗浄": "うるおいを守って洗う",
    "うるおいを守って洗う": "うるおいを守って洗う",

    "バリア保護": "バリア強化",
    "バリア強化": "バリア強化",
    "バリア修復": "バリア修復",
    "保湿": "保湿",
    "浸透保湿": "保湿",

    "鎮静ケア": "鎮静ケア",
    "赤み軽減": "鎮静ケア",
    "赤みケア": "鎮静ケア",

    "ニキビケア": "ニキビ予防",
    "ニキビ予防": "ニキビ予防",

    "毛穴改善": "毛穴改善",
    "皮脂バランス調整": "皮脂抑制",
    "皮脂抑制": "皮脂抑制",

    "くすみ除去": "透明感向上",
    "透明感向上": "透明感向上",
    "美白ケア": "美白ケア",
    "ツヤ改善": "透明感向上",

    "エイジングケア": "エイジングケア",
    "ハリ改善": "ハリ改善",
    "シワ改善": "エイジングケア",

    "紫外線防御": "紫外線防御",
    "トーンアップ": "トーンアップ",

    "キメ改善": "キメ改善",
    "なめらか肌": "なめらか肌",

    # 英語保険
    "barrier": "バリア強化",
    "soothing": "鎮静ケア",
    "hydration": "保湿",
    "aging": "エイジングケア",
    "uv_protection": "紫外線防御",
    "tone_up": "トーンアップ",
    "brightening": "透明感向上",
    "pores": "毛穴改善",
    "acne": "ニキビ予防",
    "oil_control": "皮脂抑制",
    "texture": "キメ改善",
    "dryness": "保湿",

    # これは捨てる
    "毎日使いやすい": None,
    "低刺激洗浄": None,
}

INGREDIENT_FOCUS_SYNONYM_MAP = {
    "毛穴ケア": "毛穴ケア",
    "皮脂コントロール": "皮脂コントロール",
    "ニキビケア": "ニキビケア",
    "鎮静ケア": "鎮静ケア",
    "赤みケア": "赤みケア",
    "保湿": "保湿",
    "浸透保湿": "保湿",
    "バリア修復": "バリア修復",
    "美白": "美白",
    "くすみ改善": "くすみ改善",
    "エイジングケア": "エイジングケア",
    "ハリ改善": "ハリ改善",
    "角質ケア": "角質ケア",
    "ターンオーバー促進": "ターンオーバー促進",
    "紫外線防御": "紫外線防御",

    # 成分名が入っていたら落とす
    "bha": None,
    "aha": None,
    "pha": None,
    "heartleaf": None,
    "centella_extract": None,
    "cica": None,
    "hyaluronic_acid": None,
    "niacinamide": None,
    "vitamin_c": None,
    "tranexamic_acid": None,
    "ceramide": None,
    "peptide": None,
}

def ensure_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, set):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        return [value]
    return []

def dedupe_keep_order(items):
    seen = set()
    out = []
    for x in items:
        if not x or x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out

def normalize_main_functions(values):
    values = ensure_list(values)
    normalized = []

    for v in values:
        mapped = MAIN_FUNCTION_SYNONYM_MAP.get(v, v)
        if mapped in ALLOWED_MAIN_FUNCTIONS:
            normalized.append(mapped)

    return dedupe_keep_order(normalized)

def normalize_ingredient_focus(values):
    values = ensure_list(values)
    normalized = []

    for v in values:
        mapped = INGREDIENT_FOCUS_SYNONYM_MAP.get(v, v)
        if mapped in ALLOWED_INGREDIENT_FOCUS:
            normalized.append(mapped)

    return dedupe_keep_order(normalized)

def clean_one_product(product):
    p = deepcopy(product)

    p["main_functions"] = normalize_main_functions(
        p.get("main_functions", [])
    )
    p["ingredient_focus"] = normalize_ingredient_focus(
        p.get("ingredient_focus", [])
    )

    return p

def clean_products(products):
    cleaned = []
    report = []

    for p in products:
        before_main = ensure_list(p.get("main_functions", []))
        before_focus = ensure_list(p.get("ingredient_focus", []))

        cp = clean_one_product(p)

        after_main = cp.get("main_functions", [])
        after_focus = cp.get("ingredient_focus", [])

        if before_main != after_main or before_focus != after_focus:
            report.append({
                "name": p.get("name", ""),
                "before_main_functions": before_main,
                "after_main_functions": after_main,
                "before_ingredient_focus": before_focus,
                "after_ingredient_focus": after_focus,
            })

        cleaned.append(cp)

    return cleaned, report

def main():
    with open(INPUT_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    # products.json が {"skincare_database": [...]} 型でも
    # 直接 [...] 型でも両対応
    if isinstance(data, dict) and "skincare_database" in data:
        products = data["skincare_database"]
        cleaned_products, report = clean_products(products)
        data["skincare_database"] = cleaned_products
        output_data = data
    elif isinstance(data, list):
        cleaned_products, report = clean_products(data)
        output_data = cleaned_products
    else:
        raise ValueError("products.json の構造が想定外です")

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"修正版を書き出しました: {OUTPUT_PATH}")
    print(f"変更レポートを書き出しました: {REPORT_PATH}")
    print(f"変更商品数: {len(report)}")

if __name__ == "__main__":
    main()