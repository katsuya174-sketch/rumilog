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
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone

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

    def can_afford(self, kind):
        """Step45.3: 次の処理を始める前に、現在の費用 + 次処理の保守的な推定
        費用(conservative_cost_estimate)が上限を超えるなら開始しない。
        can_continue()(開始時点で上限未満か)だけでは、最後の1件で上限を
        超過していた(RF canary: $0.25上限に対して$0.2514)。
        注意: 実際のAPI料金(特にGroundingの検索クエリ数)は呼び出し前に確定
        できないため、推定を上回る1回の呼び出しで上限を超える可能性は残る
        (完全なhard capではない)。推定は実績の最大値に余裕を持たせて算出する。"""
        if not self.can_continue():
            return False
        estimate = conservative_cost_estimate(kind)
        if estimate <= 0:
            return True
        _, cost = self.usage_snapshot()
        if cost + estimate > self.max_cost_usd:
            self.stopped_reason = "insufficient_budget_for_next"
            self.last_estimate = {"kind": kind, "current_cost": cost, "estimated_next_cost": estimate,
                                  "max_cost_usd": self.max_cost_usd}
            return False
        return True


# Step45.3: 次処理の推定費用。既存usage(product_collection_usage)の実績を
# 処理単位(stage1+stage2=1商品、discovery_search+structuring=1回の外部探索)
# にまとめ、直近の実績の最大値にCOST_ESTIMATE_SAFETY_MARGIN倍の余裕を持たせる。
# 実績が少ない場合は、料金定数から算出した悲観的な値(大きめのtoken数・
# 検索クエリ数を仮定)を下限とする(固定の楽観値で上限を抜けないため)。
COST_ESTIMATE_SAFETY_MARGIN = 1.25
COST_ESTIMATE_MIN_SAMPLES = 5
COST_ESTIMATE_LOOKBACK = 200
_COST_ESTIMATE_STAGES = {
    "product_collection": ("stage1", "stage2"),
    "external_discovery": ("discovery_search", "discovery_structuring"),
}


def _pessimistic_cost_estimate(kind):
    estimate = pipeline.estimate_call_cost_usd
    if kind == "product_collection":
        return (estimate({"prompt_token_count": 1000, "candidates_token_count": 4000}, search_query_count=8)
                + estimate({"prompt_token_count": 8000, "candidates_token_count": 2000}))
    if kind == "external_discovery":
        return (estimate({"prompt_token_count": 1000, "candidates_token_count": 2000}, search_query_count=8)
                + estimate({"prompt_token_count": 4000, "candidates_token_count": 2000}))
    return 0.0


# Step48.7: 外部Discoveryは、その後に最低1商品分のStage1/2を実行できる予算が
# 無ければ費用だけ発生して候補を処理できないため、両方の合計で事前判定する。
DISCOVERY_WITH_ONE_PRODUCT = "external_discovery_with_product"


def conservative_cost_estimate(kind):
    """kind: "product_collection" / "external_discovery" / "staging_reuse"(API
    呼び出し無し=0) / DISCOVERY_WITH_ONE_PRODUCT(外部Discovery+1商品分)。"""
    if kind == DISCOVERY_WITH_ONE_PRODUCT:
        return conservative_cost_estimate("external_discovery") + conservative_cost_estimate("product_collection")
    stages = _COST_ESTIMATE_STAGES.get(kind)
    if not stages:
        return 0.0
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT SUM(estimated_cost_usd) FROM product_collection_usage "
            "WHERE stage = ANY(%s) GROUP BY batch_id, product_label "
            "ORDER BY MAX(called_at) DESC LIMIT %s",
            (list(stages), COST_ESTIMATE_LOOKBACK),
        )
        samples = [float(r[0] or 0) for r in cur.fetchall()]
    finally:
        conn.close()
    observed = max(samples) * COST_ESTIMATE_SAFETY_MARGIN if samples else 0.0
    if len(samples) < COST_ESTIMATE_MIN_SAMPLES:
        return max(observed, _pessimistic_cost_estimate(kind))
    return observed


def _product_master_identity_keys():
    """product_masterに登録済みのidentity_key集合。Candidate Discoveryと
    executeのduplicate判定で「再収集しない」対象はこれだけにする(Step44.6)。

    以前の_existing_identity_keys()はproducts.json/verified_products_cache
    のidentityもまとめてduplicate扱いしていたため、それらにしか存在しない
    (= product_master未登録の)商品をDB reuseで見つけられなかった。
    products.json/verified_products_cacheにしか無い商品はduplicateではなく
    DB reuseの探索対象で、coverageにも数えない(app.calculate_effective_
    candidates())。identity_keyはmake_verified_product_key()と同じ形式。"""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        cur = conn.cursor()
        cur.execute("SELECT identity_key FROM product_master")
        return {row[0] for row in cur.fetchall() if row[0]}
    finally:
        conn.close()


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


