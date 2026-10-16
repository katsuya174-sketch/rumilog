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
import hashlib
import json
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit

import psycopg2
from google.genai import types

import app
import citation_verification
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


# ===== Step45.1: カテゴリ別の収集config(category adapter) =====
# 共通のDiscovery/Stage1/Stage2パイプラインのまま、カテゴリ固有の
#   - search concept(外部Discoveryで何を探すか。内部targetを人が検索する
#     意味へ変換。推薦定義app._DEVICE_DEFAULTS/_SUPPLEMENT_DEFAULTSが
#     source of truth)
#   - Discoveryで一次情報から確認させる項目
#   - Stage1の要求項目
#   - Stage2 schema(_CATEGORY_ATTRIBUTES_EXTRACTION_SPECS)
#   - validator(app.validate_category_attributes / CATEGORY_ATTRIBUTE_SCHEMAS)
# だけを切り替える。configが無いカテゴリ(化粧品)は既存の文面・挙動のまま。
# カテゴリを追加する場合はここへconfigを1件追加するだけで同じ経路を使える。

def _device_search_concept(target):
    defaults = app._DEVICE_DEFAULTS.get(target)
    if not defaults:
        return None
    function = defaults.get("device_function") or target
    purpose = app._DEVICE_PURPOSE_LABELS.get(target, "")
    return f"{function}方式の顔用美容機器" + (f"（用途: {purpose}）" if purpose else "")


def _supplement_display_name(target):
    for display_name, defaults in app._SUPPLEMENT_DEFAULTS.items():
        if target in (defaults.get("ingredient_focus") or []):
            return display_name
    return None


def _supplement_search_concept(target):
    name = _supplement_display_name(target)
    return f"{name}を含むサプリメント" if name else None


def _device_method_list():
    return "・".join(v.get("device_function", k) for k, v in app._DEVICE_DEFAULTS.items())


_CATEGORY_COLLECTION_CONFIGS = {
    "美容機器": {
        "role": "美容機器",
        # Step45.9a: coverage/診断で販売情報(item_code+楽天リンク)が必須のため、
        # reflect前に楽天の新品通常販売listingを確認する。
        "requires_sale_listing_before_reflect": True,
        "search_concept": _device_search_concept,
        "discovery_checks": [
            "その方式(上記の条件)を採用していることが、メーカー/ブランド公式サイト・"
            "取扱説明書等の一次情報で確認できること",
            "ブランド名・正式な製品名(型番があれば型番)が一次情報で確認できること",
        ],
        "stage1_items": lambda: [
            f"方式(次のうち公式情報に明記されているもの: {_device_method_list()})と、"
            "メーカーが使用している方式・技術の名称",
            "主な機能・モード",
            "使用上の注意・禁忌事項(使用できない人・部位等)",
        ],
    },
    "サプリメント": {
        "role": "サプリメント",
        "requires_sale_listing_before_reflect": True,
        "search_concept": _supplement_search_concept,
        "discovery_checks": [
            "その成分(上記の条件)を含むことが、メーカー/ブランド公式サイト・商品表示等の"
            "一次情報で確認できること",
            "ブランド名・正式な製品名が一次情報で確認できること",
            # Step48.1: 自己申告は信用せず、Stage1→商品区分ゲートで再検証する。
            "医薬品(第1類・第2類・指定第2類・第3類医薬品等)・医薬部外品は候補にしないこと"
            "(サプリメント・健康食品・機能性表示食品・栄養機能食品として販売されている商品のみ)",
        ],
        "stage1_items": lambda: [
            "商品区分(医薬品(第1類・第2類・指定第2類・第3類医薬品等)・医薬部外品・機能性表示食品・"
            "栄養機能食品・健康食品・サプリメント等のうち、公式情報・商品表示に明記されている区分表示)",
            "含有成分とその含有量(1日あたり・1粒あたり等、公式の表示どおり)",
            # Step50.2: JANの記載が無い楽天出品を規格で照合するため、販売規格ごとのJANと内容量を取得する。
            "販売規格ごとのJANコードと内容量(公式表示どおり。粒数・カプセル数・錠数・重量(g)・容量(ml)・日分を"
            "区別し、1つの規格ごとに「JANコード：内容量」の形で1行に書くこと。公式情報で確認できない規格・JANは書かない)",
            "1日の摂取目安量",
            "1回あたりの摂取量(粒数・mg等)",
            "摂取上の注意事項",
        ],
    },
}


def category_collection_config(category):
    """categoryの収集config。化粧品等、専用configの無いカテゴリはNone
    (既存の文面・挙動を使う)。"""
    return _CATEGORY_COLLECTION_CONFIGS.get(category)


def requires_sale_listing_before_reflect(category):
    """reflect前に楽天の新品通常販売listingの確認が必要なカテゴリか
    (configの無い化粧品はFalse=従来どおりreflect後にitem_code解決)。"""
    config = category_collection_config(category)
    return bool(config and config.get("requires_sale_listing_before_reflect"))


def discovery_search_concept(category, target):
    """外部Discoveryで使う検索概念。configの無いカテゴリはNone。"""
    config = category_collection_config(category)
    if config is None:
        return None
    return config["search_concept"](target)


def discovery_condition_label(category, target):
    """Discovery構造化プロンプトの「条件」欄。化粧品は従来どおりtarget。"""
    concept = discovery_search_concept(category, target)
    return concept if concept else target


# Step45.15: 外部Discovery(検索)のプロンプトに共通で入れる、検索出典を必須と
# する指示(カテゴリ非依存)。検索したふりをさせる表現は使わない。成功判定は
# 従来どおり、APIが実際のsearch evidence(grounding metadata)を返した場合だけ。
DISCOVERY_SEARCH_REQUIREMENT = (
    "- 候補は、今回の検索で得た検索結果(出典)で確認できたものだけを挙げること。\n"
    "  検索を実行できなかった場合や、検索結果の出典で確認できない場合は、\n"
    "  候補を挙げずにその旨を回答すること。"
)


def _build_category_discovery_prompt(category, target, config):
    concept = discovery_search_concept(category, target)
    checks = "\n".join(f"- {c}" for c in config["discovery_checks"])
    return f"""あなたは{config["role"]}製品の情報調査アシスタントです。
以下の条件を満たす、日本国内で実際に販売されている市販品を、最大3件まで
Web検索で調査してください。

カテゴリ: {category}
条件: {concept}

確認すること:
{checks}

必ず検索を実行し、その結果に基づいて回答してください。検索せずに一般的な
知識だけで回答することは禁止します。

厳守事項:
- 実際に検索して見つかった、実在する商品のみを挙げること。架空の商品名・
  存在が確認できない商品名を作ってはならない。
- 各商品について、ブランド名・正式な製品名・条件を満たすことが確認できた
  根拠の要約を記述すること。
- 条件を満たす商品が1件も見つからない場合は、無理に挙げず「見つかりません
  でした」と回答すること。
- 楽天市場等の販売実績だけでは条件適合の根拠にはしないこと
  (販売の有無と、条件の確認は別の情報源で行うこと)。
{DISCOVERY_SEARCH_REQUIREMENT}"""


def _build_category_stage1_prompt(brand, product_name, config):
    items = "\n".join(f"- {i}" for i in config["stage1_items"]())
    return f"""あなたは{config["role"]}製品の情報調査アシスタントです。
以下の製品についてWeb検索を実際に行い、分かったことを日本語で記述してください。

製品: {brand} {product_name}

必ず検索を実行し、その結果に基づいて回答してください。検索せずに
一般的な知識だけで回答することは禁止します。
以下の情報を、分かった範囲で記述してください(不明な項目は「不明」と
明記すること。推測で埋めることは禁止):
- ブランド名・正式な製品名
- JANコード
{items}

各情報について、どの出典で確認したかが分かるように記述してください。
公式メーカー/ブランドの情報を最優先してください。"""


# ===== Stage 1: Grounding収集 =====

def build_stage1_prompt(brand, product_name, category=None):
    """本収集Stage1のプロンプト。category_collection_config(category)が
    Noneのカテゴリ(化粧品)は従来の文面のまま。美容機器/サプリメントは
    同じ共通構造に、configの調査項目(方式・成分等)を差し込む(Step45.1)。"""
    config = category_collection_config(category)
    if config is not None:
        return _build_category_stage1_prompt(brand, product_name, config)
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

成分探索における重要な注意点(すべての成分種別に共通する一般ルール):
- メーカー/ブランド公式の「全成分」「ingredients」「INCI」表示を最優先で
  探索すること。有効成分・主要成分の説明だけでなく、対象商品の全成分情報を
  探すこと。
- Matrixyl(マトリキシル)、Argireline(アルジレロックス)等のtrade name/
  複合原料名を発見した場合、その名称だけで調査を終了しないこと。公式情報
  源でその複合原料を構成する具体的なINCI成分名(例:パルミトイルトリ
  ペプチド-1、アセチルヘキサペプチド-8等)が公開されていれば、それを
  必ず調べて報告すること。これはペプチド系成分に限らず、レチノール・
  ビタミンC等、trade nameで呼ばれるあらゆる成分種別に共通するルールである。
- 1つのtrade name/複合原料名の構成成分が複数ある場合は、構成INCI成分名を
  1つずつ個別の項目として(まとめた1文ではなく)列挙すること。
- 構成INCI成分が出典から確認できない場合は、推測で補わず「不明」と
  明記すること(trade name自体は分かった成分として報告してよい)。
公式メーカー/ブランドの情報を最優先してください。"""


def run_stage1_collection(brand, product_name, batch_id, category=None):
    """
    Stage 1: Google Search Grounding(response_schemaなし)で実際に検索を
    行う。grounding_metadata(web_search_queries/grounding_chunks)に
    実検索の証拠が無い場合はstatus="no_search_evidence"として失敗扱いに
    する(本文中のURLは一切信用しない)。
    """
    prompt = build_stage1_prompt(brand, product_name, category=category)
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

# Step43: category_attributes(カテゴリ固有情報)の抽出schema定義。
# フィールド名はapp.CATEGORY_ATTRIBUTE_SCHEMASの必須フィールドと対応する
# (定義自体はapp.pyではなくここに置き、poc側はこの定義を受け取るだけの
# category非依存な汎用ビルダーのまま維持する)。
# cosmeticsはCATEGORY_ATTRIBUTE_SCHEMASに存在しないため、ここにも対応する
# specは無い(=category_attributes自体がStage2出力に現れない。既存出力を
# 完全維持)。
#
# フィールド選定(Step42監査+Step43指示に基づく):
# - 美容機器: method(現在の推薦が実際に使うdevice_type一致判定の唯一の
#   根拠。6種のenum+unknown)のみ。modes/usage_frequencyは現在の推薦・
#   検証のどちらにも使われず根拠も乏しいため追加しない。contraindications
#   (禁忌事項)は安全情報として収集するが、必須ではない(method無しでは
#   そもそも実効候補として使えないため、methodだけが必須)。
# - サプリメント: 成分名・濃度は共通フィールドのactive_ingredientsを
#   source of truthとし、ここでは二重管理しない(Step38の"ingredients"
#   フィールドは廃止)。category_attributesは製品単位の摂取情報
#   (1日の摂取目安量・1回あたりの量・注意事項)のみを持つ。
_CATEGORY_ATTRIBUTES_EXTRACTION_SPECS = {
    "美容機器": {
        # Step45.1: enumと公式表記との対応は推薦定義(app._DEVICE_DEFAULTSの
        # キー/device_function)から生成し、方式を手書きで二重管理しない。
        "method": types.Schema(
            type="STRING",
            enum=list(app._DEVICE_DEFAULTS.keys()) + ["unknown"],
            description=(
                "調査結果本文で確認できた方式。値と方式の対応: "
                + "、".join(f"{k}={v.get('device_function', k)}" for k, v in app._DEVICE_DEFAULTS.items())
                + "。該当/不明な場合は'unknown'"
            ),
        ),
        "contraindications": types.Schema(
            type="STRING",
            description="使用上の注意・禁忌事項の要約。不明な場合は'unknown'",
        ),
    },
    "サプリメント": {
        "dosage": types.Schema(
            type="STRING",
            description="確認できた1日の摂取目安量。不明な場合は'unknown'",
        ),
        "serving_size": types.Schema(
            type="STRING",
            description="1回あたりの摂取量(粒数・mg等)。不明な場合は'unknown'",
        ),
        "precautions": types.Schema(
            type="STRING",
            description="注意事項の要約。不明な場合は'unknown'",
        ),
        # Step47.1: 主要成分。タグはコード側でactive_ingredientsに実在する
        # 成分名からだけ導出する(derive_primary_ingredient_tags)。
        "primary_ingredients": types.Schema(
            type="STRING",
            description=(
                "この商品の主要成分として出典で明示されている成分名(商品名・公式の商品種別で主要成分と"
                "されている、または商品説明でその成分の摂取が商品の主目的と明示されているもの)。"
                "active_ingredientsに挙げた成分名と同じ表記で、複数ある場合は「、」区切り。"
                "成分の記載順や含有量の多さだけで決めないこと。根拠が無い場合は'unknown'"
            ),
        ),
        # Step48.1: 商品区分。出典に区分表示が明記されている場合のみ。採否は
        # supplement_product_classification()がStage1本文と照合して決める。
        "product_classification": types.Schema(
            type="STRING",
            enum=list(app.SUPPLEMENT_PRODUCT_CLASSES),
            description=(
                "出典に明記されている商品区分。drug=医薬品(第1類・第2類・指定第2類・第3類医薬品等を含む)、"
                "quasi_drug=医薬部外品、foods_with_function_claims=機能性表示食品、"
                "nutrient_function_food=栄養機能食品、health_food=健康食品、supplement=サプリメント"
                "(栄養補助食品)。区分表示が出典に無い場合・一般知識でしか判断できない場合は'unknown'"
            ),
        ),
    },
}


def _category_attributes_extraction_spec(category):
    return _CATEGORY_ATTRIBUTES_EXTRACTION_SPECS.get(category)


def build_stage2_prompt(brand, product_name, stage1_text, citations, category_attributes_spec=None):
    citation_lines = "\n".join(f"- {c['uri']} ({c.get('title', '')})" for c in citations) or "(なし)"
    category_attributes_instructions = ""
    if category_attributes_spec:
        field_names = "、".join(category_attributes_spec.keys())
        category_attributes_instructions = f"""

【category_attributes(カテゴリ固有情報: {field_names})】
上記と同じ原則で、これらのフィールドも上記の調査結果本文に明記されている
場合のみvalueを記載し、明記されていない場合は必ずvalue="unknown"とする
こと。推測・一般知識からの補完は絶対禁止。source_urlも、active_ingredients
と同じく出典URL一覧に実在するものだけを使用し、根拠が無い場合は"unknown"
とすること。"""
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
- **複合原料名/trade name/blend名(独自のブレンド名・配合技術名等)と、
  それを構成する具体的な要素が両方とも調査結果に明記されている場合、
  複合原料名だけに縮約せず、構成要素を1つずつ個別のactive_ingredients
  項目として抽出すること。**複合原料名自体も別項目として残してよいが、
  構成要素の抽出を省略してはならない。構成要素が調査結果に明記されて
  いない場合は、知識から推測して展開してはならない(その場合は複合原料名
  のみを記載する)。これは特定の成分種別専用のルールではなく、化粧品・
  美容機器・サプリメント等、どのカテゴリの商品にも共通して適用される
  一般原則である。
- 各フィールドのsource_urlには、上記の出典URL一覧に実在するURLを
  そのまま使用すること。一覧に無いURLを生成することは絶対禁止。
  どの出典にも対応しない場合は"unknown"とする。{category_attributes_instructions}"""


# Step45.1: source_urlとStage1/Discoveryの実citationとの照合。Google Search
# Groundingのcitationは長いリダイレクトURL(vertexaisearch.cloud.google.com/
# grounding-api-redirect/...)で、モデルはこれを一覧から書き写す。完全一致を
# 原則とし、書き写し時に付きがちな前後の空白・括弧/引用符・末尾の句読点
# だけを取り除いた結果が実citationと完全一致する場合に限り同一sourceとみなす
# (ドメイン一致等の緩い判定はしない)。採用時はcitation側の正規URLに置き換える。
_CITATION_URL_WRAPPERS = "<>\"'`「」『』()（）[]［］"
_CITATION_URL_TRAILING = ".,、。;；:："


def match_citation_url(url, valid_citation_urls):
    """urlが実citationのいずれかと同一sourceならそのcitation URLを、そうで
    なければNoneを返す。"""
    if not isinstance(url, str) or not url:
        return None
    if url in valid_citation_urls:
        return url
    candidate = url.strip()
    for _ in range(3):
        candidate = candidate.strip().strip(_CITATION_URL_WRAPPERS).rstrip(_CITATION_URL_TRAILING)
        if candidate in valid_citation_urls:
            return candidate
    return None


# Step45.3: 公式情報源判定。Stage1の実citation(Google Search Groundingの
# grounding_chunks)だけを根拠に決定論的に判定する。固定のブランド→ドメイン
# 対応表・類似度判定・部分文字列だけの緩いドメイン一致・Gemini自身の主張は
# 使わない。比較時の正規化はUnicode正規化(NFKC)・大文字小文字・空白・
# ブランド名に通常含まれる区切り記号の除去だけにとどめる。
# Groundingのcitation URLはリダイレクトURL(最終ドメインを安全に確定できない)
# のため使わない。titleには実データ上ソースのドメイン名が入る。
_BRAND_SEPARATORS_RE = re.compile(r"[\s・･·\-‐‑‒–—_.'’®™]+")
_DOMAIN_TITLE_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")
_MIN_BRAND_MATCH_LENGTH = 2


def _nfkc_casefold(text):
    return unicodedata.normalize("NFKC", str(text or "")).casefold()


def _normalize_brand_text(text):
    return _BRAND_SEPARATORS_RE.sub("", _nfkc_casefold(text))


def _char_class(ch):
    if "a" <= ch <= "z" or "0" <= ch <= "9":
        return "ascii"
    code = ord(ch)
    if 0x30A0 <= code <= 0x30FF:
        return "katakana"
    if 0x3040 <= code <= 0x309F:
        return "hiragana"
    if 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF:
        return "kanji"
    return "other"


def _contains_brand_with_boundary(brand_norm, text_norm):
    """text_norm内にbrand_normが、前後を同じ文字種で挟まれずに現れるか。
    「ヤーマン」が「ヤーマンド」、「dhc」が「dhcp」に一致しないようにする。"""
    if len(brand_norm) < _MIN_BRAND_MATCH_LENGTH:
        return False
    first, last = _char_class(brand_norm[0]), _char_class(brand_norm[-1])
    start = text_norm.find(brand_norm)
    while start != -1:
        end = start + len(brand_norm)
        before_ok = start == 0 or first == "other" or _char_class(text_norm[start - 1]) != first
        after_ok = end == len(text_norm) or last == "other" or _char_class(text_norm[end]) != last
        if before_ok and after_ok:
            return True
        start = text_norm.find(brand_norm, start + 1)
    return False


# Step47.4: 公式情報源の照合だけで使うブランド名の候補。元の文字列に加え、
# 末尾の括弧注記(メーカー・運営会社等)を1つだけ除いた形を候補にする
# (例:「ネイチャーメイド（大塚製薬）」→「ネイチャーメイド」)。()と全角（）は
# NFKCで同じ扱い。括弧内の文字列単独・中間部分の削除・単語分割・略称・翻訳・
# 対応表・類似度は使わない。除去後が空/短すぎる場合は使わない。
# Product Masterのbrand・identity_key・重複判定・楽天同一性判定には影響しない。
_TRAILING_BRAND_ANNOTATION_RE = re.compile(r"^(.*?\S)\s*\([^()]*\)\s*$")


def official_brand_forms(brand):
    folded = unicodedata.normalize("NFKC", str(brand or "")).strip()
    forms = [folded] if folded else []
    match = _TRAILING_BRAND_ANNOTATION_RE.match(folded)
    if match:
        base = match.group(1).strip()
        if len(_normalize_brand_text(base)) >= _MIN_BRAND_MATCH_LENGTH and base not in forms:
            forms.append(base)
    return forms


