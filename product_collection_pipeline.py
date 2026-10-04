"""
Phase 3: AIによる商品事前収集(50商品パイロット)の実装。

採用方式(合意事項、固定):
  Stage 1(Grounding収集): gemini-3.1-flash-lite + Google Search Grounding。
    response_schemaは使わない。web_search_queries/grounding_chunks等の
    API由来の検索証拠が無ければ失敗扱い。本文中のURL文字列は信用しない。
  Stage 2(構造化): Google Searchなし。Stage 1の本文・citationのみを入力に、
    厳格response_schemaで構造化する。Stage 1に存在しない情報の推測・補完は
    禁止し、確認不能はunknownとする。source_urlはStage 1の実citation一覧に
    存在するものだけを採用する(それ以外は自動的に捨てる)。

このモジュールは診断パイプライン(Phase 2)・PRODUCT_MASTER_SKIP_RAKUTEN_
ENABLEDには一切触れない。50商品の実API収集はこのモジュール単体の実装
のみで、実行は別途の明示的な承認を経てから行う。

新規の外部サービス・新規PyPI依存は追加しない(既存のgoogle-genai/
psycopg2のみを使用)。既存診断用のGemini利用枠(GEMINI_DAILY_LIMIT/
gemini_usageテーブル)とは完全に分離した専用の使用量・費用管理を持つ。
"""

import concurrent.futures
import json
import re
import time
from datetime import datetime

import psycopg2
from google.genai import types

import app
import product_collection_grounding_poc as poc

# ===== モデル・収集用の分離された上限 =====
STAGE1_MODEL = app.DETAIL_MODEL  # gemini-3.1-flash-lite(実環境検証でGrounding動作確認済み)
STAGE2_MODEL = app.DETAIL_MODEL  # 構造化のみ(Google Searchは使わない)


def call_gemini_for_collection(model, contents, config=None, max_retries=2, timeout=60):
    """
    Phase 3専用のGemini呼び出し関数。

    インシデント修正(合意事項): 診断用の共通ラッパーapp.call_gemini_with_
    retry()は、increment_gemini_usage()(診断用gemini_usageテーブル・
    GEMINI_DAILY_LIMIT関連の警告処理)を内蔵しており、これをPhase 3から
    使うと本番の診断用カウンタを汚染してしまう(2026-10に実際に発生した
    インシデント)。そのため、Phase 3はapp.call_gemini_with_retry()を
    完全に使用せず、この専用関数のみを使う。

    リトライ・timeoutの挙動(ThreadPoolExecutorによる強制timeout、指数
    バックオフ)は診断用ラッパーと同等に実装するが、increment_gemini_
    usage()・診断用usage/警告処理は一切呼ばない。Phase 3のusage・費用・
    上限制御はproduct_collection_usage/PRODUCT_COLLECTION_COST_LIMIT_USD
    のみに限定する(record_usage_and_check_limit経由)。
    """
    last_error = None
    for attempt in range(max_retries):
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(
                    app.client.models.generate_content,
                    model=model, contents=contents, config=config,
                )
                try:
                    response = future.result(timeout=timeout)
                except concurrent.futures.TimeoutError:
                    raise TimeoutError(f"Phase3 Gemini timeout after {timeout}s (model={model})")
            return response
        except TimeoutError as e:
            last_error = e
            print(f"[PHASE3 GEMINI TIMEOUT] {e} attempt={attempt + 1}/{max_retries}", flush=True)
            if attempt >= max_retries - 1:
                raise
            time.sleep(3 * (attempt + 1))
        except Exception as e:
            last_error = e
            print(f"[PHASE3 GEMINI ERROR] {repr(e)} attempt={attempt + 1}/{max_retries}", flush=True)
            if attempt >= max_retries - 1:
                raise
            time.sleep(3 * (attempt + 1))
    raise last_error

# 診断用GEMINI_DAILY_LIMITとは完全に分離した、収集パイロット専用の費用上限。
# 未設定時は無制限ではなく安全側のデフォルト($5)を使う。
PRODUCT_COLLECTION_COST_LIMIT_USD = float(
    __import__("os").environ.get("PRODUCT_COLLECTION_COST_LIMIT_USD", "5.0")
)

# 料金根拠(2026-10時点、複数の独立した業界情報源で一致した値。公式料金表は
# 変動するため、50商品の本実行直前に必ず最新のai.google.dev/gemini-api/docs/
# pricingと照合すること):
#   - gemini-3.1-flash-lite: 入力$0.25/1M tokens、出力$1.50/1M tokens
#     (2026-10時点。将来の値上げ有無は公式ページで裏付けを確認できな
#     かったため断定しない。実行時に必ず公式料金ページで再確認すること)。
#   - Grounding with Google Search(Gemini 3.x系列): 月5,000回までは
#     無料枠(全Gemini 3.xモデル共通)、それ以降は1,000回あたり$14
#     (=1回$0.014)。旧Gemini 2.5系列は1プロンプトあたり課金で$35/1,000と
#     単価が異なるため、モデルを変更する場合は必ず見直すこと。
#     ここでは安全側に倒し、無料枠を考慮せず常に$0.014/回で見積もる
#     (実際の費用は無料枠分だけこれより安くなる)。
_ESTIMATED_INPUT_COST_PER_MILLION_TOKENS = 0.25
_ESTIMATED_OUTPUT_COST_PER_MILLION_TOKENS = 1.50
_ESTIMATED_COST_PER_GROUNDED_SEARCH_QUERY = 0.014

