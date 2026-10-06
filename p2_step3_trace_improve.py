"""P2 Step3補助: score_improvement()の内部計算を一切変更せず、同じ入力に対し
段階ごとの加点を可視化する診断専用スクリプト(本体コードは書き換えない)。
app.pyの各種補助関数(collect_product_terms, infer_improvement_targets,
CATEGORY_IMPROVEMENT_BONUS, IMPROVEMENT_KEYWORDS, term_matches)はそのまま
再利用し、score_improvement()本体のロジックを読み取り専用で再現・計測する。
"""
import os
import psycopg2
import app


def fetch(name):
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute(
        f"SELECT {', '.join(app._PRODUCT_MASTER_ROW_COLUMNS)} FROM product_master WHERE name = %s",
        (name,),
    )
    row = cur.fetchone()
    conn.close()
    return app._product_master_row_to_product(row)


def trace(product, label):
    print(f"\n===== {label}: {product.get('name')} =====")
    terms = app.collect_product_terms(product)
    targets = app.infer_improvement_targets({})
    category = str(product.get("category", "")).strip()
    name = str(product.get("name", "")).lower()
    ingredient_strength = product.get("ingredient_strength", {}) or {}
    main_functions = product.get("main_functions", []) or []
    ingredient_focus = product.get("ingredient_focus", []) or []
    sensitive_ok = str(product.get("sensitive_ok", "")).lower()

    print(f"  terms(抜粋成分系)={[t for t in terms if len(t) < 20]}")
    print(f"  targets(improvement_plan={{}}由来)={targets}")
    print(f"  ingredient_strength={ingredient_strength}")
    print(f"  main_functions={main_functions}  ingredient_focus={ingredient_focus}")
    print(f"  sensitive_ok={sensitive_ok}")

    score = 0
    s = app.CATEGORY_IMPROVEMENT_BONUS.get(category, 0)
    score += s
    print(f"  [1] CATEGORY_IMPROVEMENT_BONUS[{category}] = +{s}  (累計{score})")

    for target in targets:
        rule = app.IMPROVEMENT_KEYWORDS.get(target, {})
        if app.term_matches(terms, rule.get("strong", [])):
            score += 28
            print(f"  [2] target={target} strong match = +28 (累計{score})")
        if app.term_matches(terms, rule.get("support", [])):
            score += 14
            print(f"  [2] target={target} support match = +14 (累計{score})")

    for ingredient, strength in ingredient_strength.items():
        ing_n = app.normalize_text(ingredient)
        str_n = app.normalize_text(strength)
        if not ing_n:
            continue
        if ing_n in terms:
            if str_n in ["high", "strong"]:
                score += 10
            elif str_n in ["medium", "middle"]:
                score += 6
            elif str_n in ["low", "mild"]:
                score += 3
            print(f"  [3] ingredient_strength {ingredient}={strength} matched terms -> 累計{score}")

    function_text = " ".join(str(x) for x in main_functions + ingredient_focus).lower()
    function_bonus_keywords = {
        "美白": 8, "毛穴": 8, "ニキビ": 8, "ハリ": 8, "バリア": 7,
        "保湿": 6, "鎮静": 6, "角質": 7, "uv": 8, "紫外線": 8,
    }
    for keyword, bonus in function_bonus_keywords.items():
        if keyword.lower() in function_text:
            score += bonus
            print(f"  [4] function_bonus_keywords '{keyword}' in function_text -> +{bonus} (累計{score})")

    name_bonus_groups = [
        (18, ["メラノ", "melano"]), (18, ["ビタミンc", "vitamin c"]),
        (22, ["レチノール", "retinol"]), (24, ["レチナール", "retinal"]),
        (22, ["アゼライン", "azelaic"]), (12, ["シカ", "cica"]), (14, ["セラミド", "ceramide"]),
    ]
    for bonus, keywords in name_bonus_groups:
        if any(keyword in name for keyword in keywords):
            score += bonus
            print(f"  [5] name_bonus_groups {keywords} in name='{name}' -> +{bonus} (累計{score})")

    if category == "日焼け止め" or "sunscreen" in terms or "日焼け止め" in terms:
        score += 16
        print(f"  [6] 日焼け止めボーナス -> +16 (累計{score})")
    if category == "ピーリング" or "peeling" in terms or "ピーリング" in terms:
        score += 15
        print(f"  [7] ピーリングボーナス -> +15 (累計{score})")

    if sensitive_ok == "yes":
        score += 8
        print(f"  [8] sensitive_ok=yes -> +8 (累計{score})")
    elif sensitive_ok == "no":
        score -= 8
        print(f"  [8] sensitive_ok=no -> -8 (累計{score})")

    print(f"  --- 手動トレース合計 = {score} (score_improvement実測値と比較) ---")
    print(f"  score_improvement()実測値 = {app.score_improvement(product, {}, None)}")


def main():
    trace(fetch("松山油脂 保湿浸透水バランシング"), "松山油脂")
    trace(fetch("dプログラム モイストケア ローション EX"), "dプログラム")


if __name__ == "__main__":
    main()