def _ascii_brand_source(brand):
    """英字トークンを取り出す元の文字列。末尾に括弧注記がある場合は除いた形
    (括弧内の英字だけがトークンになるのを防ぐ)。"""
    forms = official_brand_forms(brand)
    return forms[-1] if forms else ""


def _ascii_brand_tokens(brand):
    """ブランド名中のASCII英数字表記(区切り記号を除いて連結、2文字以上)。"""
    folded = re.sub(r"[\-‐‑‒–—_.'’®™]", "", _nfkc_casefold(_ascii_brand_source(brand)))
    runs = re.findall(r"[a-z0-9]+(?:\s+[a-z0-9]+)*", folded)
    return {re.sub(r"\s+", "", r) for r in runs if len(re.sub(r"\s+", "", r)) >= _MIN_BRAND_MATCH_LENGTH}


def _free_text_confirms_brand(brand, text):
    """自由文に、正規化したブランド名(日本語表記を含む)が文字種境界付きで
    含まれるか。区切り記号を空白として残した形(語の境界を保つ)と、取り除いた
    形(ブランド名自体の区切りの有無の差を吸収)の両方で確認する。"""
    folded = _nfkc_casefold(text).strip()
    if not folded:
        return False
    spaced = _BRAND_SEPARATORS_RE.sub(" ", folded)
    joined = _normalize_brand_text(folded)
    candidates = {_normalize_brand_text(form) for form in official_brand_forms(brand)} | _ascii_brand_tokens(brand)
    return any(
        _contains_brand_with_boundary(c, t)
        for c in candidates if c
        for t in (spaced, joined)
    )


def _citation_title_confirms_brand(brand, title):
    title_folded = _nfkc_casefold(title).strip()
    if not title_folded:
        return False
    if _DOMAIN_TITLE_RE.fullmatch(title_folded):
        # ドメイン名のtitle: ASCIIブランド表記がドメインのラベル(ハイフン除去、
        # TLDを除く)と完全一致する場合のみ。部分文字列一致はしない。
        labels = {label.replace("-", "") for label in title_folded.split(".")[:-1]}
        return bool(_ascii_brand_tokens(brand) & labels)
    return _free_text_confirms_brand(brand, title_folded)


# ===== Step45.5: citation先ページ自身の「サイトの名乗り」による確認 =====
# title判定で公式確認できない場合のみ、citationを安全に追跡し(citation_
# verification.fetch_html)、最終ページのog:site_name/トップレベルJSON-LD
# Organization・Corporation・WebSite.nameだけでブランドを照合する(Step45.10で
# <title>末尾のサイト名部分は肯定材料から除外。抽出・保存は診断用に残す)。
# 最終ドメインとcanonicalはcitation title(=Groundingが示したソースの
# ドメイン)と一致(またはそのサブドメイン)している必要がある。
# 結果はstage1_citationsの各要素に"page_verification"として保存し、判定は
# 保存値から毎回決定論的に再計算する(HTML本文は保存しない)。
MAX_PAGE_VERIFICATION_DOMAINS = 5


def _domain_within(host, base):
    host = (host or "").lower().strip(".")
    base = (base or "").lower().strip(".")
    return bool(host and base) and (host == base or host.endswith("." + base))


def _citation_title_domain(citation):
    title = _nfkc_casefold(citation.get("title", "")).strip()
    return title if _DOMAIN_TITLE_RE.fullmatch(title) else None


def _page_identity_confirms_brand(brand, citation):
    verification = citation.get("page_verification") if isinstance(citation, dict) else None
    if not isinstance(verification, dict) or verification.get("fetch_status") != citation_verification.OK:
        return False
    title_domain = _citation_title_domain(citation)
    if not title_domain or not _domain_within(verification.get("final_domain"), title_domain):
        return False
    canonical_domain = verification.get("canonical_domain")
    if canonical_domain and not _domain_within(canonical_domain, title_domain):
        return False
    identity = verification.get("site_identity") or {}
    # Step45.10: 公式identityの肯定材料はog:site_nameとトップレベルJSON-LD
    # Organization/Corporation/WebSite.nameのみ。<title>(title_site_name)は
    # 比較・小売サイトでは商品名(ブランド名を含む)がサイト名の位置に来る
    # ことがあるため(例: 価格.com「価格.com - パナソニック …」)、使わない。
    # 証明できなければnot_confirmed(fail closed)。
    fields = [identity.get("og_site_name"), *(identity.get("jsonld_names") or [])]
    # Step55: ブランド名側の法人格表記(「株式会社ドクターセレクト」等)も、名乗り側と同じく境界として扱う
    # (名乗り側だけ置き換えると、同じ法人名どうしでも一致しなかったため)。
    brand_core = _corporate_designators_as_spaces(brand).strip() or brand
    return any(isinstance(f, str) and _free_text_confirms_brand(brand_core, _corporate_designators_as_spaces(f))
               for f in fields)


# Step49.1: サイトの名乗りに含まれる法人格表記(「小林製薬株式会社」等)は、ブランド名との
# 境界として扱う(「小林製薬」+「株式会社」のように漢字が連続して境界判定に失敗するため)。
# 名乗り側の法人格表記を空白に置き換えるだけで、照合規則・対象フィールドは変えない。
_CORPORATE_DESIGNATOR_RE = re.compile(r"株式会社|有限会社|合同会社|\(株\)|（株）|㈱")


def _corporate_designators_as_spaces(text):
    return _CORPORATE_DESIGNATOR_RE.sub(" ", unicodedata.normalize("NFKC", str(text or "")))


def _utcnow():
    return datetime.now(timezone.utc)


# ===== Step50: 承認済みのブランド別表記・メーカー対応の登録(brand_aliases.json) =====
# カタカナ⇔英字等の機械的な変換・推測はしない。根拠を確認し人が承認した対応関係
# (status="approved")だけを、公式情報源の照合に使う。対応関係だけで商品を確認済みに
# はしない(official_confirmation()が商品ごとの条件を追加で確認する)。
BRAND_ALIASES_PATH = __import__("os").path.join(__import__("os").path.dirname(__import__("os").path.abspath(__file__)),
                                                "brand_aliases.json")
_BRAND_ALIAS_CACHE = {"mtime": None, "entries": []}


def load_approved_brand_aliases(path=None):
    """brand_aliases.jsonのうちstatus=="approved"の項目だけを返す(ファイルが無ければ空)。"""
    os_ = __import__("os")
    path = path or BRAND_ALIASES_PATH
    try:
        mtime = os_.path.getmtime(path)
    except OSError:
        return []
    if _BRAND_ALIAS_CACHE["mtime"] != (path, mtime):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        _BRAND_ALIAS_CACHE.update(mtime=(path, mtime), entries=[
            e for e in data.get("entries", []) if isinstance(e, dict) and e.get("status") == "approved"])
    return _BRAND_ALIAS_CACHE["entries"]


def _brand_lookup_forms(brand):
    """登録との照合に使うブランドの表記(元の表記・末尾注記を除いた表記・注記の中身)。"""
    forms = set(official_brand_forms(brand))
    match = _TRAILING_BRAND_ANNOTATION_RE.match(unicodedata.normalize("NFKC", str(brand or "")).strip())
    if match:
        inner = re.sub(r"^.*\(([^()]*)\)\s*$", r"\1", match.group(0)).strip()
        if inner:
            forms.add(inner)
    return {k for k in (_normalize_brand_text(f) for f in forms) if len(k) >= _MIN_BRAND_MATCH_LENGTH}


def brand_alias_entry(brand, entries=None):
    """ブランド名に一致する承認済み登録項目。0件・複数件(曖昧)ならNone。"""
    entries = load_approved_brand_aliases() if entries is None else entries
    forms = _brand_lookup_forms(brand)
    hits = [e for e in entries if forms & {_normalize_brand_text(n) for n in e.get("names", [])}]
    return hits[0] if len(hits) == 1 else None


def _spec_source_domains(payload, citations):
    """商品の仕様(成分・カテゴリ固有項目)の根拠になったcitationのtitleドメイン集合。"""
    uri_domain = {c.get("uri"): _citation_title_domain(c) for c in citations or [] if isinstance(c, dict)}
    payload = payload or {}
    urls = [i.get("source_url") for i in payload.get("active_ingredients") or [] if isinstance(i, dict)]
    urls += [v.get("source_url") for v in (payload.get("category_attributes") or {}).values() if isinstance(v, dict)]
    return {uri_domain.get(u) for u in urls if uri_domain.get(u)}


def official_confirmation(brand, citations, payload=None, entries=None):
    """公式情報源の確認(Step50)。
    1. 既存の判定(is_official_source_confirmed)で確認できればbasis="direct"(従来どおり)。
    2. できない場合、承認済み登録の別表記(alias)・メーカー名(maker)で、既存と同じ
       照合(ドメイン名の完全一致・サイトの名乗り)を行う。ただし、その公式ドメインが
       この商品の仕様の出典にもなっていること(商品ごとの条件)を必須とする。
    条件を満たさなければ従来どおり不合格。"""
    citations = [c for c in (citations or []) if isinstance(c, dict)]
    if is_official_source_confirmed(brand, citations):
        return {"confirmed": True, "basis": "direct"}
    entry = brand_alias_entry(brand, entries)
    if not entry:
        return {"confirmed": False, "basis": None}
    spec_domains = _spec_source_domains(payload, citations)
    for basis, names in (("alias", entry.get("names", [])), ("maker", entry.get("makers", []))):
        for name in names:
            for citation in citations:
                domain = _citation_title_domain(citation)
                if not domain or domain not in spec_domains:
                    continue
                if _citation_title_confirms_brand(name, citation.get("title", "")) or _page_identity_confirms_brand(name, citation):
                    return {"confirmed": True, "basis": basis, "matched_name": name, "domain": domain,
                            "registry_id": entry.get("id")}
    return {"confirmed": False, "basis": None, "registry_id": entry.get("id"),
            "reason": "registered_names_not_confirmed_on_spec_source_domain"}


# Step55: 一時的な取得失敗(timeout・通信エラー・5xx)は、同じ確認の中で回数を制限して再試行する。
# 403/429(アクセス拒否・回数制限)はすぐには再試行しない(従来どおり期限・回数の範囲の再確認だけ)。
PAGE_IMMEDIATE_RETRIES = 2
PAGE_IMMEDIATE_RETRY_WAIT_SECONDS = (2, 5)
_page_retry_sleep = time.sleep


def _immediately_retryable(fetched):
    reason = str(fetched.get("reason") or "")
    return fetched.get("status") == citation_verification.UNVERIFIABLE and (
        reason in ("timeout", "connection_error") or reason.startswith("http_5"))


def _fetch_page_with_retry(uri):
    """戻り値: (fetched, attempts)。"""
    attempts = 0
    while True:
        fetched = citation_verification.fetch_html(uri)
        attempts += 1
        if not _immediately_retryable(fetched) or attempts > PAGE_IMMEDIATE_RETRIES:
            return fetched, attempts
        _page_retry_sleep(PAGE_IMMEDIATE_RETRY_WAIT_SECONDS[min(attempts - 1, len(PAGE_IMMEDIATE_RETRY_WAIT_SECONDS) - 1)])


def _verify_citation_page(brand, citation):
    fetched, attempts = _fetch_page_with_retry(citation.get("uri", ""))
    final_url = fetched.get("final_url") or ""
    record = {
        "fetch_status": fetched["status"],
        "final_domain": fetched.get("final_domain") or (urlsplit(final_url).hostname or None),
        "checked_at": _utcnow().isoformat(),
        "attempts": attempts,
    }
    if fetched["status"] != citation_verification.OK:
        record.update({"status": fetched["status"], "reason": fetched.get("reason")})
        return record
    identity = citation_verification.extract_site_identity(fetched.get("html", ""))
    canonical = identity.pop("canonical", None)
    record["canonical_domain"] = urlsplit(urljoin(final_url, canonical)).hostname if canonical else None
    record["site_identity"] = identity
    title_domain = _citation_title_domain(citation)
    if not _domain_within(record["final_domain"], title_domain):
        record.update({"status": "rejected", "reason": "final_domain_mismatch"})
    elif record["canonical_domain"] and not _domain_within(record["canonical_domain"], title_domain):
        record.update({"status": "rejected", "reason": "canonical_domain_mismatch"})
    else:
        confirmed = _page_identity_confirms_brand(brand, dict(citation, page_verification=record))
        record.update({"status": "confirmed" if confirmed else "not_confirmed",
                       "reason": None if confirmed else "brand_not_in_site_identity"})
    return record


# ===== Step45.13: transient citationの再確認 =====
# page確認がtimeout/通信エラー/5xxで評価できなかったcitation(transient)だけを、
# 前回の試行から PAGE_RETRY_INTERVAL 以上経過していれば、最大
# PAGE_RETRY_MAX_ATTEMPTS 回まで再確認する。正常に評価済みのcitationは
# 再取得しない。retry状態はpage_verification内に保存する(schema変更なし):
#   checked_at(初回確認)、retry_count、last_attempt_at、transient_exhausted
# 最大回数後もtransientならtransient_exhausted=True(保留)。これは非公式の
# 判定ではなく、gate failure markerとも別(スケジューリング状態)。
PAGE_RETRY_INTERVAL = timedelta(hours=24)
PAGE_RETRY_MAX_ATTEMPTS = 2
# Step50: 403/429(アクセス拒否・回数制限)は商品の不適格ではなく「確認保留」として
# 扱い、一時エラーと同じく期限・回数の範囲で再確認する(回避はしない)。
TRANSIENT_PAGE_REASONS = {"timeout", "connection_error", "http_403", "http_429"}


def is_transient_page_verification(verification):
    if not isinstance(verification, dict) or verification.get("fetch_status") != citation_verification.UNVERIFIABLE:
        return False
    reason = str(verification.get("reason") or "")
    return reason in TRANSIENT_PAGE_REASONS or reason.startswith("http_5")


def _parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def retry_transient_page_verifications(brand, citations, now=None):
    """transient citationのうち再確認期限が来たものだけを再取得する。
    戻り値: (citations, changed)。公式確認できた時点で残りは再確認しない。
    時刻情報の無い既存のtransient記録は、取得せずに現在時刻を最終試行として
    記録するだけにする(前回確認から24時間以上の条件を守るため)。"""
    now = now or _utcnow()
    if not brand or not citations:
        return citations, False
    out = [dict(c) if isinstance(c, dict) else c for c in citations]
    changed = False
    for citation in out:
        verification = citation.get("page_verification") if isinstance(citation, dict) else None
        if not is_transient_page_verification(verification):
            continue
        retry_count = int(verification.get("retry_count") or 0)
        if retry_count >= PAGE_RETRY_MAX_ATTEMPTS:
            if not verification.get("transient_exhausted"):
                citation["page_verification"] = dict(verification, transient_exhausted=True)
                changed = True
            continue
        last = _parse_time(verification.get("last_attempt_at") or verification.get("checked_at"))
        if last is None:
            citation["page_verification"] = dict(verification, last_attempt_at=now.isoformat())
            changed = True
            continue
        if now - last < PAGE_RETRY_INTERVAL:
            continue
        record = _verify_citation_page(brand, citation)
        record.update({
            "first_checked_at": verification.get("first_checked_at") or verification.get("checked_at"),
            "retry_count": retry_count + 1,
            "last_attempt_at": now.isoformat(),
        })
        if is_transient_page_verification(record) and record["retry_count"] >= PAGE_RETRY_MAX_ATTEMPTS:
            record["transient_exhausted"] = True
        citation["page_verification"] = record
        changed = True
        print(f"[CITATION PAGE RETRY] brand={brand} domain={citation.get('title')} "
              f"retry={record['retry_count']} status={record.get('status')} reason={record.get('reason')}", flush=True)
        if record.get("status") == "confirmed":
            break
    return out, changed


def has_page_verification(citations):
    return any(isinstance(c, dict) and isinstance(c.get("page_verification"), dict) for c in citations or [])


def _page_verification_order(citations):
    """Step49.1: ページ確認の順序。Stage1で多く引用されたドメイン(公式の商品
    ページは複数回引用されやすい)から、同数なら元の出現順で確認する。各ドメインの
    最初のcitationだけを返す。対応表・ドメイン名の推測は使わない。"""
    first, counts = {}, {}
    for index, citation in enumerate(citations):
        if not isinstance(citation, dict) or not citation.get("uri"):
            continue
        domain = _citation_title_domain(citation)
        if not domain:
            continue
        counts[domain] = counts.get(domain, 0) + 1
        first.setdefault(domain, (index, citation))
    ordered = sorted(first, key=lambda d: (-counts[d], first[d][0]))
    return [first[d][1] for d in ordered]


def attach_citation_page_verification(brand, citations):
    """title判定で公式確認できない場合のみ、citation先ページを最大
    MAX_PAGE_VERIFICATION_DOMAINS ドメインまで(同一ドメインは1回)確認し、
    結果を各citationの"page_verification"へ付けたリストを返す。1件で公式
    確認できた時点で終了する。timeout/403等は「そのsourceでは確認不能」として
    次へ進む。既に検証済みの場合・ブランド不明の場合は何もしない。"""
    if not brand or not citations or has_page_verification(citations):
        return citations
    if any(isinstance(c, dict) and _citation_title_confirms_brand(brand, c.get("title", "")) for c in citations):
        return citations
    out = [dict(c) if isinstance(c, dict) else c for c in citations]
    seen_domains = set()
    for citation in _page_verification_order(out):
        title_domain = _citation_title_domain(citation)
        if title_domain in seen_domains:
            continue
        if len(seen_domains) >= MAX_PAGE_VERIFICATION_DOMAINS:
            break
        seen_domains.add(title_domain)
        citation["page_verification"] = _verify_citation_page(brand, citation)
        print(f"[CITATION PAGE CHECK] brand={brand} domain={title_domain} "
              f"status={citation['page_verification'].get('status')} reason={citation['page_verification'].get('reason')}",
              flush=True)
        if citation["page_verification"].get("status") == "confirmed":
            break
    return out


# ===== Step55: 公式情報の確認の見落とし防止・再利用 =====
# (1) 仕様の出典になったドメイン(商品ごとの公式確認の条件にもなる)は、ドメイン数の上限に関係なく確認する。
#     それ以外は従来どおり MAX_PAGE_VERIFICATION_DOMAINS まで。再確認時は、まだ確認していないドメインも確認する。
# (2) 他のstaging行で確認済みの同じドメインの結果を再利用する(HTTPを行わない)。取得に成功した結果は
#     サイトの名乗りを現在のブランドで判定し直す(PAGE_REUSE_TTL以内。同じドメインの記録で最終ドメイン・名乗りが
#     すべて同じ場合だけ)。403・timeout等の取得失敗は、
#     PAGE_RETRY_INTERVAL以内なら同じ結果として扱い、同じサイトへすぐに再アクセスしない。
# (3) 確認結果(理由・試行回数・再利用元)は各citationのpage_verificationに保存する。
PAGE_REUSE_TTL = timedelta(days=30)


def _ok_record_matches_domain(record, domain):
    if record.get("fetch_status") != citation_verification.OK:
        return False
    if not _domain_within(record.get("final_domain"), domain):
        return False
    canonical = record.get("canonical_domain")
    return not canonical or _domain_within(canonical, domain)


def build_page_verification_cache(rows):
    """rows: [(staging_id, stage1_citations)]。ドメイン → [(確認時刻, page_verification, staging_id)](新しい順)。
    他の行から再利用した記録は元の確認として数えない(再利用の連鎖で古い結果が延命しないため)。"""
    cache = {}
    for staging_id, citations in rows or []:
        for citation in citations or []:
            if not isinstance(citation, dict) or not isinstance(citation.get("page_verification"), dict):
                continue
            record = citation["page_verification"]
            domain = _citation_title_domain(citation)
            if not domain or record.get("reused_from_staging") is not None:
                continue
            checked = _parse_time(record.get("last_attempt_at") or record.get("checked_at"))
            if checked is not None:
                cache.setdefault(domain, []).append((checked, record, staging_id))
    for entries in cache.values():
        entries.sort(key=lambda e: e[0], reverse=True)
    return cache