_80_PERCENT_NOTIFIED_BATCHES = set()


def estimate_call_cost_usd(usage, search_query_count=0):
    """usage(prompt/candidates token数)と検索クエリ件数から、costを粗く
    見積もる。正確な金額ではなく、上限制御・比較用の目安であることを
    明示する(関数名・ドキュメントで一貫して「estimate」と呼ぶ)。"""
    prompt_tokens = (usage or {}).get("prompt_token_count") or 0
    output_tokens = (usage or {}).get("candidates_token_count") or 0
    token_cost = (
        prompt_tokens / 1_000_000 * _ESTIMATED_INPUT_COST_PER_MILLION_TOKENS
        + output_tokens / 1_000_000 * _ESTIMATED_OUTPUT_COST_PER_MILLION_TOKENS
    )
    search_cost = search_query_count * _ESTIMATED_COST_PER_GROUNDED_SEARCH_QUERY
    return round(token_cost + search_cost, 6)


# ===== formulation_features統制語彙(poc.pyと同一語彙を再利用) =====
FORMULATION_FEATURE_VALUES = poc.FORMULATION_FEATURE_VALUES
CONFIDENCE_VALUES = poc.CONFIDENCE_VALUES


# ===== DBスキーマ =====

def init_product_collection_tables():
    """product_collection_staging/product_field_sources/
    product_collection_usageの3テーブルを作成する(冪等)。"""
    conn = None
    try:
        conn = psycopg2.connect(app.DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS product_collection_staging (
            staging_id SERIAL PRIMARY KEY,
            batch_id TEXT NOT NULL,
            brand TEXT NOT NULL,
            product_name TEXT NOT NULL,
            category TEXT NOT NULL,
            identity_key TEXT NOT NULL,
            stage1_status TEXT NOT NULL,
            stage1_raw_text TEXT,
            stage1_citations JSONB,
            stage1_search_queries JSONB,
            stage2_status TEXT,
            stage2_payload JSONB,
            conflict_status TEXT,
            conflict_detail JSONB,
            reflected_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS product_field_sources (
            id SERIAL PRIMARY KEY,
            staging_id INTEGER NOT NULL,
            field_name TEXT NOT NULL,
            field_value TEXT NOT NULL,
            source_url TEXT NOT NULL,
            source_type TEXT NOT NULL,
            confidence TEXT NOT NULL,
            confirmed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            matched_sources_count INTEGER NOT NULL DEFAULT 1
        );
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS product_collection_usage (
            id SERIAL PRIMARY KEY,
            batch_id TEXT NOT NULL,
            product_label TEXT,
            stage TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_token_count INTEGER,
            candidates_token_count INTEGER,
            total_token_count INTEGER,
            grounding_used BOOLEAN NOT NULL DEFAULT FALSE,
            estimated_cost_usd NUMERIC,
            called_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)
        conn.commit()
        print("[PRODUCT COLLECTION TABLES READY]", flush=True)
    except Exception as e:
        if conn: conn.rollback()
        print(f"[PRODUCT COLLECTION TABLES ERROR] {repr(e)}", flush=True)
    finally:
        if conn: conn.close()


# ===== 50商品の偏りを抑えた選定 =====

def select_pilot_products(target_count=50, existing_products=None):
    """
    既存products.json(load_products())の中から、category×主要成分タグの
    組み合わせで手薄な(ingredient_strengthが空など情報が不完全な)商品を
    優先して選定する。架空の商品名を新規に作らず、既存の実商品リストから
    選ぶことで、存在しない商品を調査対象にしてしまうリスクを避ける。
    """
    products = existing_products if existing_products is not None else app.load_products()
    if not isinstance(products, list):
        return []

    def _primary_tag(p):
        actives = p.get("active_ingredients") or []
        return actives[0] if actives else "(none)"

    def _is_incomplete(p):
        strength = p.get("ingredient_strength") or {}
        formulation = p.get("formulation") or []
        has_new_vocab = any(f in FORMULATION_FEATURE_VALUES for f in formulation)
        return (not strength) or (not has_new_vocab)

    buckets = {}
    for p in products:
        if not isinstance(p, dict):
            continue
        if not _is_incomplete(p):
            continue
        key = (p.get("category", ""), _primary_tag(p))
        buckets.setdefault(key, []).append(p)

    selected = []
    seen_identity = set()
    # 各(category, tag)バケットから均等に1件ずつ順番に取り、偏りを抑える。
    bucket_keys = list(buckets.keys())
    idx = 0
    while len(selected) < target_count and bucket_keys:
        progressed = False
        for key in list(bucket_keys):
            if len(selected) >= target_count:
                break
            bucket = buckets[key]
            if idx >= len(bucket):
                continue
            candidate = bucket[idx]
            identity = app._normalize_product_master_identity_key(
                candidate.get("brand", ""), candidate.get("name", ""), candidate.get("category", "")
            )
            if identity and identity not in seen_identity:
                seen_identity.add(identity)
                selected.append(candidate)
            progressed = True
        idx += 1
        if not progressed:
            break

    return selected[:target_count]


# 選定結果(どの50商品を対象にしたか)を再現可能な形で保存する先。
# 事前の目視確認は不要だが、後から「どの商品がなぜ選ばれたか」を
# 追跡できるようにする(合意事項)。
BATCH_LOG_DIR = "product_collection_batch_logs"


def log_pilot_selection(batch_id, selected_products):
    """
    select_pilot_products()が選んだ商品リストを、batch_idごとのJSONファイル
    (BATCH_LOG_DIR/<batch_id>_selection.json)として保存する。同じbatch_idで
    再実行した場合は上書きされる(再現性のため、選定理由の元になった
    brand/name/category/active_ingredientsをそのまま記録する)。
    """
    import os
    os.makedirs(BATCH_LOG_DIR, exist_ok=True)
    log_path = os.path.join(BATCH_LOG_DIR, f"{batch_id}_selection.json")
    log_data = {
        "batch_id": batch_id,
        "logged_at": datetime.utcnow().isoformat(),
        "count": len(selected_products),
        "products": [
            {
                "brand": p.get("brand", ""),
                "name": p.get("name", ""),
                "category": p.get("category", ""),
                "active_ingredients": p.get("active_ingredients", []),
            }
            for p in selected_products
        ],
    }
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log_data, f, ensure_ascii=False, indent=2)
    print(f"[PILOT SELECTION LOGGED] {log_path} ({len(selected_products)}件)", flush=True)
    return log_path


def select_and_log_pilot_products(batch_id, target_count=50, existing_products=None):
    """select_pilot_products()を実行し、結果をlog_pilot_selection()で
    再現可能な形に保存した上で返す(事前の目視確認は不要とする合意事項への
    対応。何が選ばれたかは常にログへ残る)。"""
    selected = select_pilot_products(target_count=target_count, existing_products=existing_products)
    log_pilot_selection(batch_id, selected)
    return selected


# ===== 費用記録・上限制御 =====

def record_usage_and_check_limit(batch_id, product_label, stage, model, usage, grounding_used=False, search_query_count=0):
    """
    product_collection_usageへ1回分の呼び出しを記録し、そのbatch_idの
    累計推定費用がPRODUCT_COLLECTION_COST_LIMIT_USDを超えていないかを
    返す。超えている場合は呼び出し側がバッチを即座に停止する。
    80%到達時は既存のsend_admin_emailパターンで1バッチ1回だけ通知する。
    """
    estimated_cost = estimate_call_cost_usd(usage, search_query_count)

    conn = None
    try:
        conn = psycopg2.connect(app.DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO product_collection_usage
                (batch_id, product_label, stage, model, prompt_token_count,
                 candidates_token_count, total_token_count, grounding_used, estimated_cost_usd)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            batch_id, product_label, stage, model,
            (usage or {}).get("prompt_token_count"),
            (usage or {}).get("candidates_token_count"),
            (usage or {}).get("total_token_count"),
            bool(grounding_used),
            estimated_cost,
        ))
        conn.commit()

        cur.execute(
            "SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM product_collection_usage WHERE batch_id = %s",
            (batch_id,),
        )
        total_cost = float(cur.fetchone()[0])
    except Exception as e:
        print(f"[PRODUCT COLLECTION USAGE ERROR] {repr(e)}", flush=True)
        return {"estimated_cost": estimated_cost, "batch_total_cost": None, "limit_exceeded": False}
    finally:
        if conn: conn.close()

    limit_exceeded = total_cost >= PRODUCT_COLLECTION_COST_LIMIT_USD
    if total_cost >= PRODUCT_COLLECTION_COST_LIMIT_USD * 0.8 and batch_id not in _80_PERCENT_NOTIFIED_BATCHES:
        _80_PERCENT_NOTIFIED_BATCHES.add(batch_id)
        app.send_admin_email(
            "[るみろぐ] 商品事前収集(Phase 3)の費用が80%に到達しました",
            f"バッチ {batch_id} の推定費用が上限の80%に達しました。\n"
            f"累計推定費用: ${total_cost:.4f} / ${PRODUCT_COLLECTION_COST_LIMIT_USD:.2f}\n"
            f"このまま続行すると上限に達し、バッチが自動停止します。",
        )

    return {"estimated_cost": estimated_cost, "batch_total_cost": total_cost, "limit_exceeded": limit_exceeded}


