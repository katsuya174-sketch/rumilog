"""Step41: 実DBに対するdry-run確認用の一回限りスクリプト。

現在の本番DB(product_master)はcoverage_gap=0(13/13 sufficient)である前提で、
work_items=0 -> discovery=0(candidate_sourceが一度も呼ばれない) ->
actions=0 であることを確認する。mode="dry_run"かつcandidate_sourceは
明示注入しない(Step41の既定tiered discoveryを使う)。Gemini/Rakuten実APIは
一切呼ばない想定(work_items=0ならprocess_coverage_gap_item自体が呼ばれず、
candidate_sourceも呼ばれない)。
"""
import product_master_pipeline as orchestrator

result = orchestrator.run_batch(mode="dry_run")
print(result)
assert result["work_items"] == 0, f"想定外: work_items={result['work_items']} (coverage_gapが0件のはずの前提が崩れている)"
assert result["actions"] == [], f"想定外: actions={result['actions']}"
print("[STEP41 REAL DB DRY RUN OK] work_items=0 -> discovery=0(candidate_source未呼び出し) -> actions=0")