def cached_page_verification(cache, domain, now=None):
    """再利用できる同じドメインの確認結果(無ければNone)。"""
    now = now or _utcnow()
    ok = [(checked, record, staging_id) for checked, record, staging_id in (cache or {}).get(domain, [])
          if _ok_record_matches_domain(record, domain)]
    # 同じドメインでも、サブドメインやページによってサイトの名乗りが違う場合(例: yahoo.co.jp の
    # 「Yahoo!ショッピング」と「LOHACO by ASKUL」)は、ドメイン単位の結果として扱えないため再利用しない。
    variants = {json.dumps([r.get("final_domain"), r.get("canonical_domain"), r.get("site_identity")], ensure_ascii=False,
                           sort_keys=True, default=str) for _, r, _ in ok}
    if len(variants) == 1:
        checked, record, staging_id = ok[0]
        if now - checked <= PAGE_REUSE_TTL:
            return dict(record, reused_from_staging=staging_id, reused_at=now.isoformat())
    if ok:
        return None
    for checked, record, staging_id in (cache or {}).get(domain, []):
        if is_transient_page_verification(record) and now - checked < PAGE_RETRY_INTERVAL:
            return dict(record, reused_from_staging=staging_id, reused_at=now.isoformat())
    return None


_ASCII_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-.][A-Za-z0-9]+)*")
_DOMAIN_GENERIC_LABELS = {"www", "co", "or", "ne", "ac", "go", "com", "jp", "net", "org", "shop", "store", "official"}


def relevant_domains(brand, product_name, stage1_text, citations):
    """Step55: 製品専用サイト・ブランドのサイトなど、この商品との関連が明確なドメイン。ドメイン名(ハイフン除去)が、
    ブランド名・商品名、またはStage1本文のうちブランド名を含む行に書かれた英字表記(連続する1〜3語の連結)と
    一致するもの(例: 「株式会社ユニッシュ E-Polation Supreme製品ページ」→ e-polation-supreme.jp)。
    確認の順序にだけ使い、公式かどうかの判定には使わない(判定は従来どおりサイトの名乗り・登録済みの別表記)。"""
    forms = [f for f in {_corporate_designators_as_spaces(x).strip() for x in official_brand_forms(brand)} if len(f) >= 2]
    sources = [str(brand or ""), str(product_name or "")]
    sources += [line for line in str(stage1_text or "").splitlines() if any(f in line for f in forms)]
    phrases = set()
    for text in sources:
        words = ["".join(ch for ch in w.lower() if ch.isalnum()) for w in _ASCII_WORD_RE.findall(unicodedata.normalize("NFKC", text))]
        for i in range(len(words)):
            for n in (1, 2, 3):
                phrase = "".join(words[i:i + n])
                if len(words[i:i + n]) == n and len(phrase) >= 4:
                    phrases.add(phrase)
    found = set()
    for citation in citations or []:
        domain = _citation_title_domain(citation) if isinstance(citation, dict) else None
        if not domain:
            continue
        labels = [l.replace("-", "") for l in domain.split(".")[:-1] if l not in _DOMAIN_GENERIC_LABELS]
        if any(l in phrases for l in labels):
            found.add(domain)
    return found


def refresh_page_verifications(brand, citations, payload=None, cache=None, now=None, product_name=None, stage1_text=None):
    """Step55: 公式確認が取れていないstagingのcitationを再確認する。戻り値: (citations, changed, log)。
    順序: 期限が来た一時エラーの再確認 → まだ確認していないドメイン(仕様の出典ドメイン → 製品専用サイト等の関連が
    明確なドメイン(relevant_domains)の順で上限なし、それ以外は引用の多い順に上限まで)。各ドメインはまず他の行の
    確認結果を再利用し、無ければ取得する。公式確認できた時点で終了。"""
    now = now or _utcnow()
    log = []
    if not brand or not citations or official_confirmation(brand, citations, payload)["confirmed"]:
        return citations, False, log
    out, changed = retry_transient_page_verifications(brand, citations, now=now)
    if changed:
        log.append({"action": "transient_retry"})
    if official_confirmation(brand, out, payload)["confirmed"]:
        return out, changed, log
    out = [dict(c) if isinstance(c, dict) else c for c in out]
    spec = _spec_source_domains(payload, out)
    related = relevant_domains(brand, product_name or (payload or {}).get("product_name"), stage1_text, out) - spec
    priority = spec | related
    checked = {_citation_title_domain(c) for c in out
               if isinstance(c, dict) and isinstance(c.get("page_verification"), dict)}
    other_checked = len([d for d in checked if d and d not in priority])
    order = _page_verification_order(out)
    rank = lambda c: 0 if _citation_title_domain(c) in spec else 1 if _citation_title_domain(c) in related else 2
    order = sorted(order, key=rank)   # sortedは安定(同じ優先度の中では引用の多い順を保つ)
    for citation in order:
        domain = _citation_title_domain(citation)
        if domain in checked:
            continue
        if domain not in priority:
            if other_checked >= MAX_PAGE_VERIFICATION_DOMAINS:
                log.append({"domain": domain, "action": "skipped_domain_limit"})
                continue
            other_checked += 1
        checked.add(domain)
        record = cached_page_verification(cache, domain, now)
        action = "reused" if record else "fetched"
        if record is None:
            record = _verify_citation_page(brand, citation)
        citation["page_verification"] = record
        changed = True
        log.append({"domain": domain, "action": action, "spec_source": domain in spec, "related": domain in related,
                    "status": record.get("status"),
                    "reason": record.get("reason"), "attempts": record.get("attempts")})
        print(f"[CITATION PAGE CHECK] brand={brand} domain={domain} action={action} status={record.get('status')} "
              f"reason={record.get('reason')}", flush=True)
        if official_confirmation(brand, out, payload)["confirmed"]:
            break
    return out, changed, log


def load_page_verification_cache():
    """全staging行の保存済みpage_verificationから再利用用のキャッシュを作る(読み取りのみ)。"""
    conn = psycopg2.connect(app.DATABASE_URL)
    try:
        cur = conn.cursor()
        cur.execute("SELECT staging_id, stage1_citations FROM product_collection_staging WHERE stage1_citations IS NOT NULL")
        return build_page_verification_cache(cur.fetchall())
    finally:
        conn.close()


def update_staging_citations(staging_id, citations):
    """staging再利用時に取得したpage_verificationをstage1_citations(既存JSONB)
    へ保存する(以後の再利用でHTTPを再実行しないため)。"""
    conn = None
    try:
        conn = psycopg2.connect(app.DATABASE_URL)
        cur = conn.cursor()
        cur.execute("UPDATE product_collection_staging SET stage1_citations = %s WHERE staging_id = %s",
                    (json.dumps(citations, ensure_ascii=False), staging_id))
        conn.commit()
    finally:
        if conn:
            conn.close()


def is_official_source_confirmed(brand, citations):
    """
    official_source_confirmedはモデルの自己申告を信用せず、ここで
    決定論的に判定する(合意事項)。Stage1の実citationのtitleが、ブランド名
    (日本語表記を含む)そのもの、またはASCIIブランド表記と完全一致する
    ドメインラベルを含む場合、またはcitation先ページ自身のサイトの名乗り
    (保存済みpage_verification、Step45.5)がブランドを含む場合のみTrue。
    判定不能な場合は安全側でFalse。HTTPアクセスはしない(保存値から再計算)。
    """
    if not brand or not citations:
        return False
    return any(
        isinstance(c, dict) and (
            _citation_title_confirms_brand(brand, c.get("title", ""))
            or _page_identity_confirms_brand(brand, c)
        )
        for c in citations
    )


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

    def _with_canonical_source(items, keep):
        out = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            canonical = match_citation_url(item.get("source_url"), valid_citation_urls)
            if canonical is None or not keep(item):
                continue
            out.append(item if canonical == item.get("source_url") else dict(item, source_url=canonical))
        return out

    sanitized["active_ingredients"] = _with_canonical_source(
        sanitized.get("active_ingredients"),
        lambda item: str(item.get("ingredient", "") or "").strip().lower() != "unknown",
    )
    sanitized["formulation_features"] = _with_canonical_source(
        sanitized.get("formulation_features"), lambda item: True,
    )
    sanitized["official_source_confirmed"] = is_official_source_confirmed(brand, citations or [])

    # Step43: category_attributes(美容機器/サプリメント等のカテゴリ固有
    # フィールド)。active_ingredients/formulation_featuresと同じ原則で、
    # source_urlがStage1の実citation一覧に無いフィールドはvalue/source_url
    # を"unknown"に強制する(根拠のない値を採用しない)。cosmeticsでは
    # payloadにcategory_attributesキー自体が無いため、このブロックは
    # 何もしない(既存出力を完全維持)。
    if isinstance(sanitized.get("category_attributes"), dict):
        sanitized_attrs = {}
        for field_name, field_value in sanitized["category_attributes"].items():
            if not isinstance(field_value, dict):
                continue
            source_url = match_citation_url(field_value.get("source_url"), valid_citation_urls)
            if source_url is not None:
                sanitized_attrs[field_name] = {
                    "value": field_value.get("value", "unknown"),
                    "confidence": field_value.get("confidence", "unknown"),
                    "source_url": source_url,
                }
            else:
                sanitized_attrs[field_name] = {
                    "value": "unknown", "confidence": "unknown", "source_url": "unknown",
                }
        sanitized["category_attributes"] = sanitized_attrs

    return sanitized


def run_stage2_structuring(brand, product_name, category, stage1_result, batch_id):
    """
    Stage 2: Google Searchを使わず、Stage 1の結果のみを入力に厳格な
    response_schemaで構造化する。Stage 1がno_search_evidence/errorの
    場合はStage 2自体を実行しない(検索の裏付けが無い情報を構造化しても
    意味が無く、費用も無駄になるため)。

    category(Step43): category_attributesをcosmetics以外のカテゴリでも
    抽出できるようにするためのcategory-aware化。
    _category_attributes_extraction_spec(category)がNoneを返す場合
    (cosmetics)は、プロンプト・response_schemaともcategory_attributes
    関連の変更が一切無く、既存の挙動を完全維持する。
    """
    product_label = f"{brand} {product_name}"
    if stage1_result.get("status") != "ok":
        return {"status": "skipped", "reason": stage1_result.get("status"), "payload": None}

    attrs_spec = _category_attributes_extraction_spec(category)
    prompt = build_stage2_prompt(
        brand, product_name, stage1_result["raw_text"], stage1_result["citations"],
        category_attributes_spec=attrs_spec,
    )
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=poc.build_product_collection_response_schema(category_attributes_spec=attrs_spec),
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
    sanitized = _enforce_device_method_evidence(category, sanitized, stage1_result["raw_text"])
    return {"status": "ok", "payload": sanitized, "usage_result": usage_result}


def _enforce_device_method_evidence(category, sanitized, stage1_text):
    """Step45.2: 推薦定義(app._DEVICE_DEFAULTS)でmethod_evidence_termsを持つ
    方式は、Stage1の調査本文にその語が実在する場合のみmethodとして採用する
    (jan_codeの本文実在チェックと同じ決定論的な検証)。例えば本文に
    「イオン導入」しか無い商品を、エレクトロポレーション方式として採用しない。
    method_evidence_termsを持たない方式・美容機器以外のカテゴリは変更しない。"""
    if category != "美容機器" or not isinstance(sanitized, dict):
        return sanitized
    attrs = sanitized.get("category_attributes")
    method = attrs.get("method") if isinstance(attrs, dict) else None
    if not isinstance(method, dict):
        return sanitized
    terms = (app._DEVICE_DEFAULTS.get(str(method.get("value", "") or "")) or {}).get("method_evidence_terms")
    if not terms:
        return sanitized
    text = (stage1_text or "").lower()
    if any(str(t).lower() in text for t in terms):
        return sanitized
    out = dict(sanitized)
    out["category_attributes"] = dict(attrs, method={"value": "unknown", "confidence": "unknown", "source_url": "unknown"})
    return out


# ===== Step47.1: サプリの主要成分(primary)と副成分の分離 =====
_PRIMARY_INGREDIENT_SEPARATORS_RE = re.compile(r"[、,，/／・\n]+")


def _ingredient_name_key(name):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(name or "")).lower())


def derive_primary_ingredient_tags(stage2_payload):
    """Stage2のcategory_attributes.primary_ingredients(出典照合済みの成分名)の
    うち、同じペイロードのactive_ingredientsに実在する成分名だけを
    normalize_ingredient_tag()でタグ化して返す(sorted list)。Stage2が任意の
    タグ文字列を生成しても信用しない。根拠が無い(unknown)場合は空リスト。
    primary_ingredients項目自体が無い(Step47.1以前の)データはNoneを返す
    (呼び出し元が商品名からの決定論的補完を使う。Step47.2)。"""
    payload = stage2_payload or {}
    attrs = payload.get("category_attributes") or {}
    if "primary_ingredients" not in attrs:
        return None
    field = attrs.get("primary_ingredients")
    value = field.get("value") if isinstance(field, dict) else field
    if not value or str(value).strip().lower() == "unknown":
        return []
    active_names = {
        _ingredient_name_key(i.get("ingredient")): i.get("ingredient")
        for i in (payload.get("active_ingredients") or [])
        if isinstance(i, dict) and i.get("ingredient")
    }
    active_tags = set(app.compute_ingredient_tags(list(active_names.values())))
    tags = set()
    for raw in _PRIMARY_INGREDIENT_SEPARATORS_RE.split(str(value)):
        name = active_names.get(_ingredient_name_key(raw)) or _active_name_with_supplementary_note(raw, active_names)
        if not name:
            continue
        tag = app.normalize_ingredient_tag(name)
        if tag and tag in active_tags:
            tags.add(tag)
    return sorted(tags)


_TRAILING_NOTE_RE = re.compile(r"^(.*?\S)\s*[(（]([^()（）]*)[)）]\s*$")


def _active_name_with_supplementary_note(primary_name, active_names):
    """Step50.1: 主要成分名「プラズマ乳酸菌」と成分名「プラズマ乳酸菌（L. lactis strain Plasma）」の
    ように、成分名の末尾の括弧が補足表記である場合だけ一致とみなす。条件(すべて必須):
    括弧外が主要成分名と完全一致 / 括弧付きの成分名と括弧外が同じ成分タグに正規化される /
    括弧内が別の成分名として正規化されない / 該当する成分が1つだけ。括弧を一律には削除しない。"""
    key = _ingredient_name_key(primary_name)
    if not key:
        return None
    hits = []
    for full in active_names.values():
        match = _TRAILING_NOTE_RE.match(unicodedata.normalize("NFKC", str(full)))
        if not match or _ingredient_name_key(match.group(1)) != key:
            continue
        base_tag, full_tag = app.normalize_ingredient_tag(match.group(1)), app.normalize_ingredient_tag(full)
        inner_tag = app.normalize_ingredient_tag(match.group(2))
        if base_tag and base_tag == full_tag and inner_tag in (None, base_tag):
            hits.append(full)
    return hits[0] if len(hits) == 1 else None


PRIMARY_V2 = {"enabled": True}
_FUNCTIONAL_LINE_RE = re.compile(r"機能性関与成分")


def _name_ingredient_tags(product_name):
    tokens = [w for w in re.split(r"[\s　・･()（）\[\]【】/／+＋&＆、,，]+", unicodedata.normalize("NFKC", str(product_name or ""))) if w]
    return set(app.compute_ingredient_tags(tokens)) | set(app.supplement_primary_tags_from_name(product_name, tokens))


def functional_ingredient_tags(stage1_text):
    """公式情報(Stage1本文)で「機能性関与成分」と明記された行(とその直後2行)の成分タグ。"""
    lines = unicodedata.normalize("NFKC", str(stage1_text or "")).splitlines()
    tags = set()
    for i, line in enumerate(lines):
        if _FUNCTIONAL_LINE_RE.search(line):
            for x in lines[i:i + 3]:
                tags |= set(app.compute_ingredient_tags([w for w in re.split(r"[\s　・･:：、,()（）*]+", x) if len(w) >= 2]))
    return tags


def _primary_ingredients_unknown(stage2_payload):
    """Stage2のprimary_ingredients項目はあるが、値が根拠なし(unknown・空)か。"""
    attrs = (stage2_payload or {}).get("category_attributes") or {}
    if "primary_ingredients" not in attrs:
        return False
    field = attrs.get("primary_ingredients")
    value = field.get("value") if isinstance(field, dict) else field
    return not value or str(value).strip().lower() == "unknown"


def _sourced_active_ingredient_tags(stage2_payload):
    """出典URL付きの有効成分(Stage2の照合でStage1の実citationに存在した出典)の成分タグ。"""
    names = [i.get("ingredient") for i in ((stage2_payload or {}).get("active_ingredients") or [])
             if isinstance(i, dict) and i.get("ingredient")
             and str(i.get("source_url") or "").startswith("https://")
             and str(i.get("ingredient")).strip().lower() != "unknown"]
    return set(app.compute_ingredient_tags(names))


def supplement_primary_tags_for_payload(product_name, stage2_payload, stage1_text=None):
    """Stage2のprimary_ingredientsを第一優先とし、その項目が欠落している既存
    データだけapp.supplement_primary_tags_from_name()で補完する(Step47.2)。
    Step51: Stage2が主要成分に挙げた成分のうち、商品名に表れる成分か、公式情報で
    「機能性関与成分」と明記された成分だけを主要成分とする(副成分の混入を防ぐ)。"""
    tags = derive_primary_ingredient_tags(stage2_payload)
    if tags is not None:
        if PRIMARY_V2["enabled"]:
            allowed = _name_ingredient_tags(product_name) | functional_ingredient_tags(stage1_text)
            tags = [t for t in tags if t in allowed or (t == "probiotics" and allowed & {"lactobacillus", "bifida"})]
        if not tags and _primary_ingredients_unknown(stage2_payload):
            # Step56: 主要成分が根拠なし(unknown)の場合は、商品名と出典付きの有効成分の両方で確認できる成分だけで補う。
            tags = sorted(_name_ingredient_tags(product_name) & _sourced_active_ingredient_tags(stage2_payload))
        return tags
    names = [i.get("ingredient") for i in ((stage2_payload or {}).get("active_ingredients") or [])
             if isinstance(i, dict) and i.get("ingredient")]
    return app.supplement_primary_tags_from_name(product_name, names)


# ===== Step48.1: サプリの商品区分(医薬品・医薬部外品の除外) =====
# 許可区分は、Stage2が出典付きで返した値の区分名がStage1本文に否定形でなく
# 実在する場合だけ採用する(method_evidence_termsと同じ決定論的な照合)。
_PRODUCT_CLASS_EVIDENCE_TERMS = {
    "supplement": ("サプリメント", "栄養補助食品"),
    "health_food": ("健康食品",),
    "foods_with_function_claims": ("機能性表示食品",),
    "nutrient_function_food": ("栄養機能食品",),
}
_PRODUCT_CLASS_NEGATION_RE = re.compile(
    r"^\s*[」』\"”）)]{0,2}\s*(?:[（(][^）)]{0,20}[）)])?\s*[」』\"”）)]{0,2}\s*"
    r"(?:ではな|ではありません|でなく|でもな|とは異な|には該当しな|に該当しな|には当たらな|に当たらな)")


def _iter_affirmed_term_positions(text, term):
    """termが本文中に「〜ではなく」等の否定形でない形で現れる位置。"""
    start = 0
    while True:
        index = text.find(term, start)
        if index < 0:
            return
        if not _PRODUCT_CLASS_NEGATION_RE.match(text[index + len(term):index + len(term) + 40]):
            yield index
        start = index + len(term)


def _product_class_term_affirmed(text, term):
    return next(_iter_affirmed_term_positions(text, term), None) is not None