# ===== Stage 1: Grounding収集 =====

def build_stage1_prompt(brand, product_name):
    return f"""あなたは化粧品・スキンケア製品の情報調査アシスタントです。
以下の製品についてWeb検索を実際に行い、分かったことを日本語で記述してください。

製品: {brand} {product_name}

必ず検索を実行し、その結果に基づいて回答してください。検索せずに
一般的な知識だけで回答することは禁止します。
以下の情報を、分かった範囲で記述してください(不明な項目は「不明」と
明記すること。推測で埋めることは禁止):
- ブランド名・正式な製品名
- JANコード
- 有効成分とその濃度(%等)
- 製剤特徴(リポソーム化・カプセル化・ナノ化・徐放性・安定化技術・誘導体等、
  メーカーが独自に明記している技術名称があればその名称も)
公式メーカー/ブランドの情報を最優先してください。"""


def run_stage1_collection(brand, product_name, batch_id):
    """
    Stage 1: Google Search Grounding(response_schemaなし)で実際に検索を
    行う。grounding_metadata(web_search_queries/grounding_chunks)に
    実検索の証拠が無い場合はstatus="no_search_evidence"として失敗扱いに
    する(本文中のURLは一切信用しない)。
    """
    prompt = build_stage1_prompt(brand, product_name)
    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
    )
    product_label = f"{brand} {product_name}"

    try:
        response = call_gemini_for_collection(
            STAGE1_MODEL, prompt, config=config, max_retries=1, timeout=60,
        )
    except Exception as e:
        print(f"[STAGE1 ERROR] {product_label}: {repr(e)}", flush=True)
        return {"status": "error", "error": repr(e), "raw_text": "", "citations": [], "search_queries": []}

    grounding = poc.extract_grounding_summary(response)
    usage = poc.extract_usage_summary(response)
    usage_result = record_usage_and_check_limit(
        batch_id, product_label, "stage1", STAGE1_MODEL, usage,
        grounding_used=bool(grounding["sources"]),
        search_query_count=len(grounding["web_search_queries"]),
    )

    has_evidence = bool(grounding["web_search_queries"] or grounding["sources"])
    result = {
        "status": "ok" if has_evidence else "no_search_evidence",
        "raw_text": getattr(response, "text", "") or "",
        "citations": grounding["sources"],
        "search_queries": grounding["web_search_queries"],
        "usage_result": usage_result,
    }
    if not has_evidence:
        print(f"[STAGE1 NO SEARCH EVIDENCE] {product_label}: 失敗扱い(本文のみでは採用しない)", flush=True)
    return result


