"""Product Master半自動オーケストレーター(Step40)。

Step39のwork queue(app.generate_product_master_work_queue())を入力として、
work_type別(coverage_gap/stale_reverification/needs_review)に既存のPhase3
関数(Stage1/2収集、identity/dedup、reflect、Rakuten resolver)を呼び出して
構成する。ここでは新しい収集・照合・判定ロジックは一切作らない。

安全原則:
- mode="dry_run"が既定。実行(mode="execute")には呼び出し元の明示指定が必要。
  dry_runではGemini/Rakuten実呼び出し・reflect・DB更新を一切行わない。
- needs_review/citation不足/category validator失敗/confidence不足(variant
  不明相当)は自動reflectしない。既存のdetect_conflicts()/
  validate_category_attributes()/is_official_source_confirmed()をそのまま
  再利用する(別の判定ロジックは作らない)。
- 同一coverage領域で候補が連続して失敗したら、その領域だけSTOPし他領域は
  継続する。バッチ全体の商品数/API呼び出し数/推定費用が上限へ達したら、
  残りのwork itemを実行せずバッチ全体をSTOPする。
- cosmetics専用ではない。category policy(app.COVERAGE_POLICIES)/category
  validator(app.validate_category_attributes)へ委譲する構造を維持し、
  policyが未定義のカテゴリは勝手な基準で収集しない(work queue自体に
  そのカテゴリのcoverage_gapが現れない)。
"""
import os
import time

import psycopg2

import app
import product_collection_pipeline as pipeline

DEFAULT_MAX_PRODUCTS_PER_BATCH = 5
DEFAULT_MAX_API_CALLS = 20
DEFAULT_MAX_COST_USD = 0.50
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3


def _default_candidate_source(category, target, limit):
    """候補探索の既定実装。商品名を自動で発見する機能(Web全体からの
    ブレインストーミング等)はまだ実装していない(Step37で指摘した未実装
    機能)。呼び出し元がcandidate_sourceを明示的に注入しない限り、常に
    空リストを返す(=そのcoverage_gap領域は「候補が見つからない」として
    処理され、自動で架空の商品名を作ることはない)。
    """
    return []


class BatchBudget:
    """バッチ全体で共有する上限管理(商品数・API呼び出し数・推定費用)。
    費用・呼び出し数は既存のproduct_collection_usageテーブル(record_
    usage_and_check_limitが書き込む実測値)をそのまま参照し、別の費用計算
    ロジックは作らない。"""

    def __init__(self, batch_id, max_products, max_api_calls, max_cost_usd):
        self.batch_id = batch_id
        self.max_products = max_products
        self.max_api_calls = max_api_calls
        self.max_cost_usd = max_cost_usd
        self.products_attempted = 0
        self.stopped_reason = None

    def usage_snapshot(self):
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*), COALESCE(SUM(estimated_cost_usd), 0) "
                "FROM product_collection_usage WHERE batch_id = %s",
                (self.batch_id,),
            )
            calls, cost = cur.fetchone()
            return calls, float(cost)
        finally:
            conn.close()

    def can_continue(self):
        if self.stopped_reason:
            return False
        if self.products_attempted >= self.max_products:
            self.stopped_reason = "max_products_reached"
            return False
        calls, cost = self.usage_snapshot()
        if calls >= self.max_api_calls:
            self.stopped_reason = "max_api_calls_reached"
            return False
        if cost >= self.max_cost_usd:
            self.stopped_reason = "max_cost_reached"
            return False
        return True

    def record_attempt(self):
        self.products_attempted += 1


def _existing_identity_keys():
    """db_products + verified_products_cache + product_master全体の
    identity_keyを集めたdedup用集合。新しい照合ロジックは作らず、既存の
    make_verified_product_key()をそのまま使う。"""
    db_products = app.load_products()
    verified_products = app.load_verified_products_cache()
    keys = set()
    for p in db_products + verified_products:
        if isinstance(p, dict):
            k = app.make_verified_product_key(p)
            if k:
                keys.add(k)

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        cur = conn.cursor()
        cur.execute("SELECT identity_key FROM product_master")
        keys.update(row[0] for row in cur.fetchall())
    finally:
        conn.close()
    return keys


