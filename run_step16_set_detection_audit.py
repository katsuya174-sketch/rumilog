"""Step16: 楽天セット判定ギャップ調査＋全61件再監査（read-only）。
コード変更・DB変更は一切行わない。既存関数(infer_bundle_quantity_from_title,
_is_rakuten_set_item等)はそのまま呼び出すのみで、新判定案はこのスクリプト内の
別関数としてシミュレーションする。
"""
import os
import re

import psycopg2

import app

CEZANNE_TITLE = "セザンヌ うるオフ クレンジングバーム(90g×2セット)【セザンヌ(CEZANNE)】"


def step2_reproduce_product_64():
    print("=" * 70)
    print("Step2: product_id=64 再現")
    print("=" * 70)
    print(f"title: {CEZANNE_TITLE}")
    print(f"infer_bundle_quantity_from_title() -> {app.infer_bundle_quantity_from_title(CEZANNE_TITLE)}")
    print(f"_is_rakuten_set_item() -> {app._is_rakuten_set_item(CEZANNE_TITLE)}")
    print(f"_RAKUTEN_SET_TITLE_RE.search() -> {bool(app._RAKUTEN_SET_TITLE_RE.search(CEZANNE_TITLE))}")
    hard_reject_hits = [w for w in ["詰替", "詰め替え", "つめかえ", "レフィル", "付け替え", "つけかえ",
                                     "お試し", "サンプル", "ミニサイズ", "トライアル", "まとめ買い",
                                     "2個", "3個", "4個", "5個", "6個",
                                     "2本", "3本", "4本", "5本", "6本",
                                     "中古", "廃盤", "廃番", "生産終了", "販売終了", "製造終了"]
                         if w in CEZANNE_TITLE]
    print(f"hard_reject_words(score_rakuten_item内) 一致語: {hard_reject_hits or 'なし'}")
    print()


def fetch_61_titles():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("""
        SELECT product_id, brand, name, rakuten_title, price_ref
        FROM product_master
        WHERE item_code IS NOT NULL AND item_code != ''
        ORDER BY product_id
    """)
    rows = cur.fetchall()
    conn.close()
    return rows


# ---- 新判定案（シミュレーション専用、本体コードは変更しない） ----

_NEW_UNIT_X_COUNT_RE = re.compile(
    r"(?:\d+(?:\.\d+)?)\s*(?:g|ml|mL|kg|mg|L)\s*[×xX]\s*([2-9]\d*)",
)
_NEW_IRI_BARE_RE = re.compile(r"([2-9]\d*)\s*(?:個|本)\s*入り(?!セット)")
_NEW_PACK_RE = re.compile(r"([2-9]\d*)\s*(?:個|本)\s*パック")
_NEW_PCS_RE = re.compile(r"[\(（]\s*([2-9]\d*)\s*(?:P|pcs|pc)\s*[\)）]", re.IGNORECASE)
_NEW_BARE_COUNT_SET_RE = re.compile(r"[×xX]\s*([2-9]\d*)\s*セット")


def new_is_set_candidate(title):
    """新判定案: 既存ロジックに追加するパターン群。どの規則が一致したかも返す。"""
    if not title:
        return False, []
    hits = []
    m = _NEW_UNIT_X_COUNT_RE.search(title)
    if m:
        hits.append(f"unit×count ({m.group(0)})")
    m = _NEW_BARE_COUNT_SET_RE.search(title)
    if m:
        hits.append(f"×Nセット ({m.group(0)})")
    m = _NEW_IRI_BARE_RE.search(title)
    if m:
        hits.append(f"N個/本入り(bare) ({m.group(0)})")
    m = _NEW_PACK_RE.search(title)
    if m:
        hits.append(f"N個/本パック ({m.group(0)})")
    m = _NEW_PCS_RE.search(title)
    if m:
        hits.append(f"(NP/pcs) ({m.group(0)})")
    return (len(hits) > 0), hits