# ===== Stage 2: 構造化 =====

def build_stage2_prompt(brand, product_name, stage1_text, citations):
    citation_lines = "\n".join(f"- {c['uri']} ({c.get('title', '')})" for c in citations) or "(なし)"
    return f"""以下はステージ1で実際にWeb検索を行って得られた調査結果です。
この内容に書かれている情報のみを使って、指定のJSON形式へ構造化してください。

【製品】{brand} {product_name}

【ステージ1の調査結果(検索に基づく)】
{stage1_text}

【ステージ1で実際に確認された出典URL一覧(これ以外のURLは一切使用禁止)】
{citation_lines}

厳守事項:
- 上記の調査結果に明記されていない情報を推測・補完することは絶対禁止。
  確認できない項目は必ず"unknown"とする。
- **上記の調査結果で名称が明記されている成分は、濃度(%等)が不明であっても
  active_ingredientsに必ず含めること。**「濃度が分からないから成分自体も
  省略する」という判断は禁止。名称が記載されている成分は漏らさず抽出し、
  濃度だけをconcentration="unknown"とする。
- 成分の名称自体が一つも明記されていない場合のみ、active_ingredients=[]
  (空配列)とする。「ingredient: unknown」のような不明プレースホルダー項目を
  作ってはならない。
- 各フィールドのsource_urlには、上記の出典URL一覧に実在するURLを
  そのまま使用すること。一覧に無いURLを生成することは絶対禁止。
  どの出典にも対応しない場合は"unknown"とする。"""


def is_official_source_confirmed(brand, citations):
    """
    official_source_confirmedはモデルの自己申告を信用せず、ここで
    決定論的に判定する(合意事項)。Stage1の実citationのtitleに、
    ブランド名のローマ字表記(英数字部分のみ)が実際に含まれている場合
    のみTrueとする。判定不能(ブランド名が英数字を含まない、一致する
    citationが無い等)な場合は安全側でFalseを返す。
    """
    if not brand or not citations:
        return False
    brand_token = re.sub(r"[^a-z0-9]", "", brand.lower())
    if not brand_token:
        return False
    for c in citations:
        title_token = re.sub(r"[^a-z0-9]", "", str(c.get("title", "") or "").lower())
        if title_token and brand_token in title_token:
            return True
    return False


def sanitize_stage2_payload(payload, valid_citation_urls, stage1_text, brand=None, citations=None):
    """
    Stage 2が返したsource_urlが、Stage 1の実citation一覧に存在しない場合、
    その項目(成分・formulation_feature)を丸ごと破棄する(URLだけを
    unknownにするのではなく、根拠のない値自体を採用しない。合意事項)。
    jan_codeは、文字列としてstage1_textに実在する場合のみ採用し、
    それ以外は"unknown"に強制する(jan_codeは項目別source_urlを
    持たないスキーマのため、本文中への実在チェックで代替する)。

    ingredient=="unknown"のプレースホルダー項目は、citation裏付けの有無に
    関わらず常に除外し、active_ingredients=[]へ統一する(合意事項)。

    official_source_confirmedはモデルの自己申告を使わず、
    is_official_source_confirmed()による決定論的な判定で上書きする
    (brand/citationsを渡さない場合は安全側でFalseにする)。
    """
    if not isinstance(payload, dict):
        return payload

    sanitized = dict(payload)

    jan_code = str(sanitized.get("jan_code", "") or "")
    if jan_code and jan_code != "unknown" and jan_code not in (stage1_text or ""):
        sanitized["jan_code"] = "unknown"

    sanitized["active_ingredients"] = [
        item for item in (sanitized.get("active_ingredients") or [])
        if isinstance(item, dict)
        and item.get("source_url") in valid_citation_urls
        and str(item.get("ingredient", "") or "").strip().lower() != "unknown"
    ]
    sanitized["formulation_features"] = [
        item for item in (sanitized.get("formulation_features") or [])
        if isinstance(item, dict) and item.get("source_url") in valid_citation_urls
    ]
    sanitized["official_source_confirmed"] = is_official_source_confirmed(brand, citations or [])
    return sanitized