# ===== Step45.9a: 決定論的gate failureの記録と一定期間の再利用抑制 =====
# 再評価しても同じ結果になる商品自体のgate failure(公式情報源未確認・
# category validator失敗・楽天の新品通常販売listingなし)を、未reflectの
# staging行のstage2_payload内の予約キーへ記録する(DB schema変更なし)。
# 有効なmarkerがある間は、staging再利用と外部Discoveryの両方で同一identity
# を除外する(理由をexcluded/actionsへ出力)。
# markerが無効になる条件(決定論的):
#   - evaluated_atからGATE_FAILURE_TTLが経過した
#   - 記録時のデータ指紋(stage2_payload(予約キー除く)+stage1_citations)と
#     現在の行のデータが異なる(Stage2再実行・page verification追加等で更新)
#   - 同一identityに、markerより新しいstaging行(markerの無い新しい収集結果)がある
# timeout・通信エラー等の一時的障害はmarkerを書かない。
GATE_FAILURE_MARKER_KEY = "_pipeline_gate_failure"
GATE_FAILURE_TTL = timedelta(hours=24)
_DETERMINISTIC_EVALUATION_FAILURES = {
    "official_source_not_confirmed_variant_uncertain": "official_source",
    "category_validator_failed": "category_validator",
    "no_extracted_fields": "category_validator",
    "supplement_not_eligible": "supplement_eligibility",
}
_DETERMINISTIC_RAKUTEN_REASONS = {
    "title_mismatch", "device_model_mismatch", "only_non_new_sale_listings", "only_set_items",
    # Step47.6: 検索結果はあるが、検証済みJANと一致する候補が無い(同一商品・
    # 同一variantの新品listingが確認できない)。
    "jan_mismatch",
}


def _now():
    return datetime.now(timezone.utc)