def supplement_product_classification(stage2_payload, stage1_text):
    """サプリ候補の商品区分(app.SUPPLEMENT_PRODUCT_CLASSESのいずれか)。

    1. Stage1本文に医薬品/医薬部外品の区分表示があれば、それを優先する(除外方向)。
    2. Stage2のcategory_attributes.product_classification(sanitize済み=出典照合済み)
       を使う。許可区分は区分名がStage1本文に否定形でなく実在する場合のみ採用し、
       それ以外・項目欠落(旧データ)・不正値はunknownとする。"""
    text = unicodedata.normalize("NFKC", str(stage1_text or ""))
    regulated = app.regulated_product_class_in_text(text)
    if regulated:
        return regulated
    attrs = (stage2_payload or {}).get("category_attributes") or {}
    field = attrs.get("product_classification") if isinstance(attrs, dict) else None
    value = str((field.get("value") if isinstance(field, dict) else field) or "").strip()
    if value in app.SUPPLEMENT_EXCLUDED_PRODUCT_CLASSES:
        return value
    terms = _PRODUCT_CLASS_EVIDENCE_TERMS.get(value)
    if not terms or not any(_product_class_term_affirmed(text, unicodedata.normalize("NFKC", t)) for t in terms):
        return "unknown"
    return value


def is_supplement_product_class_reflectable(product_class):
    return product_class in app.SUPPLEMENT_REFLECTABLE_PRODUCT_CLASSES


# ===== Step48.2: サプリとして扱える根拠(eligibility)。classificationとは分離 =====
# classification=unknownの場合だけ、Stage1本文に明示された摂取情報・食品表示から
# 判定する。商品形状(「粒」「カプセル」)単独・商品名・ブランドだけでは根拠にしない。
_INTAKE_UNIT = r"(?:粒|カプセル|錠|包|袋|本|スティック|杯|g|ｇ)"
_INTAKE_NUMBER = r"(?:\d+(?:\.\d+)?|[一二三四五六七八九十])(?:\s*[〜~～\-]\s*\d+)?"
_SUPPLEMENT_EVIDENCE_PATTERNS = (
    # 「1日の摂取目安量: 2粒」「1日の摂取目安量\n * 1日1粒」(見出し+値の書式も含む)
    ("daily_intake_guideline", re.compile(
        r"1日(?:の|当たりの|あたりの)?摂取目安量?[\s*:：・\-–—|]{0,20}(?:1日\s*)?"
        + _INTAKE_NUMBER + r"\s*" + _INTAKE_UNIT)),
    # 「1日2粒を目安に」「目安として1日2粒」
    ("daily_intake_guideline", re.compile(r"1日\s*" + _INTAKE_NUMBER + r"\s*" + _INTAKE_UNIT + r"\s*(?:を|程度を)?\s*目安")),
    ("daily_intake_guideline", re.compile(r"目安として\s*1日\s*" + _INTAKE_NUMBER + r"\s*" + _INTAKE_UNIT)),
    # 「お召し上がり方: 1日2粒を水などで」
    ("serving_directions", re.compile(
        r"(?:お召し上がり方|召し上がり方|摂取方法)[\s*:：・\-–—|]{0,20}[^\n]{0,40}?" + _INTAKE_NUMBER + r"\s*" + _INTAKE_UNIT)),
)
# 食品としての栄養成分表示(エネルギー+三大栄養素/食塩相当量)。摂取目安の記載と
# 組み合わさった場合だけ根拠にする。
_NUTRITION_LABEL_RE = re.compile(r"エネルギー[\s*:：]{0,5}\d+(?:\.\d+)?\s*kcal")
_NUTRITION_COMPONENT_RES = tuple(re.compile(t) for t in (r"たんぱく質|タンパク質|蛋白質", r"脂質", r"炭水化物", r"食塩相当量"))
_INTAKE_MENTION_RE = re.compile(r"摂取目安量|摂取目安")
_SUPPLEMENT_TERM_EVIDENCE = ("サプリメント", "栄養補助食品", "健康食品")
# 医薬品の用法表示に特有の語。区分表示が無くても、これらがある本文の摂取情報は
# サプリの根拠にしない(fail-closed。食品4区分が明示されている場合は対象外)。
_DRUG_CONTEXT_RE = re.compile(r"用法[・･]?用量|添付文書|効能[・･]?効果|副作用|登録販売者|服用後")


def _excerpt(text, match_start, match_end, width=20):
    return text[max(0, match_start - width):match_end + width].replace("\n", " ").strip()


def supplement_intake_evidence(stage1_text):
    """Stage1本文から、サプリとして扱える積極的な根拠を[{kind, excerpt}]で返す。"""
    text = unicodedata.normalize("NFKC", str(stage1_text or ""))
    evidence = []
    for kind, pattern in _SUPPLEMENT_EVIDENCE_PATTERNS:
        m = pattern.search(text)
        if m and not any(e["kind"] == kind for e in evidence):
            evidence.append({"kind": kind, "excerpt": _excerpt(text, m.start(), m.end())})
    for term in _SUPPLEMENT_TERM_EVIDENCE:
        term = unicodedata.normalize("NFKC", term)
        index = next(_iter_affirmed_term_positions(text, term), None)
        if index is not None:
            evidence.append({"kind": "explicit_supplement_term", "excerpt": _excerpt(text, index, index + len(term))})
            break
    label = _NUTRITION_LABEL_RE.search(text)
    if (label and sum(bool(r.search(text)) for r in _NUTRITION_COMPONENT_RES) >= 2
            and _INTAKE_MENTION_RE.search(text)):
        evidence.append({"kind": "nutrition_label_with_intake", "excerpt": _excerpt(text, label.start(), label.end())})
    return evidence


def supplement_eligibility(stage2_payload, stage1_text):
    """サプリProduct Masterへreflectしてよいか(Step48.2)。
    1. 医薬品/医薬部外品(本文の区分表示 or 出典照合済みStage2値)は常に不適格。
    2. 食品4区分のclassificationは適格。
    3. unknownは、医薬品の用法表示に特有の語が無く、Stage1本文に積極的な
       サプリ根拠(supplement_intake_evidence)がある場合だけ適格。"""
    product_class = supplement_product_classification(stage2_payload, stage1_text)
    result = {"product_classification": product_class, "eligible": False, "basis": None,
              "reason": None, "evidence": []}
    if product_class in app.SUPPLEMENT_EXCLUDED_PRODUCT_CLASSES:
        return dict(result, reason=product_class)
    if is_supplement_product_class_reflectable(product_class):
        return dict(result, eligible=True, basis="classification")
    text = unicodedata.normalize("NFKC", str(stage1_text or ""))
    drug_context = _DRUG_CONTEXT_RE.search(text)
    if drug_context:
        return dict(result, reason="drug_usage_context",
                    evidence=[{"kind": "drug_usage_context",
                               "excerpt": _excerpt(text, drug_context.start(), drug_context.end())}])
    evidence = supplement_intake_evidence(text)
    if not evidence:
        return dict(result, reason="no_supplement_evidence")
    return dict(result, eligible=True, basis="stage1_evidence", evidence=evidence)


def flatten_category_attributes(category_attributes_payload):
    """sanitize済みのcategory_attributes({field: {value,confidence,
    source_url}})を、product_master反映・relevance判定用の単純な
    {field: value}形式へ変換する(confidence/source_urlは根拠追跡用の
    一時情報であり、active_ingredients/formulation_featuresのconfidence/
    source_urlがproduct_masterへ反映時に保持されないのと同じ扱いにする)。
    value="unknown"(根拠なし)のフィールドは、確認できなかったことを示す
    だけで中身が無いため、反映後のdictには含めない。
    """
    if not isinstance(category_attributes_payload, dict):
        return {}
    flattened = {}
    for field_name, field_value in category_attributes_payload.items():
        if isinstance(field_value, dict):
            value = str(field_value.get("value", "") or "").strip()
        else:
            value = str(field_value or "").strip()
        if value and value.lower() != "unknown":
            flattened[field_name] = value
    return flattened


# ===== Candidate Discovery(Step41): coverage_gapに対する商品候補の自動探索 =====
# Stage1/2と全く同じ安全パターン(Google Search Grounding→citation検証付き
# 構造化抽出)を、"既知の1商品を検証する"用途ではなく"条件を満たす候補を
# 複数見つける"用途に適用しただけで、新しいAPI・新しい信頼モデルは導入
# していない。ここで見つかった候補はProduct Masterへの正式採用根拠には
# ならず、既存のStage1→Stage2→citation/validator経路で改めて個別に検証
# されるまでは「調査対象の提案」に過ぎない(合意事項)。

def build_discovery_prompt(category, target):
    """外部Discovery(検索)のプロンプト。category_collection_config(category)
    がNoneのカテゴリ(化粧品)は従来の文面のまま。美容機器/サプリメントは、
    内部target(RF/vitamin_c等)をそのまま入れず、推薦定義から作った検索概念
    (discovery_search_concept)と確認項目を同じ共通構造に差し込む(Step45.1)。"""
    config = category_collection_config(category)
    if config is not None:
        return _build_category_discovery_prompt(category, target, config)
    return f"""あなたは化粧品・スキンケア・美容関連製品の情報調査アシスタントです。
以下の条件を満たす、日本国内で実際に販売されている市販品を、最大3件まで
Web検索で調査してください。

カテゴリ: {category}
条件: 「{target}」に該当する成分・特徴を配合していることが、メーカー公式
サイト等の情報で確認できる商品。

必ず検索を実行し、その結果に基づいて回答してください。検索せずに一般的な
知識だけで回答することは禁止します。

厳守事項:
- 実際に検索して見つかった、実在する商品のみを挙げること。架空の商品名・
  存在が確認できない商品名を作ってはならない。
- 各商品について、ブランド名・正式な製品名・該当成分/条件が確認できた
  根拠の要約を記述すること。
- 条件を満たす商品が1件も見つからない場合は、無理に挙げず「見つかりません
  でした」と回答すること。
- 楽天市場等の販売実績だけでは商品の成分・条件適合の根拠にはしないこと
  (販売の有無と、条件(成分等)の確認は別の情報源で行うこと)。
{DISCOVERY_SEARCH_REQUIREMENT}"""


def run_discovery_search(category, target, batch_id):
    """Stage1と同じ安全パターン(Google Search Grounding、検索証拠が無い
    場合はno_search_evidenceで失敗扱い)で、候補となる実在商品をWeb検索する。
    """
    if category_collection_config(category) is not None and discovery_search_concept(category, target) is None:
        # 推薦定義に無いtargetは検索概念を作れないため、内部IDのまま検索しない。
        print(f"[DISCOVERY UNKNOWN TARGET] {category}:{target}", flush=True)
        return {"status": "unknown_target_concept", "raw_text": "", "citations": [], "search_queries": []}
    prompt = build_discovery_prompt(category, target)
    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
    )
    label = f"discovery:{category}:{target}"

    try:
        response = call_gemini_for_collection(
            STAGE1_MODEL, prompt, config=config, max_retries=1, timeout=60,
        )
    except Exception as e:
        print(f"[DISCOVERY SEARCH ERROR] {label}: {repr(e)}", flush=True)
        return {"status": "error", "error": repr(e), "raw_text": "", "citations": [], "search_queries": []}

    grounding = poc.extract_grounding_summary(response)
    usage = poc.extract_usage_summary(response)
    usage_result = record_usage_and_check_limit(
        batch_id, label, "discovery_search", STAGE1_MODEL, usage,
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
        print(f"[DISCOVERY NO SEARCH EVIDENCE] {label}: 失敗扱い", flush=True)
    return result


def build_discovery_structuring_prompt(category, target, discovery_text, citations):
    citation_lines = "\n".join(f"- {c['uri']} ({c.get('title', '')})" for c in citations) or "(なし)"
    return f"""以下はステージ1で実際にWeb検索を行って得られた、商品候補の
調査結果です。この内容に書かれている情報のみを使って、指定のJSON形式へ
構造化してください。

カテゴリ: {category}
条件: {discovery_condition_label(category, target)}

【調査結果(検索に基づく)】
{discovery_text}

【実際に確認された出典URL一覧(これ以外のURLは一切使用禁止)】
{citation_lines}

厳守事項:
- 上記の調査結果に明記されていない商品・成分情報を推測・補完することは
  絶対禁止。見つからなかった場合はcandidates=[](空配列)とする。
- 各候補のsource_urlには、上記の出典URL一覧に実在するURLをそのまま使用
  すること。一覧に無いURLを生成することは絶対禁止。"""


def build_discovery_response_schema():
    candidate_schema = types.Schema(
        type="OBJECT",
        properties={
            "brand": types.Schema(type="STRING"),
            "product_name": types.Schema(type="STRING"),
            "target_evidence": types.Schema(
                type="STRING",
                description="該当成分/条件が確認できた根拠の要約(不明な場合は'unknown')",
            ),
            "source_url": types.Schema(type="STRING"),
        },
        required=["brand", "product_name", "target_evidence", "source_url"],
    )
    return types.Schema(
        type="OBJECT",
        properties={"candidates": types.Schema(type="ARRAY", items=candidate_schema)},
        required=["candidates"],
    )


def run_discovery_structuring(category, target, discovery_result, batch_id):
    """Stage2と同じ安全パターンで、discovery検索結果を候補リストへ構造化
    する。citationに存在しないsource_urlの候補は丸ごと破棄する
    (sanitize_stage2_payloadと同じ考え方)。"""
    label = f"discovery:{category}:{target}"
    if discovery_result.get("status") != "ok":
        return {"status": "skipped", "reason": discovery_result.get("status"), "payload": None}

    prompt = build_discovery_structuring_prompt(
        category, target, discovery_result["raw_text"], discovery_result["citations"],
    )
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=build_discovery_response_schema(),
    )

    try:
        response = call_gemini_for_collection(
            STAGE2_MODEL, prompt, config=config, max_retries=1, timeout=60,
        )
    except Exception as e:
        print(f"[DISCOVERY STRUCTURING ERROR] {label}: {repr(e)}", flush=True)
        return {"status": "error", "error": repr(e), "payload": None}

    usage = poc.extract_usage_summary(response)
    usage_result = record_usage_and_check_limit(
        batch_id, label, "discovery_structuring", STAGE2_MODEL, usage, grounding_used=False,
    )

    try:
        parsed = json.loads(response.text)
    except Exception as e:
        print(f"[DISCOVERY STRUCTURING PARSE ERROR] {label}: {repr(e)}", flush=True)
        return {"status": "error", "error": repr(e), "payload": None, "usage_result": usage_result}

    valid_urls = {c["uri"] for c in discovery_result["citations"]}
    raw_candidates = parsed.get("candidates") or []
    candidates = []
    filtered = {}
    for c in raw_candidates:
        reason = None
        if not isinstance(c, dict):
            reason = "not_object"
        elif not str(c.get("brand", "") or "").strip():
            reason = "missing_brand"
        elif not str(c.get("product_name", "") or "").strip():
            reason = "missing_product_name"
        elif not str(c.get("source_url", "") or "").strip() or str(c.get("source_url")).strip().lower() == "unknown":
            reason = "missing_source_url"
        else:
            canonical = match_citation_url(c.get("source_url"), valid_urls)
            if canonical is None:
                reason = "source_url_mismatch"
            else:
                candidates.append(c if canonical == c.get("source_url") else dict(c, source_url=canonical))
        if reason:
            filtered[reason] = filtered.get(reason, 0) + 1
    return {
        "status": "ok", "payload": {"candidates": candidates}, "usage_result": usage_result,
        "structured_count": len(raw_candidates), "filtered": filtered,
    }


# Step48.5: Discovery段階の医薬品・医薬部外品の早期除外(negative gate専用)。
_DISCOVERY_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。!?！？])|\n")


def _compact_text(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or "")))


def discovery_regulated_product_class(category, candidate, discovery_text):
    """サプリのDiscovery候補について、その候補自身の根拠(target_evidenceと、
    Discovery検索本文のうち候補の商品名を含む文)に医薬品/医薬部外品の区分表示が
    あれば"drug"/"quasi_drug"を返す。それ以外・サプリ以外はNone。
    除外方向専用で、Noneでも区分(サプリ等)を確定したことにはならない。"""
    if category != "サプリメント" or not isinstance(candidate, dict):
        return None
    evidence = [str(candidate.get("target_evidence") or "")]
    name_key = _compact_text(candidate.get("product_name"))
    if name_key:
        evidence.extend(s for s in _DISCOVERY_SENTENCE_SPLIT_RE.split(str(discovery_text or ""))
                        if s and name_key in _compact_text(s))
    return app.regulated_product_class_in_text("\n".join(evidence))


def discover_candidates_via_gemini(category, target, batch_id, max_candidates=3, diagnostics=None):
    """Gemini Groundingによる候補探索の一括呼び出し(search→structuring)。
    戻り値はcitation検証済みの候補リストで、各要素はdiscovery evidence
    (source_url)とtarget_evidenceを持つ。ここで見つかった情報はProduct
    Masterへの正式採用根拠にはしない(呼び出し元が既存のStage1→Stage2→
    citation/validator経路へ個別に回して初めて正式採用を判断する)。

    diagnostics(Step45.1): 渡されたdictへ、候補0件の原因を区別できる要約
    (status/検索クエリ数/citation数/本文長と短いハッシュ/構造化件数/除外理由
    ごとの件数)を書き込み、ログにも出す。検索本文・生レスポンスは保存しない。
    """
    diag = diagnostics if diagnostics is not None else {}
    diag.update({"status": None, "search_query_count": 0, "citation_count": 0, "text_length": 0,
                 "text_sha256": "", "structured_count": 0, "filtered": {}, "returned": 0, "truncated": 0})
    label = f"discovery:{category}:{target}"
    try:
        discovery_result = run_discovery_search(category, target, batch_id)
        raw_text = discovery_result.get("raw_text") or ""
        diag.update({
            "search_query_count": len(discovery_result.get("search_queries") or []),
            "citation_count": len(discovery_result.get("citations") or []),
            "text_length": len(raw_text),
            "text_sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:12] if raw_text else "",
        })
        if discovery_result.get("status") != "ok":
            diag["status"] = discovery_result.get("status") or "search_failed"
            return []

        structuring_result = run_discovery_structuring(category, target, discovery_result, batch_id)
        if structuring_result.get("status") != "ok":
            diag["status"] = f"structuring_{structuring_result.get('status') or 'failed'}"
            return []

        candidates = (structuring_result.get("payload") or {}).get("candidates") or []
        diag["structured_count"] = structuring_result.get("structured_count", len(candidates))
        diag["filtered"] = dict(structuring_result.get("filtered") or {})
        if diag["structured_count"] == 0:
            diag["status"] = "structured_empty"
        elif not candidates:
            diag["status"] = "all_filtered"
        else:
            diag["status"] = "ok"
        # Step48.5: 医薬品等と判明した候補は印を付けて返し(呼び出し元が除外を記録)、
        # 上限max_candidatesは除外対象以外の候補にだけ適用する。
        returned, regulated, normal_count = [], 0, 0
        for c in candidates:
            regulated_class = discovery_regulated_product_class(category, c, raw_text)
            if regulated_class:
                regulated += 1
                returned.append(dict(c, discovery_regulated_product_class=regulated_class))
            elif normal_count < max_candidates:
                normal_count += 1
                returned.append(c)
        diag["returned"] = normal_count
        diag["truncated"] = max(0, len(candidates) - regulated - max_candidates)
        if regulated:
            diag["regulated_excluded"] = regulated
        return returned
    finally:
        print(f"[DISCOVERY DIAGNOSTICS] {label} {json.dumps(diag, ensure_ascii=False)}", flush=True)


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