def _product_master_lookup(brand, name, category):
    results = app.query_product_master_candidates(category)
    key = app._normalize_product_master_identity_key(brand, name, category)
    for r in results:
        if app._normalize_product_master_identity_key(
            r.get("brand", ""), r.get("name", ""), r.get("category", "")
        ) == key:
            return r
    return None


def _fetch_staging_row(staging_id):
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT stage1_status, stage1_citations, stage2_status, stage2_payload, conflict_status "
            "FROM product_collection_staging WHERE staging_id = %s",
            (staging_id,),
        )
        row = cur.fetchone()
        if not row:
            return {}
        return {
            "stage1_status": row[0], "stage1_citations": row[1] or [],
            "stage2_status": row[2], "stage2_payload": row[3] or {}, "conflict_status": row[4],
        }
    finally:
        conn.close()


def _is_confident_enough(staging_row, brand):
    """citation不足/variant不明(確信度不足)に対する安全側ゲート。新しい
    照合ロジックは作らず、既存のcitation数とis_official_source_confirmed()
    (sanitize_stage2_payload内で既に決定論的に算出され、stage2_payload
    のofficial_source_confirmedに入っている値)だけを見る。"""
    if staging_row.get("stage1_status") != "ok":
        return False, "stage1_not_ok"
    if len(staging_row.get("stage1_citations") or []) < 1:
        return False, "no_citations"
    payload = staging_row.get("stage2_payload") or {}
    if not payload.get("active_ingredients") and not payload.get("category_attributes"):
        return False, "no_extracted_fields"
    if not payload.get("official_source_confirmed"):
        return False, "official_source_not_confirmed_variant_uncertain"
    return True, None


def _would_lose_confirmed_ingredient_tags(existing_product, staged_payload):
    """再検証(stale_reverification)専用の安全ゲート: 既存のproduct_master
    行が既に持つ統制タグ(compute_ingredient_tags()の結果)のうち、新しい
    stage2_payloadの抽出結果には含まれなくなるものがあるかを調べる。
    新規収集(coverage_gap)には既存情報が無いため、このチェックは適用しない。
    """
    existing_tags = set(app.compute_ingredient_tags(existing_product.get("active_ingredients") or []))
    if not existing_tags:
        return False, set()

    new_ingredients = [
        i.get("ingredient") for i in (staged_payload.get("active_ingredients") or [])
        if str(i.get("ingredient", "") or "").strip().lower() != "unknown"
    ]
    new_tags = set(app.compute_ingredient_tags(new_ingredients))
    lost = existing_tags - new_tags
    return bool(lost), lost


def _resolve_item_code_safely(product_id, brand, name, category):
    """既存のitem_code解決経路(verify_and_resolve_item_code)をそのまま
    呼ぶだけ。新しい照合ロジックは作らない。確定できない場合はitem_code
    はNULLのまま(無理な紐付けをしない、既存の安全設計をそのまま継承)。"""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        cur = conn.cursor()
        cur.execute("SELECT jan_code FROM product_master WHERE product_id = %s", (product_id,))
        row = cur.fetchone()
        jan_code = row[0] if row else None
    finally:
        conn.close()

    update_result = pipeline.verify_and_resolve_item_code(product_id, brand, name, category, jan_code=jan_code)
    return {"status": update_result.get("status"), "item_code": update_result.get("item_code")}