def run_stage2_structuring(brand, product_name, stage1_result, batch_id):
    """
    Stage 2: Google Searchを使わず、Stage 1の結果のみを入力に厳格な
    response_schemaで構造化する。Stage 1がno_search_evidence/errorの
    場合はStage 2自体を実行しない(検索の裏付けが無い情報を構造化しても
    意味が無く、費用も無駄になるため)。
    """
    product_label = f"{brand} {product_name}"
    if stage1_result.get("status") != "ok":
        return {"status": "skipped", "reason": stage1_result.get("status"), "payload": None}

    prompt = build_stage2_prompt(brand, product_name, stage1_result["raw_text"], stage1_result["citations"])
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=poc.build_product_collection_response_schema(),
    )

    try:
        response = call_gemini_for_collection(
            STAGE2_MODEL, prompt, config=config, max_retries=1, timeout=60,
        )
    except Exception as e:
        print(f"[STAGE2 ERROR] {product_label}: {repr(e)}", flush=True)
        return {"status": "error", "error": repr(e), "payload": None}

    usage = poc.extract_usage_summary(response)
    usage_result = record_usage_and_check_limit(batch_id, product_label, "stage2", STAGE2_MODEL, usage, grounding_used=False)

    try:
        parsed = json.loads(response.text)
    except Exception as e:
        print(f"[STAGE2 PARSE ERROR] {product_label}: {repr(e)}", flush=True)
        return {"status": "error", "error": repr(e), "payload": None, "usage_result": usage_result}

    valid_urls = {c["uri"] for c in stage1_result["citations"]}
    sanitized = sanitize_stage2_payload(
        parsed, valid_urls, stage1_result["raw_text"],
        brand=brand, citations=stage1_result["citations"],
    )
    return {"status": "ok", "payload": sanitized, "usage_result": usage_result}


# ===== identity照合・conflict検出 =====

def detect_conflicts(staged_payload, existing_product):
    """
    staged_payload(Stage 2の構造化結果)を既存product_masterの該当行
    (無ければNone)とフィールド単位で比較する。既存値が空/未確認なら
    新規情報として安全にマージ可能(status="none"扱い)。既存に確認済みの
    値があり、新しい値と明確に異なる場合のみneeds_reviewとする
    (自動上書きしない、合意事項)。
    """
    if existing_product is None:
        return {"status": "new", "conflicts": []}

    conflicts = []

    existing_jan = str(existing_product.get("jan_code", "") or "").strip()
    staged_jan = str(staged_payload.get("jan_code", "") or "").strip()
    if existing_jan and staged_jan and staged_jan != "unknown" and existing_jan != staged_jan:
        conflicts.append({"field": "jan_code", "existing": existing_jan, "staged": staged_jan})

    existing_strength = existing_product.get("ingredient_strength") or {}
    if isinstance(existing_strength, dict):
        for item in (staged_payload.get("active_ingredients") or []):
            ingredient = item.get("ingredient", "")
            concentration = item.get("concentration", "")
            existing_value = existing_strength.get(ingredient)
            if existing_value and concentration and concentration != "unknown" and str(existing_value) != str(concentration):
                conflicts.append({
                    "field": f"ingredient_strength.{ingredient}",
                    "existing": existing_value,
                    "staged": concentration,
                })

    # formulation_features: 既存product_masterのformulationのうち、今回の
    # 統制語彙(FORMULATION_FEATURE_VALUES)に含まれるものだけを比較対象とする
    # (旧いformulationタグ("oil_formula"等)は今回の統制語彙と無関係のため
    # 誤って矛盾扱いしない)。既存に確認済みの統制語彙の特徴があり、今回
    # 新たに確認された特徴集合と完全に重ならない(=既存の確認内容を
    # 一切再現できていない)場合のみneeds_reviewとする。今回の確認結果が
    # 空(新しい情報が無い)場合は、既存の確認内容と比較する材料が無いため
    # 矛盾としない。
    existing_formulation = set(existing_product.get("formulation") or [])
    existing_controlled = existing_formulation & set(FORMULATION_FEATURE_VALUES)
    staged_features = {
        item.get("feature") for item in (staged_payload.get("formulation_features") or [])
        if item.get("feature") and item.get("feature") != "unknown"
    }
    if existing_controlled and staged_features and existing_controlled.isdisjoint(staged_features):
        conflicts.append({
            "field": "formulation_features",
            "existing": sorted(existing_controlled),
            "staged": sorted(staged_features),
        })

    if conflicts:
        return {"status": "needs_review", "conflicts": conflicts}
    return {"status": "none", "conflicts": []}


# ===== ステージング書き込み =====