def collect_one_product(brand, product_name, category, batch_id, existing_product=None, existing_jans=None):
    """1商品分のStage1→Stage2→conflict検出→staging保存を一貫して行う。
    費用上限(PRODUCT_COLLECTION_COST_LIMIT_USD)を超えた場合は、この商品の
    処理を完了させた上で呼び出し元(collect_batch)に伝え、バッチを停止する。
    existing_jans(Step49.1): {JAN: product_id}。Stage1本文に既存Product Masterの
    JANがあれば、公式ページ確認・Stage2を行わずduplicate_janとして返す。"""
    stage1_result = run_stage1_collection(brand, product_name, batch_id, category=category)
    duplicate = None
    if existing_jans and stage1_result.get("status") == "ok":
        matched = sorted(extract_jan_codes(stage1_result.get("raw_text")) & set(existing_jans))
        if matched:
            duplicate = {"jan": matched[0], "product_id": existing_jans[matched[0]]}
    if duplicate:
        stage2_result = {"status": "skipped", "reason": "duplicate_jan_in_product_master", "payload": None}
        staging_id = write_staging_record(batch_id, brand, product_name, category, stage1_result, stage2_result,
                                          {"status": "skipped", "conflicts": []})
        return {"staging_id": staging_id, "stage1_status": stage1_result.get("status"), "stage2_status": "skipped",
                "conflict_status": "skipped", "duplicate_jan": duplicate,
                "limit_exceeded": bool((stage1_result.get("usage_result") or {}).get("limit_exceeded"))}
    # Step48.1: 本文に医薬品/医薬部外品の区分表示があるサプリ候補は、reflect不可が
    # 確定しているため公式ページ確認のHTTPを行わない。
    regulated_supplement = category == "サプリメント" and bool(
        app.regulated_product_class_in_text(stage1_result.get("raw_text")))
    if stage1_result.get("status") == "ok" and not regulated_supplement:
        # Step45.5: Stage1直後に、title判定で公式確認できない場合のみcitation先
        # ページのサイトの名乗りを確認し、結果をcitationへ付けて保存する。
        stage1_result = dict(stage1_result, citations=attach_citation_page_verification(
            brand, stage1_result.get("citations") or [],
        ))

    stage2_result = run_stage2_structuring(brand, product_name, category, stage1_result, batch_id)

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
        stage2_result = run_stage2_structuring(brand, product_name, category, stage1_result, batch_id)
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
            "SELECT brand, product_name, category, stage2_status, stage2_payload, conflict_status, reflected_at, "
            "stage1_raw_text FROM product_collection_staging WHERE staging_id = %s",
            (staging_id,),
        )
        row = cur.fetchone()
        if not row:
            return {"status": "not_found"}
        (brand, product_name, category, stage2_status, stage2_payload, conflict_status, reflected_at,
         stage1_raw_text) = row

        if stage2_status != "ok":
            return {"status": "skipped", "reason": f"stage2_status={stage2_status}"}
        if conflict_status == "needs_review":
            return {"status": "skipped", "reason": "needs_review"}
        if reflected_at is not None:
            return {"status": "already_reflected"}

        payload = stage2_payload or {}
        product_class = eligibility = None
        if category == "サプリメント":
            # Step48.1: 医薬品・医薬部外品・区分不明はサプリProduct Masterへ入れない。
            eligibility = supplement_eligibility(payload, stage1_raw_text)
            product_class = eligibility["product_classification"]
            if not eligibility["eligible"]:
                return {"status": "skipped", "reason": "supplement_not_eligible",
                        "product_classification": product_class, "eligibility_reason": eligibility["reason"]}
        # "unknown"プレースホルダー項目は反映時にも除外する(合意事項②の
        # 後方互換: この修正より前に収集済みのstaging行にまだ残っている
        # 場合があるため、再API実行なしでも安全に除外する)。
        real_ingredients = [
            i for i in (payload.get("active_ingredients") or [])
            if str(i.get("ingredient", "") or "").strip().lower() != "unknown"
        ]
        active_ingredients = [i.get("ingredient") for i in real_ingredients]
        # Step49.4: 表示用の商品名からは型番の説明注記(「（型番：… 等）」)を除く
        # (確定した型番1件は商品名に残す。注記が無い商品名は変わらない)。
        display_name = split_discovery_product_name(product_name)[0] or product_name
        product_for_master = {
            "brand": brand,
            "name": display_name,
            "category": category,
            "active_ingredients": active_ingredients,
            # 既存診断ロジック(normalize_ingredient_tag)が比較に使う統制タグ。
            # 別の正規化ロジックは作らず、app.compute_ingredient_tags()
            # (=normalize_ingredient_tag()の集合)をそのまま再利用する。
            # upsert_product_master()側でも同じ関数から再計算されるため、
            # ここでの値は主にdry-runプレビューの可視化用。
            "active_ingredient_tags": app.compute_ingredient_tags(active_ingredients),
            "ingredient_strength": {
                i.get("ingredient"): i.get("concentration")
                for i in real_ingredients
                if i.get("concentration") and i.get("concentration") != "unknown"
            },
            "formulation": [f.get("feature") for f in (payload.get("formulation_features") or [])],
            "verified_at": time.time(),
        }
        # JANはcitation確認済み(sanitize_stage2_payloadがStage1実citationに
        # 文字列として存在する場合のみ採用、それ以外は"unknown"に強制済み)の
        # 値のみをproduct_masterへ反映する。未確認("unknown"/空)はNoneのまま
        # (upsert_product_master側でNULLになる)にして無理に埋めない。
        jan_code = str(payload.get("jan_code", "") or "").strip()
        if jan_code and jan_code.lower() != "unknown":
            product_for_master["jan_code"] = jan_code

        # Step43: category_attributes(美容機器のmethod等)をproduct_masterへ
        # 反映する。confidence/source_url等の根拠追跡用メタ情報は、
        # active_ingredients/formulation_featuresがconcentration/feature名
        # だけを残すのと同じ扱いで保持しない(flatten_category_attributes)。
        # value="unknown"(根拠なし)のフィールドは反映しない(キー自体を
        # 持たせないことで、validate_category_attributes()が「未確認」を
        # 「欠落」として正しく検出できるようにする)。
        category_attributes = flatten_category_attributes(payload.get("category_attributes"))
        if category == "サプリメント":
            # Step47.1: 主要成分タグ(coverage・診断target関連性の根拠)。
            category_attributes["primary_ingredient_tags"] = supplement_primary_tags_for_payload(product_name, payload,
                                                                                                stage1_raw_text)
            category_attributes["product_classification"] = product_class
            # Step48.2: 適格と判定した根拠(classification / stage1_evidence)。
            category_attributes["supplement_eligibility_basis"] = eligibility["basis"]
        if category_attributes:
            product_for_master["category_attributes"] = category_attributes

        identity_key = app._normalize_product_master_identity_key(brand, display_name, category)
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


# ===== Step 3: item_code取得(設計のみだった前段からの実装) =====
# 楽天実APIを呼ぶのは app.fetch_rakuten_candidates()/app.fetch_rakuten_item_by_item_code()
# の内部のみ(いずれも既存・実運用済みの関数をそのまま再利用し、新しい照合
# ロジックは作らない)。本モジュールのここから下の関数は、呼び出し元が
# 明示的に実行するまでは一切呼ばれない(このファイルのimport自体は実APIに
# 到達しない)。


# ===== Step45.7: item_code保存時の美容機器identity照合 =====
# 美容機器は診断時の商品名一致がブランド一致のみで通るため、product_masterへ
# item_codeを永続保存する用途に限り、商品名の型番(最優先)またはシリーズ/
# モデル名の識別部分が楽天タイトルにあることを必須にする。類似度判定はせず、
# 判定できない場合は一致としない(=保存しない)。
# 正規化はNFKC・小文字化・ハイフン類の除去(EH-SR85=EHSR85)と、記号の空白化のみ。
_MODEL_HYPHENS_RE = re.compile(r"[\-‐‑‒–—―_]")
_MODEL_SEPARATORS_RE = re.compile(r"[^0-9a-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff66-\uff9fー]+")
# 型番は「英字+数字」の本体で比較する。末尾2文字までの英字(EH-SR85-Kの
# -K等の色記号)は同一モデルの表記として本体に含めない。
_MODEL_NUMBER_RE = re.compile(r"(?<![a-z0-9])([a-z]{1,6}\d{2,6})[a-z]{0,2}(?![a-z0-9])")
# 型番の後ろに続いてもよい語(商品の識別を変えない一般語)。これ以外の語が
# 続く場合(例: 「フォトプラス EX」の後に「スムース」)は別モデルの可能性が
# あるため一致としない。
_DEVICE_TRAILING_GENERIC_SEGMENTS = {
    "美顔器", "美容器", "美容機器", "家庭用", "rf", "led", "ems", "ラジオ波", "正規品", "国内正規品",
    "公式", "送料無料", "日本製", "最新", "新品", "新製品", "フェイスケア",
}


def _model_text(text):
    folded = unicodedata.normalize("NFKC", str(text or "")).lower()
    return _MODEL_SEPARATORS_RE.sub(" ", _MODEL_HYPHENS_RE.sub("", folded)).strip()


def _script_segments(text):
    """空白区切りの各語を、さらに文字種の変わり目で分割する。"""
    segments = []
    for token in text.split():
        current = token[0]
        for ch in token[1:]:
            if _char_class(ch) == _char_class(current[-1]) or ch == "ー":
                current += ch
            else:
                segments.append(current)
                current = ch
        segments.append(current)
    return segments


def _model_numbers(text):
    return set(_MODEL_NUMBER_RE.findall(_model_text(text)))


def device_identity_matches(product_name, rakuten_title, brand=""):
    """美容機器のproduct_master商品名と楽天タイトルが同一モデルかを決定論的に判定する。
    戻り値: (matched: bool, reason)。"""
    product_models = _model_numbers(product_name)
    title_models = _model_numbers(rakuten_title)
    if product_models:
        if not product_models <= title_models:
            return False, "model_number_mismatch"
        prefixes = {re.match(r"[a-z]+", m).group(0) for m in product_models}
        conflicting = {m for m in title_models - product_models if re.match(r"[a-z]+", m).group(0) in prefixes}
        if conflicting:
            return False, "conflicting_model_number"
        return True, "model_number_match"

    brand_segments = set(_script_segments(_model_text(brand))) if brand else set()
    identity = [seg for seg in _script_segments(_model_text(product_name)) if seg not in brand_segments]
    while identity and identity[-1] in _DEVICE_TRAILING_GENERIC_SEGMENTS:
        identity.pop()
    while identity and identity[0] in _DEVICE_TRAILING_GENERIC_SEGMENTS:
        identity.pop(0)
    if not identity:
        return False, "identity_undeterminable"
    title_segments = _script_segments(_model_text(rakuten_title))
    n = len(identity)
    for i in range(len(title_segments) - n + 1):
        if title_segments[i:i + n] != identity:
            continue
        following = title_segments[i + n] if i + n < len(title_segments) else None
        if following is None or following in _DEVICE_TRAILING_GENERIC_SEGMENTS or following in brand_segments:
            return True, "model_name_match"
    return False, "model_name_mismatch"


# ===== Step45.17: 美容機器の型番ベース同一性(重複防止) =====
# ブランド表記だけが異なる同一美容機器(例: 「Panasonic」と「パナソニック」の
# 「ソニック RF リフト EH-SR75」)は既存identity_key(ブランド+商品名)では別扱い
# になり、二重収集・二重reflectされる。ブランドの翻訳・別名対応表は使わず、
# 商品名の型番(Step45.7の_model_numbers()と同じ正規化)で同一性を判定する。
# 型番の完全一致だけでは同一とせず、device_identity_matches()(双方向)と、
# 型番・ブランド・一般語を除いたシリーズ名の識別語に矛盾が無いことも必須。
# 型番が無い商品名は判定しない(従来のidentity_keyへfallback)。


def device_model_key(name):
    """商品名の正規化型番集合(空なら型番なし)。"""
    return frozenset(_model_numbers(name))


def _device_series_segments(name, brand):
    brand_segments = set(_script_segments(_model_text(brand))) if brand else set()
    return {
        seg for seg in _script_segments(_model_text(name))
        if seg not in brand_segments and seg not in _DEVICE_TRAILING_GENERIC_SEGMENTS
        and not _MODEL_NUMBER_RE.fullmatch(seg)
    }


def same_device_by_model(brand_a, name_a, brand_b, name_b):
    """2つの美容機器の商品名が型番ベースで同一商品か。戻り値: (same, reason)。"""
    models_a, models_b = device_model_key(name_a), device_model_key(name_b)
    if not models_a or not models_b:
        return False, "no_model_number"
    if models_a != models_b:
        return False, "model_number_differs"
    if not (device_identity_matches(name_a, name_b, brand_a)[0] and device_identity_matches(name_b, name_a, brand_b)[0]):
        return False, "model_identity_conflict"
    series_a, series_b = _device_series_segments(name_a, brand_a), _device_series_segments(name_b, brand_b)
    if series_a and series_b and not (series_a <= series_b or series_b <= series_a):
        return False, "series_conflict"
    return True, "same_model_number"


def find_device_duplicate(category, brand, name, entries):
    """category=美容機器のとき、entries([{"brand","name",...}])の中から型番ベースで
    同一と判定されるものを返す(無ければNone)。他カテゴリは常にNone。"""
    if category != "美容機器" or not device_model_key(name):
        return None
    for entry in entries or []:
        if same_device_by_model(brand, name, entry.get("brand", ""), entry.get("name", ""))[0]:
            return entry
    return None


def normalize_jan_code(jan_code):
    """検証済みJAN(8桁/13桁の数字)を正規化して返す。それ以外(unknown・空・
    桁数違い)は空文字。"""
    value = re.sub(r"[\s\-]", "", unicodedata.normalize("NFKC", str(jan_code or "")))
    return value if re.fullmatch(r"\d{8}|\d{13}", value) else ""


def _valid_jan_check_digit(code):
    digits = [int(c) for c in code]
    body, check = digits[:-1], digits[-1]
    weights = [3 if (len(body) - i) % 2 == 1 else 1 for i in range(len(body))]
    return (10 - sum(d * w for d, w in zip(body, weights)) % 10) % 10 == check


def extract_jan_codes(text):
    """Step49.1: 本文中のJAN(13桁/8桁、前後が数字でない、チェックデジット正当)の集合。"""
    folded = unicodedata.normalize("NFKC", str(text or ""))
    return {m for m in re.findall(r"(?<!\d)(\d{13}|\d{8})(?!\d)", folded) if _valid_jan_check_digit(m)}


def brand_stripped_product_name(brand, name):
    """Step49.1: 商品名の先頭にブランド名(official_brand_forms()の表記)が重複して
    付いている場合だけ、それを除いた商品名を返す(該当しなければNone)。
    「ネイチャーメイド スーパーフィッシュオイル」と「スーパーフィッシュオイル」を
    同じ商品名として扱うための重複判定専用。除去後が短すぎる場合はNone。"""
    folded = unicodedata.normalize("NFKC", str(name or "")).strip()
    for form in sorted(official_brand_forms(brand), key=len, reverse=True):
        match = re.match(rf"^{re.escape(form)}[\s・･:：/|｜\-]*", folded, flags=re.IGNORECASE)
        if match and match.end() < len(folded):
            rest = folded[match.end():].strip()
            if len(_normalize_brand_text(rest)) >= _MIN_BRAND_MATCH_LENGTH:
                return rest
    return None


# Step49.1: Discoveryの商品名に付く「（型番：RR-AT-02A 等）」のような説明注記。
_MODEL_ANNOTATION_RE = re.compile(r"\s*[（(]\s*型番\s*[:：]?\s*([^（）()]*)[）)]")
_MODEL_UNCERTAIN_RE = re.compile(r"等|など|ほか|他")


def split_discovery_product_name(name):
    """Discoveryの商品名から型番の説明注記を分離する。戻り値(商品名, 型番ヒント)。
    型番が1つで確定的なら商品名に型番だけを残す(「メディリフト プラス EPM-18BB」)。
    「等」「など」付き・複数の場合は商品名から外し、ヒント(重複判定用)にだけ残す。"""
    text = str(name or "")
    match = _MODEL_ANNOTATION_RE.search(text)
    if not match:
        return text.strip(), []
    inner = match.group(1)
    models = [m for m in re.split(r"[、,，/／・\s]+", _MODEL_UNCERTAIN_RE.sub(" ", inner)) if re.search(r"\d", m)]
    uncertain = bool(_MODEL_UNCERTAIN_RE.search(inner)) or len(models) != 1
    replacement = "" if uncertain or not models else f" {models[0]}"
    cleaned = re.sub(r"\s+", " ", (text[:match.start()] + replacement + text[match.end():])).strip()
    return cleaned, models


def rakuten_item_mentions_jan(item, jan):
    """楽天候補のタイトル/商品説明にJANが(前後を数字に挟まれずに)記載されているか。"""
    text = unicodedata.normalize("NFKC", f"{item.get('itemName', '') or ''} {item.get('itemCaption', '') or ''}")
    return bool(jan) and re.search(rf"(?<!\d){re.escape(jan)}(?!\d)", text) is not None


# Step49.6: Product Master収集経路(このresolver)だけで使う、検証済みJANがある場合の
# 語順不問の名称照合。「亜鉛（小林製薬の栄養補助食品）」と「小林製薬の栄養補助食品 亜鉛」の
# ような語順違いを、JANが楽天候補に記載されている場合に限って同一商品名とみなす。
# 診断時の照合(app.is_same_verified_rakuten_product)は変更しない。
_NAME_PART_SEPARATORS_RE = re.compile(r"[\s　・･\-_()（）\[\]［］【】「」『』/／|｜,，、]+")
_NAME_SPEC_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:粒|錠|カプセル|包|袋|本|枚|日分|mg|g|ml|μg|mcg|iu)")


def _compact_for_match(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or "")).lower())


def jan_verified_name_match(product_name, brand, item, jan):
    """次をすべて満たす場合だけTrue:
    - 検証済みJAN(jan)が楽天候補のタイトル/商品説明に記載されている
    - 商品名を区切り記号・括弧で分けた各部分(2文字以上)がすべてタイトルにある(語順は問わない)
    - ブランドの明示的な表記(括弧内外、app.explicit_brand_forms)のいずれかがタイトルにある
    - 商品名に規格(粒数・日分・容量等)があれば、同じ規格がタイトルにある
    販売形態・セット・JAN一致の判定はこの後の既存の段階でそのまま行う。"""
    if not jan or not rakuten_item_mentions_jan(item, jan):
        return False
    title = _compact_for_match(item.get("itemName", ""))
    if not title:
        return False
    folded_name = unicodedata.normalize("NFKC", str(product_name or "")).lower()
    parts = [p for p in _NAME_PART_SEPARATORS_RE.split(folded_name) if len(p) >= 2]
    if not parts or not all(_compact_for_match(p) in title for p in parts):
        return False
    forms = [_compact_for_match(f) for f in app.explicit_brand_forms(brand)]
    if forms and not any(f in title for f in forms):
        return False
    specs = [_compact_for_match(m) for m in _NAME_SPEC_RE.findall(folded_name)]
    return all(spec in title for spec in specs)


_SPEC_PARTS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(粒|錠|カプセル|包|袋|本|枚|日分|mg|g|ml|μg|mcg|iu)")


# Step52: 内容量(粒数・日分・包数・g・ml)は同じ商品の容量違いとして扱い、規格矛盾に数えない。
# 1粒あたりの含有量(mg・μg等)が違う場合は処方違いとして引き続き矛盾とする。
_STRENGTH_UNITS = ("mg", "μg", "mcg", "iu")
_PACKAGE_SIZE_RE = re.compile(r"(?:約|または|又は)?\s*\d+(?:[.,]\d+)?\s*(?:日分|粒|錠|カプセル|包|袋|本|枚)(?:入り?)?")


def name_title_spec_conflict(product_name, title):
    """Step49.6: 商品名に規格(例: 120粒・60日分)があり、楽天タイトルに同じ単位の
    別の値だけがある場合をTrue(明確な規格矛盾)とする。タイトルに同じ単位の
    記載が無い場合は矛盾としない。Step52: 内容量の単位は比較しない(含有量の単位だけ比較)。"""
    name_specs = {}
    for value, unit in _SPEC_PARTS_RE.findall(unicodedata.normalize("NFKC", str(product_name or "")).lower()):
        if PM_SELECTION_FLAGS["accept_same_product_sizes"] and unit not in _STRENGTH_UNITS:
            continue
        name_specs.setdefault(unit, set()).add(float(value))
    if not name_specs:
        return False
    title_specs = {}
    for value, unit in _SPEC_PARTS_RE.findall(unicodedata.normalize("NFKC", str(title or "")).lower()):
        title_specs.setdefault(unit, set()).add(float(value))
    return any(unit in title_specs and not (values & title_specs[unit]) for unit, values in name_specs.items())


