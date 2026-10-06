"""Step36: Step31〜35でStage1/2検証に成功した8商品を、既存のPhase3 reflect
経路(reflect_staging_to_product_master)のみでproduct_masterへ本反映し、
既存のitem_code解決経路(resolve_item_code_for_product/verify_and_resolve_
item_code)のみでRakuten item_codeを取得する(一回限りの運用スクリプト)。
新しい収集・照合ロジックは一切作らない。
"""
import os

import psycopg2

import app
import product_collection_pipeline as pipeline

# (staging_id, brand, name, category) — Step31〜35で実際にStage1/2検証に
# 成功した8件(KISOCAREはStep35の成功staging_id=96を使う。94/95は失敗した
# 過去の試行のため反映対象にしない)。
TARGETS = [
    (85, "プラスキレイ", "プラスレチAセラム", "美容液"),
    (86, "プラスキレイ", "プラスピュアVC28", "美容液"),
    (87, "ハルメク", "C35プレミアム", "美容液"),
    (88, "ETVOS", "モイスチャライジングローション", "化粧水"),
    (89, "肌ラボ", "極潤 薬用ハリ化粧水", "化粧水"),
    (91, "ちふれ", "濃厚化粧水", "化粧水"),
    (93, "SKINFOOD", "どんぐり ポア ペプチド クリーム", "クリーム"),
    (96, "KISOCARE", "キソ マトリックスセラム PE", "美容液"),
]

AREAS_13 = [
    ("化粧水", "hyaluronic_acid"), ("美容液", "vitamin_c"), ("化粧水", "ceramide"),
    ("美容液", "peptide"), ("美容液", "retinol"), ("美容液", "niacinamide"),
    ("化粧水", "niacinamide"), ("洗顔", "salicylic_acid"),
    ("化粧水", "tranexamic_acid"), ("化粧水", "amino_acid"), ("洗顔", "glycolic_acid"),
    ("クリーム", "peptide"), ("クレンジング", "centella_extract"),
]

NEUTRAL_USER_DATA = {"skin_type": "normal", "oil": "normal", "sens": "normal", "pregnant": False, "exp": "none"}
BUDGET_VALUE = 3000


def effective_candidates_real(category, tag, db_products, verified_products):
    """Step27と同じdedup後基準。今回は実DB(product_master)を直接見るので
    extra_candidatesの仮追加は不要。"""
    def keys_of(items):
        s = set()
        for p in items:
            if isinstance(p, dict):
                k = app.make_verified_product_key(p)
                if k:
                    s.add(k)
        return s

    seen = keys_of(db_products) | keys_of(verified_products)
    master = app.query_product_master_candidates(category, limit=50)
    survivors = [mp for mp in master if app.make_verified_product_key(mp) not in seen]

    step = {"category": category, "purpose": "", "ingredient_focus": tag}
    relevant = 0
    for mp in survivors:
        reasons = []
        score = app.score_product(dict(mp), step, NEUTRAL_USER_DATA, BUDGET_VALUE, reasons=reasons)
        if app._is_relevant_scored_candidate(score, reasons, tag):
            relevant += 1
    return relevant


def step1_final_check():
    print("=" * 70)
    print("[1] 対象8商品の最終チェック(identity重複・conflict・citation)")
    print("=" * 70)
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    ok_targets = []
    for staging_id, brand, name, category in TARGETS:
        cur.execute("""
            SELECT stage1_status, stage1_citations, stage2_status, conflict_status, reflected_at
            FROM product_collection_staging WHERE staging_id = %s
        """, (staging_id,))
        row = cur.fetchone()
        stage1_status, citations, stage2_status, conflict_status, reflected_at = row
        identity_key = app._normalize_product_master_identity_key(brand, name, category)
        cur.execute("SELECT product_id FROM product_master WHERE identity_key = %s", (identity_key,))
        dup = cur.fetchone()
        print(f"\n  staging_id={staging_id} {brand}/{name} ({category})")
        print(f"    stage1={stage1_status} citation数={len(citations or [])} stage2={stage2_status} conflict={conflict_status}")
        print(f"    reflected_at={reflected_at} identity重複={bool(dup)}")
        safe = (stage1_status == "ok" and stage2_status == "ok" and conflict_status != "needs_review"
                and reflected_at is None and not dup)
        print(f"    reflect可能: {safe}")
        if safe:
            ok_targets.append((staging_id, brand, name, category))
    conn.close()
    return ok_targets


def step2_reflect(ok_targets):
    print("\n" + "=" * 70)
    print("[2] product_masterへ本反映(dry_run確認 → 実反映)")
    print("=" * 70)
    results = []
    for staging_id, brand, name, category in ok_targets:
        preview = pipeline.reflect_staging_to_product_master(staging_id, dry_run=True)
        print(f"\n  staging_id={staging_id} {brand}/{name}: dry_run -> {preview.get('status')} action={preview.get('action')}")
        if preview.get("status") != "would_reflect":
            print(f"    [SKIP] dry_runがwould_reflectでないため反映しない: {preview}")
            results.append({"staging_id": staging_id, "brand": brand, "name": name, "status": "skipped_dry_run"})
            continue
        real = pipeline.reflect_staging_to_product_master(staging_id, dry_run=False)
        print(f"    実反映 -> {real}")
        results.append({"staging_id": staging_id, "brand": brand, "name": name, "category": category, **real})
    return results


