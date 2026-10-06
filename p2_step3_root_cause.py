"""P2 Step3: 化粧水×tranexamic_acidでdプログラムが松山油脂に負ける原因の
完全分解(読み取り専用、コード修正なし、実API呼び出しなし)。"""
import os
import psycopg2
import app

BUDGET_VALUE = 3000
STEP = {"category": "化粧水", "purpose": "くすみ・色素沈着が気になる", "ingredient_focus": "トラネキサム酸"}
USER_DATA = {"skin_type": "normal", "oil": "normal", "sens": "normal"}


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


def decompose(product, label):
    print(f"\n===== {label}: {product.get('brand')} / {product.get('name')} =====")
    print(f"  active_ingredients(enriched)={product.get('active_ingredients')}")
    print(f"  concerns(enriched)={product.get('concerns')}")
    print(f"  main_functions(enriched)={product.get('main_functions')}")
    print(f"  ingredient_focus(enriched)={product.get('ingredient_focus')}")

    base_reasons = []
    base_score = app.score_product(product, STEP, USER_DATA, BUDGET_VALUE, reasons=base_reasons)
    print(f"\n  --- base_score(score_product) = {base_score} ---")
    for r in base_reasons:
        print(f"    {r['rule']}: {r['points']:+} ({r['label']})")

    improve_score = app.score_improvement(product, {}, None)
    print(f"\n  --- improve_score(score_improvement, improvement_plan={{}}) = {improve_score} ---")

    routine_reasons = []
    routine_score = app.score_routine_balance(STEP, product, None, reasons=routine_reasons)
    print(f"\n  --- routine_score(score_routine_balance) = {routine_score} ---")
    for r in routine_reasons:
        print(f"    {r}")

    base_weight, improve_weight = app.get_dynamic_score_weights(STEP, USER_DATA)
    routine_weight = app.get_routine_score_weight(STEP)
    final_score = base_score * base_weight + improve_score * improve_weight + routine_score * routine_weight
    print(f"\n  --- weights: base_weight={base_weight} improve_weight={improve_weight} routine_weight={routine_weight} ---")
    print(f"  final_score = {base_score}*{base_weight} + {improve_score}*{improve_weight} + {routine_score}*{routine_weight} = {final_score:.2f}")
    return {
        "base_score": base_score, "improve_score": improve_score, "routine_score": routine_score,
        "base_weight": base_weight, "improve_weight": improve_weight, "routine_weight": routine_weight,
        "final_score": final_score,
    }


def main():
    matsuyama = fetch("松山油脂 保湿浸透水バランシング")
    dprogram = fetch("dプログラム モイストケア ローション EX")

    r1 = decompose(matsuyama, "松山油脂(勝者)")
    r2 = decompose(dprogram, "dプログラム(正タグ保持・敗者)")

    print("\n\n===== 最終比較 =====")
    print(f"松山油脂: base={r1['base_score']} improve={r1['improve_score']} routine={r1['routine_score']} -> final={r1['final_score']:.2f}")
    print(f"dプログラム: base={r2['base_score']} improve={r2['improve_score']} routine={r2['routine_score']} -> final={r2['final_score']:.2f}")
    print(f"\nbase軸での差(dプログラムが優位): {r2['base_score'] - r1['base_score']:+.1f} (重み後: {(r2['base_score']-r1['base_score'])*r2['base_weight']:+.2f})")
    print(f"improve軸での差(松山油脂が優位): {r1['improve_score'] - r2['improve_score']:+.1f} (重み後: {(r1['improve_score']-r2['improve_score'])*r1['improve_weight']:+.2f})")
    print(f"最終スコア差: {r1['final_score'] - r2['final_score']:+.2f} (松山油脂が優位)")


if __name__ == "__main__":
    main()
