"""P2 Step9検証(読み取り専用): 既存52件のタグ不変確認、staging13件の
再評価、5領域のcoverage再計算。DB変更なし。"""
import os
import psycopg2
import app

NEUTRAL_USER_DATA = {"skin_type": "normal", "oil": "normal", "sens": "normal", "pregnant": False, "exp": "none"}
BUDGET_VALUE = 3000
AREAS = [
    ("化粧水", "tranexamic_acid"), ("化粧水", "amino_acid"), ("洗顔", "glycolic_acid"),
    ("クリーム", "peptide"), ("クレンジング", "centella_extract"),
]


def current_relevant_count(category, tag):
    step = {"category": category, "purpose": "", "ingredient_focus": tag}
    candidates = [c for c in app.query_product_master_candidates(category, limit=50) if c.get("_source") == "product_master"]
    count = 0
    for c in candidates:
        reasons = []
        score = app.score_product(c, step, NEUTRAL_USER_DATA, BUDGET_VALUE, reasons=reasons)
        if app._is_relevant_scored_candidate(score, reasons, tag):
            count += 1
    return count


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    # 1. 既存52件のタグ不変確認
    cur.execute("SELECT product_id, brand, name, active_ingredients, active_ingredient_tags FROM product_master")
    rows = cur.fetchall()
    changed = []
    for product_id, brand, name, actives, old_tags in rows:
        new_tags = app.compute_ingredient_tags(actives or [])
        if sorted(new_tags) != sorted(old_tags or []):
            changed.append((brand, name, old_tags, new_tags))
    print(f"1. 既存52件のタグ変更: {len(changed)}件")
    for brand, name, old, new in changed:
        print(f"   {brand}/{name}: {old} -> {new}")

    # 2. staging13件の再評価
    cur.execute("SELECT brand, product_name, stage2_payload FROM product_collection_staging WHERE batch_id = %s",
                ("p2-step7-1791276523",))
    staging_rows = cur.fetchall()
    conn.close()

    print("\n2. staging13件の再評価:")
    newly_gained = []
    for brand, name, payload in staging_rows:
        actives = [i.get("ingredient") for i in (payload.get("active_ingredients") or [])
                   if str(i.get("ingredient", "") or "").strip().lower() != "unknown"]
        tags = app.compute_ingredient_tags(actives)
        has_amino = "amino_acid" in tags
        print(f"   {brand}/{name}: tags={tags} amino_acid={has_amino}")
        if has_amino:
            newly_gained.append(f"{brand}/{name}")

    print(f"\n   amino_acidを獲得した商品: {newly_gained}")
    assert newly_gained == ["菊正宗/菊正宗 日本酒の化粧水 高保湿"], f"予期しない結果: {newly_gained}"
    print("   -> 菊正宗のみがamino_acidを獲得(ミノンは対象外)であることを確認")

    # 4. P2各領域のcoverage再計算
    print("\n4. P2各領域のcoverage再計算(現master、staging未反映状態):")
    for category, tag in AREAS:
        base = current_relevant_count(category, tag)
        # staging分を仮に加味した場合の見込み(未反映なので実際には変化しない)
        print(f"   {category} x {tag}: 現master={base}件 sufficient={base >= app.PRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT}")


if __name__ == "__main__":
    main()