def step3_recalculate_13_areas():
    print("\n" + "=" * 70)
    print("[3] 13領域の実効候補再計算(実DB、Step27と同じdedup後基準)")
    print("=" * 70)
    db_products = app.load_products()
    verified_products = app.load_verified_products_cache()
    all_sufficient = True
    for category, tag in AREAS_13:
        n = effective_candidates_real(category, tag, db_products, verified_products)
        suff = n >= app.PRODUCT_MASTER_SUFFICIENT_CANDIDATE_COUNT
        print(f"  {category} x {tag}: {n} sufficient={suff}")
        if not suff:
            all_sufficient = False
    print(f"\n13/13すべてsufficient: {all_sufficient}")
    return all_sufficient


def step4_5_resolve_item_codes(reflect_results):
    print("\n" + "=" * 70)
    print("[4-5] 新規反映商品のitem_code取得(既存resolver、販売情報のみ更新)")
    print("=" * 70)
    item_code_results = []
    for r in reflect_results:
        if r.get("status") != "reflected":
            continue
        product_id = r["product_id"]
        brand, name, category = r["brand"], r["name"], r["category"]

        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        cur = conn.cursor()
        cur.execute("SELECT jan_code, active_ingredients, formulation FROM product_master WHERE product_id = %s", (product_id,))
        jan_code, before_actives, before_formulation = cur.fetchone()
        conn.close()

        print(f"\n  product_id={product_id} {brand}/{name} ({category})")
        resolution = pipeline.resolve_item_code_for_product(brand, name, category, jan_code=jan_code)
        print(f"    resolve status={resolution['status']}")
        if resolution["status"] != "confirmed":
            print(f"    詳細: {resolution}")
            print("    -> item_code NULLのまま(無理に紐付けない)")
            item_code_results.append({"product_id": product_id, "brand": brand, "name": name, "status": "not_found"})
            continue

        item = resolution["item"]
        print(f"    選定item: itemCode={item.get('itemCode')} shop={item.get('shopName')} price={item.get('itemPrice')}")
        print(f"    set判定={app._is_rakuten_set_item(str(item.get('itemName', '')))}")

        update_result = pipeline.verify_and_resolve_item_code(product_id, brand, name, category, jan_code=jan_code)
        print(f"    verify_and_resolve_item_code -> {update_result.get('status')}")
        item_code_results.append({
            "product_id": product_id, "brand": brand, "name": name,
            "status": update_result.get("status"), "item_code": update_result.get("item_code"),
            "before_actives": before_actives, "before_formulation": before_formulation,
        })

        if update_result.get("status") == "resolved":
            conn = psycopg2.connect(os.environ["DATABASE_URL"])
            cur = conn.cursor()
            cur.execute("SELECT active_ingredients, formulation FROM product_master WHERE product_id = %s", (product_id,))
            after_actives, after_formulation = cur.fetchone()
            conn.close()
            print(f"    成分/formulation不変: actives={before_actives == after_actives} formulation={before_formulation == after_formulation}")

    return item_code_results


def step6_final_checks():
    print("\n" + "=" * 70)
    print("[6] 最終整合性チェック")
    print("=" * 70)
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM product_master")
    total = cur.fetchone()[0]
    print(f"  product_master総数: {total}")

    cur.execute("SELECT identity_key, COUNT(*) FROM product_master GROUP BY identity_key HAVING COUNT(*) > 1")
    print(f"  identity_key重複: {cur.fetchall() or 'なし'}")
    cur.execute("SELECT item_code, COUNT(*) FROM product_master WHERE item_code IS NOT NULL AND item_code != '' GROUP BY item_code HAVING COUNT(*) > 1")
    print(f"  item_code重複: {cur.fetchall() or 'なし'}")

    cur.execute("SELECT product_id, rakuten_title FROM product_master WHERE item_code IS NOT NULL AND item_code != ''")
    rows = cur.fetchall()
    set_flagged = [(pid, t) for pid, t in rows if app._is_rakuten_set_item(t or "")]
    print(f"  item_code保有{len(rows)}件のうちセット誤登録: {len(set_flagged)} (期待: 0)")
    for pid, t in set_flagged:
        print(f"    product_id={pid} title={t}")
    conn.close()
    return total


def main():
    ok_targets = step1_final_check()
    print(f"\n反映対象として最終確認できた商品数: {len(ok_targets)}/8")

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM product_master")
    before_total = cur.fetchone()[0]
    conn.close()
    print(f"\nproduct_master総数(before): {before_total}")

    reflect_results = step2_reflect(ok_targets)

    n_reflected = sum(1 for r in reflect_results if r.get("status") == "reflected")
    n_failed = len(reflect_results) - n_reflected
    print(f"\nreflect成功: {n_reflected}/{len(reflect_results)}  失敗: {n_failed}")

    all_sufficient = step3_recalculate_13_areas()
    item_code_results = step4_5_resolve_item_codes(reflect_results)
    after_total = step6_final_checks()

    print("\n" + "=" * 70)
    print("[総括]")
    print("=" * 70)
    print(f"  product_master総数: {before_total} -> {after_total}")
    print(f"  reflect成功/失敗: {n_reflected}/{n_failed}")
    print(f"  13/13すべてsufficient: {all_sufficient}")
    n_resolved = sum(1 for r in item_code_results if r.get("status") == "resolved")
    print(f"  item_code取得: {n_resolved}/{len(item_code_results)}")
    for r in item_code_results:
        if r.get("status") != "resolved":
            print(f"    未取得: product_id={r['product_id']} {r['brand']}/{r['name']} status={r['status']}")


if __name__ == "__main__":
    main()