# ===== Step50.1: JANの記載が無い楽天出品の照合(Product Master収集経路のみ) =====
# JANが一致する出品があればそれを使い(従来どおり)、明記されたJANが違う出品は不合格。
# JANの記載が無い出品は、ブランド・正式商品名・公式情報の規格(このJANの規格)・
# 余分な成分名や別シリーズ語がないことで照合する。情報が足りない・矛盾する場合は保留。
_NO_JAN_SPEC_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(日分|粒|錠|カプセル|包|袋|本|枚|mg|g|ml)")
# 商品シリーズの修飾語。カナ表記と英字表記は同じ修飾語として扱う(一般語の固定一覧で、ブランドの対応表ではない)。
_SERIES_WORD_GROUPS = (("プレミアム", "premium"), ("ゴールド", "gold"), ("プラス", "plus"), ("ダブル", "double"),
                       ("ネオ", "neo"), ("プロ", "pro", "プロフェッショナル", "professional"), ("ライト", "lite"), ("スーパー", "super"),
                       ("アドバンス", "advance"), ("ex",), ("dx",), ("極",))
_SERIES_WORDS = tuple(w for g in _SERIES_WORD_GROUPS for w in g)


def series_word_conflict(product_name, title):
    """タイトルに、商品名に無いシリーズ修飾語(同じ修飾語のカナ/英字表記を含めて商品名に無いもの)があればその語。"""
    folded_name = unicodedata.normalize("NFKC", str(product_name or "")).lower()
    spaced_name = " " + re.sub(r"[^0-9a-zぁ-んァ-ヶー一-龥]+", " ", folded_name) + " "
    folded_title = unicodedata.normalize("NFKC", str(title or "")).lower()
    spaced_title = " " + re.sub(r"[^0-9a-zぁ-んァ-ヶー一-龥]+", " ", folded_title) + " "
    def has(word, spaced, compact):
        return (f" {word} " in spaced) if word.isascii() else (word in compact)
    for group in _SERIES_WORD_GROUPS:
        hit = next((w for w in group if has(w, spaced_title, re.sub(r"\s+", "", folded_title))), None)
        if hit and not any(has(w, spaced_name, re.sub(r"\s+", "", folded_name)) for w in group):
            return hit
    return None


def _spec_tokens(text):
    out = set()
    for value, unit in _NO_JAN_SPEC_RE.findall(unicodedata.normalize("NFKC", str(text or "")).lower()):
        number = float(value.replace(",", ""))
        if unit == "日分":
            out.add(("days", number))
        elif unit in ("粒", "錠", "カプセル"):
            out.add(("count", number))
        elif unit in ("包", "袋", "本", "枚"):
            out.add(("pack", number))
        elif unit == "mg":
            out.add(("g", number / 1000))
        else:
            out.add((unit, number))
    return out


_DOSE_PHRASE_RE = re.compile(r"1(?:日|回)[^\d\n]{0,12}?\d+(?:\.\d+)?\s*(?:粒|錠|カプセル|包|袋|本|枚|mg|g|ml)"
                             r"|\d+(?:\.\d+)?\s*(?:粒|錠|カプセル|包|袋|本|枚|mg|g|ml)\s*(?:あたり|当たり|中)")


def official_variant_specs(stage1_text, jan):
    """公式情報(Stage1本文)で、このJANと同じ行に書かれた規格(内容量・日分)。1日の摂取粒数が
    明記されていれば日分⇔粒数を換算した値も含める。このJANの規格が書かれていなければ空。"""
    text = unicodedata.normalize("NFKC", str(stage1_text or ""))
    ours = set()
    for line in text.splitlines():
        if jan in extract_jan_codes(line):
            # 摂取量の表記(「1日2粒」「1回1粒」「1粒あたり」等)は内容量ではないため除く。
            line = _DOSE_PHRASE_RE.sub(" ", line)
            ours |= {t for t in _spec_tokens(line) if t[0] in ("days", "count", "pack", "g", "ml")}
    daily = re.search(r"1日(?:の摂取目安量)?[^\d\n]{0,12}?(\d+)\s*(?:粒|錠|カプセル)", text)
    if daily and ours:
        per_day = float(daily.group(1))
        ours |= {("count", v * per_day) for k, v in ours if k == "days"}
        ours |= {("days", v / per_day) for k, v in ours if k == "count" and (v / per_day).is_integer()}
    return ours


def no_jan_identity_check(item, brand, product_name, category, jan, context):
    """JANの記載が無い楽天出品が、この商品(このJANのvariant)と同一かを判定する。
    戻り値: (ok, reason)。okでない場合は保留(この出品は使わない)。"""
    title = str(item.get("itemName", "") or "")
    compact_title = _compact_for_match(title)
    if category == "美容機器":
        # 型番の一致は前段のdevice identity判定で確認済み。型番の無い機器は保留。
        return (True, None) if device_model_key(product_name) else (False, "no_model_number")
    if not context or not context.get("stage1_text"):
        return False, "no_official_context"
    forms = list(app.explicit_brand_forms(brand))
    entry = brand_alias_entry(brand)
    if entry:
        forms += entry.get("names", [])
    if not any(_compact_for_match(f) in compact_title for f in forms if len(_compact_for_match(f)) >= 2):
        return False, "brand_not_in_title"
    folded_name = unicodedata.normalize("NFKC", str(product_name or "")).lower()
    parts = [x for x in _NAME_PART_SEPARATORS_RE.split(folded_name) if len(x) >= 2]
    if not PM_MATCH_FLAGS["name_match_v2"] and (not parts or not all(_compact_for_match(x) in compact_title for x in parts)):
        return False, "name_parts_not_in_title"
    conflict = series_word_conflict(product_name, title)
    if conflict:
        return False, f"series_word:{conflict}"
    extra = _extra_ingredient_tags(product_name, title, context.get("active_ingredients"), category)
    if extra:
        return False, "extra_ingredient:" + ",".join(sorted(extra))
    # Step51: 名称は収集時の名称照合(括弧内の別表記・カテゴリ語の省略に対応)と同じ規則で確認する。
    if PM_MATCH_FLAGS["name_match_v2"] and not pm_title_name_match(product_name, title, brand, category,
                                                                   context.get("active_ingredients")):
        return False, "name_parts_not_in_title"
    if PM_SELECTION_FLAGS["accept_same_product_sizes"]:
        # Step52: 同じ商品なら容量は問わない(公式本文に無い容量も可)。別シリーズ・別形状・
        # リニューアル前の商品を示す語がタイトルにあれば不合格。
        conflict = variant_word_conflict(product_name, title, official_sibling_words(context["stage1_text"], product_name, brand, jan))
        return (False, f"variant_word:{conflict}") if conflict else (True, None)
    ours = official_variant_specs(context["stage1_text"], jan)
    if not ours:
        return False, "official_variant_spec_unknown"
    listing = _spec_tokens(title)
    if not listing:
        return False, "listing_spec_missing"
    our_kinds = {k for k, _ in ours}
    if any(kind in our_kinds and (kind, value) not in ours for kind, value in listing):
        return False, "spec_conflict"
    if not listing & ours:
        return False, "spec_unconfirmed"
    return True, None


# ===== Step52: 同じ商品の容量違いの出品を受け入れる(Product Master収集経路のみ) =====
PM_SELECTION_FLAGS = {"accept_same_product_sizes": True, "review_first_selection": True, "cheapest_page_check": True}
# 公式本文のJANの行で、容量の記載以外に許す語(JANの表記・つなぎの語)。
_SIZE_LINE_FILLER_RE = re.compile(r"jan(?:コード)?|総重量|お?徳用|大容量|となります|です|は|の|※|約")
# 別の形状・別の版を示す一般語(ブランドごとの対応表ではない)。商品名に無いのにタイトルにあれば別商品。
_FORM_WORDS = ("ドリンク", "ゼリー", "顆粒", "粉末", "パウダー", "チュアブル", "グミ", "持続型", "タイムリリース", "液体")
_OLD_VERSION_RE = re.compile(r"旧(?:品|パッケージ|処方|デザイン|タイプ|仕様|版|商品)|リニューアル前|[【\[（(]旧[】\]）)]")
_SIBLING_STOP_WORDS = {"jan", "コード", "janコード", "製品", "商品", "タイプ", "など", "例", "サプリメント", "サプリ"}


def _size_only_line(line, jan):
    """JANの行が「JAN＋内容量」だけでできていれば、その内容量の集合。それ以外はNone。"""
    folded = unicodedata.normalize("NFKC", line).lower()
    sizes = {t for t in _spec_tokens(_DOSE_PHRASE_RE.sub(" ", folded)) if t[0] in ("days", "count", "pack", "g", "ml")}
    rest = re.sub(rf"(?<!\d){jan}(?!\d)", " ", folded)
    rest = _NO_JAN_SPEC_RE.sub(" ", rest)
    rest = re.sub(r"\d+(?:\.\d+)?\s*(?:g|ml)", " ", rest)
    rest = _SIZE_LINE_FILLER_RE.sub(" ", rest)
    rest = re.sub(r"[\s\W_]+", "", rest)
    return sizes if sizes and not rest else None


def official_size_variant_jans(stage1_text, jan):
    """公式本文で「JAN＋内容量」だけが書かれた行のJAN(この商品の容量違い)。この商品のJANは含めない。
    別シリーズ・別形状・過去の仕様など内容量以外の説明がある行のJANは含めない。
    同じ内容量に別々のJANが書かれている場合(旧品の可能性)はどちらも含めない。"""
    by_size, out = {}, set()
    for line in unicodedata.normalize("NFKC", str(stage1_text or "")).splitlines():
        for code in extract_jan_codes(line):
            sizes = _size_only_line(line, code)
            if sizes is None:
                continue
            for size in sizes:
                by_size.setdefault(size, set()).add(code)
            out.add(code)
    ambiguous = {c for codes in by_size.values() if len(codes) > 1 for c in codes}
    return out - ambiguous - {jan}


def official_sibling_words(stage1_text, product_name, brand, jan=""):
    """公式本文のJANの記載のうち、内容量だけの記載ではないもの(別シリーズ・別形状・過去の仕様の説明)に
    出てくる語で、この商品の名称・ブランド・この商品のJANの説明に無いもの。タイトルにあれば別商品の出品とみなす。
    1行に複数の記載がある場合(「、」「<br>」等で区切られた行)は区切りごとに判定する。"""
    name_text = _cm(f"{product_name} {brand}")
    words, own = set(), set()
    text = re.sub(r"<br\s*/?>", "\n", unicodedata.normalize("NFKC", str(stage1_text or "")), flags=re.IGNORECASE)
    for line in text.splitlines():
        if not extract_jan_codes(line):
            continue
        for segment in re.split(r"[、，,;；|]", line):
            codes = extract_jan_codes(segment)
            if not codes or all(_size_only_line(segment, c) is not None for c in codes):
                continue
            cleaned = _NO_JAN_SPEC_RE.sub(" ", re.sub(r"\d{8,13}", " ", segment.lower()))
            found = {_cm(w) for w in _PM_NAME_SPLIT_RE.split(re.sub(r"[*:：「」｢｣。．.<>]", " ", cleaned))}
            found = {w for w in found if len(w) >= 2 and not w.isdigit() and not re.fullmatch(r"[ぁ-ん]+", w)
                     and w not in _SIBLING_STOP_WORDS and w not in name_text}
            (own if jan and jan in codes else words).update(found)
    return words - own


def variant_word_conflict(product_name, title, sibling_words=()):
    """タイトルに、商品名に無い別形状の語・リニューアル前を示す語・公式本文上の別商品の語があればその語。"""
    folded_title = unicodedata.normalize("NFKC", str(title or ""))
    if _OLD_VERSION_RE.search(folded_title):
        return "old_version"
    name, compact_title = _cm(product_name), _cm(title)
    for word in _FORM_WORDS:
        if word in compact_title and word not in name:
            return word
    return next((w for w in sorted(sibling_words) if w in compact_title and w not in name), None)


# 一般的な栄養成分名(ブランドごとの対応表ではない)。JANで確認できない出品のタイトル本体に、
# 商品名・成分に無いこれらの語があれば別商品(例: 「カルシウム／マグ」「マルチビタミン」)とみなす。
_NUTRIENT_WORDS = ("カルシウム", "マグネシウム", "鉄", "葉酸", "イソフラボン", "ルテイン", "グルコサミン", "コンドロイチン",
                   "コエンザイムq10", "マカ", "ブルーベリー", "dha", "epa", "乳酸菌", "ビフィズス菌", "セラミド", "プラセンタ",
                   "ナットウキナーゼ", "ウコン", "アスタキサンチン", "マルチビタミン", "ビタミンa", "ビタミンb", "ビタミンc",
                   "ビタミンd", "ビタミンe", "ビタミンk", "亜鉛", "ヒアルロン酸", "コラーゲン", "シスチン", "システイン")


def _title_main_segment(title):
    """楽天タイトルの本体(「|」「｜」より前。後ろは検索用の語の羅列であることが多い)。"""
    return re.split(r"[|｜]", str(title or ""), maxsplit=1)[0]


_CM_SKIP_RE = re.compile(r"[\s\-‐‑‒–—_.・･]")


def title_identity_segment(title, product_name, category="サプリメント"):
    """Step53: タイトルのうち商品を特定している部分(「|」より前、かつ商品名の後で最初に出る内容量の表記より前)。
    内容量より後ろは検索用の語の羅列であることが多いため、余分な成分名の確認から外す。
    商品名・内容量が見つからなければ「|」より前の全体。戻り値はNFKC・小文字の文字列。"""
    folded = unicodedata.normalize("NFKC", _title_main_segment(title)).lower()
    chars, index = [], []
    for i, ch in enumerate(folded):
        if not _CM_SKIP_RE.match(ch):
            chars.append(ch)
            index.append(i)
    compact = "".join(chars)
    ends = []
    for group in _name_part_groups(product_name, category):
        key = group.get("part") or group.get("alt")
        pos = compact.find(key) if key else -1
        if pos >= 0:
            ends.append(pos + len(key))
    if not ends:
        return folded
    size = _PACKAGE_SIZE_RE.search(compact, max(ends))
    return folded[:index[size.start()]] if size else folded


def _extra_ingredient_tags(product_name, title, active_ingredients, category="サプリメント"):
    """商品名・成分に無い成分タグのうち、タイトルの商品を特定している部分にあるもの。"""
    folded_name = unicodedata.normalize("NFKC", str(product_name or "")).lower()
    parts = [x for x in _PM_NAME_SPLIT_RE.split(folded_name) if len(x) >= 2]
    product_tags = set(app.compute_ingredient_tags(list(active_ingredients or []) + parts))
    segment = title_identity_segment(title, product_name, category)
    title_tags = set(app.compute_ingredient_tags([w for w in re.split(r"[\s　・･()（）\[\]【】/|｜×,，、]+", segment) if len(w) >= 2]))
    return title_tags - product_tags


def no_jan_title_identity_conflict(product_name, title, active_ingredients=()):
    """Step52: JANで確認できないサプリの出品について、タイトル本体に商品名が無い、または商品名・成分に無い
    栄養成分名がタイトル本体にある場合、その理由。問題が無ければNone。"""
    main = _title_main_segment(title)
    groups = _name_part_groups(product_name, "サプリメント")
    parts = [g["part"] for g in groups if "part" in g]
    alts = [g["alt"] for g in groups if "alt" in g]
    compact_main = _cm(main)
    missing = [x for x in parts if x not in compact_main]
    if (not parts and not alts) or (missing and not (alts and any(a in compact_main for a in alts)
                                                     and all(_ascii_only(x) for x in missing))):
        return "name_not_in_title_main"
    product_text = _cm(" ".join([str(product_name or "")] + [str(a or "") for a in (active_ingredients or [])]))
    compact_segment = _cm(title_identity_segment(title, product_name, "サプリメント"))
    extra = next((w for w in _NUTRIENT_WORDS if w in compact_segment and w not in product_text), None)
    return f"other_nutrient:{extra}" if extra else None


def _mentioned_jans(item):
    text = unicodedata.normalize("NFKC", f"{item.get('itemName', '') or ''} {item.get('itemCaption', '') or ''}")
    labelled = set(re.findall(r"JAN(?:コード)?\s*[:：]?\s*(\d{13}|\d{8})(?!\d)", text, flags=re.IGNORECASE))
    return extract_jan_codes(text) | labelled


# 色の一般語(ブランドごとの対応表ではない)。JANで確認できない美容機器の出品の色違いを見分ける。
_COLOR_WORDS = app.DEVICE_COLOR_WORDS   # Step54: 診断時と共通
color_groups = app.device_color_groups


def device_color_conflict(title, product_name, reference_titles):
    """JANで確認できない美容機器の出品の色の確認(型番の無い商品だけ)。この商品の色(商品名の色、または同じ検索結果で
    JANが一致した出品のタイトルの色)以外の色がタイトルにあれば、その理由。JAN一致の出品が無く色が分からない場合も保留。"""
    models = full_model_numbers(product_name)
    if models:
        return None   # Step54: 型番のある商品は、同じ型式の色違いを採用する(型番の照合で別の型式は除外済み)
    extra = color_groups(title) - color_groups(product_name)
    if not extra:
        return None
    if not reference_titles:
        return "device_color_unverified"
    known = set().union(*(color_groups(t) for t in reference_titles))
    return "device_color_mismatch" if extra - known else None


def official_shop_match(shop_name, brand):
    """店舗名にブランドの明示的な表記(登録済みの別表記を含む)があればTrue(ブランド・メーカーの店舗)。"""
    forms = list(app.explicit_brand_forms(brand))
    entry = brand_alias_entry(brand)
    if entry:
        forms += entry.get("names", []) + entry.get("makers", [])
    shop = _cm(shop_name)
    return any(len(_cm(f)) >= 2 and _cm(f) in shop for f in forms)


def select_listing_by_reviews(pairs, brand, page_check=None, checks=None):
    """Step52/54: 同じ商品と確認できた出品から1件を選ぶ。レビュー件数 → 価格(安い順。送料込み・送料別を区別しない) →
    レビュー平均 → ブランド公式店 → 画像あり → itemCode(決定的) の順。
    page_check(Step54)を渡した場合、レビュー件数が最多で同数の出品が複数あり価格で選ぶときは、安い順に販売ページを
    確認し、確認できた最初の出品を選ぶ(すべて確認できなければNone=保留)。checksに確認結果を記録する。"""
    def key(pair):
        item = pair[1]
        return (-app.safe_price(item.get("reviewCount", 0)), app.safe_price(item.get("itemPrice", 0)) or float("inf"),
                -app.safe_float(item.get("reviewAverage", 0)),
                0 if official_shop_match(item.get("shopName", ""), brand) else 1,
                0 if (item.get("mediumImageUrls") or item.get("smallImageUrls")) else 1,
                str(item.get("itemCode", "") or ""))
    ordered = [item for _, item in sorted(pairs, key=key)]
    top = app.safe_price(ordered[0].get("reviewCount", 0))
    tied = [item for item in ordered if app.safe_price(item.get("reviewCount", 0)) == top]
    if page_check is None or len(tied) == 1:
        return ordered[0]
    for item in tied:
        ok, reason = page_check(item)
        if checks is not None:
            checks.append({"item_code": str(item.get("itemCode", "") or ""), "ok": ok, "reason": reason})
        if ok:
            return item
    return None


# ===== Step54: 価格で選ぶ出品の販売ページの確認(新品の通常販売・価格・在庫) =====
_LISTING_PAGE_HOST = "https://item.rakuten.co.jp/"