def _staging_data_fingerprint(stage2_payload, stage1_citations):
    payload = {k: v for k, v in (stage2_payload or {}).items() if k != GATE_FAILURE_MARKER_KEY}
    blob = json.dumps({"payload": payload, "citations": stage1_citations or []},
                      ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _active_gate_marker(stage2_payload, stage1_citations, now=None):
    marker = (stage2_payload or {}).get(GATE_FAILURE_MARKER_KEY)
    if not isinstance(marker, dict):
        return None
    try:
        evaluated_at = datetime.fromisoformat(str(marker.get("evaluated_at")))
    except ValueError:
        return None
    if evaluated_at.tzinfo is None:
        evaluated_at = evaluated_at.replace(tzinfo=timezone.utc)
    if (now or _now()) - evaluated_at >= GATE_FAILURE_TTL:
        return None
    if marker.get("data_fingerprint") != _staging_data_fingerprint(stage2_payload, stage1_citations):
        return None
    return marker


def _verification_shareable(verification, title_domain):
    """同一titleドメインの他citationのpage verificationを流用してよいか。
    取得に成功した結果は、最終ドメイン・canonicalがこのtitleドメイン(または
    サブドメイン)と整合する場合だけ流用する(整合しない結果は肯定・否定とも
    流用しない)。取得自体ができなかった結果(transient・403・安全要件での拒否)
    はドメイン単位の結果として共有する。"""
    if verification.get("fetch_status") != "ok":
        return True
    if not pipeline._domain_within(verification.get("final_domain"), title_domain):
        return False
    canonical = verification.get("canonical_domain")
    return not canonical or pipeline._domain_within(canonical, title_domain)


def classify_official_evidence(brand, stage1_citations):
    """Step45.12: 公式情報源の根拠をcitationごとに分類し、全体の判定を返す。
    各citation:
      - confirmed: 公式性を証明(citation titleの照合、または保存済みpage
        verificationを現行判定器(Step45.10)で再計算してTrue)
      - transient_unverified: citation先ページの確認がtimeout/通信エラー/5xxで
        評価できなかった
      - not_confirmed: page verificationを実施・完了したが証明しない(403/404・
        安全要件での拒否・ドメイン不整合等を含む)
      - unverified(Step45.15): page verification自体をまだ実施していない
        (最大5ドメインの確認上限・同一ドメインの重複で未取得のもの等)。
        未取得をnot_confirmedとは扱わない。
    全体:
      - confirmedが1件以上 → "confirmed"(official=True)
      - confirmedなし・transientなし・unverifiedなし → "failed"(確定failure。markerを書く)
      - confirmedなし・transientまたはunverifiedあり → "undetermined"(未確定。markerなし)
    ドメインの公式らしさ・サイト種別等の推測はしない。"""
    result = {"confirmed": [], "not_confirmed": [], "transient_unverified": [], "transient_exhausted": [],
              "unverified": []}
    citations = [c for c in (stage1_citations or []) if isinstance(c, dict)]
    # Step45.18: page verificationはドメイン単位でサイト自身のidentityを確認して
    # いるため、同じcitation titleドメインの確認結果を他のcitationでも使う。
    # 結果はコピーせず、citationごとに現在の判定器で再評価する(肯定は各
    # citationのtitleドメインに対する最終ドメイン・canonical整合が必須)。
    verifications_by_domain = {}
    for citation in citations:
        domain = pipeline._citation_title_domain(citation)
        if domain and isinstance(citation.get("page_verification"), dict):
            verifications_by_domain.setdefault(domain, []).append(citation["page_verification"])
    for citation in citations:
        title = citation.get("title", "")
        if brand and pipeline._citation_title_confirms_brand(brand, title):
            result["confirmed"].append(title)
            continue
        own = citation.get("page_verification")
        domain = pipeline._citation_title_domain(citation)
        verifications = ([own] if isinstance(own, dict) else []) + [
            v for v in verifications_by_domain.get(domain, [])
            if v is not own and _verification_shareable(v, domain)]
        if brand and any(pipeline._page_identity_confirms_brand(brand, dict(citation, page_verification=v))
                         for v in verifications):
            result["confirmed"].append(title)
        elif any(not pipeline.is_transient_page_verification(v) for v in verifications):
            # 取得・検証が完了した結果(否定)がある。否定結果の共有で公式性が
            # 生じることは無い。
            result["not_confirmed"].append(title)
        elif verifications:
            # Step45.13: 再確認の上限に達したもの(transient_exhausted)も未評価の
            # まま保留(非公式とはしない)。
            result["transient_unverified"].append(title)
            if any(v.get("transient_exhausted") for v in verifications):
                result["transient_exhausted"].append(title)
        else:
            result["unverified"].append(title)
    if result["confirmed"]:
        result["status"] = "confirmed"
    elif result["transient_unverified"] or result["unverified"]:
        result["status"] = "undetermined"
    else:
        result["status"] = "failed"
    return result


def _record_gate_failure(staging_id, gate, reason):
    """未reflectのstaging行へgate failure markerを書く(execute時のみ呼ばれる)。"""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        cur = conn.cursor()
        cur.execute("SELECT stage2_payload, stage1_citations FROM product_collection_staging "
                    "WHERE staging_id = %s AND reflected_at IS NULL", (staging_id,))
        row = cur.fetchone()
        if not row:
            return None
        payload = dict(row[0] or {})
        marker = {"gate": gate, "reason": reason, "evaluated_at": _now().isoformat(),
                  "data_fingerprint": _staging_data_fingerprint(payload, row[1])}
        payload[GATE_FAILURE_MARKER_KEY] = marker
        cur.execute("UPDATE product_collection_staging SET stage2_payload = %s WHERE staging_id = %s",
                    (json.dumps(payload, ensure_ascii=False), staging_id))
        conn.commit()
        return marker
    finally:
        conn.close()


def _gate_failed_identities(now=None):
    """有効なgate failure markerを持つidentity → marker。同一identityの最新の
    未reflect staging行だけを見る(より新しい収集結果があれば古いmarkerは無効)。"""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT ON (identity_key) identity_key, stage2_payload, stage1_citations "
            "FROM product_collection_staging WHERE reflected_at IS NULL AND identity_key IS NOT NULL "
            "ORDER BY identity_key, staging_id DESC"
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    failed = {}
    for identity_key, payload, citations in rows:
        marker = _active_gate_marker(payload, citations, now=now)
        if marker:
            failed[identity_key] = marker
    return failed


# Step45.13: 公式判定が未確定(transient/保留)のstaging再利用候補は1実行あたり
# この件数まで。残りの枠は通常のstaging・DB reuse・外部Discoveryへ回す
# (transient候補は削除・failure扱い・永久除外せず、その実行だけ見送る)。
MAX_TRANSIENT_STAGING_CANDIDATES_PER_RUN = 1


def _staging_reuse_state(brand, citations):
    """staging再利用候補の公式判定状態。page確認が一度も行われていない
    (pending)ものは通常候補として最初の評価機会を与える。"""
    if not pipeline.has_page_verification(citations):
        return "normal"
    return "transient" if classify_official_evidence(brand, citations)["status"] == "undetermined" else "normal"


# ===== Step45.17: 美容機器の型番ベース重複防止 =====
def _registered_device_entries():
    """product_masterに登録済みの美容機器(型番ベース重複判定用)。"""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        cur = conn.cursor()
        cur.execute("SELECT product_id, brand, name FROM product_master WHERE category = %s", ("美容機器",))
        return [{"product_id": r[0], "brand": r[1] or "", "name": r[2] or ""} for r in cur.fetchall()]
    finally:
        conn.close()


class DeviceDuplicateGuard:
    """Candidate Discoveryの各tierで共通に使う型番ベースの重複判定。
    product_master登録済み(duplicate_model)と、この実行で既に選んだ候補
    (already_selected_model)の両方と比較する。美容機器以外は何もしない。"""

    def __init__(self, category, registered_entries=None):
        self.category = category
        self.registered = list(registered_entries or [])
        self.selected = []

    def reason(self, brand, name):
        if pipeline.find_device_duplicate(self.category, brand, name, self.registered):
            return "duplicate_model"
        if pipeline.find_device_duplicate(self.category, brand, name, self.selected):
            return "already_selected_model"
        return None

    def add(self, brand, name):
        if self.category == "美容機器":
            self.selected.append({"brand": brand, "name": name})


def _staging_reuse_candidates(category, target, excluded_keys, limit, on_excluded=None,
                              max_transient=MAX_TRANSIENT_STAGING_CANDIDATES_PER_RUN, duplicate_guard=None):
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
            "SELECT staging_id, brand, product_name, stage2_payload, stage1_citations "
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
    transient_count = 0
    for staging_id, brand, name, payload, citations in rows:
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
            "category_attributes": dict(
                pipeline.flatten_category_attributes(payload.get("category_attributes")),
                **({"primary_ingredient_tags": pipeline.supplement_primary_tags_for_payload(name, payload)}
                   if category == "サプリメント" else {}),
            ),
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
        duplicate_reason = duplicate_guard.reason(brand, name) if duplicate_guard else None
        if duplicate_reason:
            if on_excluded:
                on_excluded(brief, key, duplicate_reason)
            if key:
                excluded_keys.add(key)
            continue
        reuse_state = _staging_reuse_state(brand, citations)
        if reuse_state == "transient" and transient_count >= max_transient:
            if on_excluded:
                on_excluded(brief, key, "transient_slot_deferred")
            if key:
                excluded_keys.add(key)  # 同一identityを他tierで再収集しない
            continue
        candidates.append({
            "brand": brand, "name": name, "category": category,
            "discovery_source": "staging_reuse",
            # Step45.3: 保存済みStage1/2結果をそのまま再評価するための参照。
            "staging_id": staging_id,
            "reuse_state": reuse_state,
            "discovery_evidence": evidence,
            "target_evidence": f"過去のstaging収集(stage2)でtarget={target}との関連性が確認済み",
        })
        if reuse_state == "transient":
            transient_count += 1
        if duplicate_guard:
            duplicate_guard.add(brand, name)
        if key:
            excluded_keys.add(key)
        if len(candidates) >= limit:
            break
    return candidates


def _db_reuse_candidates(category, target, excluded_keys, limit, on_excluded=None, duplicate_guard=None):
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
        duplicate_reason = duplicate_guard.reason(p.get("brand", ""), p.get("name", "")) if duplicate_guard else None
        if duplicate_reason:
            if on_excluded:
                on_excluded({"brand": p.get("brand", ""), "name": p.get("name", ""),
                             "discovery_source": "db_reuse"}, key, duplicate_reason)
            excluded_keys.add(key)
            continue
        if duplicate_guard:
            duplicate_guard.add(p.get("brand", ""), p.get("name", ""))
        # Step44.6: 既存DBの商品情報(成分等)はproduct_masterへコピーしない。
        # identity(brand/name/category)だけを候補として返し、採用判定は
        # 他tierと同じStage1→Stage2→citation→validator→conflict→reflectで行う。
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


# Step45.15: Gemini Grounding Discoveryは、APIが実際のsearch evidenceを返さな
# かった(no_search_evidence)場合に限り、BatchBudgetのpreflightを通過すれば
# 1回だけ再試行する(最大2 attempts)。正常に検索したが候補0件・構造化/
# フィルタ結果0件・APIエラー・予算不足等では再試行しない。
DISCOVERY_MAX_ATTEMPTS = 2


def _gemini_discovery_candidates(category, target, batch_id, excluded_keys, limit,
                                 on_excluded=None, diagnostics=None, budget=None, duplicate_guard=None):
    """探索順序③: 既存staging/DBで候補が埋まらない場合のみ、Gemini
    Groundingによる新規探索(product_collection_pipeline.
    discover_candidates_via_gemini、Step41)を行う。ここで見つかった情報は
    Product Masterへの正式採用根拠にはしない。API呼び出し・費用は既存の
    record_usage_and_check_limit()経由でbatch_idに記録され、BatchBudget
    の集計にそのまま含まれる(別の費用計算は作らない)。
    on_excluded/diagnostics(Step45.1): 除外理由と、外部Discovery自体の0件理由
    (pipeline.discover_candidates_via_gemini()のdiagnostics)を呼び出し元へ返す。"""
    if limit <= 0:
        return []
    diagnostics = diagnostics if diagnostics is not None else {}
    attempts = []
    raw_candidates = []
    for attempt in range(1, DISCOVERY_MAX_ATTEMPTS + 1):
        if attempt > 1:
            # 再試行の前にも予算preflightを必ず通す(通らなければ再試行しない)。
            if budget is None or not budget.can_afford(DISCOVERY_WITH_ONE_PRODUCT):
                attempts.append({"attempt": attempt, "status": "skipped_budget"})
                break
        attempt_diag = {}
        raw_candidates = pipeline.discover_candidates_via_gemini(
            category, target, batch_id, max_candidates=limit, diagnostics=attempt_diag,
        )
        attempts.append({"attempt": attempt, "status": attempt_diag.get("status")})
        diagnostics.clear()
        diagnostics.update(attempt_diag)
        if attempt_diag.get("status") != "no_search_evidence":
            break
    diagnostics["attempts"] = attempts

    candidates = []
    for c in raw_candidates:
        if not isinstance(c, dict):
            continue
        brand = str(c.get("brand", "") or "").strip()
        name = str(c.get("product_name", "") or "").strip()
        source_url = str(c.get("source_url", "") or "").strip()
        brief = {"brand": brand, "name": name, "discovery_source": "gemini_grounding"}
        missing = ("missing_brand" if not brand else "missing_product_name" if not name
                   else "missing_source_url" if not source_url else None)
        if missing:
            if on_excluded:
                on_excluded(brief, None, missing)
            continue
        key = app.make_verified_product_key({"brand": brand, "name": name, "category": category})
        # Step48.5: Discoveryの根拠で医薬品・医薬部外品と判明したサプリ候補は、
        # Stage1(Grounding費用)前に除外する。正常なフィルタ結果であり候補失敗
        # (連続失敗STOP)には数えない。区分の確定・保存には使わない。
        regulated_class = c.get("discovery_regulated_product_class")
        if regulated_class:
            if on_excluded:
                on_excluded(dict(brief, product_classification=regulated_class), key, "discovery_regulated_product")
            if key:
                excluded_keys.add(key)
            continue
        if key and key in excluded_keys:
            if on_excluded:
                on_excluded(brief, key, None)
            continue
        # Step45.17: Discovery結果の商品名から型番が分かった時点で、Stage1/2の
        # 課金前に既存商品・選択済み候補との型番ベース重複を除外する。
        duplicate_reason = duplicate_guard.reason(brand, name) if duplicate_guard else None
        if duplicate_reason:
            if on_excluded:
                on_excluded(brief, key, duplicate_reason)
            if key:
                excluded_keys.add(key)
            continue
        if duplicate_guard:
            duplicate_guard.add(brand, name)
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

        # Step44.6: 除外はproduct_master登録済み(duplicate)と最近の失敗
        # (recent_failure)のみ。products.json/verified_products_cache/未reflect
        # stagingにしか無い商品は再利用候補として探索する。
        master_keys = _product_master_identity_keys()
        failed_keys = _recently_failed_identity_keys()
        gate_failures = _gate_failed_identities()
        excluded_keys = master_keys | failed_keys | set(gate_failures)

        # Step44.5: dry-run計画用の記録。探索・除外の判定はexecuteと同一で、
        # ここでは結果を記録するだけ(dry-run専用の探索ロジックは持たない)。
        report = {"excluded": [], "external_discovery_requested": 0, "external_discovery_executed": False}

        def on_excluded(brief, key, reason):
            if reason is None:
                if key in gate_failures:
                    marker = gate_failures[key]
                    reason = "gate_failure_recent"
                    brief = dict(brief, gate=marker.get("gate"), gate_reason=marker.get("reason"),
                                 evaluated_at=marker.get("evaluated_at"))
                elif key in failed_keys:
                    reason = "recent_failure"
                elif key in master_keys:
                    reason = "duplicate"
                else:
                    # この領域の探索で別tier/別行として既に選んだ同一identity
                    reason = "already_selected"
            report["excluded"].append(dict(brief, reason=reason))

        duplicate_guard = DeviceDuplicateGuard(
            category, _registered_device_entries() if category == "美容機器" else [])
        candidates = []
        candidates.extend(_staging_reuse_candidates(
            category, target, excluded_keys, limit - len(candidates), on_excluded=on_excluded,
            duplicate_guard=duplicate_guard,
        ))
        if len(candidates) < limit:
            candidates.extend(_db_reuse_candidates(
                category, target, excluded_keys, limit - len(candidates), on_excluded=on_excluded,
                duplicate_guard=duplicate_guard,
            ))
        if len(candidates) < limit:
            # 外部探索(Gemini Grounding)はexecuteかつ予算内の場合のみ。dry-runでは
            # 要求件数だけを記録し、呼び出さない。
            report["external_discovery_requested"] = limit - len(candidates)
            if mode == "execute" and budget.can_afford(DISCOVERY_WITH_ONE_PRODUCT):
                report["external_discovery_executed"] = True
                diagnostics = {}
                report["external_discovery_diagnostics"] = diagnostics
                candidates.extend(_gemini_discovery_candidates(
                    category, target, batch_id, excluded_keys, limit - len(candidates),
                    on_excluded=on_excluded, diagnostics=diagnostics, budget=budget,
                    duplicate_guard=duplicate_guard,
                ))
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
            "SELECT stage1_status, stage1_citations, stage2_status, stage2_payload, conflict_status, reflected_at, "
            "stage1_raw_text FROM product_collection_staging WHERE staging_id = %s",
            (staging_id,),
        )
        row = cur.fetchone()
        if not row:
            return {}
        return {
            "stage1_status": row[0], "stage1_citations": row[1] or [],
            "stage2_status": row[2], "stage2_payload": row[3] or {}, "conflict_status": row[4],
            "reflected_at": row[5], "stage1_raw_text": row[6],
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
    # Step45.3: 保存値(収集時点の判定)ではなく、保存済みの実citationから
    # 現行のis_official_source_confirmed()で毎回決定論的に再計算する
    # (staging再利用時も新しい判定ルールで評価される)。
    if not pipeline.is_official_source_confirmed(brand, staging_row.get("stage1_citations") or []):
        return False, "official_source_not_confirmed_variant_uncertain"
    return True, None


def _staging_reuse_data_problem(staging_row):
    """保存済みstagingをStage1/2の再実行なしで再評価できるか。不足・破損が
    あれば理由を返す(自動で再収集・再課金はしない)。"""
    if not staging_row:
        return "staging_not_found"
    if staging_row.get("reflected_at") is not None:
        return "already_reflected"
    if staging_row.get("stage1_status") != "ok" or staging_row.get("stage2_status") != "ok":
        return "stage_not_ok"
    citations = staging_row.get("stage1_citations")
    if not isinstance(citations, list) or not citations or not all(
            isinstance(c, dict) and c.get("uri") for c in citations):
        return "citations_missing_or_broken"
    payload = staging_row.get("stage2_payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("active_ingredients", []), list):
        return "stage2_payload_missing_or_broken"
    return None


def _ensure_staging_page_verification(staging_id, brand):
    """Step45.5: staging再利用(execute)時、保存済みcitationにpage_verification
    がまだ無く、title判定でも公式確認できない場合だけ、citation先ページを
    一度確認して結果をstage1_citationsへ保存する(Gemini費用なし)。既に
    検証済みなら保存値をそのまま使い、HTTPは再実行しない。"""
    row = _fetch_staging_row(staging_id)
    citations = row.get("stage1_citations") if row else None
    if not row or _staging_reuse_data_problem(row) or pipeline.is_official_source_confirmed(brand, citations):
        return row
    if pipeline.has_page_verification(citations):
        # Step45.13: 評価済みのcitationは再取得せず、transientだけを期限・回数の
        # 範囲内で再確認する。
        verified, changed = pipeline.retry_transient_page_verifications(brand, citations)
    else:
        verified = pipeline.attach_citation_page_verification(brand, citations)
        changed = verified is not citations
    if changed:
        pipeline.update_staging_citations(staging_id, verified)
        row = dict(row, stage1_citations=verified)
    return row


def _supplement_eligibility(category, staging_row):
    """Step48.1/48.2: サプリとしての適格性(サプリ以外はNone)。保存済みStage1本文と
    Stage2の出典照合済み値だけで決める(API・HTTPなし)。"""
    if category != "サプリメント" or not staging_row:
        return None
    return pipeline.supplement_eligibility(
        staging_row.get("stage2_payload") or {}, staging_row.get("stage1_raw_text"))


def _supplement_eligibility_blocks(category, staging_row):
    eligibility = _supplement_eligibility(category, staging_row)
    return eligibility is not None and not eligibility["eligible"]


def evaluate_staging_for_reflect(staging_id, brand, name, category, conflict_status=None, staging_row=None):
    """保存済みstaging(Stage1/2済み)を、official source → category validator →
    conflictの順に評価する(DB書き込み・API呼び出しなし)。conflict_statusが
    渡されない場合(staging再利用)は、現在のproduct_masterに対して
    detect_conflicts()で再判定する。戻り値のreflectable=Trueのときだけ
    reflectしてよい。"""
    row = staging_row if staging_row is not None else _fetch_staging_row(staging_id)
    problem = _staging_reuse_data_problem(row) if conflict_status is None else None
    if problem:
        return {"reflectable": False, "data_complete": False, "reason": problem, "staging_id": staging_id}
    if conflict_status is None:
        conflict_status = pipeline.detect_conflicts(
            row.get("stage2_payload") or {}, _product_master_lookup(brand, name, category),
        ).get("status")
    if conflict_status == "needs_review":
        return {"reflectable": False, "data_complete": True, "reason": "needs_review", "staging_id": staging_id}
    # Step48.1/48.2: サプリの適格性ゲート。official source確認・楽天確認より前に判定し、
    # 医薬品・医薬部外品・根拠の無い区分不明は以降の判定(HTTP/楽天API)へ進めない。
    eligibility = _supplement_eligibility(category, row)
    product_class = eligibility["product_classification"] if eligibility else None
    if eligibility is not None and not eligibility["eligible"]:
        return {"reflectable": False, "data_complete": True, "staging_id": staging_id,
                "reason": "supplement_not_eligible", "product_classification": product_class,
                "eligibility_reason": eligibility["reason"],
                "page_verification_pending": False, "official_source_confirmed": None,
                "category_validator": None, "conflict_status": conflict_status}
    confident, reason = _is_confident_enough(row, brand, category)
    category_check = _category_attributes_check(category, row.get("stage2_payload") or {})
    reflectable = row.get("stage2_status") == "ok" and confident and category_check["valid"]
    citations = row.get("stage1_citations") or []
    return {
        "reflectable": reflectable, "data_complete": True, "staging_id": staging_id,
        "reason": None if reflectable else (reason or "category_validator_failed"),
        # 公式未確認かつcitation先ページ確認がまだ(dry-runではHTTPを行わない)。
        "page_verification_pending": (not pipeline.is_official_source_confirmed(brand, citations)
                                      and not pipeline.has_page_verification(citations)),
        "official_source_confirmed": pipeline.is_official_source_confirmed(brand, row.get("stage1_citations") or []),
        "category_validator": category_check, "conflict_status": conflict_status,
        **({"product_classification": product_class} if product_class is not None else {}),
    }


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


def _resolve_item_code_safely(product_id, brand, name, category, resolution=None):
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

    update_result = pipeline.verify_and_resolve_item_code(
        product_id, brand, name, category, jan_code=jan_code, resolution=resolution,
    )
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
        reuse_staging_id = cand.get("staging_id") if cand.get("discovery_source") == "staging_reuse" else None
        # Step45.3: staging再利用はStage1/2を再実行しない(API費用0)。それ以外は
        # 次の1商品分の保守的な推定費用まで含めて上限内かを確認してから始める。
        if mode == "execute" and not budget.can_afford("staging_reuse" if reuse_staging_id else "product_collection"):
            actions.append({"action": "batch_stop", "reason": budget.stopped_reason,
                            "budget_estimate": getattr(budget, "last_estimate", None)})
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
            would = {
                "action": "would_collect", "brand": brand, "name": name,
                "category": category, "target": target,
            }
            if reuse_staging_id:
                # 保存済みStage1/2で再評価した場合の結果(読み取りのみ)。
                would["staging_evaluation"] = evaluate_staging_for_reflect(reuse_staging_id, brand, name, category)
            actions.append(would)
            continue

        # Step45.17: 型番が分かっている美容機器は、Stage1/2・reflect前に登録済み
        # 商品との型番ベース重複を確認する(注入されたcandidate_source経由も含む)。
        registered_duplicate = pipeline.find_device_duplicate(
            category, brand, name, _registered_device_entries() if category == "美容機器" else [])
        if registered_duplicate:
            actions.append({"action": "skipped_duplicate_model", "brand": brand, "name": name, "category": category,
                            "duplicate_of_product_id": registered_duplicate.get("product_id"),
                            **({"staging_id": reuse_staging_id} if reuse_staging_id else {})})
            continue

        budget.record_attempt()
        collect_result = {}
        if reuse_staging_id:
            staging_id = reuse_staging_id
            staging_row = _fetch_staging_row(staging_id)
            # Step48.1: 商品区分でreflect不可が確定している場合は公式ページ確認のHTTPをしない。
            if not _supplement_eligibility_blocks(category, staging_row):
                staging_row = _ensure_staging_page_verification(staging_id, brand)
            evaluation = evaluate_staging_for_reflect(staging_id, brand, name, category, staging_row=staging_row)
            if not evaluation["data_complete"]:
                consecutive_failures += 1
                actions.append({
                    "action": "staging_needs_recollection", "brand": brand, "name": name,
                    "reason": evaluation["reason"], "staging_id": staging_id,
                })
                continue
        else:
            existing_product = _product_master_lookup(brand, name, category)
            collect_result = pipeline.collect_one_product(brand, name, category, batch_id, existing_product=existing_product)
            staging_id = collect_result["staging_id"]
            evaluation = evaluate_staging_for_reflect(
                staging_id, brand, name, category, conflict_status=collect_result["conflict_status"],
            )

        if evaluation["reason"] == "needs_review":
            consecutive_failures += 1
            actions.append({
                "action": "not_reflected", "brand": brand, "name": name,
                "reason": "needs_review", "staging_id": staging_id,
            })
            continue

        if not evaluation["reflectable"]:
            consecutive_failures += 1
            not_reflected = {
                "action": "not_reflected", "brand": brand, "name": name,
                "reason": evaluation["reason"],
                "category_validator": evaluation["category_validator"],
                **{k: evaluation[k] for k in ("product_classification", "eligibility_reason") if k in evaluation},
                "staging_id": staging_id,
                **({"reused_staging": True} if reuse_staging_id else {}),
            }
            gate = _DETERMINISTIC_EVALUATION_FAILURES.get(evaluation["reason"])
            row_citations = (_fetch_staging_row(staging_id) or {}).get("stage1_citations")
            if gate == "official_source":
                # Step45.12: 確定failure(confirmedもtransientも無い)の場合だけmarker。
                # transientがあれば未確定としてmarkerを書かない。
                official_evidence = classify_official_evidence(brand, row_citations)
                not_reflected["official_evidence"] = {
                    k: official_evidence[k] for k in ("status", "not_confirmed", "transient_unverified", "unverified")}
                if official_evidence["status"] == "failed":
                    not_reflected["gate_failure_marker"] = _record_gate_failure(staging_id, gate, evaluation["reason"])
            elif gate:
                not_reflected["gate_failure_marker"] = _record_gate_failure(staging_id, gate, evaluation["reason"])
            actions.append(not_reflected)
            continue

        # Step45.17: reflect直前の型番ベース重複ゲート(収集中に他経路で登録された
        # 場合にも同一商品を別行としてreflectしない)。
        registered_duplicate = pipeline.find_device_duplicate(
            category, brand, name, _registered_device_entries() if category == "美容機器" else [])
        if registered_duplicate:
            consecutive_failures += 1
            actions.append({"action": "not_reflected", "brand": brand, "name": name,
                            "reason": "duplicate_model_in_product_master",
                            "duplicate_of_product_id": registered_duplicate.get("product_id"),
                            "staging_id": staging_id})
            continue

        # Step45.9a: 販売情報が必須のカテゴリ(美容機器/サプリ)は、reflect前に
        # 楽天の新品通常販売listingを安全化済みresolverでread-only確認する
        # (楽天検索は1商品1回。confirmedの候補をreflect後の保存に再利用)。
        sale_resolution = None
        rakuten_calls_before = app.rakuten_api_call_snapshot()
        if pipeline.requires_sale_listing_before_reflect(category):
            staged_jan = str(((_fetch_staging_row(staging_id) or {}).get("stage2_payload") or {}).get("jan_code") or "")
            sale_resolution = pipeline.resolve_item_code_for_product(
                brand, name, category, jan_code=None if staged_jan.lower() in ("", "unknown") else staged_jan,
            )
            if sale_resolution.get("status") != "confirmed":
                consecutive_failures += 1
                rakuten_reason = sale_resolution.get("reason") or sale_resolution.get("status")
                deterministic = (sale_resolution.get("initial_candidate_count", 0) > 0
                                 and rakuten_reason in _DETERMINISTIC_RAKUTEN_REASONS)
                not_reflected = {
                    "action": "not_reflected", "brand": brand, "name": name,
                    "reason": "rakuten_new_listing_not_found" if deterministic else "rakuten_check_unavailable",
                    "rakuten_reason": rakuten_reason, "staging_id": staging_id,
                    "rakuten_api_calls": app.rakuten_api_call_delta(rakuten_calls_before),
                    **({"reused_staging": True} if reuse_staging_id else {}),
                }
                if deterministic:
                    not_reflected["gate_failure_marker"] = _record_gate_failure(
                        staging_id, "rakuten_sale_listing", rakuten_reason)
                actions.append(not_reflected)
                continue

        reflect_result = pipeline.reflect_staging_to_product_master(staging_id, dry_run=False)
        if reflect_result.get("status") == "reflected":
            consecutive_failures = 0
            reflected_count += 1
            existing_keys.add(key)
            item_code_result = _resolve_item_code_safely(
                reflect_result["product_id"], brand, name, category, resolution=sale_resolution,
            )
            actions.append({
                "action": "reflected", "brand": brand, "name": name,
                "product_id": reflect_result["product_id"], "item_code_result": item_code_result,
                # Step47.6: この候補のreflect前確認〜itemCode照会で発生した実楽天API回数。
                "rakuten_api_calls": app.rakuten_api_call_delta(rakuten_calls_before),
                **({"reused_staging": True} if reuse_staging_id else {}),
            })
        else:
            consecutive_failures += 1
            actions.append({"action": "reflect_failed", "brand": brand, "name": name, "detail": reflect_result})

        if collect_result.get("limit_exceeded"):
            actions.append({"action": "batch_stop", "reason": "cost_limit_exceeded"})
            break

    report = getattr(candidate_source, "last_report", None)
    if mode == "dry_run":
        actions.extend(_dry_run_plan_actions(item, candidates, report))
    elif report and report.get("external_discovery_diagnostics"):
        # Step45.1: 外部Discoveryを実行した場合、候補0件でも原因を区別できる
        # よう要約を残す(DBには保存せず、run_batchの結果とログのみ)。
        actions.insert(0, {
            "action": "external_discovery_result", "category": category, "target": target,
            "diagnostics": report["external_discovery_diagnostics"],
            "excluded_by_reason": _count_by_reason(report.get("excluded") or []),
        })

    return actions


def _count_by_reason(excluded):
    by_reason = {}
    for e in excluded:
        by_reason[e["reason"]] = by_reason.get(e["reason"], 0) + 1
    return by_reason


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
        by_reason = _count_by_reason(excluded)
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

    if not budget.can_afford("product_collection"):
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
        existing_keys = _product_master_identity_keys()

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