def step3_4_6_audit_61(rows):
    print("=" * 70)
    print("Step3/4/6: 61件再監査 + 新判定シミュレーション")
    print("=" * 70)
    print(f"総件数: {len(rows)}\n")

    already_caught = []
    newly_flagged = []
    clean = []

    for product_id, brand, name, rakuten_title, price_ref in rows:
        title = rakuten_title or ""
        existing = app._is_rakuten_set_item(title)
        new_flag, new_hits = new_is_set_candidate(title)

        if existing:
            already_caught.append((product_id, brand, name, title))
            continue

        if new_flag:
            newly_flagged.append((product_id, brand, name, title, price_ref, new_hits))
        else:
            clean.append((product_id, brand, name, title))

    print(f"[A] 既存判定で既にセット判定される件数(本来ここに残っているのは理論上おかしい): {len(already_caught)}")
    for p in already_caught:
        print(f"    product_id={p[0]} {p[1]}/{p[2]} title={p[3]}")

    print(f"\n[B] 既存判定は素通り、新判定案で新たにセット候補として検出: {len(newly_flagged)}")
    for pid, brand, name, title, price, hits in newly_flagged:
        print(f"    product_id={pid} {brand}/{name}")
        print(f"      title: {title}")
        print(f"      price_ref: {price}")
        print(f"      一致規則: {hits}")

    print(f"\n[C] 既存判定・新判定ともにセット判定されない(クリーン): {len(clean)}")

    return newly_flagged, clean


def step5_false_positive_probe():
    """容量そのものの表記を誤検出しないことの確認用サンプル。"""
    print("\n" + "=" * 70)
    print("Step5: false positive 回避サンプルテスト(容量単体表記)")
    print("=" * 70)
    safe_samples = [
        "テスト商品 化粧水 200ml",
        "テスト商品 美容液 30ml 高保湿タイプ",
        "テスト商品 クリーム 2g お試しサイズ",
        "テスト商品 乳液 100ml SPF30 PA+++",
        "テスト商品 パック 5枚入り",  # 単品が5枚入りパック商品(枚=シート数、個/本ではない)
        "テスト商品 洗顔フォーム 120g",
    ]
    for s in safe_samples:
        flag, hits = new_is_set_candidate(s)
        print(f"  '{s}' -> flagged={flag} hits={hits}")

    print("\n  (意図的にセットを示すサンプル)")
    bundle_samples = [
        "テスト商品 クレンジングバーム(90g×2セット)",
        "テスト商品 化粧水 100ml×2",
        "テスト商品 美容液 2個セット",
        "テスト商品 洗顔 3本入り",
        "テスト商品 マスク 2個パック",
        "テスト商品 クリーム (2P)",
    ]
    for s in bundle_samples:
        flag, hits = new_is_set_candidate(s)
        print(f"  '{s}' -> flagged={flag} hits={hits}")


def step7_check_single_unit_candidate_for_64():
    print("\n" + "=" * 70)
    print("Step7(任意): product_id=64 単品候補の有無確認(楽天API最小呼び出し, DB更新なし)")
    print("=" * 70)
    if not app.RAKUTEN_APP_ID or not app.RAKUTEN_ACCESS_KEY:
        print("[SKIP] RAKUTEN_APP_ID/RAKUTEN_ACCESS_KEY 未設定のためスキップ")
        return

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("SELECT brand, name, category, jan_code FROM product_master WHERE product_id = 64")
    row = cur.fetchone()
    conn.close()
    if not row:
        print("[SKIP] product_id=64 が見つかりません")
        return
    brand, name, category, jan_code = row
    print(f"対象: {brand} / {name} (category={category}, jan_code={jan_code})")

    scored_candidates = app.fetch_rakuten_candidates(name, category=category, brand=brand)
    print(f"検索結果件数: {len(scored_candidates) if scored_candidates else 0}")

    api_calls = 1
    single_candidates = []
    for score, c in (scored_candidates or []):
        title = str(c.get("itemName", "") or "")
        is_set_existing, _ = new_is_set_candidate(title)
        is_set_old = app._is_rakuten_set_item(title)
        if not is_set_old and not is_set_existing:
            single_candidates.append(c)

    print(f"既存+新判定の両方で「セットではない」と判定される候補数: {len(single_candidates)}")
    for c in single_candidates[:10]:
        print(f"  itemCode={c.get('itemCode')} title={c.get('itemName')} price={c.get('itemPrice')} shop={c.get('shopName')}")

    print(f"\nこの確認で使用したAPI呼び出し数: {api_calls} (search 1回, item_code検証は未実施)")
    return api_calls


def main():
    api_call_count = 0
    step2_reproduce_product_64()
    rows = fetch_61_titles()
    newly_flagged, clean = step3_4_6_audit_61(rows)
    step5_false_positive_probe()
    used = step7_check_single_unit_candidate_for_64()
    if used:
        api_call_count += used

    print("\n" + "=" * 70)
    print(f"API使用数(本スクリプト全体): {api_call_count}")
    print("=" * 70)


if __name__ == "__main__":
    main()
