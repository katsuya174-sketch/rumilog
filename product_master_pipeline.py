"""Product Master半自動オーケストレーター(Step40/Step41)。

Step39のwork queue(app.generate_product_master_work_queue())を入力として、
work_type別(coverage_gap/stale_reverification/needs_review)に既存のPhase3
関数(Stage1/2収集、identity/dedup、reflect、Rakuten resolver)を呼び出して
構成する。coverage_gapの候補探索(candidate_source)は、人間が商品名を指定
しなくても済むよう、Step41でstaging再利用→DB再利用→Gemini Grounding探索
のtiered discovery(make_discovery_candidate_source)を既定実装とした。
ここでは新しい収集・照合・判定ロジックは一切作らない。

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

# Step41: 1coverage領域あたり探索する候補数の絶対上限(shortage_countの値に
# 関わらず固定)。暴走防止のためで、shortage_count=1でも予備候補を許すが
# 無制限には増やさない。
MAX_DISCOVERY_CANDIDATES_PER_AREA = 3
# Step41: 「最近失敗した同一候補」を再探索・再Gemini投入しないための
# 遡り期間。
RECENTLY_FAILED_LOOKBACK_HOURS = 24


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


# ===== Step41: Candidate Discovery(商品候補自動探索) =====
# coverage_gapが発生したとき、人間が商品名を指定しなくても、Stage1へ調査
# させる候補を自動で見つける。探索順序は固定: ①既存staging/sourceの未反映
# 候補の再利用 → ②既存DB(load_products()/verified_products_cache)内で
# まだproduct_masterへ反映されていない利用可能候補 → ③必要な場合のみ
# Gemini Groundingによる新規探索。
#
# ここで見つかった候補はProduct Master情報として一切信用しない。brand/
# nameだけをStage1→Stage2→citation/validator経路(既存のcollect_one_product
# 以降)へ渡す「調査対象の提案」に過ぎず、採用判定は既存経路だけで行う。


def _category_has_registered_policy(category):
    """categoryがapp.COVERAGE_POLICIESのどれかに登録されているかを調べる。
    beauty_device/supplement等、policyが未定義のカテゴリでは勝手な基準で
    探索を開始しない(合意事項)。"""
    for policy in app.COVERAGE_POLICIES.values():
        if any(entry.get("category") == category for entry in policy):
            return True
    return False


def _recently_failed_identity_keys(hours=RECENTLY_FAILED_LOOKBACK_HOURS):
    """直近hours時間以内に失敗した(stage1/stage2がokでない、またはconflict
    needs_review)未reflectの候補のidentity_keyを集める。同一candidateを
    繰り返し探索・Gemini投入しないための除外集合。"""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT identity_key FROM product_collection_staging "
            "WHERE reflected_at IS NULL "
            "AND created_at >= NOW() - (%s * INTERVAL '1 hour') "
            "AND (stage1_status != 'ok' OR stage2_status != 'ok' OR conflict_status = 'needs_review')",
            (hours,),
        )
        return {row[0] for row in cur.fetchall() if row[0]}
    finally:
        conn.close()


def _staging_reuse_candidates(category, target, excluded_keys, limit, on_excluded=None):
    """探索順序①: 既存staging(product_collection_staging)の未反映候補を
    再利用する。過去に別の目的で収集済みの候補が今回のtargetにも合致する
    場合に、新しいGemini呼び出しを発生させずに再利用する。
    on_excluded(Step44.5): 関連性はあるが除外した候補を(brief, key, reason)
    で通知する任意のcallback(dry-run計画の除外理由表示用。判定自体は変えない)。"""
    if limit <= 0:
        return []
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT brand, product_name, stage2_payload, stage1_citations "
            "FROM product_collection_staging "
            "WHERE category = %s AND stage2_status = 'ok' "
            "AND conflict_status IS DISTINCT FROM 'needs_review' AND reflected_at IS NULL "
            "ORDER BY created_at DESC LIMIT 200",
            (category,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    candidates = []
    for brand, name, payload, citations in rows:
        payload = payload or {}
        ingredient_names = [
            i.get("ingredient") for i in (payload.get("active_ingredients") or [])
            if isinstance(i, dict)
        ]
        # Step43: 関連性判定はapp.is_candidate_relevant_to_target()(cosmetics/
        # サプリメント/美容機器で分岐する共通adapter)へ委譲し、ここで独自の
        # タグ一致ロジックを持たない(美容機器のmethod一致もこれで扱える)。
        # cosmetics分岐はscore_product()を呼ぶため、brand/name/categoryを
        # 省略すると_is_generic_candidate_name()の空文字判定でハード除外
        # されてしまう(合意事項ではない回帰)。実際の値を渡す。
        # score_product()のingredient_focus一致は統制タグの完全一致でしか
        # 判定しない(_product_master_row_to_product()がactive_ingredient_
        # tagsをactive_ingredientsへ合流させるのと同じ理由)ため、生の原料名
        # だけでなくcompute_ingredient_tags()の結果も合流させる。
        pseudo_product = {
            "brand": brand, "name": name, "category": category,
            "active_ingredients": list(dict.fromkeys(
                ingredient_names + app.compute_ingredient_tags(ingredient_names)
            )),
            "category_attributes": pipeline.flatten_category_attributes(payload.get("category_attributes")),
        }
        if not app.is_candidate_relevant_to_target(category, target, pseudo_product):
            continue
        key = app.make_verified_product_key({"brand": brand, "name": name, "category": category})
        brief = {"brand": brand, "name": name, "discovery_source": "staging_reuse"}
        if key and key in excluded_keys:
            if on_excluded:
                on_excluded(brief, key, None)
            continue
        evidence = [c.get("uri") for c in (citations or []) if isinstance(c, dict) and c.get("uri")]
        if not evidence:
            if on_excluded:
                on_excluded(brief, key, "no_discovery_evidence")
            continue
        candidates.append({
            "brand": brand, "name": name, "category": category,
            "discovery_source": "staging_reuse",
            "discovery_evidence": evidence,
            "target_evidence": f"過去のstaging収集(stage2)でtarget={target}との関連性が確認済み",
        })
        if key:
            excluded_keys.add(key)
        if len(candidates) >= limit:
            break
    return candidates


def _db_reuse_candidates(category, target, excluded_keys, limit, on_excluded=None):
    """探索順序②: 既存DB(load_products()/load_verified_products_cache())
    内で、まだproduct_masterへ反映されていない利用可能候補を探す。関連性
    判定はapp.is_candidate_relevant_to_target()(Step43の共通adapter。
    calculate_effective_candidates()と同じものを使う)へ委譲し、ここで
    独自の判定ロジックを持たない。
    on_excluded: _staging_reuse_candidates()と同じ(関連性ありの除外候補のみ通知)。"""
    if limit <= 0:
        return []
    category_norm = app.normalize_candidate_category(category, fallback=category)
    pool = app.load_products() + app.load_verified_products_cache()

    candidates = []
    for p in pool:
        if not isinstance(p, dict):
            continue
        if app.normalize_candidate_category(p.get("category", ""), fallback=p.get("category", "")) != category_norm:
            continue
        key = app.make_verified_product_key(p)
        if not key:
            continue
        if not app.is_candidate_relevant_to_target(category, target, p):
            continue
        if key in excluded_keys:
            if on_excluded:
                on_excluded({"brand": p.get("brand", ""), "name": p.get("name", ""),
                             "discovery_source": "db_reuse"}, key, None)
            continue
        candidates.append({
            "brand": p.get("brand", ""), "name": p.get("name", ""), "category": category,
            "discovery_source": "db_reuse",
            "discovery_evidence": ["internal_product_db"],
            "target_evidence": f"既存DB内でtarget={target}との関連性が確認済み",
        })
        excluded_keys.add(key)
        if len(candidates) >= limit:
            break
    return candidates


def _gemini_discovery_candidates(category, target, batch_id, excluded_keys, limit):
    """探索順序③: 既存staging/DBで候補が埋まらない場合のみ、Gemini
    Groundingによる新規探索(product_collection_pipeline.
    discover_candidates_via_gemini、Step41)を行う。ここで見つかった情報は
    Product Masterへの正式採用根拠にはしない。API呼び出し・費用は既存の
    record_usage_and_check_limit()経由でbatch_idに記録され、BatchBudget
    の集計にそのまま含まれる(別の費用計算は作らない)。"""
    if limit <= 0:
        return []
    raw_candidates = pipeline.discover_candidates_via_gemini(category, target, batch_id, max_candidates=limit)

    candidates = []
    for c in raw_candidates:
        if not isinstance(c, dict):
            continue
        brand = str(c.get("brand", "") or "").strip()
        name = str(c.get("product_name", "") or "").strip()
        source_url = str(c.get("source_url", "") or "").strip()
        if not brand or not name or not source_url:
            continue
        key = app.make_verified_product_key({"brand": brand, "name": name, "category": category})
        if key and key in excluded_keys:
            continue
        candidates.append({
            "brand": brand, "name": name, "category": category,
            "discovery_source": "gemini_grounding",
            "discovery_evidence": [source_url],
            "target_evidence": c.get("target_evidence", "unknown"),
        })
        if key:
            excluded_keys.add(key)
        if len(candidates) >= limit:
            break
    return candidates


def make_discovery_candidate_source(batch_id, budget, mode):
    """Step41: run_batch()の既定candidate_source。探索順序は固定で
    ①staging再利用 → ②DB再利用 → ③(必要な場合のみ)Gemini Grounding探索。
    beauty_device/supplement等、coverage policy(app.COVERAGE_POLICIES)が
    未定義のカテゴリでは何も探索しない。mode="dry_run"ではGemini呼び出し
    (③)を一切行わない(①②は既存DBの読み取りのみで安全)。
    """
    def candidate_source(category, target, limit):
        candidate_source.last_report = None
        limit = min(int(limit) if limit else 0, MAX_DISCOVERY_CANDIDATES_PER_AREA)
        if limit <= 0:
            return []
        if not _category_has_registered_policy(category):
            return []

        failed_keys = _recently_failed_identity_keys()
        excluded_keys = _existing_identity_keys() | failed_keys

        # Step44.5: dry-run計画用の記録。探索・除外の判定はexecuteと同一で、
        # ここでは結果を記録するだけ(dry-run専用の探索ロジックは持たない)。
        report = {"excluded": [], "external_discovery_requested": 0, "external_discovery_executed": False}

        def on_excluded(brief, key, reason):
            if reason is None:
                reason = "recent_failure" if key in failed_keys else "duplicate"
            report["excluded"].append(dict(brief, reason=reason))

        candidates = []
        candidates.extend(_staging_reuse_candidates(
            category, target, excluded_keys, limit - len(candidates), on_excluded=on_excluded,
        ))
        if len(candidates) < limit:
            candidates.extend(_db_reuse_candidates(
                category, target, excluded_keys, limit - len(candidates), on_excluded=on_excluded,
            ))
        if len(candidates) < limit:
            # 外部探索(Gemini Grounding)はexecuteかつ予算内の場合のみ。dry-runでは
            # 要求件数だけを記録し、呼び出さない。
            report["external_discovery_requested"] = limit - len(candidates)
            if mode == "execute" and budget.can_continue():
                report["external_discovery_executed"] = True
                candidates.extend(
                    _gemini_discovery_candidates(category, target, batch_id, excluded_keys, limit - len(candidates))
                )
        candidate_source.last_report = report

        # discovery evidenceの無い候補は正式な提案として扱わない(採用判定は
        # 既存のStage1→Stage2→citation/validator経路に委ねるにしても、
        # 「何を調査すべきか」の根拠自体が無い候補はStage1にも回さない)。
        return [c for c in candidates if c.get("discovery_evidence")][:limit]

    candidate_source.last_report = None
    return candidate_source


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


def _category_attributes_check(category, payload):
    """stage2_payloadのcategory_attributes(active_ingredients等と同じ
    {field: {value, confidence, source_url}}のwrapped形式)を、product_
    master反映時と同じflatten_category_attributes()で平坦化してから
    app.validate_category_attributes()へ渡す(Step43)。

    Gemini側のresponse_schemaは必須フィールドのキー自体を常に強制する
    ため、wrapped形式をそのまま渡すと値が全て"unknown"でもキー存在
    チェックだけでは valid=True になってしまう。flatten_category_
    attributes()がvalue="unknown"のフィールドをキーごと落とすため、
    平坦化後に渡すことで「必須フィールドに実際の根拠ある値が無い」を
    正しく"欠落"として検出できる。
    """
    flattened = pipeline.flatten_category_attributes(payload.get("category_attributes"))
    return app.validate_category_attributes(category, flattened)


def _is_confident_enough(staging_row, brand, category):
    """citation不足/variant不明(確信度不足)に対する安全側ゲート。新しい
    照合ロジックは作らず、既存のcitation数とis_official_source_confirmed()
    (sanitize_stage2_payload内で既に決定論的に算出され、stage2_payload
    のofficial_source_confirmedに入っている値)だけを見る。
    category_attributesの充足判定はapp.CATEGORY_ATTRIBUTE_SCHEMAS/
    validate_category_attributes()に委譲し、別の判定基準は作らない。"""
    if staging_row.get("stage1_status") != "ok":
        return False, "stage1_not_ok"
    if len(staging_row.get("stage1_citations") or []) < 1:
        return False, "no_citations"
    payload = staging_row.get("stage2_payload") or {}
    category_check = _category_attributes_check(category, payload)
    if not payload.get("active_ingredients") and not category_check["valid"]:
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
    # Step41: shortage_count=1だから候補も1件だけ、とはしない。候補失敗を
    # 考慮して少数の予備候補を許可するが、1領域最大MAX_DISCOVERY_CANDIDATES_
    # PER_AREA件という暴走防止の上限はshortage_countに関わらず固定する
    # (実際に反映する件数はshortageで変わらず下のreflected_count判定が担う)。
    candidates = candidate_source(category, target, MAX_DISCOVERY_CANDIDATES_PER_AREA)

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

        confident, reason = _is_confident_enough(staging_row, brand, category)
        category_check = _category_attributes_check(category, staging_row.get("stage2_payload") or {})
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

    if mode == "dry_run":
        actions.extend(_dry_run_plan_actions(item, candidates, getattr(candidate_source, "last_report", None)))

    return actions


DRY_RUN_EXCLUDED_EXAMPLES = 5


def _dry_run_plan_actions(item, candidates, report):
    """Step44.5: dry-run計画のうち、would_collect以外(外部探索の要否・除外
    理由)をcandidate_sourceの記録から組み立てる。記録を持たない注入
    candidate_sourceでは何も追加しない。"""
    if not report:
        return []
    category, target = item["category"], item["target"]
    actions = []
    excluded = report.get("excluded") or []
    if excluded:
        by_reason = {}
        for e in excluded:
            by_reason[e["reason"]] = by_reason.get(e["reason"], 0) + 1
        actions.append({
            "action": "excluded_candidates", "category": category, "target": target,
            "count": len(excluded), "by_reason": by_reason,
            "examples": excluded[:DRY_RUN_EXCLUDED_EXAMPLES],
        })
    requested = report.get("external_discovery_requested", 0)
    if requested > 0:
        actions.append({
            "action": "external_discovery_required", "category": category, "target": target,
            "requested_candidates": requested,
            "shortage_after_existing": max(0, item["shortage_count"] - len(candidates)),
        })
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
    confident, reason = _is_confident_enough(staging_row, brand, category)
    category_check = _category_attributes_check(category, staging_row.get("stage2_payload") or {})

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

    batch_id = batch_id or f"orchestrator-{int(time.time())}"

    work_queue = app.generate_product_master_work_queue(
        coverage_policy_name=coverage_policy_name, stale_category=stale_category,
    )

    budget = BatchBudget(batch_id, max_products_per_batch, max_api_calls, max_cost_usd)
    # Step41: candidate_sourceが明示注入されない場合の既定実装は、商品名を
    # 常に空リストで返す_default_candidate_source(Step40)ではなく、
    # staging再利用→DB再利用→Gemini Grounding探索のtiered discoveryとする。
    candidate_source = candidate_source or make_discovery_candidate_source(batch_id, budget, mode)
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
