"""Step17-3: product_id=64を単品へ差し替える。既存のresolve_item_code_for_product()
/verify_and_resolve_item_code()経路のみを使用(新しい照合ロジックは作らない)。
DB更新は verify_and_resolve_item_code() が内部でstatus=="resolved"のときのみ
楽天関連フィールド(item_code/price_ref/last_known_rakuten_link/last_known_image/
rakuten_title/shop_name)だけを更新する既存経路に委ねる。
"""
import os

import psycopg2

import app
import product_collection_pipeline as pipeline

PRODUCT_ID = 64


def fetch_before():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("""
        SELECT product_id, brand, name, category, jan_code, item_code, price_ref,
               rakuten_title, shop_name, active_ingredients, formulation, identity_key
        FROM product_master WHERE product_id = %s
    """, (PRODUCT_ID,))
    row = cur.fetchone()
    conn.close()
    return row


def fetch_after():
    return fetch_before()


def main():
    before = fetch_before()
    if not before:
        print(f"[ABORT] product_id={PRODUCT_ID} が見つかりません")
        return
    (pid, brand, name, category, jan_code, before_item_code, before_price,
     before_title, before_shop, before_actives, before_formulation, before_identity) = before

    print("=" * 70)
    print(f"[BEFORE] product_id={pid} {brand}/{name}")
    print(f"  item_code={before_item_code} price_ref={before_price} shop={before_shop}")
    print(f"  rakuten_title={before_title}")
    print(f"  active_ingredients={before_actives}")
    print(f"  formulation={before_formulation}")
    print(f"  identity_key={before_identity}")

    print("\n[1] resolve_item_code_for_product() で候補選定(DB更新なし、プレビューのみ)")
    resolution = pipeline.resolve_item_code_for_product(brand, name, category, jan_code=jan_code)
    print(f"  status={resolution['status']}")
    print(f"  initial_candidate_count={resolution.get('initial_candidate_count')}")
    print(f"  title_matched_count={resolution.get('title_matched_count')}")
    print(f"  single_item_count={resolution.get('single_item_count')}")
    if resolution["status"] == "confirmed":
        item = resolution["item"]
        print(f"  選定item_code={item.get('itemCode')} shop={item.get('shopName')} price={item.get('itemPrice')}")
        print(f"  itemName={item.get('itemName')}")
        print(f"  disambiguated_by={resolution.get('disambiguated_by') or 'natural'}")
        print(f"  新ロジックでset判定={app._is_rakuten_set_item(str(item.get('itemName', '')))}")
        if resolution.get("tiebreak_pool"):
            print(f"  tiebreak_pool: {resolution.get('tiebreak_pool')}")
    else:
        print(f"  詳細: {resolution}")
        print("\n[ABORT] confirmed以外のためDB更新を行わない")
        return

    print("\n[2] verify_and_resolve_item_code() で再検証+実更新")
    update_result = pipeline.verify_and_resolve_item_code(pid, brand, name, category, jan_code=jan_code)
    print(f"  status={update_result.get('status')}")
    for k in ("item_code", "http_status", "disambiguated_by"):
        if k in update_result:
            print(f"  {k}={update_result.get(k)}")

    if update_result.get("status") != "resolved":
        print("\n[結果] resolved以外のためDB未更新のまま終了")
        return

    after = fetch_after()
    (_, _, _, _, _, after_item_code, after_price, after_title, after_shop,
     after_actives, after_formulation, after_identity) = after

    print("\n" + "=" * 70)
    print(f"[AFTER] product_id={pid}")
    print(f"  item_code={after_item_code} price_ref={after_price} shop={after_shop}")
    print(f"  rakuten_title={after_title}")
    print(f"  active_ingredients={after_actives}")
    print(f"  formulation={after_formulation}")
    print(f"  identity_key={after_identity}")

    print("\n[検証]")
    print(f"  item_code変化: {before_item_code} -> {after_item_code}")
    print(f"  price_ref変化: {before_price} -> {after_price}")
    print(f"  新タイトルがset判定False: {not app._is_rakuten_set_item(after_title or '')}")
    print(f"  active_ingredients不変: {before_actives == after_actives}")
    print(f"  formulation不変: {before_formulation == after_formulation}")
    print(f"  identity_key不変: {before_identity == after_identity}")
    print(f"  jan_code不変: {jan_code}")


if __name__ == "__main__":
    main()