def _listing_page_url(item):
    """出品の販売ページのURL(アフィリエイトURLの場合は中の商品ページ)。楽天市場の商品ページ以外はNone。"""
    url = str(item.get("itemUrl", "") or "")
    if url.startswith("https://hb.afl.rakuten.co.jp/"):
        from urllib.parse import parse_qs
        url = (parse_qs(urlsplit(url).query).get("pc") or [""])[0]
    return url if url.startswith(_LISTING_PAGE_HOST) else None


def _fetch_listing_page(item):
    """販売ページのHTML(取得できなければNone)。URL・例外の本文はログに出さない(アフィリエイトIDを含むため)。"""
    url = _listing_page_url(item)
    if not url:
        return None
    try:
        import requests
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    except Exception:
        return None
    if res.status_code != 200:
        return None
    raw = res.content
    charset = re.search(rb'charset=["\']?([A-Za-z0-9_\-]+)', raw[:4000])
    try:
        return raw.decode(charset.group(1).decode() if charset else "utf-8", "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def listing_page_check(item, page=None):
    """販売ページで、通常販売(予約・定期等でない)・API価格の販売単位があること・その在庫があることを確認する。
    戻り値: (ok, reason)。ページの形式が読めない場合は確認できない(False)とする。"""
    html = _fetch_listing_page(item) if page is None else page
    if not html:
        return False, "page_unavailable"
    sell = re.search(r'"sellType":"([A-Z_]+)"', html)
    if not sell:
        return False, "page_format_unknown"
    if sell.group(1) != "NORMAL":
        return False, "not_normal_sale"
    price = app.safe_price(item.get("itemPrice", 0))
    inventories = {sku: int(qty) for sku, qty in
                   re.findall(r'"sku":"([^"]+)","inventoryId":"[^"]*","quantity":(\d+)', html)}
    sku_prices = re.findall(r'"variantId":"([^"]+)","selectorValues":\[[^\]]*\](?:(?!"variantId").){0,3000}?"taxIncludedPrice":([\d.]+)',
                            html, flags=re.S)
    if sku_prices:
        matching = {v for v, p in sku_prices if int(float(p)) == price}
        if not matching:
            return False, "price_not_on_page"
        if not any(v in inventories for v in matching):
            return False, "stock_unknown"
        return (True, "ok") if any(inventories.get(v, 0) > 0 for v in matching) else (False, "out_of_stock")
    if price not in {int(float(p)) for p in re.findall(r'"taxIncludedPrice":([\d.]+)', html)}:
        return False, "price_not_on_page"
    if not inventories:
        return False, "stock_unknown"
    return (True, "ok") if sum(inventories.values()) > 0 else (False, "out_of_stock")


def rakuten_jan_status(item, jan_code):
    """Step49.7: 楽天候補に対するJANの状態(診断記録専用。判定には使わない)。
    match=検証済みJANの記載あり / explicit_mismatch=別の正当なJANの記載あり /
    absent=商品説明はあるがJANの記載なし / undeterminable=検証済みJANが無い・商品説明が取得されていない。"""
    jan = normalize_jan_code(jan_code)
    if not jan:
        return "undeterminable"
    if rakuten_item_mentions_jan(item, jan):
        return "match"
    caption = item.get("itemCaption")
    text = unicodedata.normalize("NFKC", f"{item.get('itemName', '') or ''} {caption or ''}")
    labelled = set(re.findall(r"JAN(?:コード)?\s*[:：]?\s*(\d{13}|\d{8})(?!\d)", text, flags=re.IGNORECASE))
    # 正当なJAN、または「JAN」と明記された8/13桁のコードが別の値なら明示的な不一致。
    if (extract_jan_codes(text) | labelled) - {jan}:
        return "explicit_mismatch"
    return "undeterminable" if caption is None else "absent"


def _resolver_stage_for_item(item, brand, product_name, category, jan_code, identity_context=None, reference_items=()):
    """resolve本体と同じ判定関数・同じ順序で、候補がどの段階で外れるかを返す(診断記録専用)。"""
    title = str(item.get("itemName", "") or "")
    jan = normalize_jan_code(jan_code)
    ok, how = _pm_title_ok(product_name, title, brand, item, jan, category, (identity_context or {}).get("active_ingredients"))
    info = {"title_match": how if ok else "no"}
    if not ok:
        return "title_mismatch", info
    if name_title_spec_conflict(product_name, title):
        return "spec_conflict", info
    if category == "美容機器":
        ok, why = _pm_device_ok(product_name, title, brand, item, jan, reference_items)
        info["model_match"] = bool(ok)
        info["model_reason"] = why
        if not ok:
            return "device_model_mismatch", info
    info["sale_condition"] = app.classify_rakuten_sale_condition(title)
    if info["sale_condition"] == "new" and pm_irregular_sale(title):
        info["sale_condition"] = "irregular"
    if info["sale_condition"] != "new":
        return "non_new_sale", info
    if _pm_is_set(title):
        return "set_item", info
    if PM_SELECTION_FLAGS["accept_same_product_sizes"]:
        basis, why = listing_identity_basis(item, brand, product_name, category, jan, identity_context, reference_items)
        return (("eligible" if basis in ("jan_code", "product_without_jan") else f"eligible_{basis}") if basis else why), info
    if jan and not rakuten_item_mentions_jan(item, jan):
        # Step50.2: JAN段階の結果を区別して記録する(判定は_resolve本体のとおり)。
        status = rakuten_jan_status(item, jan)
        if status == "explicit_mismatch":
            return "jan_explicit_mismatch", info
        if status == "undeterminable":
            return "jan_undeterminable", info
        if identity_context is None:
            return "jan_absent_hold", info
        ok, why = no_jan_identity_check(item, brand, product_name, category, jan, identity_context)
        return ("eligible_no_jan_spec_match", info) if ok else (f"no_jan_rejected:{why}", info)
    return "eligible", info


def _rakuten_match_summary(trace, result, brand, product_name, category, jan_code, identity_context=None):
    trace = trace or {"searches": [], "items": [], "omitted_items": 0, "cache_hit": False}
    items = trace["items"]
    fetch_stages = {}
    score_rules = {}
    for entry in items:
        fetch_stages[entry["stage"]] = fetch_stages.get(entry["stage"], 0) + 1
        if entry["stage"] == "score_reject":
            score_rules[entry.get("rule") or "unknown"] = score_rules.get(entry.get("rule") or "unknown", 0) + 1
    jan_counts = {"match": 0, "explicit_mismatch": 0, "absent": 0, "undeterminable": 0}
    resolver_stages = {}
    title_match = model_match = sale_new = single = 0
    references = [e["_item"] for e in items if e["stage"] == "searched_candidate"]
    for entry in items:
        if entry["stage"] != "searched_candidate":
            continue
        stage, info = _resolver_stage_for_item(entry["_item"], brand, product_name, category, jan_code, identity_context,
                                               references)
        entry["jan_status"] = rakuten_jan_status(entry["_item"], jan_code)
        entry["resolver_stage"] = stage
        entry.update(info)
        jan_counts[entry["jan_status"]] += 1
        resolver_stages[stage] = resolver_stages.get(stage, 0) + 1
        title_match += stage not in ("title_mismatch", "spec_conflict")
        model_match += info.get("model_match", stage not in ("title_mismatch", "spec_conflict")) is True
        sale_new += info.get("sale_condition") == "new"
        single += stage not in ("title_mismatch", "spec_conflict", "device_model_mismatch", "non_new_sale", "set_item")
    traced_total = len(items)
    return {
        "search_count": len(trace["searches"]), "cache_hit": trace["cache_hit"],
        "searched_item_count": sum(s["item_count"] for s in trace["searches"]),
        "traced_item_count": traced_total, "omitted_item_count": trace["omitted_items"],
        "brand_match_count": (traced_total - fetch_stages.get("brand_prefilter", 0)) if category == "美容機器" else None,
        "fetch_stage_counts": fetch_stages, "score_reject_rules": score_rules,
        "title_match_count": title_match, "model_match_count": model_match if category == "美容機器" else None,
        "new_sale_count": sale_new, "single_item_count": single,
        "jan_status_counts": jan_counts, "resolver_stage_counts": resolver_stages,
        "final_reason": result.get("reason") or result.get("status"),
        "keyword_stats": _keyword_stats(trace, result),
    }


def _keyword_stats(trace, result):
    """Step53: 検索語ごとの呼び出し回数(ページ数)・取得件数・新たに見つかった出品数・採用候補になった出品数。"""
    def label(keyword):   # 調査ログにJANの値を出さない
        return "JAN" if re.fullmatch(r"\d{8}|\d{13}", str(keyword or "")) else keyword
    stats = {}
    for search in trace.get("searches", []):
        st = stats.setdefault(label(search["keyword"]), {"calls": 0, "fetched": 0, "new_items": 0, "accepted": 0, "total_count": None})
        st["calls"] += 1
        st["fetched"] += search.get("item_count", 0)
        st["total_count"] = search.get("total_count", st["total_count"])
    first = {code: label(kw) for code, kw in trace.get("item_keyword", {}).items()}
    for keyword in first.values():
        stats.setdefault(keyword, {"calls": 0, "fetched": 0, "new_items": 0, "accepted": 0, "total_count": None})["new_items"] += 1
    accepted = [p["item_code"] for p in result.get("tiebreak_pool") or []] or \
        ([result["item"].get("itemCode")] if result.get("status") == "confirmed" and result.get("item") else [])
    for code in accepted:
        if code in first:
            stats[first[code]]["accepted"] += 1
    return stats


def resolve_item_code_for_product(brand, product_name, category, jan_code=None, identity_context=None):
    """楽天照合(_resolve_item_code_for_product_impl)を実行し、Step49.7の診断記録
    (候補ごとの除外理由・集計)を付けて返す。判定・採用条件は変えない。
    集計(match_summary)は件数のみ。タイトル全文を含む候補ごとの記録は診断ログ
    ([RAKUTEN MATCH TRACE])にだけ、件数・長さの上限付きで出す。"""
    token = app.start_rakuten_match_trace()
    mode_token = app._PM_COLLECTION_MODE.set(True)  # Step51: 収集時のみの採点改善を有効化
    jan_token = app._PM_COLLECTION_JAN.set(normalize_jan_code(jan_code))  # Step51.1: 採点での同一性確認用
    try:
        result = _resolve_item_code_for_product_impl(brand, product_name, category, jan_code=jan_code,
                                                     identity_context=identity_context)
    finally:
        app._PM_COLLECTION_JAN.reset(jan_token)
        app._PM_COLLECTION_MODE.reset(mode_token)
        trace = app.stop_rakuten_match_trace(token)
    summary = _rakuten_match_summary(trace, result, brand, product_name, category, jan_code, identity_context)
    log_items = [{k: v for k, v in entry.items() if not k.startswith("_")} for entry in (trace or {}).get("items", [])]
    print("[RAKUTEN MATCH TRACE] " + json.dumps({"brand": brand, "product": product_name, "category": category,
                                                  "summary": summary, "items": log_items}, ensure_ascii=False),
          flush=True)
    return dict(result, match_summary=summary)


# ===== Step51: Product Master収集の楽天照合の改善(診断時の照合は不変) =====
PM_MATCH_FLAGS = {"name_match_v2": True, "model_v2": True, "color_variant_requires_jan": True, "pm_set_detection": True}
_GENERIC_NAME_WORDS = ("サプリメント", "サプリ", "美顔器", "美容器", "美容機器")
_PM_NAME_SPLIT_RE = re.compile(r"[\s　・･\-_()（）\[\]［］【】「」『』/／|｜,，、&＆+＋]+")


_HYPHENATED_MODEL_RE = re.compile(r"(?<![a-z0-9])[a-z]{1,6}(?:-[a-z]{1,4})?-?\d{2,6}[a-z0-9]{0,4}(?:[-/][a-z0-9]{1,3})?(?![a-z0-9])")


def _cm(text):
    """照合用の圧縮表記(NFKC・小文字・空白/ハイフン/中点/ピリオド除去)。"""
    return re.sub(r"[\s\-‐‑‒–—_.・･]+", "", unicodedata.normalize("NFKC", str(text or "")).lower())


def _name_part_groups(product_name, category=None):
    """商品名を照合用の部分に分ける。括弧内は直前の部分の別表記(どちらかが一致すればよい)、
    「サプリメント」「美顔器」等のカテゴリ語は省略可とする。"""
    folded = unicodedata.normalize("NFKC", str(product_name or "")).lower()
    if PM_SELECTION_FLAGS["accept_same_product_sizes"]:
        # Step52: 商品名に含まれる内容量(「120粒」「30日分」等)は容量違いの出品でも一致を求めない。
        folded = _PACKAGE_SIZE_RE.sub(" ", folded)
    groups, outer = [], re.sub(r"\([^()]*\)", " ", folded)
    if category == "美容機器":
        # 型番(EPM-18BB・EH-SR85・KBE-2740/K等)は型番判定で照合するため、分割前に名称から除く。
        outer = _HYPHENATED_MODEL_RE.sub(" ", outer)
    for alt in re.findall(r"\(([^()]*)\)", folded):
        alt_c = _cm(alt)
        if len(alt_c) >= 2:
            groups.append({"alt": alt_c})
    for part in _PM_NAME_SPLIT_RE.split(outer):
        for word in _GENERIC_NAME_WORDS:
            if part != word and part.endswith(word):
                part = part[: -len(word)]
        if len(part) < 2 or part in _GENERIC_NAME_WORDS:
            continue
        # 美容機器の型番は型番判定(pm_device_identity)で照合するため、名称の部分からは除く。
        if category == "美容機器" and _FULL_MODEL_RE.fullmatch(_cm(part)):
            continue
        groups.append({"part": _cm(part)})
    return groups


def pm_title_name_match(product_name, title, brand, category=None, active_ingredients=None):
    """Step51: 収集時の名称照合。ブランドの明示的な表記(登録済みの別表記を含む)がタイトルにあり、
    商品名の各部分(括弧内の別表記はどちらか一方でよい・カテゴリ語は省略可)がすべてタイトルにあり、
    タイトルに商品名に無い別シリーズ語・(サプリは)余分な成分名が無いこと。"""
    compact_title = _cm(title)
    forms = list(app.explicit_brand_forms(brand))
    entry = brand_alias_entry(brand)
    if entry:
        forms += entry.get("names", [])
    if not any(len(_cm(f)) >= 2 and _cm(f) in compact_title for f in forms):
        return False
    # Step51.1: 美容機器で型番がタイトルに完全一致する場合は、型番を同一性の根拠とし、
    # 商品名の他の部分(説明語・カナ/英字の表記違い)の一致は求めない(別モデル・色違いは型番判定で除外)。
    ours = full_model_numbers(product_name) if category == "美容機器" else set()
    if ours and (ours <= full_model_numbers(title) or _color_variant_models(ours, full_model_numbers(title))):
        return True   # Step54: 同じ型式の色違いも型番で照合する(色の語「ゴールド」等をシリーズ語として扱わない)
    if _series_or_extra_ingredient_conflict(product_name, title, category, active_ingredients):
        return False
    groups = _name_part_groups(product_name, category)
    parts = [g["part"] for g in groups if "part" in g]
    alts = [g["alt"] for g in groups if "alt" in g]
    if not parts and not alts:
        return False
    # 括弧の外側の各部分は必須。ただし外側の部分が括弧内の別表記で表されている場合(英字名↔カナ名)は
    # 別表記の一致で代替できる。
    missing = [x for x in parts if x not in compact_title]
    if missing and not (alts and any(a in compact_title for a in alts) and all(_ascii_only(x) for x in missing)):
        return False
    return True


def _series_or_extra_ingredient_conflict(product_name, title, category, active_ingredients):
    folded_name = unicodedata.normalize("NFKC", str(product_name or "")).lower()
    if series_word_conflict(product_name, title):
        return True
    if category == "サプリメント" and _extra_ingredient_tags(product_name, title, active_ingredients, category):
        return True
    return False


def _ascii_only(text):
    return bool(re.fullmatch(r"[a-z0-9.\-]+", text or ""))


_FULL_MODEL_RE = re.compile(r"(?<![a-z0-9])([a-z]{1,6}\d{2,6}[a-z0-9]{0,4})(?![a-z0-9])")


def full_model_numbers(text):
    """Step51: 末尾まで含めた型番(ハイフンは除去)。EPM-18BBとEPM-18BB2を区別する。
    「KBE-2740/K」のように型番の直後に「/英字1文字」が続く表記は、その英字まで型番に含める。"""
    folded = re.sub(r"(\d)\s*/\s*([a-z])(?![a-z0-9])", r"\1\2", unicodedata.normalize("NFKC", str(text or "")).lower())
    return set(_FULL_MODEL_RE.findall(_model_text(folded)))


_color_suffix_pair = app.device_color_suffix_pair   # Step54: 診断時と共通


def _color_variant_models(ours, theirs):
    """この商品の型番のうちタイトルに無いものが、すべてタイトルの型番の色記号違いか(同じ型式の色違い)。"""
    missing = [m for m in ours if m not in theirs]
    return bool(missing) and all(any(_color_suffix_pair(m, t) for t in theirs) for m in missing)


def device_color_variant(product_name, item, reference_items=()):
    """Step54: 出品が同じ型式の色違いで、色違いに由来すると確認できるか(型番の末尾の色記号だけが違い、色記号付きの
    型番と色の対応が出品自身または同じ検索結果の出品のタイトルで明確)。JANが色ごとに異なっていてもよい。診断時と同じ判定(app.device_model_check)。"""
    return app.device_model_check(product_name, item, reference_items)[1] == "color_variant"


def pm_device_identity(product_name, title, brand, item, jan, reference_items=()):
    """Step51: 収集時の美容機器の同一性。戻り値: (ok, reason)。
    型番あり: 末尾まで完全一致で同一。末尾1文字(色記号)だけ違う場合は、JANが一致するか、型番と色の対応が明確な出品を採用。
    型番なし: タイトルに型番があればJANが一致する出品だけ採用、無ければ従来の名称判定。"""
    ours = full_model_numbers(product_name)
    theirs = full_model_numbers(title)
    jan_ok = bool(jan) and rakuten_item_mentions_jan(item, jan)
    def color_of_ours(t):
        return any(_color_suffix_pair(m, t) for m in ours)
    if ours:
        if ours <= theirs:
            prefixes = {re.match(r"[a-z]+", m).group(0) for m in ours}
            if any(re.match(r"[a-z]+", m).group(0) in prefixes and not color_of_ours(m) for m in theirs - ours):
                return False, "conflicting_model_number"
            return True, "model_number_match"
        if _color_variant_models(ours, theirs):
            # Step54: 同じ型式の色違い(型番の末尾の色記号だけが違う)は、JANが一致するか、出品に色の記載があり
            # 色違いに由来すると確認できれば採用する(JANが色ごとに異なっていてもよい)。
            if jan_ok:
                return True, "color_variant_jan_match"
            if not PM_MATCH_FLAGS["color_variant_requires_jan"] \
                    or device_color_variant(product_name, dict(item, itemName=title), reference_items):
                return True, "color_variant"
            return False, "color_variant_unconfirmed"
        return False, "model_number_mismatch"
    if theirs:
        return (True, "model_by_jan") if jan_ok else (False, "title_has_model_product_has_none")
    if jan_ok:
        return True, "identity_by_jan"  # 型番が双方に無い場合、出品のJAN一致で同一性を確認する
    return device_identity_matches(product_name, title, brand)


def _pm_title_ok(product_name, title, brand, item, jan, category=None, active_ingredients=None):
    normal = app.is_same_verified_rakuten_product(product_name=product_name, rakuten_title=title, brand=brand,
                                                  shop_name=str(item.get("shopName", "") or ""))
    if PM_MATCH_FLAGS["name_match_v2"]:
        ok = pm_title_name_match(product_name, title, brand, category, active_ingredients) \
            or jan_verified_name_match(product_name, brand, item, jan)
        return ok, "name_v2"
    return normal or jan_verified_name_match(product_name, brand, item, jan), "normal" if normal else "jan_order_insensitive"


def _pm_device_ok(product_name, title, brand, item, jan, reference_items=()):
    if PM_MATCH_FLAGS["model_v2"]:
        return pm_device_identity(product_name, title, brand, item, jan, reference_items)
    return device_identity_matches(product_name, title, brand)


# Step53: 通常の新品販売ではない出品(収集時のみ)。期限を書いているだけの出品(「賞味期限2029.01」)は除かない。
_PM_IRREGULAR_SALE_RE = re.compile(
    r"アウトレット|訳あり|訳アリ|ワケあり|ワケアリ|わけあり|期限(?:間近|切迫|短|が近)|"
    r"(?:外箱|箱|パッケージ|外装)\s*(?:擦れ|すれ|潰れ|つぶれ|傷|キズ|汚れ|破れ|ダメージ|不良|凹み|へこみ)|難あり|"
    r"新品未開封|未開封品|試供品|非売品")


def pm_irregular_sale(title):
    return bool(_PM_IRREGULAR_SALE_RE.search(unicodedata.normalize("NFKC", str(title or ""))))


def _pm_is_set(title):
    return app.pm_is_set_item(title) if PM_MATCH_FLAGS["pm_set_detection"] else app._is_rakuten_set_item(title)


def _resolve_item_code_for_product_impl(brand, product_name, category, jan_code=None, identity_context=None):
    """brand+product_nameで楽天候補を検索し、確信を持って1件に絞れる場合のみ
    その候補を返す。

    既存処理の再利用(新規の照合ロジックは作らない):
    - app.fetch_rakuten_candidates(): キーワード検索・レート制限待機・429retry/
      cooldown・ジャンル絞り込みに加え、内部で既に
      app.is_same_verified_rakuten_product()による商品名一致判定と
      app.score_rakuten_item()によるスコアリングを適用済み。
    - app._is_rakuten_set_item(): セット/まとめ買い商品の除外。
    - app._select_single_or_set_best(): 同一商品を複数店舗が販売している場合の
      代表1店舗選択(スコア→レビュー数→画像有無→評価→価格の既存タイブレーク
      基準をそのまま使う。判定基準自体は変更しない)。

    戻り値:
      {"status": "not_found"}                                   候補0件/商品名不一致/全件セット
      {"status": "confirmed", "item": {...}}                     候補1件に自然に確定
      {"status": "confirmed", "item": {...}, "disambiguated_by": "jan_code"}            JANで一意に確定
      {"status": "confirmed", "item": {...}, "disambiguated_by": "merchant_tiebreak"}   同一商品・複数店舗を既存タイブレークで代表1件に確定

    is_same_verified_rakuten_product()とセット/まとめ買い除外を通過した候補
    だけが対象(商品同一性の判定基準自体は緩めない)。それでも複数候補が
    残る場合は「同一商品を複数店舗が販売している」ケースとみなし、JAN本文
    一致で一意に決まればJANを優先、決まらない場合のみ既存タイブレークで
    代表店舗を1件選ぶ(disambiguated_by="merchant_tiebreak")。
    JANは「追加検証材料」であり、JAN記載が無いことを理由に候補を落とす
    ことはしない(候補が最初から1件に絞れている場合はJANを見ずに確定する)。
    """
    scored_items = app.fetch_rakuten_candidates(
        product_name=product_name, category=category, brand=brand
    )
    diag = {"initial_candidate_count": len(scored_items), "title_matched_count": 0, "single_item_count": 0}
    references = [item for _, item in scored_items]   # Step54: 美容機器の色違いの確認に使う、同じ検索結果の出品
    if not scored_items:
        return {"status": "not_found", **diag}

    # fetch_rakuten_candidates()は内部で既にis_same_verified_rakuten_product()
    # による商品名一致判定を適用済みだが、item_codeをproduct_masterへ永続保存
    # する用途は診断時の一時表示より誤紐付けの許容度が低いため、ここでも
    # 同じ既存関数で二重に確認する(別の照合ロジックは作らない)。
    jan_for_title = normalize_jan_code(jan_code)
    title_matched_pairs = [
        (score, item) for score, item in scored_items
        if _pm_title_ok(product_name, str(item.get("itemName", "") or ""), brand, item, jan_for_title, category,
                        (identity_context or {}).get("active_ingredients"))[0]
    ]
    # Step49.6: 商品名とタイトルで同じ単位の規格が食い違う候補は別規格として除く。
    title_matched_pairs = [
        (score, item) for score, item in title_matched_pairs
        if not name_title_spec_conflict(product_name, str(item.get("itemName", "") or ""))
    ]
    diag["title_matched_count"] = len(title_matched_pairs)
    if not title_matched_pairs:
        return {"status": "not_found", "reason": "title_mismatch", **diag}

    # Step45.7: 処理順は identity一致 → 販売形態 → set/bundle → JAN → merchant
    # tie-break。ショップ評価より先に、同一モデルであること・新品の通常販売で
    # あることを必ず確定させる。
    if category == "美容機器":
        title_matched_pairs = [
            (score, item) for score, item in title_matched_pairs
            if _pm_device_ok(product_name, str(item.get("itemName", "") or ""), brand, item, jan_for_title, references)[0]
        ]
        diag["device_identity_matched_count"] = len(title_matched_pairs)
        if not title_matched_pairs:
            return {"status": "not_found", "reason": "device_model_mismatch", **diag}

    # 販売形態はタイトルだけで判定する(商品説明には「中古品ではありません」
    # 等の否定表現が入り得るため)。
    new_sale_pairs = [
        (score, item) for score, item in title_matched_pairs
        if app.classify_rakuten_sale_condition(str(item.get("itemName", "") or "")) == "new"
        and not pm_irregular_sale(item.get("itemName", ""))
    ]
    diag["new_sale_count"] = len(new_sale_pairs)
    if not new_sale_pairs:
        return {"status": "not_found", "reason": "only_non_new_sale_listings", **diag}

    single_pairs = [
        (score, item) for score, item in new_sale_pairs
        if not _pm_is_set(str(item.get("itemName", "") or ""))
    ]
    diag["single_item_count"] = len(single_pairs)
    if not single_pairs:
        return {"status": "not_found", "reason": "only_set_items", **diag}

    if PM_SELECTION_FLAGS["accept_same_product_sizes"]:
        return _select_same_product_listing(single_pairs, brand, product_name, category, jan_code, identity_context, diag,
                                            references)

    # Step47.6: 検証済みJANがある商品は、JANが楽天候補(タイトル/商品説明)に
    # 記載された候補だけを同一商品・同一variantとして扱う。JAN一致が1件も
    # 無ければ(候補が1件だけでも)安全側でnot_found。JAN一致候補同士では
    # 既存の新品・単品・merchant tie-breakを使う。JANが無い商品は従来どおり。
    jan_str = normalize_jan_code(jan_code)
    tiebreak_label = "merchant_tiebreak"
    if jan_str:
        jan_matched = [(score, item) for score, item in single_pairs if rakuten_item_mentions_jan(item, jan_str)]
        diag["jan_match_count"] = len(jan_matched)
        if jan_matched:
            single_pairs = jan_matched
            if len(single_pairs) == 1:
                return {"status": "confirmed", "item": single_pairs[0][1], "disambiguated_by": "jan_code", **diag}
            tiebreak_label = "jan_code+merchant_tiebreak"
        else:
            # Step50.1: JANが一致する出品が無い場合、JANの記載が無い出品だけを
            # ブランド・正式商品名・公式の規格等で照合する(明記されたJANが違う出品は使わない)。
            accepted, held, rejections = [], 0, {}
            for score, item in single_pairs:
                status = rakuten_jan_status(item, jan_str)
                if status == "absent" and identity_context is not None:
                    ok, why = no_jan_identity_check(item, brand, product_name, category, jan_str, identity_context)
                    if ok:
                        accepted.append((score, item))
                        continue
                    rejections[why.split(":")[0]] = rejections.get(why.split(":")[0], 0) + 1
                if status in ("absent", "undeterminable"):
                    held += 1
            diag.update({"no_jan_accepted_count": len(accepted), "no_jan_rejections": rejections})
            if not accepted:
                return {"status": "not_found",
                        "reason": "jan_absent_identity_unconfirmed" if held else "jan_mismatch", **diag}
            single_pairs = accepted
            if len(single_pairs) == 1:
                return {"status": "confirmed", "item": single_pairs[0][1], "disambiguated_by": "name_spec_without_jan", **diag}
            tiebreak_label = "name_spec_without_jan+merchant_tiebreak"
    elif len(single_pairs) == 1:
        return {"status": "confirmed", "item": single_pairs[0][1], **diag}

    # 同一商品・複数店舗の代表1件を既存のタイブレーク基準で選ぶ(別商品の
    # 可能性がある候補はここには残っていない前提=商品名一致・identity・販売
    # 形態・セット除外(JANがあればJAN一致)を通過済みのため)。
    best_item = app._select_single_or_set_best(single_pairs)
    tiebreak_pool = [
        {
            "item_code": item.get("itemCode", ""), "score": score,
            "shop_name": item.get("shopName", ""),
            "review_count": item.get("reviewCount", 0), "review_average": item.get("reviewAverage", 0),
            "has_image": bool(item.get("mediumImageUrls") or item.get("smallImageUrls")),
            "price": item.get("itemPrice", 0),
        }
        for score, item in single_pairs
    ]
    return {
        "status": "confirmed", "item": best_item, "disambiguated_by": tiebreak_label,
        "candidate_count": len(single_pairs), "tiebreak_pool": tiebreak_pool,
        **diag,
    }


def listing_identity_basis(item, brand, product_name, category, jan_code, identity_context, reference_items=()):
    """Step52: JAN段階で、出品がこの商品(容量違いを含む)と同一である根拠。戻り値: (basis, reason)。
    basis: jan_code=この商品のJAN / official_variant_jan=公式本文の容量違いのJAN /
    name_without_jan=JANの記載が無く名称等で確認 / product_without_jan=この商品にJANが無く名称等で確認 / None=不合格・保留。"""
    jan = normalize_jan_code(jan_code)
    context = identity_context or {}
    title = str(item.get("itemName", "") or "")
    if jan and rakuten_item_mentions_jan(item, jan):
        return "jan_code", None
    if category == "美容機器" and device_color_variant(product_name, item, reference_items):
        return "color_variant", None   # Step54: 同じ型式の色違い(JANが色ごとに異なっていてもよい)
    stage1 = context.get("stage1_text") or ""
    if category != "美容機器" and stage1:
        conflict = variant_word_conflict(product_name, title, official_sibling_words(stage1, product_name, brand, jan))
        if conflict:
            return None, f"variant_word:{conflict}"
    def title_conflict():
        if category != "サプリメント":
            return None
        return no_jan_title_identity_conflict(product_name, title, context.get("active_ingredients"))
    if not jan:
        conflict = title_conflict()
        return (None, f"variant_word:{conflict}") if conflict else ("product_without_jan", None)
    status = rakuten_jan_status(item, jan)
    if status == "explicit_mismatch":
        variants = official_size_variant_jans(stage1, jan) if category != "美容機器" else set()
        mentioned = _mentioned_jans(item)
        if mentioned & variants and mentioned <= variants | {jan}:
            return "official_variant_jan", None
        return None, "jan_explicit_mismatch"
    if status == "undeterminable" or identity_context is None:
        return None, "jan_undeterminable" if status == "undeterminable" else "jan_absent_hold"
    ok, why = no_jan_identity_check(item, brand, product_name, category, jan, identity_context)
    if ok and title_conflict():
        ok, why = False, title_conflict()
    return ("name_without_jan", None) if ok else (None, f"no_jan_rejected:{why}")


def _select_same_product_listing(single_pairs, brand, product_name, category, jan_code, identity_context, diag,
                                 reference_items=()):
    """Step52: 同じ商品(容量違いを含む)と確認できた出品をすべて候補にし、レビュー件数を優先して1件を選ぶ。"""
    accepted, rejections = [], {}
    jan = normalize_jan_code(jan_code)
    # 美容機器の色違いの確認に使う、この商品のJANが記載された出品のタイトル。
    jan_titles = [str(i.get("itemName", "") or "") for _, i in single_pairs if jan and rakuten_item_mentions_jan(i, jan)]
    for score, item in single_pairs:
        basis, why = listing_identity_basis(item, brand, product_name, category, jan_code, identity_context, reference_items)
        if basis and category == "美容機器" and basis != "jan_code":
            color = device_color_conflict(item.get("itemName", ""), product_name, jan_titles)
            if color:
                basis, why = None, color
        if basis:
            accepted.append((score, item, basis))
            continue
        key = why.split(":")[1] if why.startswith("no_jan_rejected:") else why.split(":")[0]
        rejections[key] = rejections.get(key, 0) + 1
    basis_counts = {}
    for *_, b in accepted:
        basis_counts[b] = basis_counts.get(b, 0) + 1
    diag.update({"accepted_count": len(accepted), "listing_rejections": rejections, "accepted_basis_counts": basis_counts})
    if not accepted:
        if not normalize_jan_code(jan_code):
            reason = "variant_word_conflict"
        elif rejections.get("jan_explicit_mismatch", 0) == len(single_pairs):
            reason = "jan_mismatch"
        else:
            reason = "jan_absent_identity_unconfirmed"  # JANの記載が無い・確認できない出品だけが残った(保留)
        return {"status": "not_found", "reason": reason, **diag}
    pairs = [(score, item) for score, item, _ in accepted]
    page_checks = []
    best = select_listing_by_reviews(pairs, brand, listing_page_check if PM_SELECTION_FLAGS["cheapest_page_check"] else None,
                                     page_checks) if PM_SELECTION_FLAGS["review_first_selection"] \
        else app._select_single_or_set_best(pairs)
    if page_checks:
        diag["listing_page_checks"] = page_checks
    if best is None:
        # Step54: 価格で選ぶ出品の販売ページをどれも確認できなかった(保留)。
        return {"status": "not_found", "reason": "listing_page_unconfirmed", "candidate_count": len(accepted), **diag}
    basis = next(b for _, item, b in accepted if item is best)
    result = {"status": "confirmed", "item": best, "disambiguated_by": basis if len(accepted) == 1 else f"{basis}+review_selection",
              "identity_basis": basis, "candidate_count": len(accepted), **diag}
    if basis == "official_variant_jan":
        result["official_variant_jans"] = sorted(official_size_variant_jans((identity_context or {}).get("stage1_text"),
                                                                            normalize_jan_code(jan_code)))
    if len(accepted) > 1:
        result["tiebreak_pool"] = [
            {"item_code": item.get("itemCode", ""), "basis": b, "shop_name": item.get("shopName", ""),
             "official_shop": official_shop_match(item.get("shopName", ""), brand),
             "review_count": item.get("reviewCount", 0), "review_average": item.get("reviewAverage", 0),
             "has_image": bool(item.get("mediumImageUrls") or item.get("smallImageUrls")), "price": item.get("itemPrice", 0)}
            for _, item, b in accepted
        ]
    return result


def _saved_resolution_identity_ok(resolution, jan):
    """reflect前に確認済みの結果を保存してよいか(この商品のJAN、または記録された根拠が出品と矛盾しないこと)。"""
    item = resolution.get("item") or {}
    if rakuten_item_mentions_jan(item, jan):
        return True
    basis = resolution.get("identity_basis") or str(resolution.get("disambiguated_by") or "").split("+")[0]
    if basis == "official_variant_jan":
        variants = set(resolution.get("official_variant_jans") or [])
        mentioned = _mentioned_jans(item)
        return bool(mentioned & variants) and mentioned <= variants | {jan}
    if basis in ("name_without_jan", "name_spec_without_jan"):
        return rakuten_jan_status(item, jan) == "absent"
    return False


def verify_and_resolve_item_code(product_id, brand, product_name, category, jan_code=None, resolution=None):
    """resolve_item_code_for_product()で1件に確定した候補のみ、
    app.fetch_rakuten_item_by_item_code()で実在・価格・URLを再確認し、
    検証成功した場合だけproduct_masterのitem_code/価格/URL/画像を更新する
    (成分・formulation等の低頻度フィールドは一切変更しない)。
    最終status: resolved(更新成功) / ambiguous / not_found / verification_failed。
    resolved以外はいずれも書き込まず、呼び出し元がneeds_review/未取得の
    ままproduct_masterを変更しないことを保証する。

    resolution(Step45.9a): reflect前に同じresolve_item_code_for_product()で
    確認済みの結果。渡された場合は楽天検索を再実行せず、その候補をitem_code
    による存在確認(fetch_rakuten_item_by_item_code)だけで検証して保存する。
    """
    if resolution is None:
        resolution = resolve_item_code_for_product(brand, product_name, category, jan_code=jan_code)
    elif resolution.get("status") == "confirmed" and normalize_jan_code(jan_code) \
            and not _saved_resolution_identity_ok(resolution, normalize_jan_code(jan_code)):
        # Step47.6: 事前確認の結果を再利用する場合も、検証済みJANと一致しない
        # 候補は保存しない(JANを渡さずに得た結果の取り込みを防ぐ)。
        return {"status": "not_found", "reason": "jan_mismatch", "item_code": (resolution.get("item") or {}).get("itemCode")}
    if resolution["status"] != "confirmed":
        return resolution

    diag = {k: v for k, v in resolution.items() if k not in ("status", "item", "disambiguated_by")}
    disambiguated_by = resolution.get("disambiguated_by")

    candidate = resolution["item"]
    item_code = str(candidate.get("itemCode", "") or "").strip()
    if not item_code:
        return {"status": "verification_failed", "reason": "missing_item_code_in_candidate", **diag}

    verify_result = app.fetch_rakuten_item_by_item_code(item_code)
    if not verify_result.get("ok"):
        return {
            "status": "verification_failed", "item_code": item_code,
            "http_status": verify_result.get("http_status"),
            "reason": verify_result.get("rakuten_error") or verify_result.get("http_status"),
            **diag,
        }

    verified_item = verify_result.get("item") or {}
    price = app.safe_price(verified_item.get("itemPrice", 0))
    url = str(verified_item.get("itemUrl", "") or "")
    if price <= 0 or not url:
        return {"status": "verification_failed", "item_code": item_code, "reason": "missing_price_or_url", **diag}

    images = verified_item.get("mediumImageUrls") or []
    image = ""
    if images:
        first_image = images[0]
        image = first_image.get("imageUrl", "") if isinstance(first_image, dict) else first_image

    candidate_title = str(candidate.get("itemName", "") or "")
    candidate_shop = str(candidate.get("shopName", "") or "")
    updated = app.update_product_master_item_code_fields(
        product_id, item_code=item_code, price=price, url=url, image=image,
        rakuten_title=candidate_title,
        shop_name=candidate_shop,
    )
    if not updated:
        return {"status": "error", "reason": "db_update_failed", **diag}

    return {
        "status": "resolved", "item_code": item_code, "price": price, "url": url,
        "image": image, "rakuten_title": candidate_title, "shop_name": candidate_shop,
        "disambiguated_by": disambiguated_by, "http_status": verify_result.get("http_status"),
        **diag,
    }


def select_item_code_pilot_candidates(count=5):
    """Step3の実APIパイロット対象として、JANが判明しておりカテゴリが
    分散した商品をproduct_masterから選ぶ(読み取り専用、楽天APIは呼ばない)。
    """
    conn = psycopg2.connect(app.DATABASE_URL)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT product_id, brand, name, category, jan_code
            FROM product_master
            WHERE data_source = 'ai_precollected' AND jan_code IS NOT NULL
            ORDER BY category, product_id
        """)
        rows = cur.fetchall()
    finally:
        conn.close()

    selected = []
    seen_categories = set()
    for product_id, brand, name, category, jan_code in rows:
        if category in seen_categories:
            continue
        selected.append({
            "product_id": product_id, "brand": brand, "name": name,
            "category": category, "jan_code": jan_code,
        })
        seen_categories.add(category)
        if len(selected) >= count:
            break

    if len(selected) < count:
        chosen_ids = {item["product_id"] for item in selected}
        for product_id, brand, name, category, jan_code in rows:
            if product_id in chosen_ids:
                continue
            selected.append({
                "product_id": product_id, "brand": brand, "name": name,
                "category": category, "jan_code": jan_code,
            })
            if len(selected) >= count:
                break

    return selected