def write_staging_record(batch_id, brand, product_name, category, stage1_result, stage2_result, conflict_result):
    """
    収集結果をproduct_collection_stagingへ保存する(この時点では
    product_masterへは一切書き込まない。承認後の別ステップで反映する)。
    成分・formulation_featureごとの出典はproduct_field_sourcesへ分けて
    保存する(「情報ごとにsource URL・source種別・確認日時・confidenceを
    保持する」という合意事項)。
    """
    identity_key = app._normalize_product_master_identity_key(brand, product_name, category)
    conn = None
    try:
        conn = psycopg2.connect(app.DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO product_collection_staging
                (batch_id, brand, product_name, category, identity_key,
                 stage1_status, stage1_raw_text, stage1_citations, stage1_search_queries,
                 stage2_status, stage2_payload, conflict_status, conflict_detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING staging_id
        """, (
            batch_id, brand, product_name, category, identity_key,
            stage1_result.get("status"), stage1_result.get("raw_text", ""),
            json.dumps(stage1_result.get("citations", [])),
            json.dumps(stage1_result.get("search_queries", [])),
            stage2_result.get("status"),
            json.dumps(stage2_result.get("payload")) if stage2_result.get("payload") is not None else None,
            conflict_result.get("status"),
            json.dumps(conflict_result.get("conflicts", [])),
        ))
        staging_id = cur.fetchone()[0]

        payload = stage2_result.get("payload") or {}
        for item in (payload.get("active_ingredients") or []):
            if item.get("source_url"):
                cur.execute("""
                    INSERT INTO product_field_sources
                        (staging_id, field_name, field_value, source_url, source_type, confidence)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (
                    staging_id, f"active_ingredient:{item.get('ingredient', '')}",
                    item.get("concentration", "unknown"), item["source_url"], "web", item.get("confidence", "unknown"),
                ))
        for item in (payload.get("formulation_features") or []):
            if item.get("source_url"):
                cur.execute("""
                    INSERT INTO product_field_sources
                        (staging_id, field_name, field_value, source_url, source_type, confidence)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (
                    staging_id, f"formulation_feature:{item.get('feature', '')}",
                    item.get("other_detail", "unknown"), item["source_url"], "web", item.get("confidence", "unknown"),
                ))

        conn.commit()
        return staging_id
    except Exception as e:
        if conn: conn.rollback()
        print(f"[STAGING WRITE ERROR] {repr(e)}", flush=True)
        return None
    finally:
        if conn: conn.close()


# ===== 商品単位・バッチ単位の収集処理(Stage1+Stage2+conflict+staging) =====

def collect_one_product(brand, product_name, category, batch_id, existing_product=None):
    """1商品分のStage1→Stage2→conflict検出→staging保存を一貫して行う。
    費用上限(PRODUCT_COLLECTION_COST_LIMIT_USD)を超えた場合は、この商品の
    処理を完了させた上で呼び出し元(collect_batch)に伝え、バッチを停止する。"""
    stage1_result = run_stage1_collection(brand, product_name, batch_id)

    stage2_result = run_stage2_structuring(brand, product_name, stage1_result, batch_id)

    conflict_result = {"status": "skipped", "conflicts": []}
    if stage2_result.get("status") == "ok":
        conflict_result = detect_conflicts(stage2_result["payload"], existing_product)

    staging_id = write_staging_record(
        batch_id, brand, product_name, category, stage1_result, stage2_result, conflict_result,
    )

    limit_exceeded = bool((stage1_result.get("usage_result") or {}).get("limit_exceeded")) or bool(
        (stage2_result.get("usage_result") or {}).get("limit_exceeded")
    )

    return {
        "staging_id": staging_id,
        "stage1_status": stage1_result.get("status"),
        "stage2_status": stage2_result.get("status"),
        "conflict_status": conflict_result.get("status"),
        "limit_exceeded": limit_exceeded,
    }


def collect_batch(products, batch_id, product_master_lookup=None):
    """
    products(選定済みの商品リスト、各{brand,name,category})をバッチとして
    順次収集する。費用上限を超えた時点で即座に残りをスキップし、バッチを
    停止する(合意事項)。
    """
    results = []
    for p in products:
        existing = None
        if product_master_lookup is not None:
            existing = product_master_lookup(p.get("brand", ""), p.get("name", ""), p.get("category", ""))
        result = collect_one_product(
            p.get("brand", ""), p.get("name", ""), p.get("category", ""), batch_id, existing_product=existing,
        )
        results.append(result)
        if result["limit_exceeded"]:
            print(f"[PRODUCT COLLECTION] batch_id={batch_id} 費用上限到達のためバッチを停止します。", flush=True)
            break
    return results


# ===== バッチの再開(冪等性) =====
# インシデント発生時のような中断からの再開用。完了済み商品・Stageの
# 重複API呼び出し・usage二重記録・staging重複を防ぐ(合意事項)。

def load_batch_selection_log(batch_id):
    """select_and_log_pilot_products()が保存した選定ログを読み込み、
    再開処理の対象商品リスト(そのバッチで選定された全商品)を復元する。"""
    import os
    log_path = os.path.join(BATCH_LOG_DIR, f"{batch_id}_selection.json")
    with open(log_path, encoding="utf-8") as f:
        data = json.load(f)
    return data["products"]


def get_existing_staging_record(batch_id, identity_key):
    """同一batch_id+identity_keyの既存stagingレコードを1件取得する
    (無ければNone)。複数件存在する場合は最新(staging_id最大)を使う。"""
    conn = None
    try:
        conn = psycopg2.connect(app.DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            SELECT staging_id, stage1_status, stage1_raw_text, stage1_citations,
                   stage1_search_queries, stage2_status
            FROM product_collection_staging
            WHERE batch_id = %s AND identity_key = %s
            ORDER BY staging_id DESC LIMIT 1
        """, (batch_id, identity_key))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "staging_id": row[0], "stage1_status": row[1], "stage1_raw_text": row[2],
            "stage1_citations": row[3] or [], "stage1_search_queries": row[4] or [],
            "stage2_status": row[5],
        }
    except Exception as e:
        print(f"[GET STAGING RECORD ERROR] {repr(e)}", flush=True)
        return None
    finally:
        if conn: conn.close()