def process_coverage_gap_item(item, mode, batch_id, budget, candidate_source,
                               max_consecutive_failures, existing_keys):
    category, target, shortage = item["category"], item["target"], item["shortage_count"]
    candidates = candidate_source(category, target, shortage)

    actions = []
    consecutive_failures = 0
    reflected_count = 0

    for cand in candidates:
        if reflected_count >= shortage:
            break
        if mode == "execute" and not budget.can_continue():
            actions.append({"action": "batch_stop", "reason": budget.stopped_reason})
            break
        if consecutive_failures >= max_consecutive_failures:
            actions.append({
                "action": "area_stop", "category": category, "target": target,
                "reason": "consecutive_failures_limit_reached",
            })
            break

        brand, name = cand.get("brand", ""), cand.get("name", "")
        key = app.make_verified_product_key({"brand": brand, "name": name, "category": category})
        if key and key in existing_keys:
            actions.append({"action": "skipped_duplicate", "brand": brand, "name": name, "category": category})
            continue

        if mode == "dry_run":
            actions.append({
                "action": "would_collect", "brand": brand, "name": name,
                "category": category, "target": target,
            })
            continue

        budget.record_attempt()
        existing_product = _product_master_lookup(brand, name, category)
        collect_result = pipeline.collect_one_product(brand, name, category, batch_id, existing_product=existing_product)
        staging_row = _fetch_staging_row(collect_result["staging_id"])

        if collect_result["conflict_status"] == "needs_review":
            consecutive_failures += 1
            actions.append({
                "action": "not_reflected", "brand": brand, "name": name,
                "reason": "needs_review", "staging_id": collect_result["staging_id"],
            })
            continue

        confident, reason = _is_confident_enough(staging_row, brand)
        category_check = app.validate_category_attributes(
            category, (staging_row.get("stage2_payload") or {}).get("category_attributes")
        )
        can_reflect = (
            collect_result["stage2_status"] == "ok" and confident and category_check["valid"]
        )

        if not can_reflect:
            consecutive_failures += 1
            actions.append({
                "action": "not_reflected", "brand": brand, "name": name,
                "reason": reason or "category_validator_failed",
                "category_validator": category_check,
                "staging_id": collect_result["staging_id"],
            })
            continue

        reflect_result = pipeline.reflect_staging_to_product_master(collect_result["staging_id"], dry_run=False)
        if reflect_result.get("status") == "reflected":
            consecutive_failures = 0
            reflected_count += 1
            existing_keys.add(key)
            item_code_result = _resolve_item_code_safely(reflect_result["product_id"], brand, name, category)
            actions.append({
                "action": "reflected", "brand": brand, "name": name,
                "product_id": reflect_result["product_id"], "item_code_result": item_code_result,
            })
        else:
            consecutive_failures += 1
            actions.append({"action": "reflect_failed", "brand": brand, "name": name, "detail": reflect_result})

        if collect_result.get("limit_exceeded"):
            actions.append({"action": "batch_stop", "reason": "cost_limit_exceeded"})
            break

    return actions


def process_stale_item(item, mode, batch_id, budget):
    brand, name, category = item["brand"], item["name"], item["category"]
    product_id = item["product_id"]

    # recently verified再確認(安全ゲート): 処理直前にもう一度鮮度を確認し、
    # work queue生成時点から状態が変わっていないかを確かめる。
    fresh_check = app.get_stale_product_master_candidates(category=category, limit=500)
    still_stale = any(p.get("_product_master_id") == product_id for p in fresh_check)
    if not still_stale:
        return [{"action": "skipped_recently_verified", "product_id": product_id}]

    if mode == "dry_run":
        return [{"action": "would_reverify", "brand": brand, "name": name, "product_id": product_id}]

    if not budget.can_continue():
        return [{"action": "batch_stop", "reason": budget.stopped_reason}]

    budget.record_attempt()
    existing_product = _product_master_lookup(brand, name, category)
    collect_result = pipeline.collect_one_product(brand, name, category, batch_id, existing_product=existing_product)

    if collect_result["conflict_status"] == "needs_review":
        # 確認済み情報を根拠なく削除・上書きしない: 矛盾がある場合は
        # reflectせず報告対象として残す(既存のdetect_conflicts/reflect_
        # staging_to_product_masterの挙動をそのまま使う)。
        return [{
            "action": "needs_review", "product_id": product_id,
            "staging_id": collect_result["staging_id"],
        }]

    staging_row = _fetch_staging_row(collect_result["staging_id"])
    confident, reason = _is_confident_enough(staging_row, brand)
    category_check = app.validate_category_attributes(
        category, (staging_row.get("stage2_payload") or {}).get("category_attributes")
    )

    if collect_result["stage2_status"] != "ok" or not confident or not category_check["valid"]:
        return [{
            "action": "not_reflected", "product_id": product_id,
            "reason": reason or "category_validator_failed",
            "staging_id": collect_result["staging_id"],
        }]

    # 確認済み情報を根拠なく削除・上書きしない: reflect_staging_to_product_
    # master()は新しいstage2_payloadでactive_ingredientsを丸ごと差し替える
    # 設計(新規収集時は失う情報が無いため問題にならない)だが、再検証では
    # 既存の確認済みタグより新しい抽出結果が少ない場合、そのまま反映すると
    # 既存情報を根拠なく消すことになる。その場合はreflectせず報告対象に残す
    # (別の照合ロジックは作らず、既存のcompute_ingredient_tags()で比較する
    # だけの安全ゲート)。
    would_lose, lost_tags = _would_lose_confirmed_ingredient_tags(
        existing_product or {}, staging_row.get("stage2_payload") or {}
    )
    if would_lose:
        return [{
            "action": "not_reflected", "product_id": product_id,
            "reason": "would_lose_confirmed_ingredient_tags",
            "lost_tags": sorted(lost_tags),
            "staging_id": collect_result["staging_id"],
        }]

    reflect_result = pipeline.reflect_staging_to_product_master(collect_result["staging_id"], dry_run=False)
    return [{
        "action": "reflected" if reflect_result.get("status") == "reflected" else "reflect_failed",
        "product_id": product_id, "detail": reflect_result,
    }]


