"""Step14: product_masterのitem_code未補完29件について、既存の検証済み
楽天照合経路(resolve_item_code_for_product/fetch_rakuten_item_by_item_code)
をdry-runで実行する(一回限りの運用スクリプト)。product_masterへの更新は
一切行わない。コード変更は行わず、既存関数をそのまま呼び出すのみ。

容量・限定版・旧製品・海外版・詰替え等の変種判別は、既存コードに手を
入れず、本スクリプト側の後段チェック(VARIANT_WARNING_KEYWORDS)で
resolved候補をambiguous相当に後方分類する(既存のresolve関数自体は
変更しない)。
"""
import os
import time

import psycopg2
import requests

import app
import product_collection_pipeline as pipeline

VARIANT_WARNING_KEYWORDS = [
    "詰め替え", "つめかえ", "詰替", "リフィル", "限定", "数量限定", "期間限定",
    "旧", "海外", "輸入", "訳あり", "アウトレット", "中古", "型落ち",
    "本体", "付け替え", "替え",
]


def fetch_targets():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("""
        SELECT product_id, brand, name, category, jan_code
        FROM product_master WHERE item_code IS NULL OR item_code = ''
        ORDER BY product_id
    """)
    rows = cur.fetchall()
    conn.close()
    return rows


def check_variant_warning(item_name, product_name):
    hits = [kw for kw in VARIANT_WARNING_KEYWORDS if kw in (item_name or "")]
    # 元の商品名側にも同じ語が含まれていれば(例:商品名自体が「詰め替え用」)誤検知としない
    hits = [kw for kw in hits if kw not in (product_name or "")]
    return hits


def main():
    if not app.RAKUTEN_APP_ID or not app.RAKUTEN_ACCESS_KEY:
        print("[ABORT] RAKUTEN_APP_ID/RAKUTEN_ACCESS_KEY が設定されていません。")
        return

    targets = fetch_targets()
    print(f"対象: {len(targets)}件\n")

    request_log = []
    real_get = requests.get

    def counting_get(url, params=None, **kwargs):
        res = real_get(url, params=params, **kwargs)
        kind = "item_code_verify" if (params and "itemCode" in params) else "search"
        request_log.append({"kind": kind, "status_code": res.status_code})
        return res

    results = []
    requests.get = counting_get
    try:
        for product_id, brand, name, category, jan_code in targets:
            resolution = pipeline.resolve_item_code_for_product(brand, name, category, jan_code=jan_code)
            final_status = resolution["status"]
            verify_result = None
            variant_warning = []

            if final_status == "confirmed":
                candidate = resolution["item"]
                item_title = str(candidate.get("itemName", "") or "")
                variant_warning = check_variant_warning(item_title, name)
                if variant_warning:
                    final_status = "ambiguous_variant_risk"
                else:
                    item_code = str(candidate.get("itemCode", "") or "").strip()
                    if item_code:
                        verify_result = app.fetch_rakuten_item_by_item_code(item_code)
                        v_item = (verify_result or {}).get("item") or {}
                        price = app.safe_price(v_item.get("itemPrice", 0))
                        url = v_item.get("itemUrl", "")
                        if not verify_result.get("ok") or price <= 0 or not url:
                            final_status = "verification_failed"
                        else:
                            final_status = "resolved"
                    else:
                        final_status = "verification_failed"

            results.append({
                "product_id": product_id, "brand": brand, "name": name, "category": category,
                "jan_code": jan_code, "resolution": resolution, "final_status": final_status,
                "verify_result": verify_result, "variant_warning": variant_warning,
            })
            print(f"[{product_id}] {brand}/{name} -> {final_status}", flush=True)
    finally:
        requests.get = real_get

    print("\n" + "=" * 70)
    print("===== 商品別詳細 =====")
    for r in results:
        res = r["resolution"]
        print(f"\nproduct_id={r['product_id']} [{r['category']}] {r['brand']} / {r['name']}")
        print(f"  検索候補数(initial_candidate_count): {res.get('initial_candidate_count')}")
        print(f"  identity一致数(title_matched_count): {res.get('title_matched_count')}")
        print(f"  set除外後数(single_item_count): {res.get('single_item_count')}")

        if res["status"] == "confirmed":
            item = res["item"]
            print(f"  選定item_code: {item.get('itemCode')}  shop: {item.get('shopName')}  "
                  f"price: {item.get('itemPrice')}")
            print(f"  選定根拠(disambiguated_by): {res.get('disambiguated_by') or 'natural'}")
            if r["variant_warning"]:
                print(f"  *** 変種リスク検出: {r['variant_warning']} (商品名: {item.get('itemName')}) ***")
            if r["verify_result"] is not None:
                vr = r["verify_result"]
                print(f"  itemCode再検証: ok={vr.get('ok')} http_status={vr.get('http_status')} "
                      f"price={((vr.get('item') or {}).get('itemPrice'))} url={bool((vr.get('item') or {}).get('itemUrl'))}")
        else:
            print(f"  詳細: status={res['status']} reason={res.get('reason')} "
                  f"candidate_count={res.get('candidate_count')}")
        print(f"  最終status: {r['final_status']}")

    print("\n" + "=" * 70)
    print("===== 集計 =====")
    status_counts = {}
    for r in results:
        status_counts[r["final_status"]] = status_counts.get(r["final_status"], 0) + 1
    print(f"status内訳: {status_counts}")

    all_search = [c for c in request_log if c["kind"] == "search"]
    all_verify = [c for c in request_log if c["kind"] == "item_code_verify"]
    errors = [c for c in request_log if c["status_code"] not in (200,)]
    print(f"\nAPIリクエスト総数: {len(request_log)} (search={len(all_search)}, item_code_verify={len(all_verify)})")
    print(f"429/400等エラー: {len(errors)}件")
    for e in errors:
        print(f"  {e}")

    resolved_item_codes = [r["resolution"]["item"].get("itemCode") for r in results
                            if r["final_status"] == "resolved" and r["resolution"]["status"] == "confirmed"]
    dup = {c for c in resolved_item_codes if resolved_item_codes.count(c) > 1}
    print(f"\nitem_code重複候補: {dup if dup else 'なし'}")

    variant_risk_cases = [r for r in results if r["variant_warning"]]
    print(f"\n明らかな容量/セット/変種違い候補: {len(variant_risk_cases)}件")
    for r in variant_risk_cases:
        print(f"  product_id={r['product_id']} {r['brand']}/{r['name']}: {r['variant_warning']}")


if __name__ == "__main__":
    main()
