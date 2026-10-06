"""Step19-1: product_id=49のブランド名誤記(クレラシル→クレアラシル)を訂正。
brand/name/identity_keyのみ更新。成分・formulation・JAN・tags等は変更しない。
更新前に新identity_keyが既存66件と衝突しないことを確認する。
"""
import os

import psycopg2

import app

PRODUCT_ID = 49
OLD_BRAND = "クレラシル"
NEW_BRAND = "クレアラシル"


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    cur.execute("""
        SELECT brand, name, category, jan_code, active_ingredients, formulation,
               active_ingredient_tags, item_code, price_ref, identity_key
        FROM product_master WHERE product_id = %s
    """, (PRODUCT_ID,))
    row = cur.fetchone()
    if not row:
        print(f"[ABORT] product_id={PRODUCT_ID} not found")
        return
    (brand, name, category, jan_code, actives, formulation,
     tags, item_code, price_ref, old_identity_key) = row

    if brand != OLD_BRAND:
        print(f"[ABORT] brand mismatch: expected '{OLD_BRAND}' but got '{brand}'")
        return

    new_brand = NEW_BRAND
    new_name = name.replace(OLD_BRAND, NEW_BRAND)
    new_identity_key = app._normalize_product_master_identity_key(new_brand, new_name, category)

    print(f"[BEFORE] brand={brand!r} name={name!r} identity_key={old_identity_key!r}")
    print(f"[AFTER]  brand={new_brand!r} name={new_name!r} identity_key={new_identity_key!r}")

    if not new_identity_key:
        print("[ABORT] new identity_key is empty")
        conn.close()
        return

    cur.execute(
        "SELECT product_id FROM product_master WHERE identity_key = %s AND product_id != %s",
        (new_identity_key, PRODUCT_ID),
    )
    collisions = cur.fetchall()
    if collisions:
        print(f"[ABORT] identity_key collision with existing product_id(s): {collisions}")
        conn.close()
        return
    print("[OK] no identity_key collision with the other 65 rows")

    cur.execute(
        "UPDATE product_master SET brand = %s, name = %s, identity_key = %s, updated_at = NOW() "
        "WHERE product_id = %s",
        (new_brand, new_name, new_identity_key, PRODUCT_ID),
    )
    conn.commit()
    print(f"[UPDATED] product_id={PRODUCT_ID}")

    cur.execute("""
        SELECT brand, name, category, jan_code, active_ingredients, formulation,
               active_ingredient_tags, item_code, price_ref, identity_key
        FROM product_master WHERE product_id = %s
    """, (PRODUCT_ID,))
    after_row = cur.fetchone()
    conn.close()

    (after_brand, after_name, after_category, after_jan, after_actives,
     after_formulation, after_tags, after_item_code, after_price, after_identity) = after_row

    print("\n[検証]")
    print(f"  brand: {brand} -> {after_brand}")
    print(f"  name: {name} -> {after_name}")
    print(f"  identity_key: {old_identity_key} -> {after_identity}")
    print(f"  category不変: {category == after_category}")
    print(f"  jan_code不変: {jan_code == after_jan}")
    print(f"  active_ingredients不変: {actives == after_actives}")
    print(f"  formulation不変: {formulation == after_formulation}")
    print(f"  active_ingredient_tags不変: {tags == after_tags}")
    print(f"  item_code不変: {item_code == after_item_code}")
    print(f"  price_ref不変: {price_ref == after_price}")


if __name__ == "__main__":
    main()
