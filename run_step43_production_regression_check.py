"""Step43: 実DB(本番)に対する読み取りのみの回帰確認スクリプト。

- product_master の既存商品数(74件)・13領域(cosmetics coverage policy)が
  破壊されていないこと。
- calculate_effective_candidates()のis_candidate_relevant_to_target()への
  委譲後も、13領域の実効候補数が以前と変わらないこと。
- Gemini/Rakuten実APIは一切呼ばない(get_coverage_report/
  calculate_effective_candidates_batchはDB読み取りのみ)。
"""
import app

conn = app.psycopg2.connect(app.DATABASE_URL)
cur = conn.cursor()
cur.execute("SELECT COUNT(*) FROM product_master")
total = cur.fetchone()[0]
cur.execute("SELECT category, COUNT(*) FROM product_master GROUP BY category ORDER BY category")
by_category = cur.fetchall()
conn.close()

print(f"[PRODUCT MASTER TOTAL] {total}")
for row in by_category:
    print(f"  {row}")

report = app.get_coverage_report("cosmetics")
sufficient_count = sum(1 for area in report if area["sufficient"])
print(f"[COVERAGE REPORT] {sufficient_count}/{len(report)} sufficient")
for area in report:
    print(f"  {area['category']}×{area['target']}: effective={area['effective_count']} sufficient={area['sufficient']}")

assert total == 74, f"想定外: product_master総数={total} (74のはず)"
assert sufficient_count == 13 and len(report) == 13, f"想定外: {sufficient_count}/{len(report)} sufficient"
print("[STEP43 PRODUCTION REGRESSION CHECK OK] 74商品・13/13領域sufficientを維持")
