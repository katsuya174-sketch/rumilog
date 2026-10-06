"""Step25: product_id=28(WHITH WHITE)について、検索APIによる再発見は行わず、
既知のitemCode(tsurunishi:10000242)をfetch_rakuten_item_by_item_code()で
1回だけ直接再検証する。全ゲートを満たした場合のみupdate_product_master_
item_code_fields()で楽天関連フィールドのみ更新する(成分/JAN/formulation/
tags/identity等は変更しない)。コード変更なし。
"""
import os

import psycopg2

import app

PRODUCT_ID = 28
ITEM_CODE = "tsurunishi:10000242"
EXPECTED_VOLUME_ML = 150


def fetch_before():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("""
        SELECT brand, name, category, jan_code, item_code, active_ingredients,
               formulation, active_ingredient_tags, identity_key
        FROM product_master WHERE product_id = %s
    """, (PRODUCT_ID,))
    row = cur.fetchone()
    conn.close()
    return row


def main():
    before = fetch_before()
    if not before:
        print(f"[ABORT] product_id={PRODUCT_ID} not found")
        return
    (brand, name, category, jan_code, before_item_code, before_actives,
     before_formulation, before_tags, before_identity) = before

    print(f"[BEFORE] product_id={PRODUCT_ID} {brand}/{name} item_code={before_item_code!r}")

    print(f"\n[1] fetch_rakuten_item_by_item_code('{ITEM_CODE}') を1回だけ実行")
    result = app.fetch_rakuten_item_by_item_code(ITEM_CODE)
    print(f"  http_status={result.get('http_status')} ok={result.get('ok')} rakuten_error={result.get('rakuten_error')}")

    if not result.get("ok") or not result.get("item"):
        print("\n[結果] itemCode直接取得が失敗。DB更新せず28はNULLのまま終了。")
        return

    item = result["item"]
    item_name = str(item.get("itemName", "") or "")
    item_price = app.safe_price(item.get("itemPrice", 0))
    item_url = item.get("itemUrl", "")
    item_code_returned = item.get("itemCode", "")
    item_images = item.get("mediumImageUrls") or []
    image_url = ""
    if item_images:
        first_img = item_images[0]
        image_url = first_img.get("imageUrl", "") if isinstance(first_img, dict) else str(first_img)

    print(f"\n  itemName={item_name}")
    print(f"  itemPrice={item_price}")
    print(f"  itemUrl={item_url}")
    print(f"  itemCode(返却値)={item_code_returned}")
    print(f"  availability={item.get('availability')}")

    print("\n[2] 安全ゲートの確認")
    gate_same_product = app.is_same_verified_rakuten_product(name, item_name, brand, str(item.get("shopName", "") or ""))
    gate_not_set = not app._is_rakuten_set_item(item_name)
    gate_is_emulsion = "乳液" in item_name
    gate_volume_150ml = f"{EXPECTED_VOLUME_ML}ml" in item_name.lower() or f"{EXPECTED_VOLUME_ML}mL" in item_name
    gate_price_positive = item_price > 0
    gate_url_present = bool(item_url)
    gate_item_code_match = (item_code_returned == ITEM_CODE)

    print(f"  is_same_verified_rakuten_product() = {gate_same_product} (期待: True)")
    print(f"  _is_rakuten_set_item() = {not gate_not_set} -> 単品(set=False) = {gate_not_set} (期待: True)")
    print(f"  薬用美白乳液(「乳液」含む) = {gate_is_emulsion} (期待: True)")
    print(f"  容量150ml表記あり = {gate_volume_150ml} (期待: True)")
    print(f"  price > 0 = {gate_price_positive} (price={item_price})")
    print(f"  URLあり = {gate_url_present}")
    print(f"  itemCode一致 = {gate_item_code_match}")

    all_gates = [
        gate_same_product, gate_not_set, gate_is_emulsion, gate_volume_150ml,
        gate_price_positive, gate_url_present, gate_item_code_match,
    ]

    if not all(all_gates):
        print("\n[結果] 1件以上のゲートを満たさないため、DB更新せず28はNULLのまま終了。")
        return

    print("\n[3] 全ゲート通過。update_product_master_item_code_fields()でDB更新を実行。")
    app.update_product_master_item_code_fields(
        PRODUCT_ID, item_code_returned, item_price, item_url,
        image=image_url, rakuten_title=item_name,
        shop_name=str(item.get("shopName", "") or ""),
    )
    print("  [UPDATED]")

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("""
        SELECT brand, name, category, jan_code, item_code, price_ref, rakuten_title,
               active_ingredients, formulation, active_ingredient_tags, identity_key
        FROM product_master WHERE product_id = %s
    """, (PRODUCT_ID,))
    after = cur.fetchone()
    conn.close()

    (after_brand, after_name, after_category, after_jan, after_item_code,
     after_price, after_title, after_actives, after_formulation, after_tags, after_identity) = after

    print("\n[検証]")
    print(f"  item_code: {before_item_code!r} -> {after_item_code!r}")
    print(f"  price_ref: -> {after_price}")
    print(f"  rakuten_title: -> {after_title}")
    print(f"  jan_code不変: {jan_code == after_jan}")
    print(f"  active_ingredients不変: {before_actives == after_actives}")
    print(f"  formulation不変: {before_formulation == after_formulation}")
    print(f"  active_ingredient_tags不変: {before_tags == after_tags}")
    print(f"  identity_key不変: {before_identity == after_identity}")
    print(f"  brand不変: {brand == after_brand}")
    print(f"  name不変: {name == after_name}")


if __name__ == "__main__":
    main()