def process_needs_review_item(item):
    # 自動解決・自動reflectは一切しない。報告対象として残すだけ。
    return [{
        "action": "reported_only", "staging_id": item["staging_id"],
        "brand": item["brand"], "name": item["name"], "category": item.get("category"),
        "reason": "needs_review_requires_human_decision",
    }]


def run_batch(coverage_policy_name="cosmetics", mode="dry_run", candidate_source=None,
              max_products_per_batch=DEFAULT_MAX_PRODUCTS_PER_BATCH,
              max_api_calls=DEFAULT_MAX_API_CALLS, max_cost_usd=DEFAULT_MAX_COST_USD,
              max_consecutive_failures=DEFAULT_MAX_CONSECUTIVE_FAILURES,
              stale_category=None, batch_id=None):
    """Step39のwork queueを入力として1バッチ分の処理を行う。

    mode="dry_run"(既定): Gemini/Rakuten実呼び出し・reflect・DB更新を一切
    行わず、work queue生成・候補選定・実行計画(would_*アクション)までを
    返す。mode="execute": 実際にStage1/2・category validator・conflict判定
    ・reflect・item_code解決まで実行する。
    """
    if mode not in ("dry_run", "execute"):
        raise ValueError(f"invalid mode: {mode!r} (must be 'dry_run' or 'execute')")

    candidate_source = candidate_source or _default_candidate_source
    batch_id = batch_id or f"orchestrator-{int(time.time())}"

    work_queue = app.generate_product_master_work_queue(
        coverage_policy_name=coverage_policy_name, stale_category=stale_category,
    )

    budget = BatchBudget(batch_id, max_products_per_batch, max_api_calls, max_cost_usd)
    existing_keys = set()
    if mode == "execute":
        pipeline.init_product_collection_tables()
        pipeline.PRODUCT_COLLECTION_COST_LIMIT_USD = max_cost_usd
        existing_keys = _existing_identity_keys()

    results = {
        "batch_id": batch_id, "mode": mode, "coverage_policy_name": coverage_policy_name,
        "work_items": len(work_queue), "actions": [],
    }

    for item in work_queue:
        if mode == "execute" and not budget.can_continue():
            results["actions"].append({"action": "batch_stop", "reason": budget.stopped_reason})
            break

        item_type = item.get("type")
        if item_type == "coverage_gap":
            acts = process_coverage_gap_item(
                item, mode, batch_id, budget, candidate_source, max_consecutive_failures, existing_keys,
            )
        elif item_type == "stale_reverification":
            acts = process_stale_item(item, mode, batch_id, budget)
        elif item_type == "needs_review":
            acts = process_needs_review_item(item)
        else:
            acts = [{"action": "unknown_work_item_type", "type": item_type}]

        results["actions"].extend(acts)

    if mode == "execute":
        results["coverage_after"] = app.get_coverage_report(coverage_policy_name)

    return results
