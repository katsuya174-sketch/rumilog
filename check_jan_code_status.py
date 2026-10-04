"""Step3調査: 既存38件のJAN保有状況を確認する(読み取り専用)。"""
import json
import os

import psycopg2

import app  # noqa: F401

BATCH_ID = "pilot-1791040996"


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*), COUNT(jan_code) FROM product_master WHERE data_source = 'ai_precollected'")
    total, with_jan_in_master = cur.fetchone()
    print(f"product_master: 総数={total} jan_code非NULL={with_jan_in_master}")

    cur.execute("""
        SELECT brand, product_name, stage2_payload
        FROM product_collection_staging
        WHERE batch_id = %s AND stage2_status = 'ok' AND conflict_status != 'needs_review'
    """, (BATCH_ID,))
    rows = cur.fetchall()
    conn.close()

    has_real_jan = 0
    unknown_jan = 0
    missing_key = 0
    samples = []
    for brand, name, payload in rows:
        jan = (payload or {}).get("jan_code")
        if jan is None:
            missing_key += 1
        elif str(jan).strip().lower() == "unknown" or not str(jan).strip():
            unknown_jan += 1
        else:
            has_real_jan += 1
            samples.append((brand, name, jan))

    print(f"\nstaging(38件, 反映対象)の内訳:")
    print(f"  実在するJAN値あり: {has_real_jan}件")
    print(f"  'unknown'または空: {unknown_jan}件")
    print(f"  jan_codeキー自体が無い: {missing_key}件")
    print(f"\n実JAN保有サンプル(先頭10件):")
    for b, n, j in samples[:10]:
        print(f"  {b} / {n}: {j}")


if __name__ == "__main__":
    main()