def update_staging_stage2(staging_id, stage2_result, conflict_result):
    """既存stagingレコードのstage2関連フィールドのみ更新する(Stage1は
    再実行しないStage2単独再開用。新規行は作らない)。"""
    conn = None
    try:
        conn = psycopg2.connect(app.DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            UPDATE product_collection_staging
            SET stage2_status = %s, stage2_payload = %s, conflict_status = %s, conflict_detail = %s
            WHERE staging_id = %s
        """, (
            stage2_result.get("status"),
            json.dumps(stage2_result.get("payload")) if stage2_result.get("payload") is not None else None,
            conflict_result.get("status"),
            json.dumps(conflict_result.get("conflicts", [])),
            staging_id,
        ))

        payload = stage2_result.get("payload") or {}
        for item in (payload.get("active_ingredients") or []):
            if item.get("source_url"):
                cur.execute("""
                    INSERT INTO product_field_sources
                        (staging_id, field_name, field_value, source_url, source_type, confidence)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (
                    staging_id, f"active_ingredient:{item.get('ingredient', '')}",
                    item.get("concentration", "unknown"), item["source_url"], "web", item.get("confidence", "unknown"),
                ))
        for item in (payload.get("formulation_features") or []):
            if item.get("source_url"):
                cur.execute("""
                    INSERT INTO product_field_sources
                        (staging_id, field_name, field_value, source_url, source_type, confidence)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (
                    staging_id, f"formulation_feature:{item.get('feature', '')}",
                    item.get("other_detail", "unknown"), item["source_url"], "web", item.get("confidence", "unknown"),
                ))
        conn.commit()
    except Exception as e:
        if conn: conn.rollback()
        print(f"[STAGING UPDATE ERROR] {repr(e)}", flush=True)
    finally:
        if conn: conn.close()


def resume_one_product(brand, product_name, category, batch_id, existing_product=None):
    """
    1商品分の再開判定・処理。
    - staging未作成(未実行) → フル実行(Stage1+Stage2)。
    - stage1_status=="ok" かつ stage2_status=="ok" → 完了済み、何もしない。
    - stage1_status=="ok" かつ stage2_status!="ok" → Stage1は再実行せず、
      保存済みのstage1結果を使ってStage2のみ再開し、既存staging行を更新する。
    - stage1_status!="ok"(no_search_evidence/error) → 既に完了した失敗
      として扱い、再試行しない(明示的な再試行要求ではないため)。
    """
    identity_key = app._normalize_product_master_identity_key(brand, product_name, category)
    existing = get_existing_staging_record(batch_id, identity_key)

    if existing is None:
        result = collect_one_product(brand, product_name, category, batch_id, existing_product=existing_product)
        return {"action": "full_run", **result}

    if existing["stage1_status"] == "ok" and existing["stage2_status"] == "ok":
        return {"action": "skip_complete", "staging_id": existing["staging_id"], "limit_exceeded": False}

    if existing["stage1_status"] == "ok" and existing["stage2_status"] != "ok":
        stage1_result = {
            "status": "ok",
            "raw_text": existing["stage1_raw_text"] or "",
            "citations": existing["stage1_citations"] or [],
            "search_queries": existing["stage1_search_queries"] or [],
        }
        stage2_result = run_stage2_structuring(brand, product_name, stage1_result, batch_id)
        conflict_result = {"status": "skipped", "conflicts": []}
        if stage2_result.get("status") == "ok":
            conflict_result = detect_conflicts(stage2_result["payload"], existing_product)
        update_staging_stage2(existing["staging_id"], stage2_result, conflict_result)
        limit_exceeded = bool((stage2_result.get("usage_result") or {}).get("limit_exceeded"))
        return {
            "action": "resume_stage2",
            "staging_id": existing["staging_id"],
            "stage2_status": stage2_result.get("status"),
            "conflict_status": conflict_result.get("status"),
            "limit_exceeded": limit_exceeded,
        }

    # stage1_status in ("no_search_evidence", "error"): 完了済みの失敗として再試行しない
    return {"action": "skip_failed", "staging_id": existing["staging_id"], "limit_exceeded": False}


def resume_batch(batch_id, product_master_lookup=None):
    """
    保存済みの選定ログ(load_batch_selection_log)を読み込み、商品ごとに
    resume_one_product()で再開判定・処理する。完了済み商品は一切API呼び出し
    しない(冪等)。費用上限到達時は即座に残りを停止する。
    """
    products = load_batch_selection_log(batch_id)
    results = []
    for p in products:
        existing_product = None
        if product_master_lookup is not None:
            existing_product = product_master_lookup(p.get("brand", ""), p.get("name", ""), p.get("category", ""))
        result = resume_one_product(
            p.get("brand", ""), p.get("name", ""), p.get("category", ""), batch_id, existing_product=existing_product,
        )
        results.append(result)
        if result.get("limit_exceeded"):
            print(f"[PRODUCT COLLECTION RESUME] batch_id={batch_id} 費用上限到達のため停止します。", flush=True)
            break
    return results


def get_batch_resume_status(batch_id):
    """
    再開前の状態確認用。選定ログ(50商品)と既存stagingレコードを突き合わせ、
    完了(stage1 ok・stage2 ok)/途中(stage1 okだがstage2未完了)/
    失敗済み(stage1がno_search_evidence/error)/未実行(stagingなし)の
    件数を返す。API呼び出しは一切行わない(読み取りのみ)。
    """
    products = load_batch_selection_log(batch_id)
    complete, partial, failed, not_started = 0, 0, 0, 0
    for p in products:
        identity_key = app._normalize_product_master_identity_key(
            p.get("brand", ""), p.get("name", ""), p.get("category", ""),
        )
        existing = get_existing_staging_record(batch_id, identity_key)
        if existing is None:
            not_started += 1
        elif existing["stage1_status"] == "ok" and existing["stage2_status"] == "ok":
            complete += 1
        elif existing["stage1_status"] == "ok":
            partial += 1
        else:
            failed += 1

    return {
        "batch_id": batch_id,
        "total_selected": len(products),
        "complete": complete,
        "partial_stage1_only": partial,
        "failed_stage1": failed,
        "not_started": not_started,
    }


# ===== 承認後のDB反映(dry-run可能) =====

def reflect_staging_to_product_master(staging_id, dry_run=True):
    """
    1件のstagingレコードをproduct_masterへ反映する。conflict_status=
    "needs_review"の場合は反映しない(自動上書きしない、合意事項)。
    dry_run=True(既定)では実際には書き込まず、反映されるはずの内容のみ
    返す。
    """
    conn = None
    try:
        conn = psycopg2.connect(app.DATABASE_URL)
        cur = conn.cursor()
        cur.execute(
            "SELECT brand, product_name, category, stage2_status, stage2_payload, conflict_status, reflected_at "
            "FROM product_collection_staging WHERE staging_id = %s",
            (staging_id,),
        )
        row = cur.fetchone()
        if not row:
            return {"status": "not_found"}
        brand, product_name, category, stage2_status, stage2_payload, conflict_status, reflected_at = row

        if stage2_status != "ok":
            return {"status": "skipped", "reason": f"stage2_status={stage2_status}"}
        if conflict_status == "needs_review":
            return {"status": "skipped", "reason": "needs_review"}
        if reflected_at is not None:
            return {"status": "already_reflected"}

        payload = stage2_payload or {}
        # "unknown"プレースホルダー項目は反映時にも除外する(合意事項②の
        # 後方互換: この修正より前に収集済みのstaging行にまだ残っている
        # 場合があるため、再API実行なしでも安全に除外する)。
        real_ingredients = [
            i for i in (payload.get("active_ingredients") or [])
            if str(i.get("ingredient", "") or "").strip().lower() != "unknown"
        ]
        product_for_master = {
            "brand": brand,
            "name": product_name,
            "category": category,
            "active_ingredients": [i.get("ingredient") for i in real_ingredients],
            "ingredient_strength": {
                i.get("ingredient"): i.get("concentration")
                for i in real_ingredients
                if i.get("concentration") and i.get("concentration") != "unknown"
            },
            "formulation": [f.get("feature") for f in (payload.get("formulation_features") or [])],
            "verified_at": time.time(),
        }

        identity_key = app._normalize_product_master_identity_key(brand, product_name, category)
        cur.execute("SELECT product_id FROM product_master WHERE identity_key = %s", (identity_key,))
        existing_row = cur.fetchone()
        reflect_action = "update" if existing_row else "insert"

        if dry_run:
            return {
                "status": "would_reflect", "product": product_for_master,
                "action": reflect_action,
                "existing_product_id": existing_row[0] if existing_row else None,
            }

        product_id = app.upsert_product_master(product_for_master, data_source="ai_precollected")
        if product_id is None:
            return {"status": "error", "reason": "upsert failed"}

        cur.execute(
            "UPDATE product_collection_staging SET reflected_at = %s WHERE staging_id = %s",
            (datetime.utcnow(), staging_id),
        )
        conn.commit()
        return {"status": "reflected", "product_id": product_id, "action": reflect_action}
    except Exception as e:
        if conn: conn.rollback()
        print(f"[REFLECT ERROR] staging_id={staging_id}: {repr(e)}", flush=True)
        return {"status": "error", "reason": repr(e)}
    finally:
        if conn: conn.close()


def reflect_batch_to_product_master(batch_id, dry_run=True):
    """バッチ内の全stagingレコードをreflect_staging_to_product_master()に
    かけ、商品単位の結果一覧を返す(商品単位・バッチ単位の再実行に対応)。"""
    conn = None
    try:
        conn = psycopg2.connect(app.DATABASE_URL)
        cur = conn.cursor()
        cur.execute(
            "SELECT staging_id FROM product_collection_staging WHERE batch_id = %s ORDER BY staging_id",
            (batch_id,),
        )
        staging_ids = [row[0] for row in cur.fetchall()]
    except Exception as e:
        print(f"[REFLECT BATCH ERROR] {repr(e)}", flush=True)
        return []
    finally:
        if conn: conn.close()

    return [
        {"staging_id": sid, **reflect_staging_to_product_master(sid, dry_run=dry_run)}
        for sid in staging_ids
    ]
