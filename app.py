import os
import psycopg2
from dotenv import load_dotenv
load_dotenv()

RAKUTEN_APP_ID = os.environ.get("RAKUTEN_APP_ID", "")
RAKUTEN_ACCESS_KEY = os.environ.get("RAKUTEN_ACCESS_KEY", "")
RAKUTEN_AFFILIATE_ID = os.environ.get("RAKUTEN_AFFILIATE_ID", "")
AMAZON_ASSOCIATE_TAG = os.environ.get("AMAZON_ASSOCIATE_TAG", "")
print("[ENV CHECK] RAKUTEN_APP_ID set:", bool(RAKUTEN_APP_ID), flush=True)
# ==========================================
# lumilog - AI肌診断アプリ
# Flaskメインサーバー
# Gemini APIを使った肌診断 + 履歴管理
# ==========================================

import hmac
import io
import json
import traceback
import urllib.parse
import requests
import re
import copy
import time
import threading
from psycopg2.pool import SimpleConnectionPool
import hashlib
GEMINI_ANALYSIS_CACHE = {}
ANALYSIS_CACHE_VERSION = "v4"  # use_timing フィールド追加
DATABASE_URL = os.getenv("DATABASE_URL")
RAKUTEN_COOLDOWN_UNTIL = 0
_rakuten_item_cache = {}
_rakuten_criteria_cache = {}
_rakuten_criteria_call_count = 0
MAX_RAKUTEN_CRITERIA_CALLS = 50
_step_rakuten_results = {}       # {(norm_cat, norm_ing): [products]} prefetch→Gemini連携用
_gemini_product_eval_cache = {}  # {normalized_title: {fields}} Gemini評価キャッシュ
_gemini_eval_cache_lock = None   # threading.Lock() — app起動後に初期化
_gemini_eval_cache_loaded = False
GEMINI_EVAL_CACHE_FILE = "gemini_product_eval_cache.json"
VERIFIED_PRODUCTS_CACHE_FILE = "verified_products_cache.json"
VERIFIED_PRODUCTS_CACHE_TTL_SECONDS = 60 * 60 * 24 * 45
GEMINI_EVAL_CACHE_TTL_SECONDS = 60 * 60 * 24 * 7   # 7日（商品評価の一貫性のため短縮）
RAKUTEN_SEARCH_CACHE_TTL_SECONDS = 60 * 60 * 24 * 7  # 7日

# ===== Gemini Models =====
ANALYSIS_MODEL = "gemini-3.1-flash-lite"   # 肌分析 Phase1/2
CANDIDATE_MODEL = "gemini-2.5-flash"       # 候補選定（2.0-flash-lite 429対策で統一）
ROUTINE_MODEL = "gemini-2.5-flash"         # ルーティン生成（未使用・予備）
DETAIL_MODEL = "gemini-3.1-flash-lite"     # 商品評価・名前整形

#DB_POOL = SimpleConnectionPool(
#   minconn=1,
#    maxconn=5,
#    dsn=DATABASE_URL
#)


#def get_db_conn():
#    return DB_POOL.getconn()


#def put_db_conn(conn):
#    if conn:
#        DB_POOL.putconn(conn)
print("[APP START]", flush=True)

def init_results_table():
    conn = None
    cur = None

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS results (
            id TEXT PRIMARY KEY,
            saved_at TIMESTAMP,
            payload JSONB
        );
        """)

        conn.commit()

        print("[DB TABLE READY]", flush=True)

    except Exception as e:
        if conn:
            conn.rollback()

        print("[DB TABLE ERROR]", e, flush=True)

    finally:
        if cur:
            cur.close()

        if conn:
            conn.close()

GEMINI_DAILY_LIMIT = int(os.getenv("GEMINI_DAILY_LIMIT", "20"))
GEMINI_RESET_HOUR_JST = 16


def get_gemini_usage_key(now=None):
    from datetime import datetime, timedelta, timezone

    jst = timezone(timedelta(hours=9))
    now = now or datetime.now(jst)

    if now.hour < GEMINI_RESET_HOUR_JST:
        usage_date = (now - timedelta(days=1)).date()
    else:
        usage_date = now.date()

    return usage_date.isoformat()

def init_gemini_usage_table():
    conn = None
    cur = None

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS gemini_usage (
            usage_key TEXT PRIMARY KEY,
            request_count INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)

        conn.commit()
        print("[GEMINI USAGE TABLE READY]", flush=True)

    except Exception as e:
        if conn:
            conn.rollback()
        print("[GEMINI USAGE TABLE ERROR]", e, flush=True)

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def increment_gemini_usage():
    usage_key = get_gemini_usage_key()

    conn = None
    cur = None

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        cur.execute("""
        INSERT INTO gemini_usage (usage_key, request_count, updated_at)
        VALUES (%s, 1, CURRENT_TIMESTAMP)
        ON CONFLICT (usage_key)
        DO UPDATE SET
            request_count = gemini_usage.request_count + 1,
            updated_at = CURRENT_TIMESTAMP
        RETURNING request_count;
        """, (usage_key,))

        count = cur.fetchone()[0]
        conn.commit()

        return count

    except Exception as e:
        if conn:
            conn.rollback()
        print("[GEMINI USAGE COUNT ERROR]", e, flush=True)
        return None

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


def get_gemini_usage_status():
    usage_key = get_gemini_usage_key()

    conn = None
    cur = None

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        cur.execute("""
        SELECT request_count
        FROM gemini_usage
        WHERE usage_key = %s;
        """, (usage_key,))

        row = cur.fetchone()
        used = int(row[0]) if row else 0

    except Exception as e:
        print("[GEMINI USAGE READ ERROR]", e, flush=True)
        used = 0

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

    remaining = max(0, GEMINI_DAILY_LIMIT - used)

    return {
        "usage_key": usage_key,
        "used": used,
        "limit": GEMINI_DAILY_LIMIT,
        "remaining": remaining,
        "reset_hour_jst": GEMINI_RESET_HOUR_JST
    }
def init_analysis_cache_table():
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS gemini_analysis_cache (
            cache_key TEXT PRIMARY KEY,
            payload JSONB NOT NULL,
            saved_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)
        conn.commit()
        print("[ANALYSIS CACHE TABLE READY]", flush=True)
    except Exception as e:
        if conn: conn.rollback()
        print("[ANALYSIS CACHE TABLE ERROR]", e, flush=True)
    finally:
        if conn: conn.close()

def get_analysis_cache_from_db(cache_keys):
    if not isinstance(cache_keys, list):
        return None
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        for key in cache_keys:
            cur.execute(
                "SELECT payload FROM gemini_analysis_cache WHERE cache_key = %s",
                (key,)
            )
            row = cur.fetchone()
            if row:
                print("[GEMINI DB CACHE HIT]", key, flush=True)
                return row[0]
    except Exception as e:
        print("[ANALYSIS CACHE DB GET ERROR]", e, flush=True)
    finally:
        if conn: conn.close()
    return None

def save_analysis_cache_to_db(cache_key, data):
    if not cache_key or not isinstance(data, dict):
        return
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO gemini_analysis_cache (cache_key, payload, saved_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (cache_key) DO UPDATE SET
                payload = EXCLUDED.payload,
                saved_at = CURRENT_TIMESTAMP
        """, (cache_key, json.dumps(data, ensure_ascii=False)))
        conn.commit()
        print("[ANALYSIS CACHE SAVED TO DB]", cache_key[:40], flush=True)
    except Exception as e:
        if conn: conn.rollback()
        print("[ANALYSIS CACHE DB SAVE ERROR]", e, flush=True)
    finally:
        if conn: conn.close()

def init_rakuten_search_cache_table():
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS rakuten_search_cache (
            cache_key TEXT PRIMARY KEY,
            payload JSONB NOT NULL,
            saved_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)
        conn.commit()
        print("[RAKUTEN SEARCH CACHE TABLE READY]", flush=True)
    except Exception as e:
        if conn: conn.rollback()
        print("[RAKUTEN SEARCH CACHE TABLE ERROR]", e, flush=True)
    finally:
        if conn: conn.close()

def get_rakuten_search_from_db(cache_key):
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute(
            "SELECT payload, saved_at FROM rakuten_search_cache WHERE cache_key = %s",
            (cache_key,)
        )
        row = cur.fetchone()
        if row:
            payload, saved_at = row
            age = time.time() - saved_at.timestamp()
            if age <= RAKUTEN_SEARCH_CACHE_TTL_SECONDS:
                print(f"[RAKUTEN SEARCH DB CACHE HIT] {cache_key} age={age/3600:.1f}h", flush=True)
                return payload if isinstance(payload, list) else []
            print(f"[RAKUTEN SEARCH DB CACHE EXPIRED] {cache_key} age={age/3600:.1f}h", flush=True)
    except Exception as e:
        print(f"[RAKUTEN SEARCH CACHE GET ERROR] {repr(e)}", flush=True)
    finally:
        if conn: conn.close()
    return None

def save_rakuten_search_to_db(cache_key, results):
    if not cache_key or not isinstance(results, list):
        return
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO rakuten_search_cache (cache_key, payload, saved_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (cache_key) DO UPDATE SET
                payload = EXCLUDED.payload,
                saved_at = CURRENT_TIMESTAMP
        """, (cache_key, json.dumps(results, ensure_ascii=False)))
        conn.commit()
        print(f"[RAKUTEN SEARCH CACHE SAVED] {cache_key} ({len(results)}件)", flush=True)
    except Exception as e:
        if conn: conn.rollback()
        print(f"[RAKUTEN SEARCH CACHE SAVE ERROR] {repr(e)}", flush=True)
    finally:
        if conn: conn.close()

BRAND_CACHE_TTL_SECONDS = 60 * 60 * 24 * 180  # 180日（ブランド名はほぼ不変のため長め）
_BRAND_NAME_CACHE = {}  # メモリキャッシュ（同一プロセス内での再検索を避ける）

def init_brand_cache_table():
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS brand_name_cache (
            cache_key TEXT PRIMARY KEY,
            brand TEXT NOT NULL,
            saved_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)
        conn.commit()
        print("[BRAND CACHE TABLE READY]", flush=True)
    except Exception as e:
        if conn: conn.rollback()
        print("[BRAND CACHE TABLE ERROR]", e, flush=True)
    finally:
        if conn: conn.close()

def get_cached_brand(cache_key):
    if not cache_key:
        return None

    if cache_key in _BRAND_NAME_CACHE:
        return _BRAND_NAME_CACHE[cache_key]

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute(
            "SELECT brand, saved_at FROM brand_name_cache WHERE cache_key = %s",
            (cache_key,)
        )
        row = cur.fetchone()
        if row:
            brand, saved_at = row
            age = time.time() - saved_at.timestamp()
            if age <= BRAND_CACHE_TTL_SECONDS:
                print(f"[BRAND CACHE HIT] {cache_key} -> {brand}", flush=True)
                _BRAND_NAME_CACHE[cache_key] = brand
                return brand
    except Exception as e:
        print(f"[BRAND CACHE GET ERROR] {repr(e)}", flush=True)
    finally:
        if conn: conn.close()
    return None

def save_brand_to_cache(cache_key, brand):
    if not cache_key or not brand:
        return

    _BRAND_NAME_CACHE[cache_key] = brand

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO brand_name_cache (cache_key, brand, saved_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (cache_key) DO UPDATE SET
                brand = EXCLUDED.brand,
                saved_at = CURRENT_TIMESTAMP
        """, (cache_key, brand))
        conn.commit()
        print(f"[BRAND CACHE SAVED] {cache_key} -> {brand}", flush=True)
    except Exception as e:
        if conn: conn.rollback()
        print(f"[BRAND CACHE SAVE ERROR] {repr(e)}", flush=True)
    finally:
        if conn: conn.close()

def init_auth_tables():
    conn = None
    cur = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            email VARCHAR(320) PRIMARY KEY,
            user_id VARCHAR(36) NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS magic_tokens (
            token VARCHAR(128) PRIMARY KEY,
            email VARCHAR(320) NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            used BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """)
        conn.commit()
        print("[DB AUTH TABLES READY]", flush=True)
    except Exception as e:
        if conn: conn.rollback()
        print("[DB AUTH TABLE ERROR]", e, flush=True)
    finally:
        if cur: cur.close()
        if conn: conn.close()

init_results_table()
init_gemini_usage_table()
init_analysis_cache_table()
init_rakuten_search_cache_table()
init_brand_cache_table()
init_auth_tables()
VERIFY_PRODUCT_CACHE = {}
DEV_MODE = os.getenv("DEV_MODE", "false").lower() == "true"
USE_RICH_CANDIDATE = False
DISABLE_USAGE_LIMIT = os.getenv("DISABLE_USAGE_LIMIT", "false").lower() == "true"
from constants import (
    ALLOWED_TAGS,
    PRODUCT_IMAGES,
    CATEGORY_TAGS,
    INGREDIENT_TAGS,
    ingredient_map,
    RETINOL_LEVEL_RULE,
    SENSITIVE_OK_VALUES,
    SKIN_TYPE_TAGS,
    INGREDIENT_STRENGTH_VALUES,
    formulation_labels,
    CLEANSING_FORMULATION_TAGS,
    MAIN_FUNCTION_MAP,
    MAIN_FUNCTION_TAGS,
    CLEANSING_TAGS,
    technology_labels,
    signature_ingredient_effects,
    texture_labels,
    contraindications_labels,
    signature_ingredient_labels,
    INGREDIENT_FOCUS_TAGS,
    AI_CATEGORY_MAP,
    AI_INGREDIENT_MAP,
    CONCERN_MAP
)
def call_gemini_with_retry(client, model, contents, config=None, max_retries=2, timeout=60):
    import random
    import concurrent.futures
    from google.genai import errors

    last_error = None

    retryable_words = [
        "503",
        "UNAVAILABLE",
        "429",
        "RESOURCE_EXHAUSTED",
        "overloaded",
        "temporarily unavailable",
        "try again later",
        "rate limit",
        "quota",
    ]

    max_retries = max(1, int(max_retries or 1))

    # 呼び出し前にクォータ残数とプロンプト概算トークン数をログ出力
    try:
        _usage_status = get_gemini_usage_status()
        _quota_used = _usage_status.get("used", "?")
        _quota_limit = _usage_status.get("limit", "?")
        _quota_remaining = _usage_status.get("remaining", "?")
        # テキスト部分のトークン概算（文字数÷3、日本語は1文字≒1トークン）
        _text_chars = sum(len(str(c)) for c in (contents if isinstance(contents, list) else [contents])
                          if isinstance(c, str))
        _approx_tokens = max(_text_chars // 3, _text_chars)  # 日本語重みで多めに見積もる
        print(
            f"[GEMINI PRE-CALL] model={model} quota={_quota_used}/{_quota_limit}(remaining={_quota_remaining})"
            f" prompt≈{_approx_tokens}tokens",
            flush=True
        )
    except Exception:
        pass

    for attempt in range(max_retries):
        try:
            print(
                f"[GEMINI CALL START] model={model} attempt={attempt + 1}/{max_retries} timeout={timeout}s",
                flush=True
            )
            _t_start = time.time()

            # concurrent.futures でタイムアウトを強制（SDK に timeout 引数なし）
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _ex:
                _future = _ex.submit(
                    client.models.generate_content,
                    model=model, contents=contents, config=config
                )
                try:
                    response = _future.result(timeout=timeout)
                except concurrent.futures.TimeoutError:
                    raise TimeoutError(
                        f"Gemini timeout after {timeout}s (model={model})"
                    )

            elapsed = time.time() - _t_start
            current_count = increment_gemini_usage()
            if current_count is not None:
                _near = current_count >= GEMINI_DAILY_LIMIT * 0.8
                _warn = " ⚠️ NEAR LIMIT" if _near else ""
                print(
                    f"[GEMINI REQUEST COUNT] {current_count} / {GEMINI_DAILY_LIMIT}{_warn}",
                    flush=True
                )
                # 80%到達時に管理者へ1日1回通知
                if _near:
                    _notify_key = get_gemini_usage_key()
                    if _notify_key not in _gemini_near_limit_notified_keys:
                        _gemini_near_limit_notified_keys.add(_notify_key)
                        import threading
                        threading.Thread(
                            target=send_admin_email,
                            args=(
                                f"[るみろぐ] Gemini API使用量が80%に到達しました",
                                f"Gemini API の本日使用量が上限の80%に達しました。\n\n"
                                f"使用回数: {current_count} / {GEMINI_DAILY_LIMIT}\n"
                                f"リセット時刻: 毎日16:00 JST\n\n"
                                f"このまま使用が続くと上限に達し、診断サービスが一時停止されます。\n"
                                f"GEMINI_DAILY_LIMIT の引き上げをご検討ください。"
                            ),
                            daemon=True
                        ).start()

            print(
                f"[GEMINI CALL SUCCESS] model={model} elapsed={elapsed:.1f}s attempt={attempt + 1}/{max_retries}",
                flush=True
            )

            return response

        except TimeoutError as e:
            last_error = e
            print(f"[GEMINI TIMEOUT] {e} attempt={attempt + 1}/{max_retries}", flush=True)
            if attempt >= max_retries - 1:
                raise
            # タイムアウト後は少し待ってからリトライ（指数バックオフ）
            _backoff = 3 * (attempt + 1)
            print(f"[GEMINI TIMEOUT RETRY] waiting {_backoff}s before retry", flush=True)
            time.sleep(_backoff)

        except (errors.ServerError, errors.APIError) as e:
            last_error = e
            msg = str(e)

            print(
                f"[GEMINI RETRY ERROR] attempt={attempt + 1}/{max_retries} error={msg}",
                flush=True
            )

            retryable = any(
                word.lower() in msg.lower()
                for word in retryable_words
            )

            if not retryable:
                raise

            if attempt >= max_retries - 1:
                raise

            retry_after_seconds = None

            retry_match = re.search(
                r"retry in ([0-9\.]+)s",
                msg,
                re.IGNORECASE
            )

            if retry_match:
                try:
                    retry_after_seconds = float(retry_match.group(1))
                except Exception:
                    retry_after_seconds = None

            if retry_after_seconds is not None:
                wait_seconds = min(3, max(1, retry_after_seconds))
            else:
                base_wait = min(2, 1 + attempt)
                jitter = random.uniform(0.2, 0.6)
                wait_seconds = base_wait + jitter

            print(
                "[GEMINI RETRY WAIT]",
                round(wait_seconds, 2),
                "seconds",
                flush=True
            )

            time.sleep(wait_seconds)

        except Exception as e:
            print(
                f"[GEMINI FATAL ERROR] {e}",
                flush=True
            )
            raise

    raise last_error

import stripe
import secrets
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import uuid as _uuid_mod
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import numpy as np
from PIL import Image, ImageOps, ImageFilter
from flask import Flask, render_template, request, jsonify, redirect, make_response, session as flask_session
from google import genai
from google.genai import types
# ==========================================
# Flask初期設定
# ==========================================
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "lumilog-dev-secret-change-in-prod")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=365)
RUMILOG_UID_COOKIE = "lumilog_uid"

@app.after_request
def attach_user_id_cookie(response):
    """セッションに user_id があれば永続クッキーとして付与する"""
    uid = flask_session.get("user_id", "")
    if uid and not request.cookies.get(RUMILOG_UID_COOKIE):
        response.set_cookie(
            RUMILOG_UID_COOKIE,
            uid,
            max_age=365 * 24 * 3600,
            httponly=True,
            samesite="Lax",
        )
    return response

CLICK_LOG_FILE = "product_clicks.json"

# ===== 有料会員設定 =====
ENABLE_SUBSCRIPTION = False  # 決済導入前はFalse
DEV_PREMIUM_MODE = os.getenv("DEV_PREMIUM_MODE", "false").lower() == "true"

# ===== 作成者認証 =====
CREATOR_KEY = os.getenv("CREATOR_KEY", "")
ADMIN_KEY   = os.getenv("ADMIN_KEY", "")

def _creator_token():
    """CREATOR_KEY から一方向トークンを生成する"""
    if not CREATOR_KEY:
        return ""
    return hmac.new(CREATOR_KEY.encode(), b"lumilog_creator", hashlib.sha256).hexdigest()

def is_creator():
    """cookieに有効な作成者トークンがあればTrue"""
    token = _creator_token()
    if not token:
        return False
    return request.cookies.get("creator_token") == token


def log_product_click(source, product_name, category):
    logs = []

    if os.path.exists(CLICK_LOG_FILE):
        try:
            with open(CLICK_LOG_FILE, "r", encoding="utf-8") as f:
                logs = json.load(f)
                if not isinstance(logs, list):
                    logs = []
        except Exception:
            logs = []

    logs.append({
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "product": product_name,
        "category": category,
        "ip": request.remote_addr
    })

    with open(CLICK_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# ==========================================
# 診断履歴データ管理
# results.json を読み書き
# ==========================================
RESULTS_FILE = "results.json"

PRICING_LOG_FILE = "pricing_clicks.json"

def log_pricing_view(source="unknown"):
    data = []

    if os.path.exists(PRICING_LOG_FILE):
        try:
            with open(PRICING_LOG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, list):
                    data = []
        except Exception:
            data = []

    data.append({
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "ip": request.remote_addr
    })

    with open(PRICING_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

FREE_LIMIT_FILE = "free_usage.json"
FREE_MONTHLY_LIMIT = 5
PREMIUM_MONTHLY_LIMIT = 30
GLOBAL_MONTHLY_LIMIT = 1000
GLOBAL_USAGE_FILE = "global_usage.json"

# フォーム選択肢 → 内部 concern タグ
_CONCERN_FORM_TAGS = {
    "acne":    ["acne"],
    "pores":   ["pores", "oil_control"],
    "spots":   ["whitening", "dullness"],
    "aging":   ["aging"],
    "dryness": ["dryness", "barrier"],
    "redness": ["redness", "barrier"],
}
_CONCERN_LABELS_JA = {
    "acne":    "ニキビ・吹き出物",
    "pores":   "毛穴・テカり",
    "spots":   "シミ・くすみ",
    "aging":   "シワ・たるみ",
    "dryness": "乾燥・かさつき",
    "redness": "肌荒れ・赤み",
}

def get_user_concern_tags(user_data):
    tags = []
    for c in (user_data.get("concerns") or []):
        tags.extend(_CONCERN_FORM_TAGS.get(c, []))
    return list(dict.fromkeys(tags))
PREMIUM_KEYS_FILE = "premium_keys.json"

# Stripe初期化
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "") or SMTP_USER
SITE_URL = os.getenv("SITE_URL", "")
# ==========================================
# 商品カテゴリ → 画像ファイル
# ==========================================

def validate_db(product):
    errors = []

    for key, allowed in ALLOWED_TAGS.items():
        if key not in product:
            continue

        value = product[key]

        if isinstance(value, list):
            for v in value:
                if v not in allowed:
                    errors.append(f"{key}: {v} は未定義タグ")

        elif isinstance(value, str):
            if value not in allowed:
                errors.append(f"{key}: {value} は未定義タグ")

    return errors

def auto_fix(product):
    for key, allowed in ALLOWED_TAGS.items():
        if key in product and isinstance(product[key], list):
            product[key] = [v for v in product[key] if v in allowed]
    return product

def get_product_image(category):
    filename = PRODUCT_IMAGES.get(category, "serum.jpg")
    return f"/static/images/products/{filename}"

PRODUCTS_FILE = "products.json"

def get_client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"

def get_or_create_user_id():
    """
    永続クッキーからユーザー固有IDを取得する。
    クッキーがない場合はUUIDを生成してFlaskセッション（永続化）に保存し、
    レスポンス時にクッキーとして付与できるよう flask_session にも記録する。
    """
    # まずクッキーを確認
    uid = request.cookies.get(RUMILOG_UID_COOKIE, "")
    if uid and len(uid) >= 32:
        flask_session["user_id"] = uid
        flask_session.permanent = True
        return uid
    # クッキーがない場合はセッションを確認
    uid = flask_session.get("user_id", "")
    if uid and len(uid) >= 32:
        flask_session.permanent = True
        return uid
    # 新規生成
    uid = str(_uuid_mod.uuid4())
    flask_session["user_id"] = uid
    flask_session.permanent = True
    return uid


# ==========================================
# マジックリンク認証ヘルパー
# ==========================================

def get_or_create_user_for_email(email):
    """メールアドレスに対応するuser_idを返す（なければ新規作成）"""
    conn = None
    cur = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE email = %s", (email,))
        row = cur.fetchone()
        if row:
            return row[0]
        new_uid = str(_uuid_mod.uuid4())
        cur.execute("INSERT INTO users (email, user_id) VALUES (%s, %s)", (email, new_uid))
        conn.commit()
        return new_uid
    except Exception as e:
        if conn: conn.rollback()
        print(f"[AUTH ERROR] get_or_create_user_for_email: {e}", flush=True)
        return None
    finally:
        if cur: cur.close()
        if conn: conn.close()


def create_magic_token(email):
    """マジックトークンを生成してDBに保存し、トークン文字列を返す"""
    token = secrets.token_urlsafe(32)
    conn = None
    cur = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO magic_tokens (token, email, expires_at)
            VALUES (%s, %s, NOW() + INTERVAL '15 minutes')
        """, (token, email))
        conn.commit()
        return token
    except Exception as e:
        if conn: conn.rollback()
        print(f"[AUTH ERROR] create_magic_token: {e}", flush=True)
        return None
    finally:
        if cur: cur.close()
        if conn: conn.close()


def verify_magic_token(token):
    """トークンを検証。有効なら email を返し、使用済みにする。無効なら None。"""
    conn = None
    cur = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            SELECT email FROM magic_tokens
            WHERE token = %s AND used = FALSE AND expires_at > NOW()
        """, (token,))
        row = cur.fetchone()
        if not row:
            return None
        email = row[0]
        cur.execute("UPDATE magic_tokens SET used = TRUE WHERE token = %s", (token,))
        conn.commit()
        return email
    except Exception as e:
        if conn: conn.rollback()
        print(f"[AUTH ERROR] verify_magic_token: {e}", flush=True)
        return None
    finally:
        if cur: cur.close()
        if conn: conn.close()


def migrate_results_to_email_user(old_user_id, new_user_id):
    """旧UUIDの診断結果を新user_idに移行する（デバイス引き継ぎ）"""
    if not old_user_id or not new_user_id or old_user_id == new_user_id:
        return 0
    conn = None
    cur = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            UPDATE results
            SET payload = jsonb_set(payload, '{user_id}', to_jsonb(%s::text))
            WHERE payload->>'user_id' = %s
        """, (new_user_id, old_user_id))
        count = cur.rowcount
        conn.commit()
        print(f"[AUTH MIGRATE] {old_user_id} -> {new_user_id}: {count} records", flush=True)
        return count
    except Exception as e:
        if conn: conn.rollback()
        print(f"[AUTH ERROR] migrate_results: {e}", flush=True)
        return 0
    finally:
        if cur: cur.close()
        if conn: conn.close()


def send_magic_link_email(to_email, token):
    """マジックリンクメールを送信する"""
    if not SMTP_USER or not SMTP_PASSWORD:
        print(f"[MAGIC LINK EMAIL] SMTP未設定 token={token}", flush=True)
        return False
    base_url = SITE_URL.rstrip("/") if SITE_URL else ""
    link = f"{base_url}/auth/{token}"
    subject = "るみろぐ ログインリンク"
    body = f"""るみろぐへのログインリクエストを受け付けました。

以下のリンクをタップしてログインしてください。
このリンクは15分間有効です。

{link}

━━━━━━━━━━━━━━━━━━━━
このメールに心当たりがない場合は無視してください。
るみろぐ
"""
    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls(context=context)
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, to_email, msg.as_string())
        print(f"[MAGIC LINK EMAIL] 送信成功: {to_email}", flush=True)
        return True
    except Exception as e:
        print(f"[MAGIC LINK EMAIL ERROR] {repr(e)}", flush=True)
        return False


# ==========================================
# プレミアムキー管理
# ==========================================

def load_premium_keys():
    if not os.path.exists(PREMIUM_KEYS_FILE):
        return {}
    try:
        with open(PREMIUM_KEYS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def save_premium_keys(data):
    with open(PREMIUM_KEYS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def generate_premium_key():
    return secrets.token_urlsafe(32)

def validate_premium_key(key):
    if not key:
        return False
    keys = load_premium_keys()
    entry = keys.get(key)
    if not entry:
        return False
    if entry.get("revoked", False):
        return False
    valid_until = entry.get("valid_until", "")
    if valid_until:
        try:
            expiry = datetime.fromisoformat(valid_until)
            if datetime.now() > expiry:
                return False
        except Exception:
            return False
    return True

def issue_premium_key(email, stripe_customer_id, stripe_subscription_id):
    keys = load_premium_keys()
    # 既存キーがあれば延長
    for key, entry in keys.items():
        if entry.get("email") == email and not entry.get("revoked", False):
            entry["valid_until"] = (datetime.now() + timedelta(days=35)).isoformat()
            entry["stripe_subscription_id"] = stripe_subscription_id
            save_premium_keys(keys)
            return key
    # 新規発行
    new_key = generate_premium_key()
    keys[new_key] = {
        "email": email,
        "stripe_customer_id": stripe_customer_id,
        "stripe_subscription_id": stripe_subscription_id,
        "valid_until": (datetime.now() + timedelta(days=35)).isoformat(),
        "revoked": False,
        "created_at": datetime.now().isoformat()
    }
    save_premium_keys(keys)
    return new_key

def issue_premium_key_manual(email, days=35):
    """管理者によるStripe不経由の手動発行"""
    keys = load_premium_keys()
    for key, entry in keys.items():
        if entry.get("email") == email and not entry.get("revoked", False):
            entry["valid_until"] = (datetime.now() + timedelta(days=days)).isoformat()
            entry["manual"] = True
            save_premium_keys(keys)
            return key
    new_key = generate_premium_key()
    keys[new_key] = {
        "email": email,
        "stripe_customer_id": None,
        "stripe_subscription_id": None,
        "valid_until": (datetime.now() + timedelta(days=days)).isoformat(),
        "revoked": False,
        "manual": True,
        "created_at": datetime.now().isoformat(),
    }
    save_premium_keys(keys)
    return new_key

def revoke_premium_key_direct(key):
    keys = load_premium_keys()
    if key in keys:
        keys[key]["revoked"] = True
        save_premium_keys(keys)
        return True
    return False

def cleanup_expired_premium_keys():
    """期限切れ・失効済みエントリをpremium_keys.jsonから削除する"""
    try:
        keys = load_premium_keys()
        now_iso = datetime.now().isoformat()
        to_delete = [
            k for k, entry in keys.items()
            if entry.get("revoked", False) or (
                entry.get("valid_until", "") and entry["valid_until"] < now_iso
            )
        ]
        for k in to_delete:
            del keys[k]
        if to_delete:
            save_premium_keys(keys)
            print(f"[CLEANUP] 期限切れキーを{len(to_delete)}件削除しました", flush=True)
    except Exception as e:
        print(f"[CLEANUP ERROR] {repr(e)}", flush=True)

def _start_cleanup_scheduler():
    def loop():
        while True:
            time.sleep(24 * 3600)  # 24時間ごと
            cleanup_expired_premium_keys()
    t = threading.Thread(target=loop, daemon=True)
    t.start()

_start_cleanup_scheduler()


def _enrich_db_products_images():
    """起動時バックグラウンド: 商品名・ブランドでキーワード検索し画像URLを products.json に永続保存する。
    image フィールドが空の商品のみ対象。一度保存すれば次回起動から API 呼び出し不要。
    """
    import time as _time
    _time.sleep(10)  # アプリ起動完了を待つ

    print("[ENRICH DB IMAGES] Start background image enrichment for DB products", flush=True)

    try:
        with open(PRODUCTS_FILE, "r", encoding="utf-8") as _f:
            _raw = json.load(_f)

        if isinstance(_raw, dict):
            _products = (
                _raw.get("skincare_database")
                or _raw.get("products")
                or _raw.get("items")
                or []
            )
        elif isinstance(_raw, list):
            _products = _raw
        else:
            print("[ENRICH DB IMAGES] Unexpected products.json format", flush=True)
            return

        _needs_save = False
        _done = 0
        _failed = 0

        for _p in _products:
            if not isinstance(_p, dict):
                continue
            if str(_p.get("image") or "").strip():
                continue  # 既に画像あり

            _name = str(_p.get("name", "") or "").strip()
            _brand = str(_p.get("brand", "") or "").strip()
            _category = str(_p.get("category", "") or "").strip()

            if not _name:
                _failed += 1
                continue

            # レート制限待ち
            global RAKUTEN_COOLDOWN_UNTIL
            if _time.time() < RAKUTEN_COOLDOWN_UNTIL:
                _wait = RAKUTEN_COOLDOWN_UNTIL - _time.time() + 1
                print(f"[ENRICH DB IMAGES] Rate limit, waiting {_wait:.1f}s", flush=True)
                _time.sleep(_wait)

            # itemCode APIは400を返し続けるため、商品名でキーワード検索する
            _result = fetch_rakuten_item(
                product_name=_name,
                category=_category,
                brand=_brand,
            )

            if _result and _result.get("image"):
                _p["image"] = _result["image"]
                if _result.get("rakuten_link") and not str(_p.get("rakuten_link") or "").strip():
                    _p["rakuten_link"] = _result["rakuten_link"]
                _needs_save = True
                _done += 1
                print(
                    f"[ENRICH DB IMAGES] OK  {_name[:35]:35s} -> {_result['image'][:50]}",
                    flush=True,
                )
            else:
                print(f"[ENRICH DB IMAGES] FAIL {_name[:40]}", flush=True)
                _failed += 1

            _time.sleep(0.5)  # API レート制限への配慮

        if _needs_save:
            with open(PRODUCTS_FILE, "w", encoding="utf-8") as _f:
                json.dump(_raw, _f, ensure_ascii=False, indent=2)
            print(
                f"[ENRICH DB IMAGES] Saved {_done} images to {PRODUCTS_FILE} (failed={_failed})",
                flush=True,
            )
        else:
            print(
                f"[ENRICH DB IMAGES] All products already have images — nothing to save (failed={_failed})",
                flush=True,
            )

    except Exception as _e:
        print(f"[ENRICH DB IMAGES ERROR] {_e}", flush=True)


def _start_image_enrichment():
    # Phase3: 固定DB廃止により不要になったため無効化
    pass


_start_image_enrichment()


def revoke_premium_key_by_subscription(subscription_id):
    keys = load_premium_keys()
    for entry in keys.values():
        if entry.get("stripe_subscription_id") == subscription_id:
            entry["revoked"] = True
    save_premium_keys(keys)

def send_premium_email(to_email, key):
    if not SMTP_USER or not SMTP_PASSWORD:
        print(f"[EMAIL] SMTP未設定 key={key}", flush=True)
        return False
    base_url = SITE_URL.rstrip("/") if SITE_URL else ""
    premium_url = f"{base_url}/lab?premium_key={key}"
    subject = "るみろぐ プレミアムプランへようこそ！"
    body = f"""プレミアムプランへのご登録ありがとうございます。

以下のリンクからプレミアム機能をご利用いただけます。
ブックマークして毎回ご利用ください。

{premium_url}

━━━━━━━━━━━━━━━━━━━━
このリンクはあなた専用です。第三者と共有しないでください。
サブスクリプションが有効な限り、毎月自動で延長されます。
━━━━━━━━━━━━━━━━━━━━
るみろぐ サポートチーム
"""
    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls(context=context)
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, to_email, msg.as_string())
        print(f"[EMAIL] 送信成功: {to_email}", flush=True)
        return True
    except Exception as e:
        print(f"[EMAIL ERROR] {repr(e)}", flush=True)
        return False

def send_admin_email(subject, body):
    """管理者メールアドレスに通知メールを送信する"""
    if not SMTP_USER or not SMTP_PASSWORD or not ADMIN_EMAIL:
        print(f"[ADMIN EMAIL] SMTP未設定のため送信スキップ subject={subject}", flush=True)
        return False
    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = ADMIN_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls(context=context)
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, ADMIN_EMAIL, msg.as_string())
        print(f"[ADMIN EMAIL] 送信成功: {ADMIN_EMAIL} subject={subject}", flush=True)
        return True
    except Exception as e:
        print(f"[ADMIN EMAIL ERROR] {repr(e)}", flush=True)
        return False


# Gemini 80%通知の送信済みキーを記録（1日1回のみ通知）
_gemini_near_limit_notified_keys: set = set()


def is_premium_user():
    """
    有料会員判定をここに集約する。
    将来、ログイン・決済・DB管理に移行しても、
    各画面側のコードは変更しない。
    """
    if DEV_PREMIUM_MODE:
        return True
    if is_creator():
        return True

    premium_key = request.args.get("premium_key", "")

    # Stripe発行キーの検証
    if validate_premium_key(premium_key):
        return True

    # 旧プレビューキー（後方互換）
    valid_key = os.getenv("PREMIUM_PREVIEW_KEY", "")
    if valid_key and premium_key == valid_key:
        return True

    return False

def load_free_usage():
    if not os.path.exists(FREE_LIMIT_FILE):
        return {}

    try:
        with open(FREE_LIMIT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, FileNotFoundError):
        return {}

def load_global_usage():
    if not os.path.exists(GLOBAL_USAGE_FILE):
        return {}

    try:
        with open(GLOBAL_USAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_global_usage(data):
    with open(GLOBAL_USAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_global_usage_count():
    data = load_global_usage()
    month_key = get_current_month_key()
    record = data.get(month_key, {})
    return int(record.get("count", 0))


def can_use_global_diagnosis():
    return get_global_usage_count() < GLOBAL_MONTHLY_LIMIT


def increment_global_usage():
    data = load_global_usage()
    month_key = get_current_month_key()

    if month_key not in data:
        data[month_key] = {"count": 1}
    else:
        data[month_key]["count"] = int(data[month_key].get("count", 0)) + 1

    save_global_usage(data)
    return data[month_key]["count"]

def save_free_usage(data):
    with open(FREE_LIMIT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_current_month_key():
    return date.today().strftime("%Y-%m")


def get_free_usage_count(ip):
    data = load_free_usage()
    month_key = get_current_month_key()

    if ip not in data:
        return 0

    record = data.get(ip, {})
    if record.get("month") != month_key:
        return 0

    return int(record.get("count", 0))


def increment_free_usage(ip):
    if DISABLE_USAGE_LIMIT:
        return 0

    data = load_free_usage()
    month_key = get_current_month_key()

    if ip not in data or data[ip].get("month") != month_key:
        data[ip] = {"month": month_key, "count": 1}
    else:
        data[ip]["count"] = int(data[ip].get("count", 0)) + 1

    save_free_usage(data)
    return data[ip]["count"]


def can_use_free_diagnosis(ip):
    used_count = get_free_usage_count(ip)
    return used_count < FREE_MONTHLY_LIMIT


def get_remaining_free_count(ip):
    if DISABLE_USAGE_LIMIT:
        return 999

    used_count = get_free_usage_count(ip)
    remaining = FREE_MONTHLY_LIMIT - used_count
    return max(0, remaining)


def get_premium_usage_count(key):
    keys = load_premium_keys()
    entry = keys.get(key)
    if not entry:
        return 0
    month_key = get_current_month_key()
    return int(entry.get("monthly_usage", {}).get(month_key, 0))


def increment_premium_usage(key):
    if DISABLE_USAGE_LIMIT:
        return 0
    keys = load_premium_keys()
    entry = keys.get(key)
    if not entry:
        return 0
    if entry.get("revoked", False):
        return 0
    valid_until = entry.get("valid_until", "")
    if valid_until:
        try:
            if datetime.now() > datetime.fromisoformat(valid_until):
                return 0
        except Exception:
            return 0
    month_key = get_current_month_key()
    monthly = entry.setdefault("monthly_usage", {})
    monthly[month_key] = int(monthly.get(month_key, 0)) + 1
    save_premium_keys(keys)
    return monthly[month_key]


def can_use_premium_diagnosis(key):
    return get_premium_usage_count(key) < PREMIUM_MONTHLY_LIMIT


def get_remaining_premium_count(key):
    if DISABLE_USAGE_LIMIT:
        return 999
    return max(0, PREMIUM_MONTHLY_LIMIT - get_premium_usage_count(key))


def load_products():
    with open("products.json", "r", encoding="utf-8") as f:
        raw_data = json.load(f)

    if isinstance(raw_data, list):
        products = raw_data

    elif isinstance(raw_data, dict):
        if isinstance(raw_data.get("skincare_database"), list):
            products = raw_data["skincare_database"]
        elif isinstance(raw_data.get("products"), list):
            products = raw_data["products"]
        elif isinstance(raw_data.get("items"), list):
            products = raw_data["items"]
        else:
            products = []

    else:
        products = []

    products = [p for p in products if isinstance(p, dict)]

    for p in products:
        # price_ref / raw_price 正規化
        p["price_ref"] = safe_price(
            p.get("price_ref")
            or p.get("normalized_price")
            or p.get("itemPrice")
            or p.get("price")
            or 0
        )

        p["raw_price"] = safe_price(
            p.get("raw_price")
            or p.get("itemPrice")
            or p.get("price_ref")
            or p.get("price")
            or 0
        )

        # category 正規化
        category = p.get("category", "")
        category_aliases = {
            "導入美容液": "美容液",
            "ブースター": "美容液",
            "導入液": "美容液",
            "シートマスク": "パック",
            "フェイスマスク": "パック",
            "UV": "日焼け止め",
            "日焼け止めクリーム": "日焼け止め",
        }

        category = category_aliases.get(category, category)

        p["category"] = normalize_candidate_category(
            AI_CATEGORY_MAP.get(str(category).lower(), category),
            fallback=category
        )

        # concerns 正規化
        concerns = p.get("concerns", [])
        if not isinstance(concerns, list):
            concerns = []

        concern_aliases = {
            "sensitive": "barrier",
        }

        new_concerns = []
        for c in concerns:
            c = concern_aliases.get(c, c)
            mapped = CONCERN_MAP.get(c, c)
            if mapped is None:
                continue
            new_concerns.append(mapped)

        p["concerns"] = list(dict.fromkeys(new_concerns))

        # main_functions 正規化
        main_functions = p.get("main_functions", [])
        if not isinstance(main_functions, list):
            main_functions = []

        main_function_aliases = {
            "メイク除去": "メイク落とし",
            "うるおい保持洗浄": "うるおいを守って洗う",
            "低刺激洗浄": "うるおいを守って洗う",
            "皮脂汚れ除去": "皮脂汚れオフ",
            "黒ずみ除去": "黒ずみ予防",
            "角質ケア": "キメ改善",
            "くすみ除去": "透明感向上",
            "くすみ改善": "透明感向上",
            "赤みケア": "鎮静ケア",
            "ニキビケア": "ニキビ予防",
            "皮脂バランス調整": "皮脂抑制",
            "毛穴ケア": "毛穴改善",
            "色素沈着ケア": "美白ケア",
            "ニキビ跡ケア": "透明感向上",
            "シワ改善": "エイジングケア",
            "ツヤ改善": "透明感向上",
            "ツヤ付与": "透明感向上",
            "低刺激ケア": "鎮静ケア",
            "浸透保湿": "保湿",
            "保護": "バリア強化",
            "毎日使いやすい": "うるおいを守って洗う",
            "毛穴詰まり予防": "毛穴詰まり予防",
            "洗いすぎ防止": "洗いすぎ防止",
        }

        new_main_functions = []
        for mf in main_functions:
            mapped = MAIN_FUNCTION_MAP.get(mf, mf)
            mapped = main_function_aliases.get(mapped, mapped)

            if mapped in MAIN_FUNCTION_TAGS:
                new_main_functions.append(mapped)

        p["main_functions"] = list(dict.fromkeys(new_main_functions))

    print("[PRODUCTS AFTER LOAD]", len(products), flush=True)

    return products

AFFILIATE_LINKS_AI_FILE = "affiliate_links_ai.json"
def load_affiliate_links_ai():
    if not os.path.exists(AFFILIATE_LINKS_AI_FILE):
        return []

    try:
        with open(AFFILIATE_LINKS_AI_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, FileNotFoundError):
        return []

def normalize_affiliate_text(text):
    if not text:
        return ""

    text = str(text).strip().lower()
    text = text.replace("　", " ")
    text = text.replace("・", "")
    text = text.replace("-", "")
    text = text.replace("（", "")
    text = text.replace("）", "")
    text = text.replace("(", "")
    text = text.replace(")", "")
    text = text.replace("  ", " ")
    return text

def build_amazon_link(name):
    if not name:
        return "#"
    url = "https://www.amazon.co.jp/s?k=" + urllib.parse.quote(name)
    if AMAZON_ASSOCIATE_TAG:
        url += "&tag=" + urllib.parse.quote(AMAZON_ASSOCIATE_TAG)
    return url

def build_rakuten_link(name):
    if not name:
        return "#"

    if not RAKUTEN_AFFILIATE_ID:
        return "https://search.rakuten.co.jp/search/mall/" + urllib.parse.quote(name)

    return (
        f"https://hb.afl.rakuten.co.jp/hgc/{RAKUTEN_AFFILIATE_ID}/"
        f"?pc=https://search.rakuten.co.jp/search/mall/{urllib.parse.quote(name)}"
    )
def build_rakuten_keywords(product_name, brand=""):
    name = clean_rakuten_keyword(product_name)
    brand = clean_rakuten_keyword(brand)

    keywords = []

    if name:
        keywords.append(name)

    compact = name.replace("　", " ").replace("・", " ")
    compact = " ".join(compact.split())

    if compact and compact not in keywords:
        keywords.append(compact)

    parts = compact.split()

    if len(parts) >= 2:
        keywords.append(" ".join(parts[:2]))

    return list(dict.fromkeys(keywords))[:3]

def clean_rakuten_image_url(url):
    if not url:
        return ""
    url = str(url).strip()
    if url.startswith("//"):
        url = "https:" + url
    return url



_last_rakuten_request_time = 0
_rakuten_rate_lock = threading.Lock()
_rakuten_call_count_lock = threading.Lock()


def _mem_mb() -> float:
    """/proc/self/status から RSS (MB) を返す。Linux以外は resource モジュールで代替。"""
    try:
        with open("/proc/self/status") as _f:
            for _line in _f:
                if _line.startswith("VmRSS:"):
                    return int(_line.split()[1]) / 1024  # kB → MB
    except Exception:
        pass
    try:
        import resource as _resource
        return _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:
        return -1.0


def _log_mem(label: str) -> float:
    """メモリ使用量を [MEM] プレフィックスで出力し MB 値を返す。"""
    mb = _mem_mb()
    print(f"[MEM] {label}: {mb:.1f}MB", flush=True)
    return mb

# Gemini評価キャッシュ用ロック（並列Phase B対応）
import threading as _threading_mod
_gemini_eval_cache_lock = _threading_mod.Lock()

RAKUTEN_RATE_GAP = 1.1  # 429対策で0.8s→1.1sに延長

def wait_for_rakuten_rate_limit():
    """スレッドセーフなレートリミッター。
    スロットを事前予約してからロック外でスリープすることで、
    複数スレッドのHTTPリクエストが並列に重なれる。
    """
    global _last_rakuten_request_time
    with _rakuten_rate_lock:
        now = time.time()
        next_allowed = _last_rakuten_request_time + RAKUTEN_RATE_GAP
        sleep_secs = max(0.0, next_allowed - now)
        _last_rakuten_request_time = max(now, next_allowed)
    if sleep_secs > 0:
        time.sleep(sleep_secs)

    
def build_rakuten_search_keywords(product_name, brand="", category="", ingredient_focus="", purpose=""):
    name = clean_rakuten_keyword(product_name)
    brand = clean_rakuten_keyword(brand)

    # サプリメント専用キーワード生成
    # 「ナイアシンアミド サプリ」+「DHC」→ Rakutenが「DHC サプリフィットマスク」を返す誤ヒットを防ぐ。
    # 成分名から「サプリ」接尾語を除去し「サプリメント」で検索する。
    if category == "サプリメント":
        _supp_name = name
        for _sfx in ("サプリメント", "サプリ", "supplement"):
            if _supp_name.lower().endswith(_sfx.lower()):
                _supp_name = _supp_name[:-len(_sfx)].strip()
                break
        _supp_kws = []
        if brand and _supp_name and not _supp_name.lower().startswith(brand.lower()):
            _supp_kws.append(clean_rakuten_keyword(f"{brand} {_supp_name} サプリメント"))
        if _supp_name:
            _supp_kws.append(clean_rakuten_keyword(f"{_supp_name} サプリメント"))
        # ブランドなし・成分名のみもフォールバックに追加
        if _supp_name:
            _supp_kws.append(clean_rakuten_keyword(_supp_name))
        print("[RAKUTEN KEYWORDS]", _supp_kws, flush=True)
        return [k for k in _supp_kws if k]

    keywords = []

    def add(value):
        value = clean_rakuten_keyword(value)
        if value and value not in keywords:
            keywords.append(value)

    if not name:
        return []

    is_high_risk_ai_name = any(
        word in name
        for word in OLD_PRODUCT_WORDS
    )

    if is_high_risk_ai_name:
        return []

    if brand and name and not name.lower().startswith(brand.lower()):
        add(f"{brand} {name}")

    add(name)

    parts = name.split()

    meaningful_parts = [
        p for p in parts
        if p not in {"ザ", "the", "THE"}
    ]

    # brand + meaningful_parts は name が brand で始まらない場合のみ追加
    # （始まる場合は「brand brand name」という冗長キーワードになるため）
    if brand and meaningful_parts and not name.lower().startswith(brand.lower()):
        add(f"{brand} {' '.join(meaningful_parts)}")

    if meaningful_parts:
        add(" ".join(meaningful_parts))

    if brand and len(meaningful_parts) >= 2:
        add(f"{brand} {' '.join(meaningful_parts[-2:])}")

    if len(meaningful_parts) >= 2:
        add(" ".join(meaningful_parts[-2:]))

    # 英字のみの長いキーワードは楽天APIが拒否する場合がある
    # → 独立した数字トークンを除いた短縮版を追加してフォールバックを確保する
    def _strip_number_tokens(k):
        return " ".join(p for p in k.split() if not p.isdigit()).strip()

    for kw in list(keywords):
        if len(kw) > 25 and all(ord(c) < 128 or c == " " for c in kw):
            _short = _strip_number_tokens(kw)
            if _short and _short != kw and len(_short) >= 4:
                add(_short)

    print("[RAKUTEN KEYWORDS]", keywords, flush=True)

    return keywords[:6]

def score_current_product_signal(item):
    if not isinstance(item, dict):
        return 0

    name = str(item.get("itemName", ""))
    shop = str(item.get("shopName", ""))

    text = f"{name} {shop}"

    score = 0

    if any(word in text for word in OLD_PRODUCT_WORDS):
        score -= 80

    if any(word in text for word in CURRENT_PRODUCT_WORDS):
        score += 20

    if item.get("mediumImageUrls") or item.get("smallImageUrls"):
        score += 18
    else:
        score -= 20

    review_count = safe_int(item.get("reviewCount", 0))

    if review_count >= 50:
        score += 8

    if review_count >= 200:
        score += 12

    if "公式" in shop:
        score += 25

    return score
OLD_PRODUCT_WORDS = [
    "旧",
    "旧品",
    "旧商品",
    "旧パッケージ",
    "リニューアル前",
    "廃盤",
    "生産終了",
    "在庫処分",
    "訳あり",
    "アウトレット",
    "箱なし",
    "外箱なし"
]
CURRENT_PRODUCT_WORDS = [
    "新",
    "新商品",
    "リニューアル",
    "現行",
    "最新",
    "正規品",
    "公式",
    "本体",
    "医薬部外品"
]
def extract_product_identity_tokens(text):
    text = str(text or "").lower()

    percents = set(
        re.findall(r"\d+(?:\.\d+)?\s*%", text)
    )

    volumes = set(
        re.findall(r"\d+(?:\.\d+)?\s*(?:ml|g|個|本|枚)", text)
    )

    normalized = clean_rakuten_keyword(text).lower()
    words = set(normalized.split())

    return {
        "percents": percents,
        "volumes": volumes,
        "words": words
    }


def is_same_product_for_market(ai_name, rakuten_title):
    ai = extract_product_identity_tokens(ai_name)
    rk = extract_product_identity_tokens(rakuten_title)

    # 濃度が両方にあるのに違う場合は別商品
    if ai["percents"] and rk["percents"]:
        if ai["percents"] != rk["percents"]:
            return False

    # 容量が両方にあるのに違う場合は別商品
    if ai["volumes"] and rk["volumes"]:
        if ai["volumes"] != rk["volumes"]:
            return False

    ai_words = ai["words"]
    rk_words = rk["words"]

    if not ai_words:
        return False

    matched = ai_words & rk_words
    match_ratio = len(matched) / max(len(ai_words), 1)

    return match_ratio >= 0.45

def score_rakuten_item(item, product_name, brand="", category=""):
    title = str(item.get("itemName", "") or "")
    title_norm = title.lower()

    name = clean_rakuten_keyword(product_name).lower()
    brand = clean_rakuten_keyword(brand).lower()

    category = normalize_candidate_category(
        category,
        fallback=category
    )

    if not title or not name:
        return -9999

    # セット・まとめ買い商品は単品より大幅に低スコアにする（単品がなければセットを選ぶ）
    _SET_PENALTY_KEYWORDS = ["セット", "まとめ買い", "トライアルセット", "お試しセット"]
    _is_set_product = any(kw in title for kw in _SET_PENALTY_KEYWORDS)


    def compact_text(value):
        return re.sub(
            r"[\s　・_\-ー/／\(\)（）\[\]【】+＋\.。,:：,]",
            "",
            str(value).lower()
        )

    title_compact = compact_text(title)
    name_compact = compact_text(name)

    name_tokens = [
        compact_text(t)
        for t in re.split(r"[\s　・_\-ー/／\(\)（）\[\]【】+＋\.。,:：,]", name)
        if compact_text(t)
    ]

    important_tokens = [
        t for t in name_tokens
        if len(t) >= 2 and t not in {"the", "rx", "neo", "ex", "n"}
    ]

    exact_name_match = bool(name_compact and name_compact in title_compact)

    matched_tokens = [
        t for t in important_tokens
        if t in title_compact
    ]

    if exact_name_match:
        name_match_score = 90
    else:
        required_matches = 1 if len(important_tokens) <= 2 else 2

        if len(matched_tokens) < required_matches:
            # 美容機器: Geminiが「イオン導入器」等のカテゴリ説明名を出すため名称一致免除
            if category == "美容機器":
                name_match_score = 10
            # サプリメント: 成分名は通常タイトルに含まれるが品番・ブランド接頭辞で
            # 一致しない場合もあるため低スコアで通過させる
            elif category == "サプリメント":
                name_match_score = 8
            # ピーリング・パック: Gemini日本語名 vs Rakuten英語名の乖離が多いため
            # カテゴリ固有キーワードチェック(_peel_required等)に一致を委ねる
            elif category in ("ピーリング", "パック"):
                name_match_score = 8
            else:
                return -9999
        else:
            name_match_score = 35 + (len(matched_tokens) * 12)

    hard_reject_words = [
        "詰替",
        "詰め替え",
        "つめかえ",
        "レフィル",
        "付け替え",
        "つけかえ",
        "お試し",
        "サンプル",
        "ミニサイズ",
        "トライアル",
        "まとめ買い",
        "2個",
        "3個",
        "4個",
        "5個",
        "6個",
        "2本",
        "3本",
        "4本",
        "5本",
        "6本",
        "中古",
        "廃盤",
        "廃番",
        "生産終了",
        "販売終了",
        "製造終了",
        # 抱き合わせ・同梱専用商品（単品購入不可）
        "他の商品と一緒",
        "一緒買い専用",
        "この商品のみのご購入は不可",
        "同梱専用",
        "セット購入専用",
        "まとめ購入専用",
        "単品購入不可",
        "単品での購入不可",
    ]

    if any(word in title for word in hard_reject_words):
        return -9999

    if infer_bundle_quantity_from_title(title) > 1:
        return -9999

    if category == "美容液":
        _serum_wrong = [
            "化粧水", "ローション", "トナー", "toner", "lotion",
            "乳液", "ミルク", "クリーム", "ジェルクリーム",
            "オールインワン", "シートマスク", "フェイスマスク", "薬用マスク", "パック"
        ]
        if any(word in title for word in _serum_wrong):
            return -9999
        _serum_required = ["美容液", "セラム", "エッセンス", "アンプル", "serum", "essence", "ampoule"]
        if not any(w.lower() in title_norm or w in title for w in _serum_required):
            return -9999

    elif category == "クリーム":
        cream_required_words = [
            "クリーム",
            "cream",
            "バーム",
            "balm",
            "モイスチャー",
            "moisture",
            "保湿"
        ]

        cream_wrong_words = [
            "化粧水",
            "ローション",
            "トナー",
            "toner",
            "lotion",
            "シートマスク",
            "フェイスマスク",
            "薬用マスク",
            "パック",
            "洗顔",
            "クレンジング"
        ]

        if any(word in title for word in cream_wrong_words):
            return -9999

        if not any(word in title_norm or word in title for word in cream_required_words):
            if any(word in title for word in ["美容液", "セラム"]):
                return -9999
            score_category_penalty = -12
        else:
            score_category_penalty = 0

    elif category == "日焼け止め":
        if any(word in title for word in ["シートマスク", "フェイスマスク", "薬用マスク", "パック"]):
            return -9999

        if not any(word in title for word in ["日焼け止め", "UV", "uv", "SPF", "spf", "PA", "pa", "サンスクリーン"]):
            return -9999

    elif category == "パック":
        pack_words = [
            "パック",
            "マスク",
            "シートマスク",
            "フェイスマスク",
            "フェイスパック",
            "sheet mask",
            "face mask",
            "mask"
        ]

        if not any(word in title for word in pack_words):
            return -9999

        hard_wrong_pack_words = [
            "洗顔",
            "クレンジング",
            "メイク落とし",
            "日焼け止め",
            "サンスクリーン"
        ]

        if any(word in title for word in hard_wrong_pack_words):
            return -9999

        score_category_penalty = 0

        if any(word in title for word in ["化粧水", "ローション", "トナー"]):
            score_category_penalty = -10

    elif category == "化粧水":
        if any(word in title for word in ["クリーム", "乳液", "ミルク", "美容液", "セラム", "シートマスク", "フェイスマスク", "パック"]):
            return -9999

    elif category == "ピーリング":
        # クレンジング・洗顔・保湿系はピーリングカテゴリから除外
        _peel_wrong = [
            "クレンジング", "メイク落とし", "クレンジングオイル", "クレンジングミルク",
            "クレンジングジェル", "クレンジングバーム", "クレンジングクリーム",
            "クレンジングフォーム", "ダブル洗顔不要",
            "乳液", "ミルク", "クリーム", "保湿クリーム",
            "化粧水", "ローション",
            "シートマスク", "フェイスマスク", "パック",
        ]
        if any(word in title for word in _peel_wrong):
            return -9999
        # ピーリング固有キーワードが1つもなければ除外
        _peel_required = [
            "ピーリング", "peel", "aha", "bha", "pha", "lha",
            "スクラブ", "scrub", "exfoliant", "exfoliating",
            "ゴマージュ", "角質ケア", "角質除去", "角質", "酵素洗顔", "酵素",
            "グリコール酸", "乳酸", "サリチル酸",
        ]
        if not any(w in title_norm or w in title for w in _peel_required):
            return -9999

    elif category == "乳液":
        # 乳液カテゴリ: 乳液/ミルク/エマルジョン系キーワードが必須
        _emulsion_required = ["乳液", "ミルク", "エマルジョン", "emulsion", "milk", "moisturizer"]
        _emulsion_wrong = ["化粧水", "ローション", "トナー", "洗顔", "クレンジング",
                           "日焼け止め", "パック", "シートマスク"]
        if any(w in title for w in _emulsion_wrong):
            return -9999
        if not any(w.lower() in title_norm or w in title for w in _emulsion_required):
            return -9999

    elif category == "洗顔":
        # 洗顔カテゴリ: 明確なクレンジング専用品・パック系のみ除外（クリーム洗顔等は許容）
        _sengan_wrong = [
            "クレンジングオイル", "クレンジングバーム", "クレンジングミルク",
            "クレンジングクリーム", "メイク落とし専用",
            "シートマスク", "フェイスマスク", "パック",
        ]
        if any(word in title for word in _sengan_wrong):
            return -9999

    elif category == "クレンジング":
        # クレンジングカテゴリ: シートマスク・パック系のみ除外
        _cleansing_wrong = ["シートマスク", "フェイスマスク", "パック"]
        if any(word in title for word in _cleansing_wrong):
            return -9999

    elif category == "サプリメント":
        # スキンケア・美容液・化粧品がヒットしないよう除外
        _supp_skincare_words = [
            "化粧水", "美容液", "クリーム", "乳液", "セラム", "ローション",
            "洗顔", "クレンジング", "日焼け止め", "パック", "マスク",
            "美容機器", "美顔器", "スチーマー",
        ]
        if any(word in title for word in _supp_skincare_words):
            return -9999
        # サプリらしいキーワードが1つもなければ低スコア
        _supp_ok_words = [
            "サプリ", "supplement", "錠", "粒", "カプセル", "mg", "μg", "mcg",
            "iu", "IU", "栄養", "ビタミン", "ミネラル", "アミノ酸", "コラーゲン",
            "プロテイン", "酵素", "乳酸菌", "食品", "飲料", "健康",
        ]
        if not any(w.lower() in title_norm or w in title for w in _supp_ok_words):
            score_category_penalty = -40
        else:
            score_category_penalty = 0

    elif category == "美容機器":
        # スキンケア商品・サプリがヒットしないよう除外
        _dev_skincare_words = [
            "化粧水", "美容液", "クリーム", "乳液", "セラム", "ローション",
            "洗顔", "クレンジング", "日焼け止め", "パック", "マスク",
            "サプリ", "supplement", "錠", "粒", "カプセル",
        ]
        if any(word in title for word in _dev_skincare_words):
            return -9999
        # 美容機器らしいキーワードがなければ低スコア
        _dev_ok_words = [
            "美顔器", "led", "LED", "ems", "EMS", "超音波", "イオン",
            "マッサージ", "スチーマー", "フェイスケア", "美容器", "美容機器",
            "器具", "機器", "デバイス", "device", "美容家電",
        ]
        if not any(w.lower() in title_norm or w in title for w in _dev_ok_words):
            score_category_penalty = -40
        else:
            score_category_penalty = 0

    else:
        score_category_penalty = 0

    score = name_match_score
    score += locals().get("score_category_penalty", 0)

    if brand:
        brand_compact = compact_text(brand)
        if brand_compact and brand_compact in title_compact:
            # サプリ・美容機器はブランド指定が商品選定の主軸のため加点を強化
            score += 60 if category in ("サプリメント", "美容機器") else 25
        elif brand_compact and category == "美容機器":
            # 美容機器のみブランド不一致に強ペナルティ（サプリはローマ字/カタカナ
            # 表記ゆれで誤ペナルティが起きるため除外、pre-filterも外しスコアに委ねる）
            score -= 70

    if any(word in title for word in CURRENT_PRODUCT_WORDS):
        score += 15

    soft_risk_words = [
        "旧品",
        "旧型",
        "旧モデル",
        "旧パッケージ",
        "旧処方",
        "リニューアル前",
        "在庫限り",
        "アウトレット",
        "訳あり",
        "箱なし",
        "外箱なし",
        "パッケージ不良",
        "期限間近",
        "使用期限間近",
        "並行輸入",
        "海外発送",
    ]

    for word in soft_risk_words:
        if word in title:
            score -= 45

    price = safe_price(item.get("itemPrice", 0))

    if price <= 0:
        score -= 30
    elif price >= 12000:
        score -= 20
    elif price >= 8000:
        score -= 12

    if item.get("mediumImageUrls"):
        score += 10

    if item.get("affiliateUrl"):
        score += 18
    elif item.get("itemUrl"):
        score += 5

    review_average = safe_float(item.get("reviewAverage", 0))
    review_count = safe_int(item.get("reviewCount", 0))

    if review_average >= 4.6:
        score += 12
    elif review_average >= 4.3:
        score += 8
    elif review_average >= 4.0:
        score += 4
    elif 0 < review_average < 3.7:
        score -= 18

    if review_count >= 5000:
        review_score = 60
    elif review_count >= 3000:
        review_score = 50
    elif review_count >= 1000:
        review_score = 40
    elif review_count >= 500:
        review_score = 30
    elif review_count >= 300:
        review_score = 22
    elif review_count >= 100:
        review_score = 14
    elif review_count >= 30:
        review_score = 6
    elif review_count > 0:
        review_score = 1
    else:
        review_score = 0

    # サプリ・美容機器は商品名が汎用的なため名称一致スコアが拮抗しやすい。
    # レビュー数を主たる優劣基準にするため重みを2倍にする。
    if category in ("サプリメント", "美容機器"):
        review_score = int(review_score * 2)

    score += review_score

    if any(word in title for word in ["公式", "正規品", "正規販売店", "認定ショップ", "メーカー公式"]):
        score += 12

    score += score_current_product_signal(item)

    return score

def save_rakuten_cache(cache):
    try:
        with open(RAKUTEN_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[RAKUTEN CACHE SAVE ERROR]", e, flush=True)


def make_rakuten_cache_key(product_name, brand=""):
    return f"{brand}::{product_name}".strip()

def clean_ai_product_name(name):
    if not isinstance(name, str):
        return ""

    text = name.strip()

    remove_tokens = [
        "name",
        "brand",
        "confidence",
        "category",
        "score",
        "product",
    ]

    parts = text.split()
    cleaned_parts = []

    skip_next = False

    for part in parts:
        if skip_next:
            skip_next = False
            continue

        lower = part.lower()

        if lower in remove_tokens:
            skip_next = True
            continue

        if lower.isdigit():
            continue

        cleaned_parts.append(part)

    return " ".join(cleaned_parts).strip()

def infer_bundle_quantity_from_title(title):
    if not title:
        return 1

    text = str(title)
    text = text.replace("　", " ")

    set_patterns = [
        r"(\d+)\s*個\s*セット",
        r"(\d+)\s*本\s*セット",
        r"(\d+)\s*枚\s*セット",
        r"(\d+)\s*袋\s*セット",
        r"(\d+)\s*箱\s*セット",
        r"(\d+)\s*点\s*セット",   # 「2点セット」「3点セット」
        r"(\d+)\s*種\s*セット",   # 「3種セット」
        r"(\d+)\s*色\s*セット",   # 「2色セット」
        r"(\d+)\s*個組",
        r"(\d+)\s*本組",
        r"(\d+)\s*枚組",
        r"(\d+)\s*個\s*まとめ",
        r"(\d+)\s*本\s*まとめ",
        r"(\d+)\s*個\s*入り\s*セット",
        r"(\d+)\s*本\s*入り\s*セット",
    ]

    for pattern in set_patterns:
        match = re.search(pattern, text)
        if not match:
            continue

        qty = int(match.group(1))
        if 2 <= qty <= 24:
            return qty

    return 1


# セット販売・まとめ買いを示すキーワードパターン
# （infer_bundle_quantity_from_title で拾えない単体キーワードを補完）
_SET_PRODUCT_RE = re.compile(
    r"まとめ買い|まとめ購入|セット品|セット販売|お得セット|詰め合わせ"
    r"|同梱専用|セット購入専用|まとめ購入専用|単品購入不可",
    re.IGNORECASE,
)


def _is_set_product_name(name: str) -> bool:
    """商品名がセット販売・まとめ買い商品と判定できる場合 True を返す。
    infer_bundle_quantity_from_title（数字+個/本+セット 等）と
    キーワードパターンの両方でチェックする。
    """
    if not name:
        return False
    if _SET_PRODUCT_RE.search(name):
        return True
    if infer_bundle_quantity_from_title(name) > 1:
        return True
    return False


# 楽天アイテムタイトルがセット販売商品かどうかを判定するパターン
_RAKUTEN_SET_TITLE_RE = re.compile(
    r"スキンケアセット|化粧品セット|ケアセット|基礎セット|基本セット"
    r"|スペシャルセット|ギフトセット|トライアルセット|入門セット"
    r"|まとめセット|セット品|セット商品|スタートセット|始めるセット"
    r"|お試しセット|初回セット|導入セット",
    re.IGNORECASE,
)

def _is_rakuten_set_item(title: str) -> bool:
    """楽天アイテムタイトルがセット販売かどうか判定。
    hard_reject_words（score_rakuten_item内）が捕捉できないセットパターンを補完する。
    """
    if not title:
        return False
    if _RAKUTEN_SET_TITLE_RE.search(title):
        return True
    # ○点セット / ○種セット 等（infer_bundle_quantity_from_titleで追加済みだが念のため）
    if infer_bundle_quantity_from_title(title) > 1:
        return True
    return False


def normalize_rakuten_item_price(item):
    if not isinstance(item, dict):
        return item

    title = (
        item.get("itemName")
        or item.get("name")
        or item.get("productName")
        or ""
    )

    raw_price = (
        item.get("itemPrice")
        or item.get("price_ref")
        or item.get("price")
        or item.get("estimated_price")
        or 0
    )

    raw_price = safe_price(raw_price)
    if raw_price <= 0:
        return item

    quantity = infer_bundle_quantity_from_title(title)

    normalized_price = raw_price
    if quantity > 1:
        normalized_price = round(raw_price / quantity)

    item["raw_price"] = raw_price
    item["bundle_quantity"] = quantity
    item["normalized_price"] = normalized_price
    item["price_ref"] = normalized_price

    return item

def clean_rakuten_keyword(keyword):
    if isinstance(keyword, list):
        keyword = " ".join(str(x) for x in keyword if str(x).strip())

    if not isinstance(keyword, str):
        return ""

    keyword = keyword.strip()
    keyword = keyword.replace("\n", " ")
    keyword = keyword.replace("\r", " ")
    keyword = keyword.replace("　", " ")

    keyword = re.sub(r"\buv\b", "UV", keyword, flags=re.IGNORECASE)

    replace_to_space = [
        "＋", "+", "/", "／", "&", "＆", "・", "_", "-", "－",
        "(", ")", "（", "）", "[", "]", "【", "】", "{", "}",
        "'", "'", '"', "“", "”", "%", "％"
    ]

    for ch in replace_to_space:
        keyword = keyword.replace(ch, " ")

    keyword = re.sub(
        r"[^A-Za-z0-9\sぁ-んァ-ヶ一-龥ー]",
        " ",
        keyword
    )

    keyword = re.sub(r"\s+", " ", keyword).strip()

    remove_tokens = {
        "ザ",
        "the",
        "THE",
    }

    parts = [
        p for p in keyword.split()
        if p and p not in remove_tokens
    ]

    # 単独1桁の数字トークン（例: "6" in "COSRX RX 6 ペプチド"）はRakutenが400を返すため除去
    parts = [p for p in parts if not (len(p) == 1 and p.isdigit())]

    if parts and len(parts[-1]) == 1 and parts[-1].isascii():
        parts = parts[:-1]

    keyword = " ".join(parts).strip()

    if len(keyword) < 2:
        return ""

    return keyword[:120]

def load_verified_products_cache():
    try:
        with open(VERIFIED_PRODUCTS_CACHE_FILE, "r", encoding="utf-8") as f:
            items = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        print("[VERIFIED CACHE LOAD ERROR]", e, flush=True)
        return []

    if not isinstance(items, list):
        return []

    now = time.time()
    valid_items = []

    for item in items:
        if not isinstance(item, dict):
            continue

        verified_at = safe_price(item.get("verified_at", 0))

        if verified_at <= 0:
            continue

        if now - verified_at > VERIFIED_PRODUCTS_CACHE_TTL_SECONDS:
            continue

        name = str(item.get("name", "") or "").strip()
        category = str(item.get("category", "") or "").strip()

        if not name or not category:
            continue

        # 過去にキャッシュされた「カテゴリ名（またはその同義語）そのまま」等の
        # 不完全な候補を読み込み時点で弾く。TTL(45日)を待たずに汚染データを自己修復するため。
        brand = str(item.get("brand", "") or "").strip()
        if _is_generic_candidate_name(brand, name, category):
            continue

        valid_items.append(item)

    return valid_items


def save_verified_products_cache(items):
    if not isinstance(items, list):
        return

    cleaned = [
        item for item in items
        if isinstance(item, dict)
        and str(item.get("name", "") or "").strip()
        and str(item.get("category", "") or "").strip()
    ]

    try:
        with open(VERIFIED_PRODUCTS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[VERIFIED CACHE SAVE ERROR]", e, flush=True)


def make_verified_product_key(product):
    if not isinstance(product, dict):
        return ""

    category = normalize_candidate_category(
        product.get("category", ""),
        fallback=product.get("category", "")
    )

    identity = normalize_product_identity(
        product.get("brand", ""),
        product.get("name", "")
    )

    if not identity or not category:
        return ""

    return f"{identity}|{category}"
    
def upsert_verified_product_cache(product):
    if not isinstance(product, dict):
        return

    key = make_verified_product_key(product)

    if not key:
        return

    items = load_verified_products_cache()

    updated = []
    replaced = False

    for item in items:
        if make_verified_product_key(item) == key:
            updated.append(product)
            replaced = True
        else:
            updated.append(item)

    if not replaced:
        updated.append(product)

    save_verified_products_cache(updated)

    print(
        "[VERIFIED CACHE SAVED]",
        product.get("category", ""),
        product.get("brand", ""),
        product.get("name", ""),
        flush=True
    )

_KATAKANA_ROMAJI_MAP = {
    'ア': 'a',  'イ': 'i',  'ウ': 'u',  'エ': 'e',  'オ': 'o',
    'カ': 'ka', 'キ': 'ki', 'ク': 'ku', 'ケ': 'ke', 'コ': 'ko',
    'サ': 'sa', 'シ': 'si', 'ス': 'su', 'セ': 'se', 'ソ': 'so',
    'タ': 'ta', 'チ': 'ti', 'ツ': 'tu', 'テ': 'te', 'ト': 'to',
    'ナ': 'na', 'ニ': 'ni', 'ヌ': 'nu', 'ネ': 'ne', 'ノ': 'no',
    'ハ': 'ha', 'ヒ': 'hi', 'フ': 'fu', 'ヘ': 'he', 'ホ': 'ho',
    'マ': 'ma', 'ミ': 'mi', 'ム': 'mu', 'メ': 'me', 'モ': 'mo',
    'ヤ': 'ya', 'ユ': 'yu', 'ヨ': 'yo',
    'ラ': 'la', 'リ': 'li', 'ル': 'lu', 'レ': 'le', 'ロ': 'lo',
    'ワ': 'wa', 'ヲ': 'o',  'ン': 'n',
    'ガ': 'ga', 'ギ': 'gi', 'グ': 'gu', 'ゲ': 'ge', 'ゴ': 'go',
    'ザ': 'za', 'ジ': 'zi', 'ズ': 'zu', 'ゼ': 'ze', 'ゾ': 'zo',
    'ダ': 'da', 'ヂ': 'di', 'ヅ': 'du', 'デ': 'de', 'ド': 'do',
    'バ': 'ba', 'ビ': 'bi', 'ブ': 'bu', 'ベ': 'be', 'ボ': 'bo',
    'パ': 'pa', 'ピ': 'pi', 'プ': 'pu', 'ペ': 'pe', 'ポ': 'po',
    'ァ': 'a',  'ィ': 'i',  'ゥ': 'u',  'ェ': 'e',  'ォ': 'o',
    'ャ': 'ya', 'ュ': 'yu', 'ョ': 'yo',
    'ー': '',   'ッ': '',
}

def katakana_to_romaji_simple(text):
    return ''.join(_KATAKANA_ROMAJI_MAP.get(ch, ch) for ch in str(text or ''))

VERIFIED_BRAND_ALIAS_GROUPS = [
    ("エストラ", "aestura"),
    ("サナ", "sana", "なめらか本舗"),
    ("トゥヴェール", "toutvert", "tout vert", "touver", "tvert"),
]


def is_same_verified_brand_by_alias(brand_identity, rakuten_identity):
    if not brand_identity:
        return True

    if brand_identity in rakuten_identity:
        return True

    brand_compact = brand_identity.replace(" ", "")
    rakuten_compact = rakuten_identity.replace(" ", "")

    for aliases in VERIFIED_BRAND_ALIAS_GROUPS:
        normalized_aliases = [
            normalize_candidate_name_for_merge(alias)
            for alias in aliases
            if str(alias or "").strip()
        ]

        normalized_aliases = [
            alias
            for alias in normalized_aliases
            if alias
        ]

        alias_compacts = [
            alias.replace(" ", "")
            for alias in normalized_aliases
        ]

        brand_is_in_group = (
            brand_identity in normalized_aliases
            or brand_compact in alias_compacts
        )

        rakuten_is_in_group = any(
            alias in rakuten_identity
            or alias.replace(" ", "") in rakuten_compact
            for alias in normalized_aliases
        )

        if brand_is_in_group and rakuten_is_in_group:
            return True

    # カタカナ→ロマ字変換で照合（ルルルン↔LuLuLun など r/l 表記ゆれを吸収）
    brand_romaji = katakana_to_romaji_simple(brand_compact)
    rakuten_romaji = katakana_to_romaji_simple(rakuten_compact)
    if brand_romaji and rakuten_romaji and brand_romaji in rakuten_romaji:
        return True

    return False

def is_same_verified_rakuten_product(product_name, rakuten_title, brand=""):
    product_name = clean_display_product_name(product_name)
    rakuten_title = str(rakuten_title or "").strip()
    brand = str(brand or "").strip()

    if not product_name or not rakuten_title:
        return False

    product_identity = normalize_candidate_name_for_merge(product_name)
    rakuten_identity = normalize_candidate_name_for_merge(rakuten_title)
    brand_identity = normalize_candidate_name_for_merge(brand)

    if not product_identity or not rakuten_identity:
        return False

    brand_ok = is_same_verified_brand_by_alias(
        brand_identity,
        rakuten_identity
    )

    product_compact = product_identity.replace(" ", "")
    rakuten_compact = rakuten_identity.replace(" ", "")

    if product_compact and product_compact in rakuten_compact:
        return True

    product_tokens = [
        token
        for token in re.split(r"[\s　・･\-_ー]+", product_name.lower())
        if len(token) >= 2
        and normalize_candidate_name_for_merge(token) not in {
            "美容液",
            "化粧水",
            "乳液",
            "クリーム",
            "洗顔",
            "洗顔料",
            "日焼け止め",
            "パック",
            "マスク",
            "ピーリング",
            "セラム",
            "ローション",
            "ジェル",
            "バーム",
            "エッセンス",
            "アンプル",
            "トナー",
            "ミルク",
            "クレンジング",
            "フォーム",
            "ウォッシュ",
            "ソープ",
            "serum",
            "cream",
            "lotion",
            "toner",
            "essence",
            "ampoule",
            "mask",
            "cleansing",
        }
    ]

    product_tokens = [
        normalize_candidate_name_for_merge(token)
        for token in product_tokens
        if normalize_candidate_name_for_merge(token)
    ]

    if not product_tokens:
        return False

    matched_tokens = [
        token
        for token in product_tokens
        if token in rakuten_compact
    ]

    # 意味トークンが1つ（成分名+カテゴリ名 等）の場合は1件一致で十分
    # Rakuten検索キーワード自体が既に絞り込み済みのため誤照合リスクは低い
    if len(product_tokens) == 1:
        required_matches = 1
    elif brand_ok:
        required_matches = 1 if len(product_tokens) <= 2 else 2
    else:
        required_matches = 2 if len(product_tokens) <= 2 else min(3, len(product_tokens))

    if len(matched_tokens) < required_matches:
        return False

    return True

def build_verified_product_from_step(step, rakuten_item):
    if not isinstance(step, dict) or not isinstance(rakuten_item, dict):
        return None

    def as_list(value):
        if isinstance(value, list):
            return value
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    def as_dict(value):
        return value if isinstance(value, dict) else {}

    product_name = clean_display_product_name(step.get("product", ""))
    brand = str(step.get("brand", "") or "").strip()

    category = normalize_candidate_category(
        step.get("category", ""),
        fallback=step.get("category", "")
    )

    if not product_name or not category:
        return None

    rakuten_title = str(rakuten_item.get("rakuten_title", "") or "").strip()

    if not is_same_verified_rakuten_product(
        product_name=product_name,
        rakuten_title=rakuten_title,
        brand=brand
    ):
        print(
            "[VERIFIED CACHE REJECT TITLE MISMATCH]",
            {
                "product": product_name,
                "brand": brand,
                "rakuten_title": rakuten_title
            },
            flush=True
        )
        return None

    candidate = {
        "brand": brand,
        "name": product_name,
        "category": category,
        "price_ref": safe_price(rakuten_item.get("price", 0)),
        "raw_price": safe_price(rakuten_item.get("raw_price", 0)),
        "bundle_quantity": safe_bundle_quantity(rakuten_item.get("bundle_quantity", 1)),
        "active_ingredients": as_list(step.get("active_ingredients", [])),
        "support_ingredients": as_list(step.get("support_ingredients", [])),
        "signature_ingredients": as_list(step.get("signature_ingredients", [])),
        "concerns": purpose_to_concern_tags(step.get("purpose", "")),
        "skin_types": as_list(step.get("skin_types", [])),
        "sensitive_ok": str(step.get("sensitive_ok", "unknown") or "unknown"),
        "retinol_level": safe_retinol_level(step.get("retinol_level", 0)),
        "main_functions": as_list(step.get("main_functions", [])),
        "ingredient_focus": as_list(step.get("ingredient_focus", [])),
        "ingredient_strength": as_dict(step.get("ingredient_strength", {})),
        "formulation": as_list(step.get("formulation", [])),
        "technology": as_list(step.get("technology", [])),
        "texture": str(step.get("texture", "") or ""),
        "contraindications": as_list(step.get("contraindications", [])),
        "availability_japan": ["rakuten"],
        "uv_level": as_dict(step.get("uv_level", {})),
    }

    product = build_virtual_product_from_ai_candidate(
        step,
        candidate
    )

    product["brand"] = candidate["brand"]
    product["name"] = product_name
    product["category"] = category
    product["price_ref"] = safe_price(rakuten_item.get("price", 0))
    product["price"] = safe_price(rakuten_item.get("price", 0))
    product["raw_price"] = safe_price(rakuten_item.get("raw_price", 0))
    product["bundle_quantity"] = safe_bundle_quantity(
        rakuten_item.get("bundle_quantity", 1)
    )
    product["image"] = rakuten_item.get("image", "")
    product["rakuten_link"] = rakuten_item.get("rakuten_link", "")
    product["rakuten_title"] = rakuten_item.get("rakuten_title", "")
    product["item_code"] = rakuten_item.get("item_code", "")
    product["shop_name"] = rakuten_item.get("shop_name", "")
    product["verified_at"] = time.time()
    product["_source_hint"] = "verified_cache"
    product["_source"] = "verified_cache"

    return product
    
def extract_rakuten_image_url(item):
    if not isinstance(item, dict):
        return ""

    image_keys = [
        "mediumImageUrls",
        "smallImageUrls",
        "imageUrls",
        "itemImageUrls",
        "images",
    ]

    for image_key in image_keys:
        images = item.get(image_key) or []

        if isinstance(images, str):
            images = [images]

        if not isinstance(images, list):
            continue

        for image in images:
            if isinstance(image, dict):
                candidate_image_url = (
                    image.get("imageUrl")
                    or image.get("url")
                    or image.get("mediumImageUrl")
                    or image.get("smallImageUrl")
                    or ""
                )
            elif isinstance(image, str):
                candidate_image_url = image
            else:
                candidate_image_url = ""

            candidate_image_url = str(candidate_image_url or "").strip()

            if candidate_image_url:
                return candidate_image_url.replace("http://", "https://")

    direct_image_url = (
        item.get("imageUrl")
        or item.get("mediumImageUrl")
        or item.get("smallImageUrl")
        or item.get("thumbnailUrl")
        or ""
    )

    direct_image_url = str(direct_image_url or "").strip()

    if direct_image_url:
        return direct_image_url.replace("http://", "https://")

    return ""

def fetch_rakuten_item(product_name, category="", brand="", ingredient_focus="", purpose=""):
    global RAKUTEN_COOLDOWN_UNTIL

    cache_key = (
        normalize_product_name(product_name),
        normalize_candidate_category(category, fallback=category),
        normalize_product_name(brand),
        normalize_product_name(ingredient_focus),
        normalize_product_name(purpose),
    )

    if cache_key in _rakuten_item_cache:
        print("[RAKUTEN CACHE HIT]", product_name, flush=True)
        return _rakuten_item_cache[cache_key]

    print("[RAKUTEN CACHE MISS]", product_name, flush=True)

    if time.time() < RAKUTEN_COOLDOWN_UNTIL:
        print(
            "[RAKUTEN COOLDOWN ACTIVE]",
            round(RAKUTEN_COOLDOWN_UNTIL - time.time(), 1),
            "seconds left",
            flush=True
        )
        return None

    # サプリ・美容機器のカテゴリ表記ゆれを正規化
    _cat_lower = category.lower()
    if "サプリ" in category or _cat_lower in ("supplement", "サプリ"):
        category = "サプリメント"
    elif "美容機器" in category or "美容家電" in category or _cat_lower in ("device", "beauty device"):
        category = "美容機器"

    if category in ("サプリメント", "美容機器"):
        print(f"[RAKUTEN EXTRA] category={category} product={product_name} brand={brand}", flush=True)

    product_name = clean_display_product_name(product_name)

    if not product_name:
        print("[RAKUTEN API] product_name empty", flush=True)
        return None

    product_name = clean_ai_product_name(product_name)

    if not RAKUTEN_APP_ID:
        print("[RAKUTEN API] RAKUTEN_APP_ID is empty", flush=True)
        return None

    if not RAKUTEN_ACCESS_KEY:
        print("[RAKUTEN API] RAKUTEN_ACCESS_KEY is empty", flush=True)
        return None

    endpoint = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260401"

    headers = {
        "Referer": SITE_URL or "https://lumilog.jp",
        "Origin": SITE_URL or "https://lumilog.jp",
        "User-Agent": "Mozilla/5.0",
    }

    raw_keywords = build_rakuten_search_keywords(
        product_name=product_name,
        brand=brand,
        category=category,
        ingredient_focus=ingredient_focus,
        purpose=purpose
    )

    keywords = []
    seen_keywords = set()

    for keyword in raw_keywords:
        cleaned_keyword = clean_rakuten_keyword(keyword)

        if not cleaned_keyword:
            continue

        keyword_key = normalize_product_name(cleaned_keyword)

        if keyword_key in seen_keywords:
            continue

        seen_keywords.add(keyword_key)
        keywords.append(cleaned_keyword)

    # 「keyword is not valid」400 が返った場合は continue して次を試すため
    # 英語のみの長いキーワードが弾かれてもフォールバックが機能するよう上限を上げる
    MAX_RAKUTEN_KEYWORDS = 4

    for keyword in keywords[:MAX_RAKUTEN_KEYWORDS]:
        keyword = clean_rakuten_keyword(keyword)

        if not keyword:
            print("[RAKUTEN SKIP INVALID KEYWORD]", product_name, flush=True)
            continue

        try:
            print(f"[RAKUTEN TRY KEYWORD] {keyword}", flush=True)
            print("[RAKUTEN KEYWORD LEN]", len(keyword), flush=True)

            wait_for_rakuten_rate_limit()

            params = {
                "applicationId": RAKUTEN_APP_ID,
                "accessKey": RAKUTEN_ACCESS_KEY,
                "keyword": keyword,
                "hits": 10,
                "format": "json",
                "formatVersion": 2,
                "imageFlag": 1,
            }

            # サプリ・美容機器はスキンケア親ジャンル外のためジャンル絞り込みを外す
            # それ以外はスキンケア親ジャンル(100944)で無関係商品をAPIレベルで排除
            if category and category not in ("サプリメント", "美容機器"):
                _gid = get_rakuten_genre_id(category, parent_only=True)
                if _gid:
                    params["genreId"] = _gid

            if RAKUTEN_AFFILIATE_ID:
                params["affiliateId"] = RAKUTEN_AFFILIATE_ID

            res = requests.get(
                endpoint,
                params=params,
                headers=headers,
                timeout=(2, 4)
            )

            print(f"[RAKUTEN API STATUS] {res.status_code}", flush=True)

            if res.status_code != 200:
                print("[RAKUTEN API ERROR BODY]", res.text, flush=True)

                if res.status_code == 429:
                    retry_seconds = 10
                    retry_match = re.search(
                        r"Try again in ([0-9\.]+) seconds?",
                        res.text,
                        re.IGNORECASE
                    )
                    if retry_match:
                        try:
                            retry_seconds = max(3, float(retry_match.group(1)) + 2)
                        except Exception:
                            retry_seconds = 10

                    # ① 待機してから1回だけリトライ
                    print(
                        f"[RAKUTEN 429] {keyword}: sleeping {retry_seconds}s then retrying once",
                        flush=True
                    )
                    time.sleep(retry_seconds)

                    try:
                        wait_for_rakuten_rate_limit()
                        res = requests.get(
                            endpoint, params=params, headers=headers, timeout=(2, 4)
                        )
                        print(f"[RAKUTEN RETRY STATUS] {res.status_code}", flush=True)
                    except Exception as _retry_e:
                        print(f"[RAKUTEN RETRY ERROR] {_retry_e}", flush=True)
                        # ② リトライ自体が例外 → クールダウン設定して諦める
                        RAKUTEN_COOLDOWN_UNTIL = time.time() + retry_seconds
                        return None

                    if res.status_code == 429:
                        # ② リトライ後も429 → グローバルクールダウンを設定
                        RAKUTEN_COOLDOWN_UNTIL = time.time() + retry_seconds
                        print(
                            f"[RAKUTEN 429 PERSISTENT] cooldown {retry_seconds}s",
                            flush=True
                        )
                        return None

                    if res.status_code != 200:
                        print(f"[RAKUTEN RETRY NON-200] {res.status_code}", flush=True)
                        continue
                    # リトライ成功（res.status_code == 200）→ fall-through してペイロード処理へ

                if res.status_code == 400 and "keyword is not valid" in res.text:
                    print("[RAKUTEN INVALID KEYWORD SKIP]", keyword, flush=True)
                    continue

                continue

            payload = res.json()

            items = (
                payload.get("items")
                or payload.get("Items")
                or []
            )

            # genreId 指定で0件 → ジャンルなしで再試行
            if not items and "genreId" in params:
                print(f"[RAKUTEN] 0 items with genreId={params['genreId']}, retrying without", flush=True)
                _params_ng = {k: v for k, v in params.items() if k != "genreId"}
                wait_for_rakuten_rate_limit()
                _res2 = requests.get(endpoint, params=_params_ng, headers=headers, timeout=(2, 4))
                if _res2.status_code == 200:
                    _pl2 = _res2.json()
                    items = _pl2.get("items") or _pl2.get("Items") or []

            if not items:
                continue

            scored_items = []

            for raw_item in items:
                item = raw_item.get("Item", raw_item) if isinstance(raw_item, dict) else raw_item

                if not isinstance(item, dict):
                    continue

                rakuten_title = str(item.get("itemName", "") or "").strip()

                # 非スキンケア商品を即リジェクト（口腔洗浄機・家電等の混入防止）
                # サプリメント・美容機器はスキンケア外カテゴリのためこのチェックをスキップ
                if category and category not in ("サプリメント", "美容機器"):
                    if not _is_rakuten_item_valid_for_category(rakuten_title, category, strict=False):
                        continue

                # --- title match チェック ---
                # 美容機器: 製品名がカテゴリ説明語（「イオン導入器」等）のためブランド一致のみ
                # 成分名+カテゴリ名パターン（「セラミド 乳液」「ナイアシンアミド 美容液」等）:
                #   楽天タイトルに成分名が含まれないため is_same_verified_rakuten_product が
                #   全件 False になる → score_rakuten_item のカテゴリ固有チェックに委ねる
                # サプリメント: ブランド表記揺れが多く pre-filter なし
                # 上記以外の具体的な製品名: is_same_verified_rakuten_product で照合
                _bypass_title_match = (
                    category == "サプリメント"
                    or _is_ingredient_category_name(product_name)
                )

                if category == "美容機器":
                    if brand:
                        _bc = "".join(c for c in brand.lower() if c.strip())
                        _tc = "".join(c for c in rakuten_title.lower() if c.strip())
                        if _bc and _bc not in _tc:
                            continue
                elif not _bypass_title_match:
                    if not is_same_verified_rakuten_product(
                        product_name=product_name,
                        rakuten_title=rakuten_title,
                        brand=brand
                    ):
                        print(
                            "[RAKUTEN REJECT TITLE MISMATCH]",
                            {"product": product_name, "brand": brand, "rakuten_title": rakuten_title},
                            flush=True
                        )
                        continue

                score = score_rakuten_item(
                    item,
                    product_name=product_name,
                    brand=brand,
                    category=category
                )

                if _bypass_title_match or category == "美容機器":
                    print(
                        f"[RAKUTEN BYPASS SCORE] cat={category} bypass={_bypass_title_match} "
                        f"score={score} title={rakuten_title[:50]}",
                        flush=True
                    )

                # バイパス時はスコア閾値を 0 に緩める（-9999 リジェクト以外は通す）
                _min_score = 0 if (_bypass_title_match or category == "美容機器") else 20
                if score < _min_score:
                    continue

                scored_items.append((score, item))

            if not scored_items:
                if _bypass_title_match or category == "美容機器":
                    print(
                        f"[RAKUTEN BYPASS NO ITEMS] keyword={keyword} cat={category}",
                        flush=True
                    )
                continue

            def _sort_key(pair):
                """単品優先 → スコア → レビュー数 → 画像あり → 評価平均 → 価格(安い順)"""
                _, it = pair
                return (
                    pair[0],
                    safe_price(it.get("reviewCount", 0)),
                    1 if (it.get("mediumImageUrls") or it.get("smallImageUrls")) else 0,
                    safe_price(it.get("reviewAverage", 0)),
                    -safe_price(it.get("itemPrice", 0)),
                )

            # パス1: 単品のみ（セット商品除外）
            single_items = [
                (sc, it) for sc, it in scored_items
                if not _is_rakuten_set_item(str(it.get("itemName", "") or ""))
            ]
            if single_items:
                single_items.sort(key=_sort_key, reverse=True)
                best_score, best = single_items[0]
                print(
                    f"[RAKUTEN SELECT] single item: {best.get('itemName','')[:50]} "
                    f"score={best_score} reviews={best.get('reviewCount',0)}",
                    flush=True
                )
            else:
                # パス2: 単品が見つからない場合はセット商品も許容
                scored_items.sort(key=_sort_key, reverse=True)
                best_score, best = scored_items[0]
                print(
                    f"[RAKUTEN SELECT] set item fallback: {best.get('itemName','')[:50]} "
                    f"score={best_score} reviews={best.get('reviewCount',0)}",
                    flush=True
                )

            

            image_url = extract_rakuten_image_url(best)

            best = normalize_rakuten_item_price(best)

            raw_price = safe_price(
                best.get("raw_price")
                or best.get("itemPrice")
                or 0
            )

            result = {
                "name": clean_display_product_name(product_name),
                "rakuten_title": best.get("itemName", ""),
                "price": raw_price,
                "normalized_price": raw_price,
                "raw_price": raw_price,
                "bundle_quantity": 1,
                "rakuten_link": (
                    best.get("affiliateUrl")
                    or best.get("itemUrl")
                    or "#"
                ),
                "image": image_url,
                "item_code": best.get("itemCode", ""),
                "shop_name": best.get("shopName", ""),
            }

            _rakuten_item_cache[cache_key] = result
            return result

        except requests.exceptions.RequestException as e:
            print("[RAKUTEN API REQUEST ERROR]", e, flush=True)
            continue

        except Exception as e:
            print("[RAKUTEN API UNKNOWN ERROR]", repr(e), flush=True)
            continue

    print(
        f"[RAKUTEN API] no items for product={product_name}",
        flush=True
    )

    _rakuten_item_cache[cache_key] = None
    return None




# === Phase 2: criteria-based Rakuten search ===

_TITLE_INGREDIENT_KEYWORDS = {
    "ナイアシンアミド": "niacinamide",
    "ニアシンアミド": "niacinamide",
    "レチノール": "retinol",
    "レチナール": "retinal",
    "ビタミンc": "vitamin_c",
    "ビタミンC": "vitamin_c",
    "セラミド": "ceramide",
    "ヒアルロン酸": "hyaluronic",
    "bha": "bha",
    "BHA": "bha",
    "aha": "aha",
    "AHA": "aha",
    "pha": "pha",
    "PHA": "pha",
    "グリコール酸": "glycolic_acid",
    "乳酸": "lactic_acid",
    "サリチル酸": "salicylic_acid",
    "アゼライン酸": "azelaic_acid",
    "cica": "cica",
    "CICA": "cica",
    "シカ": "cica",
    "ツボクサ": "centella",
    "センテラ": "centella",
    "トラネキサム酸": "tranexamic_acid",
    "パンテノール": "panthenol",
    "アルブチン": "arbutin",
    "コウジ酸": "kojic_acid",
    "ペプチド": "peptide",
    "グルタチオン": "glutathione",
    "スクワラン": "squalane",
    "グリセリン": "glycerin",
    "アミノ酸": "amino_acid",
    "コラーゲン": "collagen",
    "酵素": "enzyme",
}


def infer_ingredients_from_rakuten_title(title):
    if not title:
        return []
    found = []
    title_lower = str(title).lower()
    for keyword, ingredient_id in _TITLE_INGREDIENT_KEYWORDS.items():
        if keyword.lower() in title_lower and ingredient_id not in found:
            found.append(ingredient_id)
    return found


# タイトルキーワード → contraindications 推定マッピング
# DB商品は products.json に実データが入っているため、このルールは楽天商品専用
_TITLE_CONTRAINDICATION_RULES = [
    # レチノイド系：朝NG・光感受性・酸との併用注意
    (
        ["レチノール", "レチナール", "retinol", "retinal", "レチノイン", "tretinoin"],
        ["high_irritation_risk", "morning_use_caution", "retinol_same_routine", "photosensitivity"]
    ),
    # AHA系：光感受性・酸重複注意・毎日使い注意
    (
        ["aha", "グリコール酸", "glycolic", "乳酸", "lactic acid", "マンデル酸", "mandelic",
         "フルーツ酸", "酒石酸", "クエン酸 酸"],
        ["high_irritation_risk", "acid_same_routine", "photosensitivity", "morning_use_caution"]
    ),
    # BHA系：光感受性あり・朝使用NG
    (
        ["bha", "サリチル酸", "salicylic"],
        ["high_irritation_risk", "acid_same_routine", "photosensitivity", "morning_use_caution", "sensitive_skin"]
    ),
    # PHA系：マイルドだが酸系であることは明記
    (
        ["pha", "グルコノラクトン", "ラクトビオン酸"],
        ["acid_same_routine", "daily_use_caution"]
    ),
    # ピーリング製品全般：朝使用NG
    (
        ["ピーリング", "ピール", "スキンピール", "角質除去", "ゴマージュ", "スクラブ"],
        ["high_irritation_risk", "acid_same_routine", "photosensitivity", "morning_use_caution", "daily_use_caution"]
    ),
    # 高濃度表記
    (
        ["高濃度", "高配合", "30%", "20%", "15%", "10%"],
        ["high_irritation_risk", "sensitive_skin"]
    ),
    # 精油・エッセンシャルオイル
    (
        ["精油", "エッセンシャルオイル", "essential oil"],
        ["essential_oil_caution", "sensitive_skin"]
    ),
]


def infer_contraindications_from_title(title):
    """楽天商品タイトルから contraindications タグを推定する。"""
    if not title:
        return []
    title_lower = str(title).lower()
    result = []
    seen = set()
    for keywords, contras in _TITLE_CONTRAINDICATION_RULES:
        if any(kw.lower() in title_lower for kw in keywords):
            for c in contras:
                if c not in seen:
                    result.append(c)
                    seen.add(c)
    return result


# 推定成分ID → concerns / main_functions / ingredient_focus の補完マッピング
_RAKUTEN_INGREDIENT_METADATA = {
    "retinol":        {"concerns": ["aging", "texture", "pores", "dullness", "acne_marks"],    "main_functions": ["エイジングケア", "毛穴改善", "ターンオーバー促進", "ハリ補給"], "ingredient_focus": ["retinoid"]},
    "retinal":        {"concerns": ["aging", "texture", "pores", "dullness", "acne_marks"],    "main_functions": ["エイジングケア", "毛穴改善", "ターンオーバー促進"],             "ingredient_focus": ["retinoid"]},
    "vitamin_c":      {"concerns": ["dullness", "whitening", "aging", "pigmentation"],         "main_functions": ["美白", "ハリ補給", "くすみ改善", "抗酸化"],                    "ingredient_focus": ["vitamin_c"]},
    "niacinamide":    {"concerns": ["pores", "dullness", "whitening", "oiliness", "barrier"],  "main_functions": ["美白", "毛穴改善", "バリア強化", "皮脂コントロール"],           "ingredient_focus": ["niacinamide"]},
    "azelaic_acid":   {"concerns": ["acne", "redness", "dullness", "pigmentation"],            "main_functions": ["ニキビ改善", "赤み鎮静", "美白"],                              "ingredient_focus": ["azelaic"]},
    "tranexamic_acid":{"concerns": ["dullness", "whitening", "pigmentation"],                  "main_functions": ["美白", "シミ改善", "くすみ改善"],                              "ingredient_focus": ["tranexamic"]},
    "peptide":        {"concerns": ["aging", "firmness"],                                      "main_functions": ["エイジングケア", "ハリ補給", "弾力補給"],                      "ingredient_focus": ["peptide"]},
    "ceramide":       {"concerns": ["dryness", "barrier", "redness"],                          "main_functions": ["バリア強化", "保湿", "鎮静"],                                  "ingredient_focus": ["ceramide"]},
    "hyaluronic":     {"concerns": ["dryness", "barrier"],                                     "main_functions": ["保湿", "潤い補給"],                                            "ingredient_focus": ["hyaluronic_acid"]},
    "cica":           {"concerns": ["redness", "barrier", "acne"],                             "main_functions": ["鎮静", "バリア強化", "修復"],                                  "ingredient_focus": ["centella"]},
    "centella":       {"concerns": ["redness", "barrier", "acne"],                             "main_functions": ["鎮静", "バリア強化", "修復"],                                  "ingredient_focus": ["centella"]},
    "panthenol":      {"concerns": ["redness", "barrier", "dryness"],                          "main_functions": ["鎮静", "バリア強化", "保湿"],                                  "ingredient_focus": ["panthenol"]},
    "bha":            {"concerns": ["pores", "acne", "texture", "oiliness"],                   "main_functions": ["毛穴改善", "ニキビ改善", "角質ケア"],                           "ingredient_focus": ["aha_bha"]},
    "aha":            {"concerns": ["texture", "dullness", "pores"],                           "main_functions": ["角質ケア", "くすみ改善", "毛穴改善"],                           "ingredient_focus": ["aha_bha"]},
    "pha":            {"concerns": ["texture", "dullness", "pores"],                           "main_functions": ["角質ケア", "くすみ改善"],                                       "ingredient_focus": ["pha"]},
    "glycolic_acid":  {"concerns": ["texture", "dullness", "pores"],                           "main_functions": ["角質ケア", "くすみ改善"],                                       "ingredient_focus": ["aha_bha"]},
    "lactic_acid":    {"concerns": ["texture", "dullness", "dryness"],                         "main_functions": ["角質ケア", "保湿"],                                            "ingredient_focus": ["aha_bha"]},
    "salicylic_acid": {"concerns": ["pores", "acne", "oiliness"],                              "main_functions": ["毛穴改善", "ニキビ改善"],                                       "ingredient_focus": ["aha_bha"]},
    "arbutin":        {"concerns": ["dullness", "whitening", "pigmentation"],                  "main_functions": ["美白", "シミ改善"],                                            "ingredient_focus": ["arbutin"]},
    "kojic_acid":     {"concerns": ["dullness", "whitening", "pigmentation"],                  "main_functions": ["美白", "シミ改善"],                                            "ingredient_focus": ["kojic_acid"]},
    "glutathione":    {"concerns": ["dullness", "whitening"],                                  "main_functions": ["美白", "抗酸化"],                                              "ingredient_focus": ["glutathione"]},
    "squalane":       {"concerns": ["dryness", "barrier"],                                     "main_functions": ["保湿", "バリア強化"],                                           "ingredient_focus": ["squalane"]},
    "glycerin":       {"concerns": ["dryness"],                                                "main_functions": ["保湿", "潤い補給"],                                            "ingredient_focus": ["glycerin"]},
    "amino_acid":     {"concerns": ["dryness", "barrier"],                                     "main_functions": ["保湿", "バリア強化"],                                           "ingredient_focus": ["amino_acid"]},
    "collagen":       {"concerns": ["firmness", "dryness", "aging"],                           "main_functions": ["エイジングケア", "ハリ補給", "保湿"],                           "ingredient_focus": ["collagen"]},
    "enzyme":         {"concerns": ["pores", "texture", "dullness"],                           "main_functions": ["角質ケア", "毛穴改善", "くすみ改善"],                           "ingredient_focus": ["enzyme"]},
}

# タイトルから肌タイプを推定するキーワード
_TITLE_SKIN_TYPE_KEYWORDS = {
    "乾燥肌": "dry", "乾燥": "dry",
    "敏感肌": "sensitive", "低刺激": "sensitive", "刺激レス": "sensitive",
    "オイリー": "oily", "脂性肌": "oily", "皮脂": "oily",
    "混合肌": "combination",
    "普通肌": "normal",
}

# 商品タイトルから成分キーを推定するマッピング（楽天商品の active_ingredients 補完用）
_TITLE_TO_INGREDIENT_KEY = {
    "セラミド": "ceramide", "ceramide": "ceramide",
    "ヒアルロン酸": "hyaluronic", "ヒアルロン": "hyaluronic",
    "ナイアシンアミド": "niacinamide", "niacinamide": "niacinamide",
    "ビタミンc": "vitamin_c", "ビタミンC": "vitamin_c", "アスコルビン": "vitamin_c",
    "レチノール": "retinol", "retinol": "retinol",
    "レチナール": "retinal",
    "アゼライン酸": "azelaic_acid", "アゼライン": "azelaic_acid",
    "トラネキサム酸": "tranexamic_acid",
    "アルブチン": "arbutin",
    "コラーゲン": "collagen", "collagen": "collagen",
    "ペプチド": "peptide", "peptide": "peptide",
    "cica": "cica", "シカ": "cica", "ツボクサ": "cica",
    "センテラ": "centella", "centella": "centella",
    "パンテノール": "panthenol", "パントテン": "panthenol",
    "スクワラン": "squalane", "squalane": "squalane",
    "グリセリン": "glycerin", "glycerin": "glycerin",
    "アミノ酸": "amino_acid",
    "aha": "aha", "グリコール酸": "glycolic_acid",
    "乳酸": "lactic_acid",
    "bha": "bha", "サリチル酸": "salicylic_acid",
    "pha": "pha", "グルコノラクトン": "pha",
    "酵素": "enzyme",
    "グルタチオン": "glutathione",
    "コウジ酸": "kojic_acid",
}


def enrich_product_metadata_from_ingredients(product):
    """
    active_ingredients からDB・楽天商品を問わず concerns / main_functions /
    ingredient_focus / skin_types を補完する。
    既存フィールドは上書きせず、欠けている項目だけを追加する。
    楽天商品はフィールドが空なので補完量が多く、DB商品はすでに充実しているので
    影響は最小限になる。スコアリングの基準は両者で同一。
    """
    if not isinstance(product, dict):
        return product

    title = str(product.get("rakuten_title") or product.get("name") or "")
    title_lower = title.lower()

    inferred = list(product.get("active_ingredients") or [])

    # active_ingredients が空の楽天商品はタイトルから成分キーを補完する
    if not inferred:
        seen_inferred = set()
        for keyword, ing_key in _TITLE_TO_INGREDIENT_KEY.items():
            if keyword.lower() in title_lower and ing_key not in seen_inferred:
                inferred.append(ing_key)
                seen_inferred.add(ing_key)
        if inferred:
            product["active_ingredients"] = inferred
        else:
            # タイトルからも成分が取れない場合は肌タイプのみ補完して終了
            skin_types = list(product.get("skin_types") or [])
            seen_skin_types = set(skin_types)
            for keyword, st in _TITLE_SKIN_TYPE_KEYWORDS.items():
                if keyword in title and st not in seen_skin_types:
                    skin_types.append(st)
                    seen_skin_types.add(st)
            product["skin_types"] = skin_types
            return product

    concerns         = list(product.get("concerns") or [])
    main_functions   = list(product.get("main_functions") or [])
    ingredient_focus = list(product.get("ingredient_focus") or [])
    skin_types       = list(product.get("skin_types") or [])

    seen_concerns  = set(concerns)
    seen_functions = set(main_functions)
    seen_focus     = set(ingredient_focus)

    for ing in inferred:
        meta = _RAKUTEN_INGREDIENT_METADATA.get(ing, {})
        for c in meta.get("concerns", []):
            if c not in seen_concerns:
                concerns.append(c)
                seen_concerns.add(c)
        for f in meta.get("main_functions", []):
            if f not in seen_functions:
                main_functions.append(f)
                seen_functions.add(f)
        for focus in meta.get("ingredient_focus", []):
            if focus not in seen_focus:
                ingredient_focus.append(focus)
                seen_focus.add(focus)

    # タイトルまたは商品名から肌タイプを推定（DB商品も name フィールドがある）
    seen_skin_types = set(skin_types)
    for keyword, st in _TITLE_SKIN_TYPE_KEYWORDS.items():
        if keyword in title and st not in seen_skin_types:
            skin_types.append(st)
            seen_skin_types.add(st)

    product["concerns"]          = concerns
    product["main_functions"]    = main_functions
    product["ingredient_focus"]  = ingredient_focus
    product["skin_types"]        = skin_types

    return product


# 楽天市場 スキンケア関連ジャンルID
# 親カテゴリ: 100944 (スキンケア全般) — fetch_rakuten_item の広域フィルタに使用
# 個別カテゴリ ID — search_rakuten_by_criteria の厳密フィルタに使用
_RAKUTEN_SKINCARE_PARENT_GENRE_ID = 100944

_CATEGORY_GENRE_IDS = {
    "クレンジング":  210450,
    "洗顔":          216301,
    "ブースター":    216348,  # 導入・先行美容液は美容液カテゴリ扱い
    "化粧水":        216307,
    "美容液":        216348,
    "導入美容液":    216348,
    "乳液":          216387,
    "クリーム":      216424,
    "日焼け止め":    216492,
    "パック":        503020,
    "ピーリング":    503044,
}


def get_rakuten_genre_id(category: str, parent_only: bool = False) -> int | None:
    """カテゴリ名から楽天ジャンルIDを返す。
    parent_only=True なら常にスキンケア親カテゴリ(100944)を返す。
    """
    if parent_only:
        return _RAKUTEN_SKINCARE_PARENT_GENRE_ID
    return _CATEGORY_GENRE_IDS.get(category) or _RAKUTEN_SKINCARE_PARENT_GENRE_ID


_CATEGORY_REQUIRED_KEYWORDS = {
    "クレンジング":  ["クレンジング", "cleansing", "メイク落とし", "make up remover", "makeup remover"],
    "洗顔":          ["洗顔", "フォーム", "フェイスウォッシュ", "face wash", "cleanser"],
    "ブースター":    ["ブースター", "導入", "先行美容液", "booster"],
    "化粧水":        ["化粧水", "トナー", "ローション", "toner", "lotion"],
    "美容液":        ["美容液", "セラム", "エッセンス", "アンプル", "serum", "essence", "ampoule"],
    "導入美容液":    ["美容液", "セラム", "エッセンス", "ブースター", "導入", "serum"],
    "乳液":          ["乳液", "ミルク", "エマルジョン", "emulsion", "milk"],
    "クリーム":      ["クリーム", "バーム", "cream", "balm", "moisturizer"],
    "日焼け止め":    ["日焼け止め", "spf", "pa+", "サンスクリーン", "sunscreen", "uv"],
    "パック":        ["パック", "マスク", "mask", "シートマスク", "フェイスマスク"],
    "ピーリング":    ["ピーリング", "aha", "bha", "スクラブ", "exfoliant", "peel"],
}

# スキンケアと無関係なカテゴリのキーワード → 含まれていたら問答無用で除外
_NON_SKINCARE_REJECT_KEYWORDS = [
    # 口腔ケア・歯科
    "口腔", "口内", "デンタル", "歯科", "歯ブラシ", "歯磨き", "フロス",
    "電動歯ブラシ", "ジェットウォッシャー", "ウォーターフロッサー",
    "水圧洗浄", "口腔洗浄",
    # サプリ・健康食品
    "サプリ", "サプリメント", "健康食品", "内服", "飲む美容",
    # 家電・生活家電
    "家電", "電化製品", "洗濯機", "掃除機", "電動シェーバー",
    "電気シェーバー", "ドライヤー", "ヘアドライヤー",
    # 食品・飲料
    "食品", "飲料", "プロテイン", "コラーゲン飲料",
    # スポーツ・フィットネス
    "スポーツ用品", "フィットネス", "ダンベル",
    # 抱き合わせ・同梱専用（単品購入不可商品）
    "他の商品と一緒", "一緒買い専用", "この商品のみのご購入は不可",
    "同梱専用", "セット購入専用", "まとめ購入専用", "単品購入不可", "単品での購入不可",
]


def _is_rakuten_item_valid_for_category(item_name: str, category: str, strict: bool = True) -> bool:
    """楽天検索結果の商品名がスキンケアカテゴリに属するか確認する。

    strict=True  (criteria search): 非スキンケア拒否 + カテゴリキーワード必須チェック
    strict=False (keyword search) : 非スキンケア拒否のみ（正規商品タイトルに必ずしもカテゴリ語が入らないため）
    """
    if not item_name:
        return False
    name_lower = item_name.lower()

    # 非スキンケアキーワードが含まれていたら即リジェクト（モード問わず）
    for ng in _NON_SKINCARE_REJECT_KEYWORDS:
        if ng in name_lower:
            print(f"[RAKUTEN REJECT non-skincare] '{item_name[:50]}' contains '{ng}'", flush=True)
            return False

    if not strict:
        return True  # キーワード検索では非スキンケア拒否のみ（cross_rejectは適用しない）

    # ---- strict モードのみ: カテゴリ横断の除外チェック ----
    _CATEGORY_CROSS_REJECT = {
        "ピーリング": [
            "クレンジング", "メイク落とし", "クレンジングオイル", "クレンジングミルク",
            "クレンジングジェル", "クレンジングバーム", "クレンジングクリーム",
            "クレンジングフォーム", "ダブル洗顔不要",
            "乳液", "クリーム", "化粧水", "シートマスク", "フェイスマスク", "パック",
        ],
        "パック": ["クレンジング", "メイク落とし", "日焼け止め", "サンスクリーン"],
    }
    cross_reject = _CATEGORY_CROSS_REJECT.get(category, [])
    if cross_reject and any(w in item_name for w in cross_reject):
        print(f"[RAKUTEN REJECT cross-category] '{item_name[:50]}' is wrong for category='{category}'", flush=True)
        return False

    # criteria search: カテゴリ固有キーワードが1つ以上含まれているか確認
    required = _CATEGORY_REQUIRED_KEYWORDS.get(category, [])
    if not required:
        all_skincare_words = [w for kws in _CATEGORY_REQUIRED_KEYWORDS.values() for w in kws]
        return any(w in name_lower for w in all_skincare_words)

    if any(w in name_lower for w in required):
        return True

    print(f"[RAKUTEN REJECT wrong category] '{item_name[:50]}' for category='{category}'", flush=True)
    return False


# ================================================================
# Gemini候補のカテゴリ整合性バリデーション
# 楽天の _CATEGORY_CROSS_REJECT と同じ考え方をGemini出力に適用する。
# ================================================================

# カテゴリ別・商品名に含まれてはいけないキーワード
_CANDIDATE_CATEGORY_FORBIDDEN: dict[str, list[str]] = {
    "美容液": [
        "洗顔", "ウォッシュ", "フォーム", "クレンジング", "石けん", "石鹸",
        "ジェルウォッシュ", "フェイスウォッシュ", "洗顔フォーム", "洗顔料",
        "face wash", "cleanser", "cleansing",
    ],
    "化粧水": [
        "洗顔", "ウォッシュ", "フォーム", "クレンジング",
        # 乳液/ミルク/エマルジョンは日本のローション系商品に含まれることがあるため除外
        "日焼け止め", "sunscreen", "サンスクリーン",
    ],
    "乳液": [
        "洗顔", "ウォッシュ", "クレンジング",
        "化粧水", "ローション", "トナー",
        "日焼け止め", "sunscreen",
    ],
    "クリーム": [
        "洗顔", "ウォッシュ", "クレンジング",
        "化粧水", "ローション", "トナー",
        "日焼け止め", "sunscreen",
    ],
    "洗顔": [
        "化粧水", "ローション", "トナー",
        "美容液", "セラム", "エッセンス", "アンプル",
        "乳液", "ミルク",
        "クリーム",
        "日焼け止め", "sunscreen",
    ],
    "クレンジング": [
        "化粧水", "ローション", "トナー",
        "美容液", "セラム",
        "乳液", "ミルク",
        "日焼け止め", "sunscreen",
    ],
    "日焼け止め": [
        "洗顔", "ウォッシュ", "クレンジング",
        "乳液",
        # "ミルク" は日焼け止めのテクスチャ名として多用されるため除外しない
        # 例: "パーフェクトUV スキンケアミルク", "スキンアクア UVミルク"
    ],
    "ピーリング": [
        "クレンジング", "メイク落とし",
        "乳液", "クリーム", "化粧水", "シートマスク", "フェイスマスク",
    ],
    "パック": [
        "クレンジング", "メイク落とし",
        "日焼け止め", "sunscreen",
        "洗顔", "ウォッシュ",
    ],
}


def is_candidate_wrong_for_category(step_category: str, product_name: str) -> bool:
    """Geminiがstepカテゴリとミスマッチなproductを出力した場合Trueを返す。

    例: step_category="美容液" なのに product_name="クリアリングジェルウォッシュ" → True
    """
    forbidden = _CANDIDATE_CATEGORY_FORBIDDEN.get(step_category, [])
    if not forbidden:
        return False
    name_lower = (product_name or "").lower()
    name_orig = product_name or ""
    for kw in forbidden:
        if kw.lower() in name_lower or kw in name_orig:
            print(
                f"[CANDIDATE REJECT category_mismatch] "
                f"step={step_category} product='{product_name[:40]}' contains '{kw}'",
                flush=True,
            )
            return True
    return False


# 肌悩みタグ → 楽天検索キーワード変換マップ
_CONCERN_TO_RAKUTEN_KEYWORD = {
    "dryness":     "保湿",
    "hydration":   "保湿",
    "barrier":     "バリアケア",
    "aging":       "エイジングケア",
    "firmness":    "ハリ",
    "wrinkles":    "シワ",
    "whitening":   "美白",
    "brightening": "美白",
    "dullness":    "くすみ",
    "acne":        "ニキビ",
    "pores":       "毛穴",
    "oiliness":    "皮脂",
    "redness":     "鎮静",
    "soothing":    "鎮静",
    "sensitivity": "敏感肌",
    "texture":     "毛穴",
    "uv":          "日焼け止め",
}


def _rakuten_criteria_search_single(keyword, category):
    """
    単一キーワードで楽天商品検索しカテゴリ適合商品リストを返す。
    セッションキャッシュ・レート制限・セッション上限すべて共通管理。
    """
    global _rakuten_criteria_cache, _rakuten_criteria_call_count, RAKUTEN_COOLDOWN_UNTIL

    if not RAKUTEN_APP_ID or not RAKUTEN_ACCESS_KEY:
        return []

    if time.time() < RAKUTEN_COOLDOWN_UNTIL:
        return []

    with _rakuten_call_count_lock:
        if _rakuten_criteria_call_count >= MAX_RAKUTEN_CRITERIA_CALLS:
            print("[RAKUTEN CRITERIA] session call limit reached", flush=True)
            return []
        _rakuten_criteria_call_count += 1
        current_count = _rakuten_criteria_call_count

    keyword = clean_rakuten_keyword(keyword)
    if not keyword:
        return []

    norm_cat = normalize_candidate_category(category, fallback=category)
    cache_key = (norm_cat, keyword)
    db_cache_key = f"rakuten:{norm_cat}:{keyword}"

    # 1. メモリキャッシュ（最速、セッション内）
    if cache_key in _rakuten_criteria_cache:
        print(f"[RAKUTEN CRITERIA CACHE HIT] {cache_key}", flush=True)
        with _rakuten_call_count_lock:
            _rakuten_criteria_call_count -= 1
        return _rakuten_criteria_cache[cache_key]

    # 2. DB永続キャッシュ（7日TTL、デプロイ後も維持）
    db_result = get_rakuten_search_from_db(db_cache_key)
    if db_result is not None:
        _rakuten_criteria_cache[cache_key] = db_result
        with _rakuten_call_count_lock:
            _rakuten_criteria_call_count -= 1
        return db_result

    print(f"[RAKUTEN CRITERIA SEARCH] keyword={keyword} (call#{current_count})", flush=True)

    endpoint = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260401"
    headers = {
        "Referer": SITE_URL or "https://lumilog.jp",
        "Origin": SITE_URL or "https://lumilog.jp",
        "User-Agent": "Mozilla/5.0",
    }

    _t_func_start = time.time()
    try:
        _t_wait_start = time.time()
        wait_for_rakuten_rate_limit()
        _wait_elapsed = time.time() - _t_wait_start

        params = {
            "applicationId": RAKUTEN_APP_ID,
            "accessKey": RAKUTEN_ACCESS_KEY,
            "keyword": keyword,
            "hits": 15,
            "format": "json",
            "formatVersion": 2,
            "imageFlag": 1,
        }

        _genre_id = get_rakuten_genre_id(category, parent_only=False)
        if _genre_id:
            params["genreId"] = _genre_id

        if RAKUTEN_AFFILIATE_ID:
            params["affiliateId"] = RAKUTEN_AFFILIATE_ID

        _t_http = time.time()
        res = requests.get(endpoint, params=params, headers=headers, timeout=(2, 4))
        _http_elapsed = time.time() - _t_http
        print(
            f"[RAKUTEN TIMING] keyword={keyword!r} wait={_wait_elapsed:.2f}s http={_http_elapsed:.2f}s "
            f"status={res.status_code} call#{current_count}",
            flush=True
        )

        if res.status_code == 429:
            retry_seconds = 10
            retry_match = re.search(
                r"Try again in ([0-9\.]+) seconds?", res.text, re.IGNORECASE
            )
            if retry_match:
                try:
                    retry_seconds = max(3, float(retry_match.group(1)) + 2)
                except Exception:
                    retry_seconds = 10
            print(
                f"[RAKUTEN CRITERIA 429] {keyword}: sleeping {retry_seconds}s then retrying once",
                flush=True
            )
            time.sleep(retry_seconds)
            try:
                wait_for_rakuten_rate_limit()
                res = requests.get(endpoint, params=params, headers=headers, timeout=(2, 4))
                print(f"[RAKUTEN CRITERIA RETRY STATUS] {res.status_code}", flush=True)
            except Exception as _re:
                print(f"[RAKUTEN CRITERIA RETRY ERROR] {_re}", flush=True)
                RAKUTEN_COOLDOWN_UNTIL = time.time() + retry_seconds
                _rakuten_criteria_cache[cache_key] = []
                return []
            if res.status_code == 429:
                RAKUTEN_COOLDOWN_UNTIL = time.time() + retry_seconds
                _rakuten_criteria_cache[cache_key] = []
                return []
            if res.status_code != 200:
                _rakuten_criteria_cache[cache_key] = []
                return []
            # リトライ成功 → fall-through してペイロード処理へ

        if res.status_code != 200:
            _rakuten_criteria_cache[cache_key] = []
            return []

        payload = res.json()
        items = payload.get("items") or payload.get("Items") or []

        if not items and "genreId" in params:
            # genreIdリトライ: レートリミットを正しく経由する
            params_no_genre = {k: v for k, v in params.items() if k != "genreId"}
            wait_for_rakuten_rate_limit()
            _t_retry = time.time()
            res2 = requests.get(endpoint, params=params_no_genre, headers=headers, timeout=(2, 4))
            print(
                f"[RAKUTEN TIMING] genreId-retry keyword={keyword!r} http={time.time()-_t_retry:.2f}s status={res2.status_code}",
                flush=True
            )
            if res2.status_code == 200:
                payload = res2.json()
                items = payload.get("items") or payload.get("Items") or []
                print(f"[RAKUTEN CRITERIA] retry without genreId -> {len(items)} items", flush=True)

        results = []
        _n_invalid_cat = 0
        _n_no_image = 0

        for raw_item in items:
            item = raw_item.get("Item", raw_item) if isinstance(raw_item, dict) else raw_item
            if not isinstance(item, dict):
                continue

            item_name = str(item.get("itemName", "") or "").strip()
            if not item_name:
                continue

            if not _is_rakuten_item_valid_for_category(item_name, category):
                _n_invalid_cat += 1
                continue

            item = normalize_rakuten_item_price(item)
            price = safe_price(item.get("raw_price") or item.get("itemPrice") or 0)
            image_url = extract_rakuten_image_url(item)
            if not image_url:
                _n_no_image += 1
                continue

            inferred = infer_ingredients_from_rakuten_title(item_name)

            results.append({
                "name": clean_display_product_name(item_name[:50]),
                "brand": "",
                "category": category,
                "active_ingredients": inferred,
                "support_ingredients": [],
                "concerns": [],
                "skin_types": [],
                "formulation": "",
                "ingredient_strength": {},
                "main_functions": [],
                "ingredient_focus": [],
                "price_ref": price,
                "raw_price": price,
                "image": image_url,
                "rakuten_link": (item.get("affiliateUrl") or item.get("itemUrl") or "#"),
                "rakuten_title": item_name,
                "item_code": item.get("itemCode", ""),
                "shop_name": item.get("shopName", ""),
                "_source_hint": "rakuten_criteria",
            })

        print(
            f"[RAKUTEN CRITERIA] keyword='{keyword}' "
            f"raw={len(items)} / invalid_cat={_n_invalid_cat} / no_image={_n_no_image} / final={len(results)}",
            flush=True
        )
        _rakuten_criteria_cache[cache_key] = results
        # DB永続キャッシュに保存（空でない結果のみ）
        if results:
            save_rakuten_search_to_db(db_cache_key, results)
        print(
            f"[RAKUTEN TIMING] total keyword={keyword!r} {time.time()-_t_func_start:.2f}s → {len(results)}件",
            flush=True
        )
        return results

    except Exception as e:
        print(f"[RAKUTEN CRITERIA ERROR] {repr(e)} total={time.time()-_t_func_start:.2f}s", flush=True)
        _rakuten_criteria_cache[cache_key] = []
        return []


def search_rakuten_by_criteria(category, improvement_plan):
    """後方互換: improvement_plan の key_ingredients + category で検索するラッパー。"""
    if not isinstance(improvement_plan, dict):
        return []
    key_ingredients = improvement_plan.get("key_ingredients") or []
    top_ingredient = next(
        (clean_rakuten_keyword(i) for i in key_ingredients if clean_rakuten_keyword(i)),
        None
    )
    if not top_ingredient:
        return []
    category_clean = clean_rakuten_keyword(category)
    if not category_clean:
        return []
    return _rakuten_criteria_search_single(f"{category_clean} {top_ingredient}", category)


def search_rakuten_for_step(step, improvement_plan):
    """
    ステップごとに最大2クエリをアダプティブ実行し楽天商品候補を返す。
    Q1: ingredient_focus + category  （常時実行）
    Q2: concern_keyword + category   （Q1が8件未満のときのみ実行）
    ※Q3（カテゴリのみ）は品質が低いため廃止
    重複排除して最大25件まとめて返す。
    """
    category = str(step.get("category", "") or "").strip()
    category_clean = clean_rakuten_keyword(category)
    if not category_clean:
        return []

    ingredient_focus = str(step.get("ingredient_focus", "") or "").strip()
    concern_tags = purpose_to_concern_tags(step.get("purpose", ""))

    key_ingredients = list((improvement_plan or {}).get("key_ingredients", []) or []) if improvement_plan else []
    top_from_plan = next(
        (clean_rakuten_keyword(i) for i in key_ingredients if clean_rakuten_keyword(i)),
        None
    )

    def make_q1():
        if ingredient_focus:
            ing_clean = clean_rakuten_keyword(ingredient_focus)
            if ing_clean:
                return f"{ing_clean} {category_clean}"
        if top_from_plan:
            return f"{top_from_plan} {category_clean}"
        return category_clean

    def make_q2():
        concern_kw = next(
            (_CONCERN_TO_RAKUTEN_KEYWORD[tag] for tag in concern_tags if tag in _CONCERN_TO_RAKUTEN_KEYWORD),
            None
        )
        if concern_kw:
            return f"{concern_kw} {category_clean}"
        return None

    seen_keys = set()
    all_results = []

    def collect(results):
        for r in results:
            k = normalize_product_name(r.get("rakuten_title", "") or r.get("name", ""))
            if k and k not in seen_keys:
                seen_keys.add(k)
                all_results.append(r)

    _t_step = time.time()
    q1 = make_q1()
    if q1:
        collect(_rakuten_criteria_search_single(q1, category))

    # Q1が5件未満のときだけQ2を実行（8→5に変更してQ2実行頻度を削減）
    q2_ran = False
    if len(all_results) < 5:
        q2 = make_q2()
        if q2 and normalize_text(q2) != normalize_text(q1 or ""):
            collect(_rakuten_criteria_search_single(q2, category))
            q2_ran = True

    step_label = str(step.get("step_name") or step.get("category") or "")
    print(
        f"[RAKUTEN FOR STEP] step={step_label!r} q1={bool(q1)} q2={q2_ran} "
        f"total={len(all_results)} elapsed={time.time()-_t_step:.2f}s",
        flush=True
    )
    # prefetch→Gemini評価の橋渡し用にステップキーで保存
    _sk = _get_step_rakuten_key(step)
    _step_rakuten_results[_sk] = all_results
    return all_results


# ================================================================
# Phase 2: Gemini バッチ商品評価
# 楽天検索結果の上位10件をステップごとに1回のGemini呼び出しで評価し
# active_ingredients / ingredient_strength / concerns / skin_types /
# sensitive_ok / main_functions / formulation を補完する
# ================================================================

_PRODUCT_EVAL_VALID_INGREDIENTS = {
    "retinol", "retinal", "vitamin_c", "niacinamide", "azelaic_acid",
    "tranexamic_acid", "peptide", "ceramide", "hyaluronic", "cica",
    "centella", "panthenol", "bha", "aha", "pha", "glycolic_acid",
    "lactic_acid", "salicylic_acid", "arbutin", "kojic_acid",
    "glutathione", "squalane", "glycerin", "amino_acid", "collagen", "enzyme",
}

_PRODUCT_EVAL_VALID_CONCERNS = {
    "dryness", "aging", "whitening", "dullness", "acne", "pores",
    "redness", "barrier", "oiliness", "texture", "firmness", "wrinkles",
    "sensitivity", "pigmentation", "acne_marks",
}

_PRODUCT_EVAL_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "idx":                {"type": "integer"},
            "active_ingredients": {"type": "array", "items": {"type": "string"}},
            "ingredient_strength": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ingredient": {"type": "string"},
                        "level":      {"type": "string"},
                    },
                    "required": ["ingredient", "level"],
                },
            },
            "concerns":      {"type": "array", "items": {"type": "string"}},
            "skin_types":    {"type": "array", "items": {"type": "string"}},
            "sensitive_ok":  {"type": "string"},
            "main_functions":{"type": "array", "items": {"type": "string"}},
            "formulation":   {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "idx", "active_ingredients", "ingredient_strength",
            "concerns", "skin_types", "sensitive_ok", "main_functions", "formulation",
        ],
    },
}


def _get_step_rakuten_key(step):
    """ステップを (norm_cat, norm_ing) のキーに変換する。"""
    cat = normalize_candidate_category(
        str(step.get("category", "") or ""), fallback=str(step.get("category", "") or "")
    )
    ing = normalize_ingredient_tag(str(step.get("ingredient_focus", "") or ""))
    return (cat, ing)


def _build_product_eval_prompt(step, products):
    category        = str(step.get("category", "") or "")
    purpose         = str(step.get("purpose", "") or "")
    ingredient_focus= str(step.get("ingredient_focus", "") or "")

    lines = []
    for i, p in enumerate(products, start=1):
        title = str(p.get("rakuten_title") or p.get("name") or "")[:80]
        price = int(safe_price(p.get("price_ref", 0)))
        lines.append(f"{i}. {title} (¥{price:,})")

    return (
        "あなたはスキンケア成分の専門家です。楽天市場の商品タイトルを評価し、"
        "各商品の成分・効果を推定してください。\n\n"
        f"【ステップ情報】カテゴリ: {category} / 目的: {purpose} / 注目成分: {ingredient_focus}\n\n"
        "【成分IDリスト】\n"
        + ", ".join(sorted(_PRODUCT_EVAL_VALID_INGREDIENTS)) + "\n\n"
        "【肌悩みタグ】\n"
        + ", ".join(sorted(_PRODUCT_EVAL_VALID_CONCERNS)) + "\n\n"
        "【機能タグ（main_functions）】\n"
        "保湿, バリア強化, 鎮静, 美白, くすみ改善, エイジングケア, ハリ補給, "
        "毛穴改善, ニキビ改善, 角質ケア, ターンオーバー促進, 皮脂コントロール, 抗酸化, 修復\n\n"
        "【処方タグ（formulation）】\n"
        "low_irritation, barrier_formula, oil_control, tone_up, mild_formula\n\n"
        "【評価対象商品】\n"
        + "\n".join(lines) + "\n\n"
        "各商品の idx（1始まり）に対して以下を推定してください:\n"
        "- active_ingredients: タイトルや成分名から推測できる有効成分IDリスト\n"
        "- ingredient_strength: 濃度表記がある成分のみ [{ingredient, level}] 形式で\n"
        "- concerns: 対応する肌悩みタグリスト\n"
        "- skin_types: 適した肌タイプ (dry/oily/combination/sensitive/normal)\n"
        "- sensitive_ok: 敏感肌向け表記や低刺激成分中心なら yes、刺激性成分多ければ no、不明なら unknown\n"
        "- main_functions: 機能タグリスト\n"
        "- formulation: 処方特性タグリスト\n"
    )


_GEMINI_EVAL_CACHE_MAX = 500  # メモリ上の最大保持件数（Renderフリープラン対応）

def _load_gemini_eval_cache_if_needed():
    """gemini_product_eval_cache.json からメモリキャッシュにロード（初回のみ、最新500件）。"""
    global _gemini_product_eval_cache, _gemini_eval_cache_loaded
    if _gemini_eval_cache_loaded:
        return
    with _gemini_eval_cache_lock:
        if _gemini_eval_cache_loaded:
            return
        try:
            with open(GEMINI_EVAL_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                now = time.time()
                # TTL内のものだけ抽出し、新しい順に最大500件をロード
                valid = [
                    (k, v) for k, v in data.items()
                    if isinstance(v, dict)
                    and now - v.get("cached_at", 0) <= GEMINI_EVAL_CACHE_TTL_SECONDS
                ]
                valid.sort(key=lambda x: x[1].get("cached_at", 0), reverse=True)
                for k, v in valid[:_GEMINI_EVAL_CACHE_MAX]:
                    _gemini_product_eval_cache[k] = {
                        key: val for key, val in v.items() if key != "cached_at"
                    }
                print(
                    f"[GEMINI EVAL CACHE] ファイルから{len(_gemini_product_eval_cache)}件ロード"
                    f"（全{len(valid)}件中）",
                    flush=True
                )
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[GEMINI EVAL CACHE LOAD ERROR] {repr(e)}", flush=True)
        _gemini_eval_cache_loaded = True


def _save_gemini_eval_cache():
    """メモリキャッシュを gemini_product_eval_cache.json に保存。"""
    with _gemini_eval_cache_lock:
        try:
            now = time.time()
            data = {}
            for k, v in _gemini_product_eval_cache.items():
                entry = dict(v)
                entry.setdefault("cached_at", now)
                data[k] = entry
            with open(GEMINI_EVAL_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"[GEMINI EVAL CACHE] {len(data)}件を保存", flush=True)
        except Exception as e:
            print(f"[GEMINI EVAL CACHE SAVE ERROR] {repr(e)}", flush=True)


# カテゴリ別Gemini評価上限（メモリ・コスト・レイテンシ削減）
_GEMINI_EVAL_LIMIT_BY_CATEGORY = {
    "美容液": 5, "化粧水": 5, "乳液": 5, "クリーム": 5,
    "洗顔": 3, "クレンジング": 3, "日焼け止め": 3, "パック": 3, "ピーリング": 3,
}
_GEMINI_EVAL_LIMIT_DEFAULT = 3


def _gemini_eval_limit(step):
    """ステップのカテゴリに応じたGemini評価上限件数を返す。"""
    category = str(step.get("category") or "").strip()
    return _GEMINI_EVAL_LIMIT_BY_CATEGORY.get(category, _GEMINI_EVAL_LIMIT_DEFAULT)


def gemini_evaluate_rakuten_batch(step, rakuten_products):
    """
    楽天商品をカテゴリ別上限件数まとめてGeminiで評価し、結果を _gemini_product_eval_cache に保存する。
    キャッシュ済みの商品はスキップする。スレッドセーフ（並列Phase B対応）。
    """
    if not rakuten_products:
        return

    limit = _gemini_eval_limit(step)

    # 未キャッシュ商品だけを対象にする（ロック内でスナップショット取得）
    with _gemini_eval_cache_lock:
        uncached = [
            p for p in rakuten_products[:limit]
            if normalize_product_name(str(p.get("rakuten_title") or p.get("name") or ""))
            not in _gemini_product_eval_cache
        ]

    if not uncached:
        step_label2 = str(step.get("step_name") or step.get("category") or "")
        print(f"[GEMINI EVAL] step={step_label2!r} all cached → skip", flush=True)
        return

    step_label = str(step.get("step_name") or step.get("category") or "")
    print(f"[GEMINI EVAL] step={step_label!r} limit={limit} evaluating {len(uncached)} products", flush=True)
    _t_gemini = time.time()

    try:
        prompt = _build_product_eval_prompt(step, uncached)
        config = types.GenerateContentConfig(
            temperature=0,
            seed=42,
            thinking_config=types.ThinkingConfig(thinking_budget=0),  # 商品評価の一貫性のためthinking無効
            max_output_tokens=1200,
            response_mime_type="application/json",
            response_schema=_PRODUCT_EVAL_SCHEMA,
        )
        response = call_gemini_with_retry(
            client, DETAIL_MODEL, prompt, config=config,
            max_retries=1, timeout=30  # 商品評価はスキップ可能なため短めのタイムアウト
        )
        if response is None or not response.text:
            return

        raw = response.text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()

        results = json.loads(raw)
        if not isinstance(results, list):
            return

        for item in results:
            if not isinstance(item, dict):
                continue
            idx = int(item.get("idx", 0)) - 1  # 1始まり→0始まり
            if idx < 0 or idx >= len(uncached):
                continue

            product = uncached[idx]
            title = str(product.get("rakuten_title") or product.get("name") or "")
            norm_title = normalize_product_name(title)
            if not norm_title:
                continue

            # ingredient_strength をリスト形式からdict形式に変換
            raw_strength = item.get("ingredient_strength", []) or []
            strength_dict = {}
            if isinstance(raw_strength, list):
                for s in raw_strength:
                    if isinstance(s, dict) and s.get("ingredient") and s.get("level"):
                        ing = str(s["ingredient"]).strip()
                        lvl = str(s["level"]).strip().lower()
                        if ing in _PRODUCT_EVAL_VALID_INGREDIENTS and lvl in ("high", "medium", "low"):
                            strength_dict[ing] = lvl

            # 有効タグのみフィルタ
            actives = [x for x in (item.get("active_ingredients") or [])
                       if x in _PRODUCT_EVAL_VALID_INGREDIENTS]
            concerns = [x for x in (item.get("concerns") or [])
                        if x in _PRODUCT_EVAL_VALID_CONCERNS]
            skin_types = [x for x in (item.get("skin_types") or [])
                          if x in ("dry", "oily", "combination", "sensitive", "normal")]
            sensitive_ok = str(item.get("sensitive_ok", "unknown") or "unknown").lower()
            if sensitive_ok not in ("yes", "no", "unknown"):
                sensitive_ok = "unknown"
            main_functions = [str(x) for x in (item.get("main_functions") or []) if x]
            formulation = [x for x in (item.get("formulation") or [])
                           if x in ("low_irritation", "barrier_formula", "oil_control",
                                    "tone_up", "mild_formula")]

            with _gemini_eval_cache_lock:
                _gemini_product_eval_cache[norm_title] = {
                    "active_ingredients": actives,
                    "ingredient_strength": strength_dict,
                    "concerns":           concerns,
                    "skin_types":         skin_types,
                    "sensitive_ok":       sensitive_ok,
                    "main_functions":     main_functions,
                    "formulation":        formulation,
                }
            print(
                f"[GEMINI EVAL] cached title={title[:40]!r} "
                f"actives={actives} sens={sensitive_ok} concerns={len(concerns)}",
                flush=True
            )

    except TimeoutError as e:
        # 商品評価タイムアウト: 診断全体は継続（Gemini評価なしで楽天商品をスコアリング）
        print(f"[GEMINI EVAL TIMEOUT] step={step_label!r} {e} → skip eval", flush=True)
    except Exception as e:
        print(f"[GEMINI EVAL ERROR] step={step_label!r} {repr(e)}", flush=True)
    finally:
        print(
            f"[GEMINI TIMING] step={step_label!r} items={len(uncached)} elapsed={time.time()-_t_gemini:.2f}s",
            flush=True
        )


def apply_gemini_product_eval(product):
    """
    _gemini_product_eval_cache からGemini評価を取得して商品dictに適用する。
    Geminiが返したフィールドで title-inference の値を上書きする。
    """
    title = str(product.get("rakuten_title") or product.get("name") or "")
    norm_title = normalize_product_name(title)
    if not norm_title:
        return product

    with _gemini_eval_cache_lock:
        eval_result = _gemini_product_eval_cache.get(norm_title)
    if not eval_result:
        return product

    # active_ingredients: Geminiの方が詳細ならマージ（既存を上書き）
    if eval_result.get("active_ingredients"):
        product["active_ingredients"] = eval_result["active_ingredients"]

    # ingredient_strength: Geminiが設定した成分のみ反映
    if eval_result.get("ingredient_strength"):
        existing = dict(product.get("ingredient_strength") or {})
        existing.update(eval_result["ingredient_strength"])
        product["ingredient_strength"] = existing

    # concerns / skin_types / main_functions / formulation: 上書き（Geminiが信頼性高い）
    if eval_result.get("concerns"):
        product["concerns"] = eval_result["concerns"]
    if eval_result.get("skin_types"):
        product["skin_types"] = eval_result["skin_types"]
    if eval_result.get("main_functions"):
        product["main_functions"] = eval_result["main_functions"]
    if eval_result.get("formulation"):
        existing_form = list(product.get("formulation") or [])
        for f in eval_result["formulation"]:
            if f not in existing_form:
                existing_form.append(f)
        product["formulation"] = existing_form

    # sensitive_ok: unknown → Geminiの値で更新
    if eval_result.get("sensitive_ok") and eval_result["sensitive_ok"] != "unknown":
        product["sensitive_ok"] = eval_result["sensitive_ok"]

    return product


def clean_product_title(text):

    remove = [

        "送料無料",
        "公式",
        "正規品",
        "ポイント10倍",
        "レビュー",

    ]

    if not text:
        return ""

    for r in remove:
        text = text.replace(r, "")

    return " ".join(
        text.split()
    )

def apply_rakuten_image_and_link(step):
    if not isinstance(step, dict):
        return step

    product_name = (
        step.get("product")
        or step.get("name")
        or step.get("product_name")
        or step.get("item_name")
        or step.get("title")
        or ""
    )

    print(
        "[RAKUTEN STEP KEYS]",
        list(step.keys()),
        flush=True
    )

    print(
        "[RAKUTEN PRODUCT NAME]",
        product_name,
        flush=True
    )
    category = step.get("category", "")

    rakuten_item = fetch_rakuten_item(
        product_name=product_name,
        category=step.get("category", ""),
        brand=step.get("brand", "")
    )
    if not rakuten_item:
        print(f"[RAKUTEN IMAGE] no rakuten item: product={product_name}, category={category}")
        return step

    if rakuten_item.get("rakuten_link"):
        step["rakuten_link"] = rakuten_item["rakuten_link"]

    current_image = step.get("image", "")
    print(
        "[IMAGE BEFORE]",
        current_image,
        flush=True
    )
    if (not current_image) or ("/static/images/products/" in str(current_image)):
        if rakuten_item.get("image"):
            step["image"] = rakuten_item["image"]
            print("[RAKUTEN IMAGE APPLIED]",product_name,"->",step["image"],flush=True)
        else:
            print("[RAKUTEN IMAGE EMPTY]",product_name,flush=True)
    else:
        print("[RAKUTEN IMAGE EMPTY]",current_image,flush=True)

    if rakuten_item.get("price"):
        step["price"] = safe_price(rakuten_item["price"])
        step["estimated_price"] = safe_price(rakuten_item["price"])
        step["price_band"] = build_price_band(step["price"])

    return step

def find_affiliate_links_for_ai_product(product_name, category, affiliate_ai_db):
    target_name = normalize_affiliate_text(product_name)
    target_category = str(category or "").strip()

    if not target_name:
        return None

    for item in affiliate_ai_db:
        if not isinstance(item, dict):
            continue

        item_category = str(item.get("category", "")).strip()
        if target_category and item_category and target_category != item_category:
            continue

        keywords = item.get("match_keywords", [])
        for kw in keywords:
            kw_norm = normalize_affiliate_text(kw)
            if not kw_norm:
                continue

            if kw_norm in target_name or target_name in kw_norm:
                return item.get("affiliate_links", {})

    return None


def infer_brand_from_image(image_url: str, product_name: str, category: str = "") -> str:
    """商品画像のURLからGeminiでブランド名を読み取る。
    画像DL失敗・Gemini失敗時は空文字を返す。タイムアウトは短めに設定してレスポンスを妨げない。
    infer_brand_from_title() とキャッシュを共有する（同じ商品名なら再検索しない）。"""
    if not image_url:
        return ""

    _normalized_name = normalize_product_name(product_name)
    cache_key = f"brand_v1:{_normalized_name}" if _normalized_name else ""
    if cache_key:
        cached = get_cached_brand(cache_key)
        if cached is not None:
            return cached

    try:
        import base64 as _b64
        _r = requests.get(image_url, timeout=(2, 5))
        if _r.status_code != 200 or not _r.content:
            return ""
        mime_type = (_r.headers.get("Content-Type", "image/jpeg") or "image/jpeg").split(";")[0].strip()
        img_bytes = _b64.b64decode(_b64.b64encode(_r.content))
        _prompt = (
            f"この商品画像に写っているブランド名・メーカー名を日本語または英語で1つだけ答えてください。"
            f"商品名は「{product_name}」です。"
            f"ブランド名が読み取れない場合は空文字を返してください。"
            f"余計な説明は不要です。ブランド名のみ回答してください。"
        )
        _response = call_gemini_with_retry(
            client=client,
            model=DETAIL_MODEL,
            contents=[
                types.Part.from_bytes(data=img_bytes, mime_type=mime_type),
                _prompt,
            ],
            config=types.GenerateContentConfig(temperature=0, max_output_tokens=30),
            max_retries=1,
            timeout=10,
        )
        _brand = (_response.text or "").strip()
        _brand = _brand.replace("「", "").replace("」", "").replace('"', "").replace("'", "").strip()
        if not _brand or len(_brand) > 40:
            return ""
        print(f"[BRAND INFER FROM IMAGE] {product_name} → {_brand}", flush=True)
        save_brand_to_cache(cache_key, _brand)
        return _brand
    except Exception as _e:
        print(f"[BRAND INFER FROM IMAGE ERROR] {_e}", flush=True)
        return ""


def infer_brand_from_title(product_name: str, category: str = "") -> str:
    """商品名（楽天タイトル等）だけからGeminiでブランド名を推測する。
    画像取得を伴わない軽量版。楽天検索由来の候補はブランド欄が常に空文字のため、
    商品名は分かっているのにブランドだけ欠落しているケースを補完する目的。
    Gemini失敗時は空文字を返す。"""
    product_name = str(product_name or "").strip()
    if not product_name:
        return ""

    cache_key = f"brand_v1:{normalize_product_name(product_name)}"
    cached = get_cached_brand(cache_key)
    if cached is not None:
        return cached

    try:
        _prompt = (
            f"次の日本のスキンケア商品名から、ブランド名・メーカー名を1つだけ答えてください。\n"
            f"商品名: 「{product_name}」\n"
            f"カテゴリ: {category or '不明'}\n"
            f"ブランド名が商品名から確実に読み取れない場合は空文字を返してください。"
            f"余計な説明は不要です。ブランド名のみ回答してください。"
        )
        _response = call_gemini_with_retry(
            client=client,
            model=DETAIL_MODEL,
            contents=[_prompt],
            config=types.GenerateContentConfig(temperature=0, max_output_tokens=30),
            max_retries=1,
            timeout=8,
        )
        _brand = (_response.text or "").strip()
        _brand = _brand.replace("「", "").replace("」", "").replace('"', "").replace("'", "").strip()
        if not _brand or len(_brand) > 40:
            return ""
        if _brand.strip() in _GENERIC_CATEGORY_NAMES:
            return ""
        print(f"[BRAND INFER FROM TITLE] {product_name} → {_brand}", flush=True)
        save_brand_to_cache(cache_key, _brand)
        return _brand
    except Exception as _e:
        print(f"[BRAND INFER FROM TITLE ERROR] {_e}", flush=True)
        return ""


def attach_affiliate_links_to_step(step, affiliate_ai_db):
    if not isinstance(step, dict):
        return step

    product_name = str(step.get("product", "") or "").strip()
    category = str(step.get("category", "") or "").strip()
    brand = str(step.get("brand", "") or "").strip()

    if not brand:
        top_candidates = step.get("top_candidates", [])
        if isinstance(top_candidates, list) and top_candidates:
            first_candidate = top_candidates[0]
            if isinstance(first_candidate, dict):
                brand = str(first_candidate.get("brand", "") or "").strip()
                if brand:
                    step["brand"] = brand

    if not product_name:
        step["amazon_link"] = ""
        step["rakuten_link"] = ""
        return step

    existing_rakuten_link = str(step.get("rakuten_link", "") or "").strip()

    existing_image = str(step.get("image", "") or "").strip()
    product_source = str(step.get("product_source", "") or "").strip()

    # Only skip Rakuten fetch when we have a REAL image (not SVG placeholder) + link
    _is_placeholder = "/static/images/products/" in existing_image
    if existing_image and not _is_placeholder and existing_rakuten_link:
        step["amazon_link"] = build_amazon_link(product_name)
        return normalize_step_price_fields(step)

    if product_source not in ["db", "ai+db", "fallback_db"]:
        if "affiliate_links" in step and isinstance(step["affiliate_links"], dict):
            step["amazon_link"] = step["affiliate_links"].get("amazon", "")
            step["rakuten_link"] = step["affiliate_links"].get("rakuten", "")
            existing_rakuten_link = step["rakuten_link"]

            # 画像＋楽天リンクの両方が揃っている場合のみ早期return
            if existing_image and existing_rakuten_link:
                step["amazon_link"] = build_amazon_link(product_name)
                return normalize_step_price_fields(step)

        matched_links = find_affiliate_links_for_ai_product(
            product_name,
            category,
            affiliate_ai_db
        )

        if matched_links:
            step["amazon_link"] = matched_links.get("amazon", "")
            step["rakuten_link"] = matched_links.get("rakuten", "")
            existing_rakuten_link = step["rakuten_link"]

            # 画像＋楽天リンクの両方が揃っている場合のみ早期return
            if existing_image and existing_rakuten_link:
                step["amazon_link"] = build_amazon_link(product_name)
                return normalize_step_price_fields(step)

    rakuten_item = fetch_rakuten_item(
        product_name=product_name,
        category=category,
        brand=brand
    )

    if rakuten_item:
        new_rakuten_link = str(rakuten_item.get("rakuten_link", "") or "").strip()

        if new_rakuten_link:
            step["rakuten_link"] = new_rakuten_link

        if rakuten_item.get("image"):
            step["image"] = rakuten_item.get("image", "")

        # 楽天リンク取得後もブランドが不明なら画像から推測して補完
        if not str(step.get("brand", "") or "").strip() and step.get("image"):
            _inferred = infer_brand_from_image(
                step["image"], product_name, category
            )
            if _inferred:
                step["brand"] = _inferred

        if safe_price(step.get("price", 0)) <= 0:
            step["price"] = safe_price(rakuten_item.get("price", 0))

        if safe_price(step.get("estimated_price", 0)) <= 0:
            step["estimated_price"] = safe_price(rakuten_item.get("price", 0))

        step["raw_price"] = safe_price(rakuten_item.get("raw_price", 0))
        step["bundle_quantity"] = safe_bundle_quantity(
            rakuten_item.get("bundle_quantity", 1)
        )

        try:
            verified_product = build_verified_product_from_step(
                step,
                rakuten_item
            )

            if verified_product:
                upsert_verified_product_cache(verified_product)

        except Exception as e:
            print("[VERIFIED CACHE UPSERT ERROR]", e, flush=True)

    else:
        step["rakuten_link"] = existing_rakuten_link
        step["image"] = existing_image

    step["amazon_link"] = build_amazon_link(product_name)

    return normalize_step_price_fields(step)

   
def _try_rakuten_fallback_candidate(step, affiliate_ai_db):
    """
    主商品のRakutenリンク取得が（リトライ後も）失敗した場合、
    top_candidates の 2位・3位を順に試してリンクを取得する。
    成功した場合はステップの product / brand / image / price を次点商品に更新する。
    クールダウン中は呼び出さないこと（呼び出し元で確認する）。
    """
    cands = step.get("top_candidates") or []
    for fallback in cands[1:3]:
        if not isinstance(fallback, dict):
            continue
        fb_name = clean_display_product_name(str(fallback.get("name", "") or "").strip())
        fb_brand = str(fallback.get("brand", "") or "").strip()
        if not fb_name:
            continue

        print(f"[RAKUTEN FALLBACK] trying next candidate: {fb_name}", flush=True)
        rakuten_item = fetch_rakuten_item(
            product_name=fb_name,
            category=step.get("category", ""),
            brand=fb_brand,
        )

        if not rakuten_item or not rakuten_item.get("rakuten_link"):
            continue

        # 次点でリンク取得成功 → ステップを差し替え
        print(
            f"[RAKUTEN FALLBACK OK] {step.get('product', '?')} → {fb_name}",
            flush=True
        )
        step["product"] = fb_name
        if fb_brand:
            step["brand"] = fb_brand
        step["rakuten_link"] = rakuten_item["rakuten_link"]
        step["amazon_link"] = build_amazon_link(fb_name)
        if rakuten_item.get("image"):
            step["image"] = rakuten_item["image"]
        if rakuten_item.get("price"):
            step["price"] = safe_price(rakuten_item["price"])
            step["estimated_price"] = safe_price(rakuten_item["price"])
            step["price_band"] = build_price_band(step["price"])
        step["product_source"] = "rakuten_fallback"

        # top_candidates を差し替え後の状態に同期する。
        # ここを更新しないと、繰り上がった商品が「1位」として表示されつつ、
        # 古い top_candidates[1:3] にもそのまま残って「2位」等に重複表示されてしまう。
        step["top_candidates"] = [fallback] + [
            c for c in cands if c is not fallback
        ]
        return True

    return False


def attach_affiliate_links_to_all_steps(data, affiliate_ai_db):
    """
    全ステップにアフィリエイトリンクを付与する。

    処理順:
    1. 全ステップを順次処理（fetch_rakuten_item 内で429時は待機+1回リトライ）
    2. クールダウンでスキップされたステップをクールダウン解消後に再試行
    3. 再試行後もリンクなし かつ クールダウン解消済み → top_candidates次点へフォールバック
    """
    skipped = []  # クールダウンでスキップされたステップ

    def _attach(step):
        if not isinstance(step, dict):
            return
        link_before = str(step.get("rakuten_link", "") or "")
        attach_affiliate_links_to_step(step, affiliate_ai_db)
        if not str(step.get("rakuten_link", "") or "") and not link_before:
            if time.time() < RAKUTEN_COOLDOWN_UNTIL:
                skipped.append(step)

    for section in ["morning", "night"]:
        for step in data.get(section, {}).get("steps", []):
            _attach(step)

    for step in data.get("weekly_care", []):
        _attach(step)

    for step in data.get("supplements", []):
        _attach(step)

    for step in data.get("beauty_devices", []):
        _attach(step)

    # クールダウン解消後に再試行（最大30秒待機）
    if skipped and RAKUTEN_COOLDOWN_UNTIL > time.time():
        wait_sec = min(RAKUTEN_COOLDOWN_UNTIL - time.time() + 0.5, 30)
        print(
            f"[RAKUTEN COOLDOWN] waiting {wait_sec:.1f}s then retrying {len(skipped)} skipped steps",
            flush=True
        )
        time.sleep(wait_sec)

    still_no_link = []
    for step in skipped:
        attach_affiliate_links_to_step(step, affiliate_ai_db)
        if not str(step.get("rakuten_link", "") or ""):
            still_no_link.append(step)

    # ③ 再試行後もリンクなし かつ クールダウンが解消されている → 次点候補へフォールバック
    if still_no_link:
        cooldown_cleared = time.time() >= RAKUTEN_COOLDOWN_UNTIL
        for step in still_no_link:
            if cooldown_cleared:
                _try_rakuten_fallback_candidate(step, affiliate_ai_db)
            else:
                print(
                    f"[RAKUTEN FALLBACK SKIP] cooldown still active for {step.get('product', '?')}",
                    flush=True
                )

    return data


# 楽天商品名 Gemini 整形キャッシュ（同一タイトルを複数回処理しない）
_rakuten_name_clean_cache: dict = {}


def _rule_based_clean_rakuten_title(title: str) -> str:
    """ルールベースで楽天商品名をクリーニング（Gemini失敗・部分欠損時のフォールバック）"""
    t = title
    # 【...】《...》『...』〔...〕を繰り返し除去（複数ブロック対応）
    for _ in range(5):
        t2 = re.sub(r'[【《『〔][^】》』〕]{0,80}[】》』〕]', '', t)
        if t2 == t:
            break
        t = t2
    # ※ から始まる注釈を除去（行末または次の日本語区切りまで）
    t = re.sub(r'※[^※\n]{0,120}', '', t)
    # 先頭の装飾記号
    t = re.sub(r'^[\s　★☆◆◇▼▽△▲●○■□♪♦♥❤✨💕🌸]+', '', t)
    # 各種マーケティング文言（順番に除去）
    _junk = [
        r'送料無料', r'ポイント\s*\d+\s*[倍%]?', r'レビュー(?:特典|プレゼント|で(?:もらえる|プレ))',
        r'(?:メーカー|国内)?公式', r'正規(?:品|代理店|輸入品)', r'国内正規',
        r'(?:期間|数量|在庫)?限定', r'タイムセール', r'スーパーSALE?', r'クーポン(?:使用可)?',
        r'\d+\s*%\s*OFF', r'\d+\s*円\s*(?:OFF|引き|割引)',
        r'お買い?得', r'特価', r'激安', r'最安値?',
        r'あす楽(?:対応)?', r'即日(?:出荷|発送)', r'翌日(?:配送|お届け)', r'最短翌日',
        r'楽天\d+冠', r'ランキング\s*\d+\s*位', r'売れ筋',
        r'新品', r'未使用',
    ]
    for p in _junk:
        t = re.sub(p, '', t)
    # 連続スペース・全角スペースを整理
    t = re.sub(r'[\s　]+', ' ', t).strip()
    # 残った先頭・末尾の記号・区切り文字を除去
    t = t.strip('/ ・|｜,，、。　 ')
    return t if t.strip() else title


_GEMINI_NAME_CLEAN_PROMPT_PREFIX = """\
以下の楽天市場の商品タイトルから、「商品を正確に識別できる名称」を抽出してください。

【最重要原則】
省略しすぎるより、情報が多い方がよい。
「ロゼット ゴマージュ」だけでは複数の商品が存在する場合、製品ラインを示す語（クリアピール・ブライトピール等）も必ず残す。
商品を一意に特定できないほど短くなった場合は、必要な語を補って出力する。

【残すもの（必ず保持）】
  ① ブランド名・メーカー名
  ② 製品名・シリーズ名（固有名詞）
  ③ 製品ライン名・サブ名称（同ブランド内で商品を区別する語）
      例: クリアピール / ブライトピール / モイストリペア / エクストラリッチ / VC100 など
  ④ 商品カテゴリを示す語（ゴマージュ / エッセンス / ローション / セラム など）
  ⑤ バリエーションを区別する語（I / II / EX / N / リッチ / ライト など）
  ⑥ 主要な成分名が製品名に含まれる場合（アゼライン酸化粧水・BHAスクラブ など）

【削除するもの（これだけ削除）】
  - 容量・内容量・個数（100mL、30g、2個セット、3本組 など）
  - 価格・割引・クーポン（500円OFF、クーポンで●円、税込●円 など）
  - ポイント・特典（ポイント3倍、P10倍、レビュー特典 など）
  - 送料・配送情報（送料無料、即日発送 など）
  - 販促ラベル（公式、正規品、楽天1位、ランキング1位、★受賞 など）
  - 括弧・装飾記号の中身（【】《》『』〔〕の内容ごと除去）
  - 製品名とは独立した汎用キャッチコピー（「うるおい」「美白効果」「エイジングケア」などの後付け説明文。ただし製品名に組み込まれている語は除かない）
  - 記号・装飾（★☆◆◇▼▽●○■□ など単体で意味を持たないもの）

【製品ライン名を保持すべき具体例（重要）】
  ※「ピール」「クリア」「ブライト」等の語が含まれていても、製品を区別する固有名なら削除しない
  入力: ロゼット ゴマージュ クリアピール 90g 送料無料 ポイント3倍 楽天1位
  出力: ロゼット ゴマージュ クリアピール   ← 「クリアピール」は製品ラインなので保持

  入力: ロゼット ゴマージュ ブライトピール 90g 【公式】
  出力: ロゼット ゴマージュ ブライトピール   ← ライン名を保持

  入力: コーセー モイスチュア マイルド ホワイト ローション III 200mL 敏感肌 送料無料
  出力: コーセー モイスチュア マイルド ホワイト ローション III   ← 「III」はバリエーション、保持

【ブランド名の形式】
以下はすべてブランド名として保持:
  - 小文字英字のみ（o.cos, cosrx, some by mi, b.glen）
  - ピリオド・ハイフン含む（SK-II, by.S, b-ex）
  - 記号含む短い名前（&be, #be, de+in）
  - 漢字1〜2文字（雪肌精、澄肌）

【出力例】
入力: 資生堂 ベネフィーク ローションI 200mL 【送料無料】ポイント10倍 楽天1位
出力: 資生堂 ベネフィーク ローションI

入力: o.cos アゼライン酸化粧水 100mL 送料無料 楽天ランキング1位 美白 毛穴
出力: o.cos アゼライン酸化粧水

入力: COSRX AHA 7 ホワイトヘッドパワーリキッド 100mL ポイント3倍 お試し価格
出力: COSRX AHA 7 ホワイトヘッドパワーリキッド

入力: ☆クーポンで1980円★ コーセー アルビオン エクサージュ モイスチュア ミルク 200mL 500円OFF
出力: コーセー アルビオン エクサージュ モイスチュア ミルク

入力: SK-II フェイシャル トリートメント エッセンス 230mL 正規品 母の日ギフト ポイント3倍
出力: SK-II フェイシャル トリートメント エッセンス

入力: 【公式】オルビス ユードット ウォッシュ 120g 詰め替え 2個セット お得
出力: ORBIS(オルビス) ユードット ウォッシュ

入力: 【楽天ランキング1位】ドクターシーラボ VC100エッセンスローションEX 120mL 美白 うるおい
出力: ドクターシーラボ VC100エッセンスローションEX

入力: 無印良品 敏感肌用化粧水 高保湿タイプ 200mL スキンケア セット 送料無料
出力: 無印良品 敏感肌用化粧水 高保湿タイプ

入力: &be ファンデーション リキッド 30mL 正規品 送料無料 楽天1位
出力: &be ファンデーション リキッド

入力: 花王 キュレル 潤浸保湿 フェイスクリーム 40g 敏感肌 乾燥肌 うるおい補給 送料無料
出力: 花王 キュレル 潤浸保湿 フェイスクリーム

【回答形式】
番号: 抽出した「ブランド名 製品名」（1行1件、それ以外の説明は不要）

対象リスト:
"""


_ALL_DAYS = ["月", "火", "水", "木", "金", "土", "日"]
# レチノール系は retinol / retinal / retinoid どの表記でも検出できるよう全バリアントを含める
# AHA/BHA系も aha / bha / aha_bha 全パターン対応
_IRRITANT_FOCUS_TAGS = {"retinoid", "retinol", "retinal", "aha_bha", "aha", "bha", "pha"}

# 「成分名+カテゴリ名」パターン検出用カテゴリ集合
# 例: "セラミド 乳液" "ナイアシンアミド 美容液" "ビタミンC 化粧水"
_SKINCARE_CATEGORY_SUFFIXES = {
    "洗顔", "洗顔料", "化粧水", "美容液", "乳液", "クリーム", "保湿クリーム",
    "日焼け止め", "クレンジング", "パック", "マスク", "ピーリング",
    "導入美容液", "美顔器", "サプリ", "サプリメント",
}

def _is_ingredient_category_name(product_name: str) -> bool:
    """
    製品名が「成分名+カテゴリ名」または「カテゴリ名のみ」パターンかを判定。
    このパターンは楽天タイトルに成分名が含まれないため is_same_verified_rakuten_product を
    通ると全件 False になる。score_rakuten_item のカテゴリ固有チェックに委ねる。
    例: "セラミド 乳液" "ナイアシンアミド 美容液" "ビタミンC 化粧水" → True
        "キュレル 潤浸保湿乳液" "COSRX スネイルムチン96エッセンス" → False
    """
    name = (product_name or "").strip()
    for cat in _SKINCARE_CATEGORY_SUFFIXES:
        if name == cat:
            return True
        if name.endswith(" " + cat) or name.endswith("　" + cat):
            return True
    return False


def resolve_weekly_care_day_conflicts(data):
    """
    週ケア（ピーリング）と night の刺激成分（レチノイド・AHA/BHA/PHA）の曜日衝突を解消。

    ケースA: 刺激成分が特定曜日 かつ ピーリングも特定曜日 → 重複あればピーリングを安全日へ移動
    ケースB: 刺激成分が毎日(use_days=[]) かつ ピーリングが特定曜日
             → 刺激成分のuse_daysからピーリング曜日を除外する（最小変更）
             → 例: レチノール毎日 + ピーリング["土"] → レチノール["月","火","水","木","金","日"]
    """
    # ---- ケースA: 刺激成分の明示的曜日を収集 ----
    irritant_days: set = set()
    # ケースB 用: 毎日使用の刺激ステップを別に収集
    every_day_irritant_steps: list = []

    for step in data.get("night", {}).get("steps", []):
        if not isinstance(step, dict):
            continue
        raw_focus = step.get("ingredient_focus") or []
        # ingredient_focus が文字列で返ることがあるので必ずリスト化
        focus = set(as_list(raw_focus))
        if not focus & _IRRITANT_FOCUS_TAGS:
            continue
        days = step.get("use_days") or []
        if days:
            irritant_days.update(days)
        else:
            every_day_irritant_steps.append(step)

    # ---- ケースA: ピーリング側を安全な曜日へ移動 ----
    if irritant_days:
        for step in data.get("weekly_care", []):
            if not isinstance(step, dict):
                continue
            if step.get("category") not in ["ピーリング", "パック"]:
                continue
            use_days = list(step.get("use_days") or [])
            if not use_days:
                continue
            conflict = set(use_days) & irritant_days
            if not conflict:
                continue
            safe = [d for d in ["日", "月", "火", "水", "木", "金", "土"]
                    if d not in irritant_days]
            if not safe:
                continue
            new_days = [safe[0]]
            print(
                f"[DAY CONFLICT A] {step.get('product', '週ケア')} {use_days} → {new_days} "
                f"(irritant_days={sorted(irritant_days)})",
                flush=True,
            )
            step["use_days"] = new_days

    # ---- ケースB: 刺激成分が毎日 → ピーリング曜日を刺激成分から除外 ----
    if every_day_irritant_steps:
        peeling_days: set = set()
        for step in data.get("weekly_care", []):
            if not isinstance(step, dict):
                continue
            if step.get("category") not in ["ピーリング", "パック"]:
                continue
            days = step.get("use_days") or []
            if days:
                peeling_days.update(days)

        if peeling_days:
            new_irritant_days = [d for d in _ALL_DAYS if d not in peeling_days]
            for step in every_day_irritant_steps:
                print(
                    f"[DAY CONFLICT B] {step.get('product', '刺激成分')} "
                    f"use_days=[] → {new_irritant_days} "
                    f"(excluding peeling_days={sorted(peeling_days)})",
                    flush=True,
                )
                step["use_days"] = new_irritant_days

    return data


def resolve_night_irritant_conflicts(data):
    """
    夜ルーティン内の刺激成分同士の曜日衝突を優先順位ベースで汎用的に解消。

    優先順位（高い順に固定し、低い方を別曜日へ移動）:
      1. レチノイド（retinol/retinal/retinoid）
      2. 高濃度ビタミンC（vitamin_c/strong_vitamin_c）
      3. アゼライン酸（azelaic_acid）
      4. AHA/BHA/PHA（bha/aha/aha_bha/pha）
    """
    _PRIORITY_GROUPS = [
        ({"retinoid", "retinol", "retinal"},          "レチノイド"),
        ({"vitamin_c", "strong_vitamin_c"},            "高濃度ビタミンC"),
        ({"azelaic_acid"},                             "アゼライン酸"),
        ({"aha_bha", "aha", "bha", "pha"},             "AHA/BHA/PHA"),
    ]

    night_steps = [s for s in data.get("night", {}).get("steps", []) if isinstance(s, dict)]

    # 優先度の高いグループから順に「確定済み曜日」を積み上げ、
    # 低いグループが重複していたら安全な曜日へ移動する
    fixed_days: set = set()

    for tags, label in _PRIORITY_GROUPS:
        conflict_steps = []
        for step in night_steps:
            focus = set(as_list(step.get("ingredient_focus") or []))
            days = step.get("use_days") or []
            if not (focus & tags) or not days:
                continue
            if set(days) & fixed_days:
                conflict_steps.append(step)

        for step in conflict_steps:
            use_days = list(step.get("use_days") or [])
            safe = [d for d in _ALL_DAYS if d not in fixed_days]
            if not safe:
                continue
            new_days = safe[:max(len(use_days), 1)]
            print(
                f"[NIGHT CONFLICT] {step.get('product','夜ステップ')} ({label}) "
                f"{use_days} → {new_days} (fixed={sorted(fixed_days)})",
                flush=True,
            )
            step["use_days"] = new_days

        # このグループの（調整後の）曜日を fixed_days に追加
        for step in night_steps:
            focus = set(as_list(step.get("ingredient_focus") or []))
            days = step.get("use_days") or []
            if focus & tags and days:
                fixed_days.update(days)

    return data


def gemini_clean_rakuten_product_names(data):
    """楽天商品タイトルからブランド名+製品名のみをGeminiで抽出する。
    1位ステップ（rakuten_criteria / ai_rakuten_verified）と
    top_candidates の2位・3位候補名を同一バッチで処理する。
    """
    all_steps = []
    for section in ["morning", "night"]:
        for s in data.get(section, {}).get("steps", []):
            if isinstance(s, dict):
                all_steps.append(s)
    for s in data.get("weekly_care", []):
        if isinstance(s, dict):
            all_steps.append(s)

    print(f"[NAME CLEAN] 全ステップ数={len(all_steps)} sources={[s.get('product_source','') for s in all_steps]}", flush=True)

    # --- 対象タイトルを収集 ---
    # items: {"title": str, "apply": callable(cleaned)}
    items = []

    # 1位ステップの product 名
    for step in all_steps:
        source = step.get("product_source", "")
        if source not in ["ai_rakuten_verified", "rakuten_criteria"]:
            continue
        raw_title = str(step.get("rakuten_title", "") or step.get("product", "") or "").strip()
        if not raw_title:
            continue
        _step = step  # closure capture
        items.append({
            "title": raw_title,
            "apply": lambda cleaned, s=_step: s.update({"product": cleaned}),
        })

    # 2位・3位候補の name フィールド（top_candidates[1:3]）
    for step in all_steps:
        for cand in (step.get("top_candidates") or [])[1:3]:
            if not isinstance(cand, dict):
                continue
            raw_title = str(cand.get("name", "") or "").strip()
            if not raw_title:
                continue
            _cand = cand
            items.append({
                "title": raw_title,
                "apply": lambda cleaned, c=_cand: c.update({"name": cleaned}),
            })

    print(f"[NAME CLEAN] 整形対象={len(items)}件 (1位ステップ+2位3位候補)", flush=True)

    if not items:
        return data

    # キャッシュ済みを即適用
    uncached = []
    for item in items:
        if item["title"] in _rakuten_name_clean_cache:
            cleaned = _rakuten_name_clean_cache[item["title"]]
            item["apply"](cleaned)
            print(f"[NAME CLEAN CACHE HIT] {item['title'][:40]} -> {cleaned}", flush=True)
        else:
            uncached.append(item)

    print(f"[NAME CLEAN] キャッシュヒット={len(items)-len(uncached)}件 Gemini送信={len(uncached)}件", flush=True)

    if not uncached:
        return data

    lines = [f"{i+1}. {t['title']}" for i, t in enumerate(uncached)]
    prompt = _GEMINI_NAME_CLEAN_PROMPT_PREFIX + "\n".join(lines)
    print(f"[NAME CLEAN] Gemini送信タイトル: {[t['title'][:30] for t in uncached]}", flush=True)

    gemini_applied = set()
    try:
        config = types.GenerateContentConfig(
            temperature=0.0,   # 抽出タスクは決定論的に
            max_output_tokens=2000,
        )
        response = call_gemini_with_retry(client, DETAIL_MODEL, prompt, config=config)
        resp_text = response.text.strip() if response and response.text else ""
        print(f"[NAME CLEAN GEMINI RESPONSE] {resp_text[:400]}", flush=True)

        for line in resp_text.split("\n"):
            line = line.strip()
            m = re.match(r'^(\d+)[.:）\s]+(.+)$', line)
            if not m:
                continue
            idx = int(m.group(1)) - 1
            cleaned = m.group(2).strip()
            if 0 <= idx < len(uncached) and cleaned:
                original_title = uncached[idx]["title"]
                _rakuten_name_clean_cache[original_title] = cleaned
                uncached[idx]["apply"](cleaned)
                gemini_applied.add(idx)
                print(f"[NAME CLEAN OK] {original_title[:40]} -> {cleaned}", flush=True)

        print(f"[NAME CLEAN] Gemini整形 {len(gemini_applied)}/{len(uncached)}件適用", flush=True)

    except Exception as e:
        print(f"[NAME CLEAN GEMINI ERROR] {e}", flush=True)

    # Gemini未処理分にルールベースフォールバック
    fallback_count = 0
    for i, item in enumerate(uncached):
        if i not in gemini_applied:
            cleaned = _rule_based_clean_rakuten_title(item["title"])
            _rakuten_name_clean_cache[item["title"]] = cleaned
            item["apply"](cleaned)
            fallback_count += 1
            print(f"[NAME CLEAN FALLBACK] {item['title'][:50]} -> {cleaned}", flush=True)
    if fallback_count:
        print(f"[NAME CLEAN] フォールバック適用 {fallback_count}件", flush=True)

    # クリーニング後に top_candidates 内の重複を除去する
    # （クリーニング前は別タイトルでも、クリーニング後に同名になるケースに対処）
    dedup_removed = 0
    for step in all_steps:
        cands = step.get("top_candidates")
        if not isinstance(cands, list) or len(cands) <= 1:
            continue
        seen_cleaned: set = set()
        deduped = []
        for cand in cands:
            key = normalize_candidate_name_for_merge(
                f"{cand.get('brand', '')} {cand.get('name', '')}".strip()
            )
            if not key or key in seen_cleaned:
                print(f"[TOP_CAND DEDUP] post-clean duplicate removed: {cand.get('name', '')}", flush=True)
                dedup_removed += 1
                continue
            seen_cleaned.add(key)
            deduped.append(cand)
        step["top_candidates"] = deduped
    if dedup_removed:
        print(f"[NAME CLEAN] クリーニング後重複除去 {dedup_removed}件", flush=True)

    return data


# ===== Gemini 選定理由・比較文生成 =====
_SELECTION_REASON_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "step_idx":         {"type": "integer"},
            "recommend_reason": {"type": "string"},
            "purpose":          {"type": "string"},
        },
        "required": ["step_idx", "recommend_reason", "purpose"],
    }
}

_INGREDIENT_LABEL_JA = {
    "retinol": "レチノール", "retinal": "レチナール", "vitamin_c": "ビタミンC",
    "niacinamide": "ナイアシンアミド", "azelaic_acid": "アゼライン酸",
    "tranexamic_acid": "トラネキサム酸", "ceramide": "セラミド",
    "hyaluronic_acid": "ヒアルロン酸", "peptide": "ペプチド", "pdrn": "PDRN",
    "aha": "AHA(グリコール酸等)", "bha": "BHA(サリチル酸等)",
    "pha": "PHA", "cica": "CICA(ツボクサ)", "centella": "センテラ",
    "glycerin": "グリセリン", "collagen": "コラーゲン",
}

def _fmt_candidate_for_gemini(c, rank):
    """top_candidatesの1件をGemini向け短文に整形"""
    name = c.get("name", "不明")
    brand = c.get("brand", "")
    label = f"{brand} {name}".strip() if brand else name
    ings = [_INGREDIENT_LABEL_JA.get(i, i) for i in (c.get("active_ingredients") or [])[:4]]
    funcs = (c.get("main_functions") or [])[:2]
    score_total = c.get("score", 0)
    score_base = c.get("base_score", 0)
    score_improve = c.get("improve_score", 0)
    score_routine = c.get("routine_score", 0)
    func_str = "・".join(funcs) if funcs else ""
    sens = "低刺激" if c.get("sensitive_ok") == "yes" else ("刺激あり" if c.get("sensitive_ok") == "no" else "")
    parts = [f"{rank}位: {label}"]
    if ings: parts.append(f"成分={'・'.join(ings)}")
    if func_str: parts.append(f"機能={func_str}")
    if sens: parts.append(sens)
    parts.append(f"スコア={score_total}(適合={score_base}/改善={score_improve}/相乗={score_routine})")
    return " / ".join(parts)

def gemini_generate_selection_reasons(data, user_data):
    """
    全ステップのTOP3候補をGeminiに渡し、選定理由と1位vs2位比較文を生成する。
    既存のrecommend_reasonを上書きし、vs_2nd_reasonを追加する。
    """
    all_steps = []
    for section in ["morning", "night"]:
        for s in data.get(section, {}).get("steps", []):
            if isinstance(s, dict):
                all_steps.append(s)
    for s in data.get("weekly_care", []):
        if isinstance(s, dict):
            all_steps.append(s)

    # 商品が選ばれているステップのみ対象
    target_steps = [
        (i, s) for i, s in enumerate(all_steps)
        if s.get("product") and s.get("product_source") not in ("none", "")
    ]

    if not target_steps:
        return data

    _concerns_ja = ", ".join([_CONCERN_LABELS_JA.get(c, c) for c in (user_data.get("concerns") or [])]) or "なし"
    user_info = (
        f"年齢:{user_data.get('age','')} 皮脂:{user_data.get('oil','')} "
        f"敏感度:{user_data.get('sens','')} 悩み:{_concerns_ja}"
    )

    steps_desc = []
    idx_map = {}  # gemini_idx → (all_steps idx, step dict)
    for gemini_idx, (orig_idx, step) in enumerate(target_steps):
        top = step.get("top_candidates", [])
        candidates_text = []
        allowed_ings = set()
        for rank, c in enumerate(top[:3], 1):
            candidates_text.append(_fmt_candidate_for_gemini(c, rank))
            for ing in (c.get("active_ingredients") or []):
                allowed_ings.add(_INGREDIENT_LABEL_JA.get(ing, ing))
        if not candidates_text:
            candidates_text.append(f"1位: {step.get('product','')} / 候補なし")

        allowed_str = "・".join(sorted(allowed_ings)) if allowed_ings else "記載なし"

        steps_desc.append(
            f"[step{gemini_idx}] カテゴリ:{step.get('category','')}\n"
            f"  言及可能成分（このstepの1位商品のみ）: {allowed_str}\n"
            + "\n".join(f"  {t}" for t in candidates_text)
        )
        idx_map[gemini_idx] = step

    prompt = f"""日本語で回答してください。スキンケアアプリの商品選定AIです。

ユーザー情報: {user_info}

以下の各ステップ(step0〜)について、1位商品の選定理由と目的をJSONで生成してください。
各ステップには「1位: 商品名 / 成分=X / 機能=Y」と「言及可能成分」が記載されています。

{chr(10).join(steps_desc)}

【絶対厳守ルール】
- recommend_reasonとpurposeは必ず各step固有の「言及可能成分」欄に記載された成分・機能のみに基づいて書く。
- 「言及可能成分」に記載されていない成分（レチノール・AHA・ビタミンC・ナイアシンアミド等）を書くことは絶対禁止。
- 他stepの商品・成分（例: 美容液stepのレチノール）を別stepの理由に書くことは絶対禁止。
- 日焼け止めstepの理由に「レチノール」「AHA」「ビタミンC」等のスキンケア成分を書いてはいけない。UV成分・SPF・PAのみに言及すること。
- 1位商品がセラミド・ペプチド系なら保湿・バリア観点で書く。UV系なら紫外線防御観点で書く。成分に忠実に。

出力フィールド:
- recommend_reason: 1位商品が選ばれた理由。成分名・機能を具体的に。50-80字。
- purpose: 1位商品の成分・機能に基づく使用目的。商品名を含めず30-50字。

JSONのみ返す。形式: [{{"step_idx":0,"recommend_reason":"...","purpose":"..."}}]"""

    try:
        config = types.GenerateContentConfig(
            temperature=0,
            seed=42,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            max_output_tokens=1500,
            response_mime_type="application/json",
            response_schema=_SELECTION_REASON_SCHEMA,
        )
        response = call_gemini_with_retry(
            client, DETAIL_MODEL, prompt, config=config,
            max_retries=1, timeout=45
        )
        if not response or not response.text:
            return data

        results = json.loads(response.text.strip())
        if not isinstance(results, list):
            return data

        applied = 0
        for item in results:
            if not isinstance(item, dict):
                continue
            idx = int(item.get("step_idx", -1))
            step = idx_map.get(idx)
            if step is None:
                continue
            reason = str(item.get("recommend_reason", "")).strip()
            purpose = str(item.get("purpose", "")).strip()
            if reason:
                step["recommend_reason"] = reason
                applied += 1
            if purpose:
                step["purpose"] = purpose
        print(f"[SELECTION REASON] Gemini生成 {applied}/{len(target_steps)}件適用", flush=True)

    except Exception as e:
        print(f"[SELECTION REASON ERROR] {repr(e)} → ルールベース維持", flush=True)

    return data


AI_PRODUCT_IMAGES_FILE = "ai_product_images.json"

def load_ai_product_images():
    if not os.path.exists(AI_PRODUCT_IMAGES_FILE):
        return []

    try:
        with open(AI_PRODUCT_IMAGES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, FileNotFoundError):
        return []

def normalize_product_name(name):
    if not name:
        return ""

    text = str(name).strip().lower()

    replace_map = {
        "　": "",
        " ": "",
        "・": "",
        "･": "",
        "-": "",
        "－": "",
        "ー": "",
        "_": "",
        "(": "",
        ")": "",
        "（": "",
        "）": "",
        "[": "",
        "]": "",
        "【": "",
        "】": "",
        "∞": "",
        "％": "",
        "%": "",
        "ザ": "",
        "the": "",
        "ｒ": "r",
        "Ｒ": "r",
        "ａ": "a",
        "Ａ": "a",
        "ipsa": "イプサ",
    }

    for before, after in replace_map.items():
        text = text.replace(before, after)

    text = text.replace("セラム", "serum")
    text = text.replace("美容液", "serum")
    text = text.replace("アンプル", "ampoule")

    # リニューアル表記や末尾の版表記を正規化
    text = re.sub(r"(n|neo|ex)$", "", text)

    return text

def find_db_product_by_name(products, product_name, category=None):
    target = normalize_product_name(product_name)

    if not target:
        return None

    normalized_target_category = normalize_candidate_category(
        category,
        fallback=category
    ) if category else ""

    category_aliases = {
        "洗顔料": "洗顔",
        "フェイスウォッシュ": "洗顔",
        "クレンザー": "洗顔",
        "保湿クリーム": "クリーム",
        "フェイスクリーム": "クリーム",
        "乳液・ミルク": "乳液",
        "ミルク": "乳液",
        "セラム": "美容液",
        "エッセンス": "美容液",
        "導入液": "導入美容液",
        "ブースター": "導入美容液",
    }

    def canonical_category(value):
        normalized = normalize_candidate_category(
            value,
            fallback=value
        ) if value else ""

        return category_aliases.get(normalized, normalized)

    target_category = canonical_category(category)

    def category_matches(product_category):
        if not target_category:
            return True

        product_category = canonical_category(product_category)

        if product_category == target_category:
            return True

        return False

    def is_usable_db_product(p):
        if not isinstance(p, dict):
            return False

        if is_discontinued_or_suspicious_product(p):
            return False

        if not category_matches(p.get("category", "")):
            return False

        return True

    usable_products = [
        p for p in products
        if is_usable_db_product(p)
    ]

    def name_matches(p):
        db_name = normalize_product_name(p.get("name", ""))
        db_brand = normalize_product_name(p.get("brand", ""))

        if not db_name:
            return False

        if target == db_name:
            return True

        db_name_without_brand = db_name

        if db_brand and db_name_without_brand.startswith(db_brand):
            db_name_without_brand = db_name_without_brand[len(db_brand):].strip()

        if target == db_name_without_brand:
            return True

        if target in db_name and len(target) >= 4:
            return True

        if db_name in target and len(db_name) >= 4:
            return True

        return False

    for p in usable_products:
        if name_matches(p):
            return p

    return None

def find_ai_candidate_data(product_name, ai_image_db):
    target = normalize_product_name(product_name)

    if not target:
        return None, 0

    for item in ai_image_db:
        db_name = normalize_product_name(item.get("name", ""))

        if target != db_name:
            continue

        image_file = item.get("image", "")
        price = item.get("price", 0)

        image_path = f"/static/images/products/{image_file}" if image_file else None
        return image_path, price

    return None, 0

def normalize_text(value):
    if value is None:
        return ""
    return str(value).strip().lower()

def safe_step_list(value):
    if isinstance(value, list):
        return [
            item for item in value
            if isinstance(item, dict)
        ]

    return []


def safe_section_steps(section):
    if not isinstance(section, dict):
        return []

    return safe_step_list(section.get("steps", []))


def normalize_result_sections(data):
    if not isinstance(data, dict):
        data = {}

    if not isinstance(data.get("morning"), dict):
        data["morning"] = {"steps": []}

    if not isinstance(data.get("night"), dict):
        data["night"] = {"steps": []}

    data["morning"]["steps"] = safe_section_steps(data.get("morning"))
    data["night"]["steps"] = safe_section_steps(data.get("night"))
    data["weekly_care"] = safe_step_list(data.get("weekly_care", []))
    data["supplements"] = [s for s in (data.get("supplements") or []) if isinstance(s, dict)]
    data["beauty_devices"] = [s for s in (data.get("beauty_devices") or []) if isinstance(s, dict)]

    return data

def ensure_result_structure(data):
    return normalize_result_sections(data)

def normalize_skin_type(oil, sens):
    result = []

    oil = (oil or "").lower()
    sens = (sens or "").lower()

    # 皮脂タイプ
    if oil in ["oily", "脂性"]:
        result.append("oily")
    elif oil in ["dry", "乾燥"]:
        result.append("dry")
    elif oil in ["mixed", "混合"]:
        result.append("mixed")
    else:
        result.append("normal")

    # 敏感
    if sens in ["high", "敏感"]:
        result.append("sensitive")

    return result

def get_budget_fit_score(price_ref, budget_value):
    if not isinstance(price_ref, (int, float)) or price_ref <= 0:
        return 0

    if budget_value <= 0:
        return 0

    if price_ref <= budget_value:
        return 4

    over_ratio = (price_ref - budget_value) / max(budget_value, 1)

    if over_ratio <= 0.15:
        return -2
    elif over_ratio <= 0.30:
        return -4
    elif over_ratio <= 0.50:
        return -7
    else:
        return -10

def normalize_budget_range(budget_value):
    """
    ざっくり価格帯判定用
    """
    if budget_value <= 0:
        return "free"
    if budget_value <= 3000:
        return "low"
    if budget_value <= 7000:
        return "mid"
    return "high"


def normalize_retinol_limit(retinol_exp):
    """
    ユーザー経験から許容レベルを返す
    """
    retinol_exp = normalize_text(retinol_exp)

    if retinol_exp == "beginner":
        return 1
    if retinol_exp == "middle":
        return 2
    if retinol_exp == "advanced":
        return 3     
    return 0


def purpose_to_concern_tags(purpose_text):
    text = normalize_text(purpose_text)
    tags = []
    # -------------------------
    # ビタミンC誘導体・別名
    # -------------------------
    if "ascorbic acid" in text or "アスコルビン酸" in text:
        return "vitamin_c"
    if "ethyl ascorbic" in text or "3-o-ethyl ascorbic" in text or "vcエチル" in text:
        return "vitamin_c"
    if "ascorbyl glucoside" in text or "アスコルビルグルコシド" in text:
        return "vitamin_c"
    if "magnesium ascorbyl phosphate" in text or "リン酸アスコルビルmg" in text:
        return "vitamin_c"
    if "tetrahexyldecyl ascorbate" in text or "テトラヘキシルデカン酸アスコルビル" in text:
        return "vitamin_c"
    if text == "aps" or text == "apps":
        return "vitamin_c"

    # -------------------------
    # セラミドの細分化を統一
    # -------------------------
    if "ceramide np" in text or "ceramide ap" in text or "ceramide eop" in text:
        return "ceramide"

    # -------------------------
    # ヒアルロン酸の細分化を統一
    # -------------------------
    if "sodium hyaluronate" in text or "加水分解ヒアルロン酸" in text or "ヒアルロン酸na" in text:
        return "hyaluronic_acid"

    # -------------------------
    # ペプチドの細分化を統一
    # -------------------------
    if "acetyl hexapeptide" in text or "hexapeptide" in text or "palmitoyl peptide" in text or "sh-oligopeptide" in text:
        return "peptide"

    # -------------------------
    # 保湿・バリア追加
    # -------------------------
    if "ectoin" in text or "エクトイン" in text:
        return "ectoin"
    if "glycerin" in text or "グリセリン" in text:
        return "glycerin"
    if "trehalose" in text or "トレハロース" in text:
        return "trehalose"
    if text == "nmf" or "天然保湿因子" in text:
        return "nmf"
    if "fatty acid" in text or "脂肪酸" in text:
        return "fatty_acid"

    # -------------------------
    # 鎮静追加
    # -------------------------
    if "mugwort" in text or "ヨモギ" in text or "アルテミシア" in text:
        return "mugwort"
    if "azulene" in text or "アズレン" in text:
        return "azulene"
    if "calamine" in text or "カラミン" in text:
        return "calamine"

    # -------------------------
    # 毛穴・角質追加
    # -------------------------
    if text == "lha" or " lha" in text or "lha " in text:
        return "lha"
    if "gluconolactone" in text or "グルコノラクトン" in text:
        return "gluconolactone"
    if "succinic" in text or "コハク酸" in text:
        return "succinic_acid"
    if "papain" in text or "パパイン" in text:
        return "papain"
    if "bromelain" in text or "ブロメライン" in text:
        return "bromelain"
    if "sulfur" in text or "硫黄" in text:
        return "sulfur"
    if text == "zinc" or "亜鉛" in text:
        return "zinc"

    # -------------------------
    # UVフィルター追加
    # -------------------------
    if "zinc oxide" in text or "酸化亜鉛" in text:
        return "zinc_oxide"
    if "titanium dioxide" in text or "酸化チタン" in text:
        return "titanium_dioxide"

    # -------------------------
    # 発酵系追加
    # -------------------------
    if "bifida" in text or "ビフィズス" in text:
        return "bifida"
    if "galactomyces" in text or "ガラクトミセス" in text:
        return "galactomyces"

    if any(w in text for w in ["毛穴", "pores"]):
        tags.append("pores")

    if any(w in text for w in ["ニキビ", "acne"]):
        tags.append("acne")

    if any(w in text for w in ["赤み", "redness"]):
        tags.append("redness")

    if any(w in text for w in ["皮脂", "テカリ", "oil"]):
        tags.append("oil_control")

    if any(w in text for w in ["乾燥", "保湿", "うるおい"]):
        tags.append("dryness")
        tags.append("barrier")

    if any(w in text for w in ["くすみ", "透明感"]):
        tags.append("dullness")

    if any(w in text for w in ["美白", "シミ"]):
        tags.append("whitening")

    if any(w in text for w in ["ハリ", "エイジング", "しわ"]):
        tags.append("aging")

    if "jojoba" in text or "ホホバ" in text:
        return "jojoba_oil"
    if "olive oil" in text or "オリーブ" in text:
        return "olive_oil"
    if "argan" in text or "アルガン" in text:
        return "argan_oil"
    if "tea tree" in text or "ティーツリー" in text:
        return "tea_tree_oil"
    if "mineral oil" in text or "ミネラルオイル" in text:
        return "mineral_oil"
    
    # 重複削除
    return list(dict.fromkeys(tags))


def normalize_ingredient_tag(text):
    text = normalize_text(text)

    if not text:
        return None

    # =========================
    # 攻め・美白・透明感
    # =========================
    if "vitamin c" in text or "vitamin_c" in text or "ビタミンc" in text or "アスコルビン" in text:
        return "vitamin_c"
    if "ethyl ascorbic" in text or "3-o-ethyl ascorbic" in text or "vcエチル" in text:
        return "vitamin_c"
    if "ascorbyl glucoside" in text or "アスコルビルグルコシド" in text:
        return "vitamin_c"
    if "magnesium ascorbyl phosphate" in text or "リン酸アスコルビルmg" in text:
        return "vitamin_c"
    if "tetrahexyldecyl ascorbate" in text or "テトラヘキシルデカン酸アスコルビル" in text:
        return "vitamin_c"
    if text in ["aps", "apps"]:
        return "vitamin_c"

    if "vitamin e" in text or "vitamin_e" in text or "ビタミンe" in text:
        return "vitamin_e"
    if "tocopherol" in text or "トコフェロール" in text:
        return "tocopherol"
    if "niacinamide" in text or "ナイアシンアミド" in text:
        return "niacinamide"
    if "tranexamic" in text or "トラネキサム" in text:
        return "tranexamic_acid"
    if "alpha arbutin" in text or "alpha_arbutin" in text or "αアルブチン" in text:
        return "alpha_arbutin"
    if "arbutin" in text or "アルブチン" in text:
        return "arbutin"
    if "glutathione" in text or "グルタチオン" in text:
        return "glutathione"
    if "kojic" in text or "コウジ酸" in text:
        return "kojic_acid"
    if "ferulic" in text or "フェルラ酸" in text:
        return "ferulic_acid"
    if "cysteamine" in text or "システアミン" in text:
        return "cysteamine"

    # =========================
    # レチノイド・ハリ・再生
    # =========================
    if "retinal" in text or "retinaldehyde" in text or "レチナール" in text:
        return "retinal"
    if "retinol" in text or "レチノール" in text:
        return "retinol"
    if "retinoid" in text or "レチノイド" in text:
        return "retinoid"
    if "bakuchiol" in text or "バクチオール" in text:
        return "bakuchiol"
    if "peptide" in text or "ペプチド" in text or "acetyl hexapeptide" in text or "hexapeptide" in text or "palmitoyl peptide" in text or "sh-oligopeptide" in text:
        return "peptide"
    if text == "egf" or "上皮成長因子" in text:
        return "egf"
    if text == "fgf" or "線維芽細胞増殖因子" in text:
        return "fgf"
    if "pdrn" in text:
        return "pdrn"
    if "adenosine" in text or "アデノシン" in text:
        return "adenosine"
    if "collagen" in text or "コラーゲン" in text:
        return "collagen"
    if "elastin" in text or "エラスチン" in text:
        return "elastin"
    if "coenzyme q10" in text or "q10" in text or "コエンザイムq10" in text:
        return "coenzyme_q10"

    # =========================
    # 保湿・バリア
    # =========================
    if "ceramide np" in text or "ceramide ap" in text or "ceramide eop" in text:
        return "ceramide"
    if "ceramide" in text or "セラミド" in text:
        return "ceramide"
    if "cholesterol" in text or "コレステロール" in text:
        return "cholesterol"
    if "fatty acid" in text or "fatty_acid" in text or "脂肪酸" in text:
        return "fatty_acid"
    if "sodium hyaluronate" in text or "加水分解ヒアルロン酸" in text or "ヒアルロン酸na" in text:
        return "hyaluronic_acid"
    if "hyaluronic" in text or "ヒアルロン酸" in text:
        return "hyaluronic_acid"
    if "polyglutamic" in text or "ポリグルタミン酸" in text:
        return "polyglutamic_acid"
    if "beta glucan" in text or "βグルカン" in text or "ベータグルカン" in text:
        return "beta_glucan"
    if "panthenol" in text or "パンテノール" in text:
        return "panthenol"
    if "allantoin" in text or "アラントイン" in text:
        return "allantoin"
    if "squalane" in text or "スクワラン" in text:
        return "squalane"
    if "amino acid" in text or "amino_acid" in text or "アミノ酸" in text:
        return "amino_acid"
    if "urea" in text or "尿素" in text:
        return "urea"
    if "glycerin" in text or "グリセリン" in text:
        return "glycerin"
    if "trehalose" in text or "トレハロース" in text:
        return "trehalose"
    if "ectoin" in text or "エクトイン" in text:
        return "ectoin"
    if text == "nmf" or "天然保湿因子" in text:
        return "nmf"
    if "mucin" in text or "ムチン" in text:
        return "mucin"
    if "snail" in text or "スネイル" in text:
        return "snail"

    # =========================
    # 鎮静・抗炎症
    # =========================
    if text == "cica" or "cica" in text:
        return "cica"
    if text == "teca" or "teca" in text:
        return "teca"
    if "madecassoside" in text or "マデカッソシド" in text or "マデカ" in text:
        return "madecassoside"
    if "centella" in text or "ツボクサ" in text:
        return "centella_extract"
    if "heartleaf" in text or "ドクダミ" in text:
        return "heartleaf"
    if "mugwort" in text or "ヨモギ" in text or "アルテミシア" in text:
        return "mugwort"
    if "glycyrrhizate" in text or "グリチルリチン" in text:
        return "dipotassium_glycyrrhizate"
    if "propolis" in text or "プロポリス" in text:
        return "propolis"
    if "azulene" in text or "アズレン" in text:
        return "azulene"
    if "calamine" in text or "カラミン" in text:
        return "calamine"

    # =========================
    # 角質・毛穴・皮脂
    # =========================
    if "azelaic" in text or "アゼライン" in text:
        return "azelaic_acid"
    if text == "aha" or " aha" in text or "aha " in text:
        return "aha"
    if text == "bha" or " bha" in text or "bha " in text:
        return "bha"
    if text == "pha" or " pha" in text or "pha " in text:
        return "pha"
    if text == "lha" or " lha" in text or "lha " in text:
        return "lha"
    if "salicylic" in text or "サリチル酸" in text:
        return "salicylic_acid"
    if "glycolic" in text or "グリコール酸" in text:
        return "glycolic_acid"
    if "lactic" in text or "乳酸" in text:
        return "lactic_acid"
    if "mandelic" in text or "マンデル酸" in text:
        return "mandelic_acid"
    if "gluconolactone" in text or "グルコノラクトン" in text:
        return "gluconolactone"
    if "succinic" in text or "コハク酸" in text:
        return "succinic_acid"
    if "enzyme" in text or "酵素" in text:
        return "enzyme"
    if "papain" in text or "パパイン" in text:
        return "papain"
    if "bromelain" in text or "ブロメライン" in text:
        return "bromelain"
    if "clay" in text or "クレイ" in text:
        return "clay"
    if "charcoal" in text or "炭" in text or "活性炭" in text:
        return "charcoal"
    if text == "zinc" or "亜鉛" in text:
        return "zinc"
    if "sulfur" in text or "硫黄" in text:
        return "sulfur"

    # =========================
    # 発酵
    # =========================
    if "bifidus" in text or "bifida" in text or "ビフィズス" in text:
        return "bifida"
    if "galactomyces" in text or "ガラクトミセス" in text:
        return "galactomyces"
    if "saccharomyces" in text or "サッカロミセス" in text:
        return "saccharomyces"
    if "lactobacillus" in text or "乳酸菌" in text:
        return "lactobacillus"
    if "ferment" in text or "発酵" in text:
        return "probiotic_ferment"

    # =========================
    # UV
    # =========================
    if "uv filter" in text or "uv_filter" in text or "紫外線吸収剤" in text or text == "uv":
        return "uv_filter"
    if "zinc oxide" in text or "酸化亜鉛" in text:
        return "zinc_oxide"
    if "titanium dioxide" in text or "酸化チタン" in text:
        return "titanium_dioxide"

    # =========================
    # 抗酸化・補助
    # =========================
    if "caffeine" in text or "カフェイン" in text:
        return "caffeine"
    if "resveratrol" in text or "レスベラトロール" in text:
        return "resveratrol"
    if "idebenone" in text or "イデベノン" in text:
        return "idebenone"

    # =========================
    # オイル系
    # =========================
    if "mineral oil" in text or "ミネラルオイル" in text:
        return "mineral_oil"
    if "ester oil" in text or "エステルオイル" in text:
        return "ester_oil"
    if "plant oil" in text or "植物油" in text or "botanical oil" in text:
        return "plant_oil"
    if "jojoba" in text or "ホホバ" in text:
        return "jojoba_oil"
    if "olive oil" in text or "オリーブ油" in text or "オリーブオイル" in text:
        return "olive_oil"
    if "argan" in text or "アルガン" in text:
        return "argan_oil"
    if "sunflower" in text or "ヒマワリ種子油" in text:
        return "sunflower_oil"
    if "grapeseed" in text or "グレープシード" in text:
        return "grapeseed_oil"
    if "rosehip" in text or "ローズヒップ" in text:
        return "rosehip_oil"
    if "tea tree oil" in text or "ティーツリー油" in text or "ティーツリーオイル" in text:
        return "tea_tree_oil"

    # =========================
    # 独自成分・独自複合体
    # =========================
    if "ライスパワーno11" in text or "rice power no.11" in text or "rice_power_no11" in text:
        return "rice_power_no11"
    if "ライスパワーno6" in text or "rice power no.6" in text or "rice_power_no6" in text:
        return "rice_power_no6"

    if "multi ceramide complex" in text or "multi_ceramide_complex" in text:
        return "multi_ceramide_complex"
    if "ceramide complex ex" in text or "ceramide_complex_ex" in text:
        return "ceramide_complex_ex"
    if "derma barrier complex" in text or "derma_barrier_complex" in text:
        return "derma_barrier_complex"
    if "moisture lock complex" in text or "moisture_lock_complex" in text:
        return "moisture_lock_complex"
    if "hyaluronic 5d complex" in text or "hyaluronic_5d_complex" in text:
        return "hyaluronic_5d_complex"
    if "aqua sphere complex" in text or "aqua_sphere_complex" in text:
        return "aqua_sphere_complex"
    if "ectoin protect complex" in text or "ectoin_protect_complex" in text:
        return "ectoin_protect_complex"

    if "madewhite" in text or "マデホワイト" in text:
        return "madewhite"
    if "melazero v2" in text or "melazero_v2" in text or "メラゼロv2" in text:
        return "melazero_v2"
    if "melazero" in text or "メラゼロ" in text:
        return "melazero"
    if "white tranex complex" in text or "white_tranex_complex" in text:
        return "white_tranex_complex"
    if "tone up complex" in text or "tone_up_complex" in text:
        return "tone_up_complex"
    if "gluta bright complex" in text or "gluta_bright_complex" in text:
        return "gluta_bright_complex"
    if "vitamin c booster complex" in text or "vitamin_c_booster_complex" in text:
        return "vitamin_c_booster_complex"
    if "dark spot corrector complex" in text or "dark_spot_corrector_complex" in text:
        return "dark_spot_corrector_complex"

    if "cica reedle" in text or "cica_reedle_complex" in text or "シカリードル" in text:
        return "cica_reedle_complex"
    if "cica complex" in text or "cica_complex" in text:
        return "cica_complex"
    if "centella complex" in text or "centella_complex" in text:
        return "centella_complex"
    if "centella asiatica 5x" in text or "centella_asiatica_5x" in text:
        return "centella_asiatica_5x"
    if "heartleaf complex" in text or "heartleaf_complex" in text:
        return "heartleaf_complex"
    if "soothing complex" in text or "soothing_complex" in text:
        return "soothing_complex"
    if "anti redness complex" in text or "anti_redness_complex" in text:
        return "anti_redness_complex"
    if "calming barrier complex" in text or "calming_barrier_complex" in text:
        return "calming_barrier_complex"

    if "pore refining complex" in text or "pore_refining_complex" in text:
        return "pore_refining_complex"
    if "pore minimizing complex" in text or "pore_minimizing_complex" in text:
        return "pore_minimizing_complex"
    if "sebum control complex" in text or "sebum_control_complex" in text:
        return "sebum_control_complex"
    if "oil balancing complex" in text or "oil_balancing_complex" in text:
        return "oil_balancing_complex"
    if "anti shine complex" in text or "anti_shine_complex" in text:
        return "anti_shine_complex"
    if "blackhead clear complex" in text or "blackhead_clear_complex" in text:
        return "blackhead_clear_complex"
    if "clay detox complex" in text or "clay_detox_complex" in text:
        return "clay_detox_complex"

    if "acne clear complex" in text or "acne_clear_complex" in text:
        return "acne_clear_complex"
    if "anti acne complex" in text or "anti_acne_complex" in text:
        return "anti_acne_complex"
    if "trouble care complex" in text or "trouble_care_complex" in text:
        return "trouble_care_complex"
    if "spot control complex" in text or "spot_control_complex" in text:
        return "spot_control_complex"
    if "blemish control complex" in text or "blemish_control_complex" in text:
        return "blemish_control_complex"

    if "peptide complex 5" in text or "peptide_complex_5" in text:
        return "peptide_complex_5"
    if "peptide complex" in text or "peptide_complex" in text:
        return "peptide_complex"
    if "collagen boost complex" in text or "collagen_boost_complex" in text:
        return "collagen_boost_complex"
    if "firming complex" in text or "firming_complex" in text:
        return "firming_complex"
    if "elasticity complex" in text or "elasticity_complex" in text:
        return "elasticity_complex"
    if "retinol booster complex" in text or "retinol_booster_complex" in text:
        return "retinol_booster_complex"
    if "retinal repair complex" in text or "retinal_repair_complex" in text:
        return "retinal_repair_complex"
    if "lifting complex" in text or "lifting_complex" in text:
        return "lifting_complex"

    if "bifida complex" in text or "bifida_complex" in text:
        return "bifida_complex"
    if "galactomyces complex" in text or "galactomyces_complex" in text:
        return "galactomyces_complex"
    if "fermented yeast complex" in text or "fermented_yeast_complex" in text:
        return "fermented_yeast_complex"
    if "probiotic complex" in text or "probiotic_complex" in text:
        return "probiotic_complex"
    if "microbiome complex" in text or "microbiome_complex" in text:
        return "microbiome_complex"

    if "derma complex" in text or "derma_complex" in text:
        return "derma_complex"
    if "skin repair complex" in text or "skin_repair_complex" in text:
        return "skin_repair_complex"
    if "multi care complex" in text or "multi_care_complex" in text:
        return "multi_care_complex"
    if "total skin solution complex" in text or "total_skin_solution_complex" in text:
        return "total_skin_solution_complex"

    return None

# =========================================================
# SCORE BLOCK START
# 貼る場所:
# normalize_ingredient_tag() の下
# select_best_market_candidate() / select_best_product() の上
# =========================================================

def get_strength_score(level):
    """
    ingredient_strength 用
    基本は active_ingredients に対して使う
    """
    if level == "high":
        return 18
    if level == "medium":
        return 10
    if level == "low":
        return 4
    return 0


def get_availability_score(values):
    """
    availability_japan 用
    日本での買いやすさ加点
    DB例:
    ["amazon", "rakuten", "qoo10", "drugstore"]
    """
    if not isinstance(values, list):
        return 0

    score = 0
    normalized = [normalize_text(v) for v in values]

    if "drugstore" in normalized:
        score += 4
    if "variety_shop" in normalized:
        score += 4
    if "amazon" in normalized:
        score += 2
    if "rakuten" in normalized:
        score += 2
    if "qoo10" in normalized:
        score += 2
    if "official" in normalized:
        score += 2

    return score


def score_goal_fit(product, step):
    """
    stepの目的とDB商品の concerns / main_functions / ingredient_focus の一致を点数化
    """
    score = 0

    purpose = normalize_text(step.get("purpose", ""))
    concern_tags = purpose_to_concern_tags(step.get("purpose", ""))

    product_concerns = product.get("concerns", [])
    product_functions = product.get("main_functions", [])
    product_focuses = product.get("ingredient_focus", [])

   # score_goal_fit 内の concerns 加点をこれに置換
    match_count = 0
    for tag in product.get("concerns", []):
        if tag in concern_tags:
            match_count += 1

    score += min(match_count * 12, 24)  # 上限24（=最大2つ分）

    # main_functions一致
    for f in product_functions:
        f_norm = normalize_text(f)
        if not f_norm:
            continue
        if f_norm in purpose or purpose in f_norm:
            score += 8

    # ingredient_focus一致
    for focus in product_focuses:
        focus_norm = normalize_text(focus)
        if not focus_norm:
            continue
        if focus_norm in purpose or purpose in focus_norm:
            score += 8

    # 目的キーワード補正
    if "毛穴" in purpose and "pores" in product_concerns:
        score += 6
    if "ニキビ" in purpose and "acne" in product_concerns:
        score += 6
    if "赤み" in purpose and "redness" in product_concerns:
        score += 6
    if ("乾燥" in purpose or "保湿" in purpose) and (
        "dryness" in product_concerns or "barrier" in product_concerns
    ):
        score += 6
    if ("くすみ" in purpose or "透明感" in purpose or "美白" in purpose) and (
        "dullness" in product_concerns or "whitening" in product_concerns
    ):
        score += 6
    if ("ハリ" in purpose or "エイジング" in purpose or "しわ" in purpose) and "aging" in product_concerns:
        score += 6

    return score


def score_signature_ingredients(product, step):
    """
    signature_ingredients の加点
    signature_ingredient_effects が上で定義されている前提
    """
    score = 0

    sigs = product.get("signature_ingredients", [])
    concern_tags = purpose_to_concern_tags(step.get("purpose", ""))

    for sig in sigs:
        effects = signature_ingredient_effects.get(sig, [])

        for c in concern_tags:
            if c in effects:
                score += 10

        if len(effects) >= 2:
            score += 2

    return score


def apply_common_score_rules(product, step, user_data, budget_value, concern_tags, ingredient_tag):
    """
    カテゴリ共通スコア
    このDB項目に対応:
    - category
    - price_ref
    - active_ingredients
    - support_ingredients
    - formulation
    - concerns
    - skin_types
    - retinol_level
    - sensitive_ok
    - availability_japan
    - ingredient_strength
    - signature_ingredients
    - main_functions
    - ingredient_focus
    - technology
    - texture
    - contraindications
    """
    score = 0

    product_concerns = list(product.get("concerns", []) or [])
    product_actives = list(product.get("active_ingredients", []) or [])
    product_support = list(product.get("support_ingredients", []) or [])
    product_skin_types = list(product.get("skin_types", []) or [])
    sensitive_ok = product.get("sensitive_ok", "unknown")
    retinol_level = safe_retinol_level(
        product.get("retinol_level", 0)
    )
    price_ref = safe_price(product.get("price_ref", 0))
    availability = product.get("availability_japan", [])
    product_functions = list(product.get("main_functions", []) or [])
    product_focuses = list(product.get("ingredient_focus", []) or [])
    product_formulation = list(product.get("formulation", []) or [])
    product_technology = list(product.get("technology", []) or [])
    product_texture = normalize_text(product.get("texture", ""))
    product_contra = list(product.get("contraindications", []) or [])
    ingredient_strength_map = product.get("ingredient_strength", {})
    if not isinstance(ingredient_strength_map, dict):
        ingredient_strength_map = {}
    user_skin_types = normalize_skin_type(
        user_data.get("oil", ""),
        user_data.get("sens", "")
    )
    sens = normalize_text(user_data.get("sens", ""))
    oil = normalize_text(user_data.get("oil", ""))
    retinol_limit = normalize_retinol_limit(user_data.get("exp", ""))

    # 楽天商品などメタデータが空の場合はタイトルから補完（公平評価）
    _has_meta = bool(product_formulation or product_functions or sensitive_ok != "unknown")
    if not _has_meta:
        _t = normalize_text(str(product.get("rakuten_title") or product.get("name") or ""))
        if any(w in _t for w in ["低刺激", "敏感肌", "sensitive", "マイルド", "mild", "刺激レス", "ノンコメドジェニック"]):
            sensitive_ok = "yes"
            product_formulation = product_formulation + ["low_irritation"]
        if any(w in _t for w in ["バリア", "保湿", "しっとり", "うるおい", "セラミド"]):
            product_formulation = list(set(product_formulation + ["barrier_formula"]))
        if any(w in _t for w in ["オイリー", "さっぱり", "脂性", "皮脂"]):
            product_formulation = list(set(product_formulation + ["oil_control"]))

    # -------------------------------------------------
    # 1. ingredient_focus（step側）と active/support 一致
    # Geminiが指定した成分を持つ商品を優先的に選定する
    # -------------------------------------------------
    if ingredient_tag:
        if ingredient_tag in product_actives:
            score += 25
            score += get_strength_score(ingredient_strength_map.get(ingredient_tag))

        elif ingredient_tag in product_support:
            score += 10

        else:
            # stepのfocus成分を一切持たない商品は選定優先度を下げる
            score -= 15

    # -------------------------------------------------
    # 2. concerns一致
    # -------------------------------------------------
    for c in concern_tags:
        if c in product_concerns:
            score += 8

    # -------------------------------------------------
    # 3. DBのingredient_focus一致
    # -------------------------------------------------
    purpose = normalize_text(step.get("purpose", ""))
    for focus in product_focuses:
        focus_norm = normalize_text(focus)
        if not focus_norm:
            continue
        if focus_norm in purpose or purpose in focus_norm:
            score += 6

    # -------------------------------------------------
    # 4. skin_types一致
    # -------------------------------------------------
    for st in user_skin_types:
        if st in product_skin_types:
            score += 6

    if "normal" in product_skin_types and not any(st in product_skin_types for st in user_skin_types):
        score += 2

    # -------------------------------------------------
    # 5. sensitive_ok
    # -------------------------------------------------
    if sens == "high":
        if sensitive_ok == "yes":
            score += 12
        elif sensitive_ok == "no":
            score -= 15
        else:
            score += 0

    # -------------------------------------------------
    # 6. retinol_level
    # -------------------------------------------------
    if retinol_level > 0:
        if retinol_limit == 0:
            score -= 20
        elif retinol_level > retinol_limit:
            score -= 12
        elif retinol_level == retinol_limit:
            score += 4

    # -------------------------------------------------
    # 7. contraindications
    # -------------------------------------------------
    if sens == "high":
        if "sensitive_skin" in product_contra:
            score -= 12
        if "high_irritation_risk" in product_contra:
            score -= 15
        if "redness_prone" in product_contra:
            score -= 10

    if "acid_same_routine" in product_contra and ingredient_tag in [
        "aha", "bha", "pha", "lha",
        "glycolic_acid", "lactic_acid", "mandelic_acid", "salicylic_acid"
    ]:
        score -= 8

    if "retinol_same_routine" in product_contra and ingredient_tag in [
        "retinol", "retinal", "retinoid"
    ]:
        score -= 10

    # morning_use_caution / photosensitivity は score_product で -9999 除外済みのため
    # ここでの追加ペナルティは不要

    # -------------------------------------------------
    # 8. formulation / technology / texture
    # -------------------------------------------------
    if sens == "high":
        if "low_irritation" in product_formulation:
            score += 8
        if "mild_formula" in product_formulation:
            score += 6
        if "barrier_formula" in product_formulation:
            score += 5

    if "dryness" in concern_tags or "barrier" in concern_tags:
        if "barrier_formula" in product_formulation:
            score += 8
        if "ceramide" in product_support:
            score += 6
        if "cholesterol" in product_support:
            score += 5
        if "fatty_acid" in product_support:
            score += 4
        if product_texture in ["cream", "rich"]:
            score += 5

    if "oil_control" in concern_tags or "pores" in concern_tags or "acne" in concern_tags:
        if product_texture in ["light", "watery", "gel", "essence", "foam"]:
            score += 6
        if "low_ph" in product_formulation:
            score += 3

    if "whitening" in concern_tags or "dullness" in concern_tags:
        if "tone_up" in product_formulation:
            score += 6
        if "stabilized_vitamin_c" in product_technology:
            score += 8

    if "aging" in concern_tags:
        if "liposome" in product_formulation:
            score += 12
        if "nano_capsule" in product_technology:
            score += 8

    # -------------------------------------------------
    # 9. main_functions 一致
    # -------------------------------------------------
    for f in product_functions:
        f_norm = normalize_text(f)
        if f_norm and (f_norm in purpose or purpose in f_norm):
            score += 6

    # -------------------------------------------------
    # 10. availability_japan
    # -------------------------------------------------
    # ai_virtual は Gemini が生成した架空商品のため availability_japan は実在を保証しない
    # 加点をゼロにすることで実在商品（rakuten_criteria / verified_cache）との公平性を確保する
    if product.get("_source_hint") != "ai_virtual":
        score += get_availability_score(availability)

    # -------------------------------------------------
    # 11. 予算適合
    # -------------------------------------------------
    if isinstance(price_ref, (int, float)) and budget_value > 0:
        score += get_budget_fit_score(price_ref, budget_value)

    # -------------------------------------------------
    # 12. brand軽補正（任意）
    # ここではまだ使わない
    # name / brand / image はスコアに直接使わない
    # -------------------------------------------------

    return score


def apply_cleansing_score_rules(product, user_data, concern_tags):
    """
    クレンジング向けスコア
    """
    score = 0

    product_actives = product.get("active_ingredients", [])
    product_support = list(product.get("support_ingredients", []) or [])
    formulation = list(product.get("formulation", []) or [])
    texture = normalize_text(product.get("texture", ""))
    sensitive_ok = product.get("sensitive_ok", "unknown")
    functions = list(product.get("main_functions", []) or [])
    technology = list(product.get("technology", []) or [])
    contraindications = list(product.get("contraindications", []) or [])

    skin = normalize_text(user_data.get("oil", ""))
    sens = normalize_text(user_data.get("sens", ""))
    makeup_level = normalize_text(user_data.get("makeup_level", "medium"))
    morning_cleanse = normalize_text(user_data.get("morning_cleanse", "no"))

    # 楽天商品など構造化メタデータが空の場合はタイトルから補完（公平な評価のため）
    _no_meta = not functions and not formulation and sensitive_ok == "unknown"
    if _no_meta:
        _title = normalize_text(str(product.get("rakuten_title") or product.get("name") or ""))
        if any(w in _title for w in ["低刺激", "敏感肌", "マイルド", "sensitive", "mild", "刺激レス"]):
            sensitive_ok = "yes"
            functions = functions + ["low_irritation"]
        if any(w in _title for w in ["しっとり", "潤い", "保湿", "バリア", "うるおい"]):
            functions = functions + ["barrier_preserving", "non_stripping"]
        if any(w in _title for w in ["エッセンシャルオイル", "精油", "アロマ"]):
            contraindications = contraindications + ["essential_oil_caution"]

    # =========================
    # 敏感肌対応
    # =========================
    if sens == "high":
        if sensitive_ok == "yes":
            score += 12
        elif sensitive_ok == "unknown":
            score += 4
        elif sensitive_ok == "no":
            score -= 12

        if "low_irritation" in formulation:
            score += 8

        if "low_friction" in functions or "low_friction_system" in technology:
            score += 8

        if "non_stripping" in functions:
            score += 8

        if "barrier_preserving" in functions or "barrier_preserving" in formulation:
            score += 8

        if "essential_oil_caution" in contraindications:
            score -= 8

    # =========================
    # 乾燥・バリア
    # =========================
    if "dryness" in concern_tags or "barrier" in concern_tags:
        if "ceramide" in product_support:
            score += 8
        if "panthenol" in product_support:
            score += 6
        if "beta_glucan" in product_support:
            score += 5
        if "glycerin" in product_support:
            score += 4
        if "squalane" in product_support:
            score += 4

        if "mild_formula" in formulation or "low_irritation" in formulation:
            score += 6

        if "non_stripping" in functions:
            score += 10

        if "barrier_preserving" in functions or "barrier_preserving" in formulation:
            score += 10

    # =========================
    # 赤み・ニキビ
    # =========================
    if "acne" in concern_tags or "redness" in concern_tags:
        if "cica" in product_support:
            score += 6
        if "heartleaf" in product_support:
            score += 5
        if "dipotassium_glycyrrhizate" in product_support:
            score += 5
        if "low_irritation" in formulation:
            score += 6
        if "pore_preventive" in functions:
            score += 6

    # =========================
    # 毛穴・皮脂
    # =========================
    if "oil_control" in concern_tags or "pores" in concern_tags:
        if texture in ["light", "gel", "watery", "foam"]:
            score += 5
        if "clay" in product_actives or "clay" in product_support:
            score += 4
        if "enzyme" in product_actives or "enzyme" in product_support:
            score += 4
        if "charcoal" in product_actives or "charcoal" in product_support:
            score += 3
        if "sebum_cleansing" in functions:
            score += 7
        if "pore_preventive" in functions:
            score += 8
        if "blackhead_prevention" in functions:
            score += 6

    # =========================
    # 基本機能
    # =========================
    if "makeup_removal" in functions:
        score += 10
    if "sunscreen_removal" in functions:
        score += 6
    if "daily_use_friendly" in functions:
        score += 4
    if "easy_rinse" in functions or "easy_rinse_system" in technology:
        score += 4
    if "residue_free" in functions:
        score += 4

    # =========================
    # メイク濃さとの相性
    # =========================
    if makeup_level == "heavy":
        if "heavy_makeup_ok" in functions:
            score += 10
        elif "light_makeup_ok" in functions:
            score -= 6
        else:
            score -= 3

    elif makeup_level == "light":
        if "light_makeup_ok" in functions:
            score += 5
        if "low_friction" in functions or "low_friction_system" in technology:
            score += 3

    # =========================
    # 朝洗顔兼用適性
    # =========================
    if morning_cleanse == "yes":
        if "morning_cleanse_ok" in functions:
            score += 5
        if "daily_use_friendly" in functions:
            score += 4
        if "non_stripping" in functions:
            score += 4

    # =========================
    # 肌質との相性
    # =========================
    if skin == "dry":
        if "non_stripping" in functions:
            score += 6
        if "barrier_preserving" in functions or "barrier_preserving" in formulation:
            score += 6
        if texture in ["gel", "milk", "balm"]:
            score += 3

    if skin in ["oily", "mixed"]:
        if "sebum_cleansing" in functions:
            score += 6
        if "pore_preventive" in functions:
            score += 5
        if texture in ["gel", "watery", "foam"]:
            score += 4
        if texture in ["oil", "balm"] and "easy_rinse" not in functions and "easy_rinse_system" not in technology:
            score -= 3

    return score

def build_cleansing_subscores(product, user_data, concern_tags):
    """
    クレンジング向けサブスコア
    """
    functions = product.get("main_functions", [])
    formulation = product.get("formulation", [])
    technology = product.get("technology", [])
    support = product.get("support_ingredients", [])
    contraindications = product.get("contraindications", [])
    sensitive_ok = product.get("sensitive_ok", "unknown")
    texture = normalize_text(product.get("texture", ""))

    cleanse_score = 50
    irritation_score = 50
    barrier_score = 50
    pore_score = 50

    # =========================
    # 洗浄力
    # =========================
    if "makeup_removal" in functions:
        cleanse_score += 15
    if "sunscreen_removal" in functions:
        cleanse_score += 8
    if "sebum_cleansing" in functions:
        cleanse_score += 8
    if "heavy_makeup_ok" in functions:
        cleanse_score += 8
    if "easy_rinse" in functions or "easy_rinse_system" in technology:
        cleanse_score += 4

    # =========================
    # 低刺激性
    # =========================
    if sensitive_ok == "yes":
        irritation_score += 12
    elif sensitive_ok == "unknown":
        irritation_score += 4
    elif sensitive_ok == "no":
        irritation_score -= 12

    if "low_irritation" in formulation:
        irritation_score += 12
    if "low_friction" in functions or "low_friction_system" in technology:
        irritation_score += 10
    if "essential_oil_caution" in contraindications:
        irritation_score -= 8

    # =========================
    # バリア保持
    # =========================
    if "non_stripping" in functions:
        barrier_score += 15
    if "barrier_preserving" in functions or "barrier_preserving" in formulation:
        barrier_score += 15
    if "ceramide" in support:
        barrier_score += 6
    if "panthenol" in support:
        barrier_score += 5
    if "beta_glucan" in support:
        barrier_score += 5
    if "glycerin" in support:
        barrier_score += 4
    if "squalane" in support:
        barrier_score += 4

    # =========================
    # 毛穴相性
    # =========================
    if "pore_preventive" in functions:
        pore_score += 15
    if "blackhead_prevention" in functions:
        pore_score += 10
    if "sebum_cleansing" in functions:
        pore_score += 6
    if texture == "gel":
        pore_score += 3
    if "clay" in support:
        pore_score += 4
    if "enzyme" in support:
        pore_score += 4

    return {
        "cleanse_score": max(0, min(cleanse_score, 100)),
        "irritation_score": max(0, min(irritation_score, 100)),
        "barrier_score": max(0, min(barrier_score, 100)),
        "pore_score": max(0, min(pore_score, 100)),
    }

def apply_sunscreen_score_rules(product, step, user_data, concern_tags):
    """
    日焼け止め向け
    """
    score = 0

    product_actives = product.get("active_ingredients", [])
    product_support = product.get("support_ingredients", [])
    product_formulation = product.get("formulation", [])
    product_texture = normalize_text(product.get("texture", ""))
    sensitive_ok = product.get("sensitive_ok", "unknown")
    functions = product.get("main_functions", [])

    skin = normalize_text(user_data.get("oil", ""))
    sens = normalize_text(user_data.get("sens", ""))

    if sens == "high":
        if sensitive_ok == "yes":
            score += 12
        elif sensitive_ok == "no":
            score -= 15

    if "acne" in concern_tags or "redness" in concern_tags:
        if "low_irritation" in product_formulation:
            score += 8
        if "cica" in product_support:
            score += 6

    if skin == "oily":
        if product_texture in ["light", "watery", "gel", "essence"]:
            score += 8
        if "waterproof" in product_formulation:
            score += 6

    if skin == "dry":
        if product_texture in ["cream", "rich"]:
            score += 8
        if "hyaluronic_acid" in product_support:
            score += 6
        if "ceramide" in product_support:
            score += 6

    if "whitening" in concern_tags or "dullness" in concern_tags:
        if "tone_up" in product_formulation:
            score += 6

    if "uv_filter" in product_actives:
        score += 5
    if "zinc_oxide" in product_actives:
        score += 4
    if "titanium_dioxide" in product_actives:
        score += 4

    if "紫外線防御" in functions:
        score += 8
    if "光ダメージケア" in functions:
        score += 4

    if "光ダメージケア" in functions:
        score += 4

    uv_info = product.get("uv_level") or {}
    spf_raw = str(uv_info.get("spf", 0) or "0").strip()
    spf = int(re.sub(r"[^0-9]", "", spf_raw) or 0)
    pa = str(uv_info.get("pa", "") or "")

    # uv_level が空の楽天商品はタイトルから SPF/PA を抽出してフォールバック
    if not spf and not pa:
        title_raw = str(product.get("rakuten_title") or product.get("name") or "")
        spf_m = re.search(r'(?i)spf\s*(\d+)', title_raw)
        if spf_m:
            spf = int(spf_m.group(1))
        pa_m = re.search(r'(?i)(pa\+{1,4})', title_raw)
        if pa_m:
            pa = pa_m.group(1).replace("PA", "").replace("pa", "")  # → "++++", "+++" 等

    if spf >= 50:
        score += 10
    elif spf >= 30:
        score += 6
    elif spf >= 15:
        score += 3

    if pa == "++++":
        score += 8
    elif pa == "+++":
        score += 5
    elif pa == "++":
        score += 2

    return score

NON_COSMETIC_KEYWORDS = [
    "ドリンク",
    "サプリ",
    "サプリメント",
    "錠剤",
    "カプセル",
    "飲む",
    "インナー",
    "美容補助食品",
    "健康食品",
    "粉末",
    "タブレット",
    "shot",
    "drink",
    "supplement",
]

def is_non_cosmetic(product):
    name = str(product.get("name", "") or product.get("product", "")).lower()
    category = str(product.get("category", "") or "").lower()

    if any(keyword.lower() in name for keyword in NON_COSMETIC_KEYWORDS):
        return True

    if category in ["food", "supplement", "drink", "食品", "サプリ", "健康食品"]:
        return True

    return False

CLEANSER_KEYWORDS = [
    "洗顔",
    "フォーム",
    "フォーマー",
    "クレンザー",
    "ウォッシュ",
    "ジェルウォッシュ",
    "泡",
    "ホイップ",
    "石鹸",
    "せっけん",
    "サボン",
    "soap",
    "cleanser",
    "cleansing foam",
    "face wash",
    "facial wash",
]

TONER_KEYWORDS = [
    "化粧水",
    "トナー",
    "ローション",
    "toner",
    "lotion",
    "ampoule toner",
    "アンプルトナー",
]

def is_wrong_cleanser_candidate(product, step):
    step_category = str(step.get("category", "") or "").strip()

    if step_category != "洗顔":
        return False

    name = str(product.get("name", "") or product.get("product", "")).lower()

    # モジュールレベルの TONER_KEYWORDS / CLEANSER_KEYWORDS を直接参照する。
    # 以前ここにローカルの重複リストがあり、片方だけ更新されて定義がずれる
    # バグを引き起こしていたため、単一の定義元に統一した。
    if any(k.lower() in name for k in TONER_KEYWORDS):
        return True

    if not any(k.lower() in name for k in CLEANSER_KEYWORDS):
        return True

    return False

def infer_active_profile(product):
    if not isinstance(product, dict):
        return {
            "families": set(),
            "strength": "low",
            "irritation_risk": "low",
            "pair_well_with": set(),
            "avoid_with": set()
        }

    active_ingredients = product.get("active_ingredients") or []
    support_ingredients = product.get("support_ingredients") or []
    ingredient_focus = product.get("ingredient_focus") or []
    main_functions = product.get("main_functions") or []
    ingredient_strength = product.get("ingredient_strength") or {}

    if not isinstance(ingredient_strength, dict):
        ingredient_strength = {}

    text_parts = []
    text_parts.extend(active_ingredients)
    text_parts.extend(support_ingredients)
    text_parts.extend(ingredient_focus)
    text_parts.extend(main_functions)

    text = " ".join(str(x).lower() for x in text_parts)

    families = set()
    pair_well_with = set()
    avoid_with = set()

    strength = "low"
    irritation_risk = "low"

    if any(x in text for x in [
        "retinol",
        "retinal",
        "retinoid",
        "レチノール",
        "レチナール",
        "レチノイド"
    ]):
        families.add("retinoid")
        pair_well_with.update([
            "ceramide",
            "panthenol",
            "peptide",
            "pdrn",
            "niacinamide",
            "barrier"
        ])
        avoid_with.update([
            "aha_bha",
            "strong_vitamin_c"
        ])

        retinol_level = product.get("retinol_level", "")

        if retinol_level in ["high", "strong"]:
            strength = "high"
            irritation_risk = "high"
        else:
            strength = "medium"
            irritation_risk = "medium"

    if any(x in text for x in [
        "vitamin_c",
        "ascorbic",
        "アスコルビン酸",
        "ビタミンc",
        "ビタミンC"
    ]):
        families.add("vitamin_c")
        pair_well_with.update([
            "niacinamide",
            "tranexamic",
            "glutathione",
            "azelaic",
            "barrier"
        ])

        vitamin_c_strength = ingredient_strength.get("vitamin_c", "")

        if vitamin_c_strength in ["high", "strong"]:
            families.add("strong_vitamin_c")
            strength = "high"
            irritation_risk = "medium"

    if any(x in text for x in [
        "azelaic",
        "azelaic_acid",
        "アゼライン酸"
    ]):
        families.add("azelaic")
        pair_well_with.update([
            "vitamin_c",
            "niacinamide",
            "barrier",
            "ceramide"
        ])

    if any(x in text for x in [
        "niacinamide",
        "ナイアシンアミド"
    ]):
        families.add("niacinamide")
        pair_well_with.update([
            "vitamin_c",
            "retinoid",
            "azelaic",
            "barrier"
        ])

    if any(x in text for x in [
        "pdrn"
    ]):
        families.add("pdrn")
        pair_well_with.update([
            "retinoid",
            "peptide",
            "barrier"
        ])

    if any(x in text for x in [
        "tranexamic",
        "tranexamic_acid",
        "トラネキサム酸"
    ]):
        families.add("tranexamic")
        pair_well_with.update([
            "vitamin_c",
            "niacinamide"
        ])

    if any(x in text for x in [
        "glutathione",
        "グルタチオン"
    ]):
        families.add("glutathione")
        pair_well_with.update([
            "vitamin_c"
        ])

    if any(x in text for x in [
        "aha",
        "bha",
        "pha",
        "lha",
        "glycolic_acid",
        "lactic_acid",
        "salicylic_acid",
        "mandelic_acid",
        "グリコール酸",
        "乳酸",
        "サリチル酸",
        "マンデル酸",
        "ピーリング"
    ]):
        families.add("aha_bha")
        avoid_with.update([
            "retinoid",
            "strong_vitamin_c"
        ])
        strength = "medium"
        irritation_risk = "medium"

    if any(x in text for x in [
        "ceramide",
        "セラミド",
        "barrier",
        "バリア"
    ]):
        families.add("ceramide")
        families.add("barrier")

    if any(x in text for x in [
        "panthenol",
        "パンテノール",
        "cica",
        "ツボクサ",
        "madecassoside",
        "マデカッソシド"
    ]):
        families.add("panthenol")
        families.add("barrier")

    if any(x in text for x in [
        "peptide",
        "ペプチド"
    ]):
        families.add("peptide")
        pair_well_with.update([
            "retinoid",
            "pdrn",
            "barrier"
        ])

    return {
        "families": families,
        "strength": strength,
        "irritation_risk": irritation_risk,
        "pair_well_with": pair_well_with,
        "avoid_with": avoid_with
    }

# カテゴリ名そのまま（ブランド無し・具体的な製品名なし）の候補を弾くための共通リスト。
# 2位/3位候補の表示フィルタ（preserve_ranked_top_candidates）・
# score_product() のメイン候補選定・verified_products_cache の読み込みの
# 3箇所から参照し、定義のずれを防ぐ。
_GENERIC_CATEGORY_NAMES = {
    "化粧水", "美容液", "乳液", "クリーム", "日焼け止め", "洗顔", "洗顔料",
    "クレンジング", "パック", "マスク", "ピーリング", "導入美容液", "保湿クリーム",
    "美顔器", "サプリ", "サプリメント",
}


def _is_generic_candidate_name(brand, name, category=""):
    """
    ブランドなし・具体的な製品名がない候補かどうかを判定する。
    カテゴリ名そのもの（_GENERIC_CATEGORY_NAMES）だけでなく、
    「エマルジョン」（乳液の同義語）のような _CATEGORY_REQUIRED_KEYWORDS の
    同義語単体で名乗っている候補も同様に汎用名として弾く。
    """
    _, name_only = clean_brand_and_product_name(str(brand or ""), str(name or ""))
    name_only = name_only.strip()

    if not name_only:
        return True

    if name_only in _GENERIC_CATEGORY_NAMES:
        return True

    category_key = str(category or "").strip()
    if category_key:
        synonyms = _CATEGORY_REQUIRED_KEYWORDS.get(category_key, [])
        name_key = name_only.lower()
        if any(name_key == str(kw).strip().lower() for kw in synonyms):
            return True

    return False

def score_product_combination(
    selected_products,
    candidate
):
    score = 0

    current = [
        infer_active_profile(x)
        for x in selected_products
    ]

    new = infer_active_profile(candidate)

    families = set()

    for p in current:
        families.update(
            p["families"]
        )

    overlap = (
        families
        &
        new["families"]
    )

    synergy = (
        families
        &
        new["pair_well_with"]
    )

    conflict = (
        families
        &
        new["avoid_with"]
    )

    score += len(synergy) * 10
    score -= len(conflict) * 18

    total_strength = sum(
        2 if p["strength"] == "high"
        else 1
        for p in current
    )

    if new["strength"] == "high":
        total_strength += 2

    if total_strength >= 4:
        score -= 22

    return score

def score_product(product, step, user_data, budget_value):
    if is_wrong_cleanser_candidate(product,step):
        return -9999
    
    """
    DB商品のベーススコア
    このDB項目に対応:
    - name
    - brand
    - category
    - price_ref
    - image
    - active_ingredients
    - support_ingredients
    - formulation
    - concerns
    - skin_types
    - retinol_level
    - sensitive_ok
    - availability_japan
    - ingredient_strength
    - signature_ingredients
    - main_functions
    - ingredient_focus
    - technology
    - texture
    - contraindications
    """
    
    score = 0

    if is_non_cosmetic(product):
        return -9999

    if is_discontinued_or_suspicious_product(product):
        return -9999

    # ===== カテゴリ名（またはその同義語）そのまま／ブランド除去後に空になる候補は強制除外 =====
    # 例: 商品名が「日焼け止め」「エマルジョン」だけ、ブランド無しで具体的な製品名がない候補。
    # ここで弾かないと、そのまま「1位」として選定・表示され、
    # さらに verified_products_cache に永続キャッシュされて以後の診断にも
    # 汚染が広がる。
    if _is_generic_candidate_name(
        product.get("brand", ""),
        product.get("name", ""),
        step.get("category", "")
    ):
        return -9999

    # ===== ピーリング強制判定 =====
    if step.get("category") == "ピーリング":
        PEELING_INGREDIENTS = [
            "aha",
            "bha",
            "pha",
            "lha",
            "glycolic_acid",
            "lactic_acid",
            "salicylic_acid",
            "mandelic_acid",
            "papain",
            "bromelain",
            "protease",
            "enzyme"
        ]

        PEELING_NAME_KEYWORDS = [
            "ピーリング",
            "ピール",
            "スキンピール",
            "ゴマージュ",
            "角質",
            "角質ケア",
            "スクラブ",
            "アクアジェル",
            "酵素",
            "パパイン",
            "ブロメライン",
            "プロテアーゼ"
        ]

        product_actives = product.get("active_ingredients", []) or []
        product_name = str(product.get("name", "") or "")

        has_peeling_ingredient = any(
            i in product_actives
            for i in PEELING_INGREDIENTS
        )

        has_peeling_name = any(
            word in product_name
            for word in PEELING_NAME_KEYWORDS
        )

        if not has_peeling_ingredient and not has_peeling_name:
            return -9999 # 強制除外

    user_skin = normalize_text(user_data.get("skin_type", ""))
    if not user_skin:
        user_skin = normalize_text(user_data.get("oil", ""))

    purpose = step.get("purpose", "")
    ingredient_focus = step.get("ingredient_focus", "")
    category = step.get("category", "")
    product_category = normalize_candidate_category(
        product.get("category", ""),
        fallback=product.get("category", "")
    )

    step_category = normalize_candidate_category(
        category,
        fallback=category
    )
    # 朝のレチノール・レチナール系は、減点ではなく選定対象から除外する
    if step.get("_section") == "morning":
        product_actives_for_morning = product.get("active_ingredients", []) or []
        product_focuses_for_morning = product.get("ingredient_focus", []) or []
        product_contra_for_morning = product.get("contraindications", []) or []
        product_name_for_morning = normalize_text(product.get("name", ""))

        has_morning_retinoid = (
            safe_retinol_level(product.get("retinol_level", 0)) > 0
            or "retinol" in product_actives_for_morning
            or "retinal" in product_actives_for_morning
            or "retinoid" in product_actives_for_morning
            or "retinol" in product_focuses_for_morning
            or "retinal" in product_focuses_for_morning
            or "retinoid" in product_focuses_for_morning
            or "morning_use_caution" in product_contra_for_morning
            or "レチノール" in product_name_for_morning
            or "レチナール" in product_name_for_morning
        )

        if has_morning_retinoid:
            return -9999

        # 光感受性物質（AHA/BHA/ピーリング系）も朝NG: ソフトペナルティでは不十分なため除外
        if "photosensitivity" in product_contra_for_morning:
            return -9999
    # カテゴリ一致は最優先
    if product_category != step_category:
        return -9999

    product_name_for_category = normalize_text(product.get("name", ""))

    if step_category == "美容液":
        if any(w in product_name_for_category for w in [
            "ローション",
            "トナー",
            "化粧水",
            "lotion",
            "toner"
        ]):
            return -9999
    # 化粧水枠にオールインワンジェル・ゲルは入れない
    if step_category == "化粧水":
        product_name_for_category = normalize_text(product.get("name", ""))
        product_formulation_for_category = " ".join([
            normalize_text(x)
            for x in product.get("formulation", [])
        ])

        all_in_one_text = " ".join([
            product_name_for_category,
            product_formulation_for_category
        ])

        if any(w in all_in_one_text for w in [
            "オールインワン",
            "allinone",
            "all-in-one",
            "オールインワンジェル",
            "オールインワンゲル"
        ]):
            return -9999
    score += 40

    concern_tags = purpose_to_concern_tags(purpose)
    ingredient_tag = normalize_ingredient_tag(ingredient_focus)

    score += score_goal_fit(product, step)
    score += score_signature_ingredients(product, step)

    score += apply_common_score_rules(
        product=product,
        step=step,
        user_data=user_data,
        budget_value=budget_value,
        concern_tags=concern_tags,
        ingredient_tag=ingredient_tag
    )

        # カテゴリ別補正
    if step_category in ["クレンジング", "洗顔"]:
        score += apply_cleansing_score_rules(
            product=product,
            user_data=user_data,
            concern_tags=concern_tags
        )

    elif step_category == "日焼け止め":
        score += apply_sunscreen_score_rules(
            product=product,
            step=step,
            user_data=user_data,
            concern_tags=concern_tags
        )

    elif step_category in ["クリーム", "乳液"]:
        product_text = normalize_text(" ".join([
            str(product.get("name", "") or ""),
            str(product.get("texture", "") or ""),
            " ".join([str(x) for x in product.get("active_ingredients", []) or []]),
            " ".join([str(x) for x in product.get("support_ingredients", []) or []]),
            " ".join([str(x) for x in product.get("main_functions", []) or []]),
        ]))

        if any(w in product_text for w in [
            "セラミド",
            "ヒアルロン酸",
            "ナイアシンアミド",
            "パンテノール",
            "cica",
            "シカ",
            "バリア",
            "保湿",
            "鎮静"
        ]):
            score += 18

        if any(tag in concern_tags for tag in ["dryness", "barrier", "redness"]):
            score += 12

        if any(w in product_text for w in ["重い", "こってり", "高保湿"]) and normalize_text(user_data.get("oil", "")) == "oily":
            score -= 6

    elif step_category == "パック":
        product_text = normalize_text(" ".join([
            str(product.get("name", "") or ""),
            str(product.get("texture", "") or ""),
            " ".join([str(x) for x in product.get("active_ingredients", []) or []]),
            " ".join([str(x) for x in product.get("support_ingredients", []) or []]),
            " ".join([str(x) for x in product.get("main_functions", []) or []]),
        ]))

        if any(w in product_text for w in [
            "パック",
            "マスク",
            "シートマスク",
            "フェイスマスク",
            "cica",
            "シカ",
            "ヒアルロン酸",
            "セラミド",
            "パンテノール",
            "鎮静",
            "保湿",
            "バリア"
        ]):
            score += 20

        if any(tag in concern_tags for tag in ["dryness", "barrier", "redness", "dullness"]):
            score += 12

    elif step_category == "ピーリング":
        product_text = normalize_text(" ".join([
            str(product.get("name", "") or ""),
            " ".join([str(x) for x in product.get("active_ingredients", []) or []]),
            " ".join([str(x) for x in product.get("main_functions", []) or []]),
        ]))

        if any(w in product_text for w in [
            "aha",
            "bha",
            "pha",
            "lha",
            "グリコール酸",
            "乳酸",
            "サリチル酸",
            "マンデル酸",
            "ピーリング",
            "ピール",
            "角質",
            "ゴマージュ"
        ]):
            score += 18

        if any(tag in concern_tags for tag in ["pores", "texture", "dullness"]):
            score += 10

 
    product_actives = product.get("active_ingredients", [])
    if ingredient_tag and ingredient_tag in product_actives:
        score += 15  # ←強めにする（10〜20調整可）

    product_skin_types = product.get("skin_types") or []
    if user_skin in product_skin_types:
        score += 5
    elif "normal" in product_skin_types:
        score += 2

    # ユーザーが明示した肌悩みと商品 concerns の一致ボーナス
    user_concern_tags = get_user_concern_tags(user_data)
    if user_concern_tags:
        product_concerns = product.get("concerns") or []
        for tag in user_concern_tags:
            if tag in product_concerns:
                score += 8

    return score


def normalize_text_value(value):
    if value is None:
        return ""
    return str(value).strip().lower()


def as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def collect_product_terms(product):
    terms = set()

    for key in [
        "active_ingredients",
        "support_ingredients",
        "main_functions",
        "ingredient_focus",
        "formulation",
        "technology",
        "signature_ingredients",
        "concerns",
    ]:
        for item in as_list(product.get(key)):
            if item:
                terms.add(normalize_text_value(item))

    strength = product.get("ingredient_strength", {})
    if isinstance(strength, dict):
        for key, value in strength.items():
            if value:
                terms.add(normalize_text_value(key))
                terms.add(normalize_text_value(value))

    name = normalize_text_value(product.get("name", ""))
    brand = normalize_text_value(product.get("brand", ""))

    if name:
        terms.add(name)
    if brand:
        terms.add(brand)

    # 楽天タイトルを個別トークンに分解して追加（score_improvement の keyword マッチに使用）
    rakuten_title = str(product.get("rakuten_title") or "")
    if rakuten_title:
        import re as _re
        for token in _re.split(r'[\s　・,/\-（）()【】「」]', rakuten_title):
            t = normalize_text_value(token.strip())
            if t and len(t) >= 2:
                terms.add(t)

    return terms


IMPROVEMENT_KEYWORDS = {
    "acne": {
        "strong": [
            "azelaic_acid", "アゼライン酸", "salicylic_acid", "サリチル酸",
            "bha", "グリチルリチン酸", "tea_tree", "ティーツリー",
        ],
        "support": [
            "niacinamide", "ナイアシンアミド", "cica", "シカ",
            "centella", "ツボクサ", "panthenol", "パンテノール",
            "low_irritation", "低刺激",
        ],
    },
    "acne_marks_red": {
        "strong": [
            "cica", "シカ", "centella", "ツボクサ", "madecassoside",
            "マデカッソシド", "azelaic_acid", "アゼライン酸",
            "tranexamic_acid", "トラネキサム酸",
        ],
        "support": [
            "panthenol", "パンテノール", "allantoin", "アラントイン",
            "ceramide", "セラミド", "low_irritation", "低刺激",
        ],
    },
    "pigmentation": {
        "strong": [
            "vitamin_c", "ビタミンc", "ascorbic", "アスコルビン酸",
            "tranexamic_acid", "トラネキサム酸", "arbutin", "アルブチン",
            "kojic_acid", "コウジ酸", "glutathione", "グルタチオン",
            "niacinamide", "ナイアシンアミド",
        ],
        "support": [
            "retinol", "レチノール", "retinal", "レチナール",
            "peeling", "ピーリング", "turnover", "ターンオーバー",
            "sunscreen", "日焼け止め", "uv", "spf", "pa",
        ],
    },
    "pores": {
        "strong": [
            "retinol", "レチノール", "retinal", "レチナール",
            "niacinamide", "ナイアシンアミド", "azelaic_acid", "アゼライン酸",
            "salicylic_acid", "サリチル酸", "bha",
        ],
        "support": [
            "peptide", "ペプチド", "vitamin_c", "ビタミンc",
            "clay", "クレイ", "酵素", "enzyme",
        ],
    },
    "firmness": {
        "strong": [
            "retinol", "レチノール", "retinal", "レチナール",
            "peptide", "ペプチド", "pdrn", "nad",
        ],
        "support": [
            "ceramide", "セラミド", "panthenol", "パンテノール",
            "hyaluronic", "ヒアルロン酸", "collagen", "コラーゲン",
        ],
    },
    "barrier": {
        "strong": [
            "ceramide", "セラミド", "panthenol", "パンテノール",
            "cica", "シカ", "centella", "ツボクサ",
        ],
        "support": [
            "hyaluronic", "ヒアルロン酸", "beta_glucan", "βグルカン",
            "allantoin", "アラントイン", "squalane", "スクワラン",
            "low_irritation", "低刺激",
        ],
    },
    "dryness": {
        "strong": [
            "ceramide", "セラミド", "hyaluronic", "ヒアルロン酸",
            "panthenol", "パンテノール", "squalane", "スクワラン",
        ],
        "support": [
            "glycerin", "グリセリン", "amino_acid", "アミノ酸",
            "beta_glucan", "βグルカン",
        ],
    },
}


CATEGORY_IMPROVEMENT_BONUS = {
    "美容液": 18,
    "セラム": 18,
    "クリーム": 14,
    "乳液": 12,
    "化粧水": 10,
    "パック": 12,
    "洗顔": 7,
    "洗顔料": 7,
    "クレンジング": 5,
    "ピーリング": 13,
    "日焼け止め": 16,
}

def build_score_based_improvement_plan(scores, existing_plan=None):
    if not isinstance(scores, dict):
        scores = {}

    existing_plan = existing_plan if isinstance(existing_plan, dict) else {}

    priority_concerns = []
    key_ingredients = []

    rules = [
        {
            "score_key": "oil_balance",
            "label": "皮脂バランス",
            "ingredients": ["アゼライン酸", "ナイアシンアミド", "BHA"]
        },
        {
            "score_key": "pores",
            "label": "毛穴",
            "ingredients": ["レチノール", "ナイアシンアミド", "アゼライン酸"]
        },
        {
            "score_key": "tone_evenness",
            "label": "色ムラ",
            "ingredients": ["ビタミンC", "トラネキサム酸", "ナイアシンアミド"]
        },
        {
            "score_key": "dullness",
            "label": "くすみ",
            "ingredients": ["ビタミンC", "トラネキサム酸", "AHA"]
        },
        {
            "score_key": "acne",
            "label": "ニキビ",
            "ingredients": ["アゼライン酸", "BHA", "CICA"]
        },
        {
            "score_key": "texture",
            "label": "キメ",
            "ingredients": ["セラミド", "ナイアシンアミド", "PHA"]
        },
        {
            "score_key": "hydration",
            "label": "保湿",
            "ingredients": ["ヒアルロン酸", "セラミド", "パンテノール"]
        },
        {
            "score_key": "barrier",
            "label": "バリア",
            "ingredients": ["セラミド", "パンテノール", "CICA"]
        },
        {
            "score_key": "redness",
            "label": "赤み",
            "ingredients": ["CICA", "アゼライン酸", "パンテノール"]
        },
        {
            "score_key": "firmness",
            "label": "ハリ",
            "ingredients": ["レチノール", "レチナール", "ペプチド"]
        }
    ]

    scored_rules = []

    for rule in rules:
        value = safe_int(scores.get(rule["score_key"], 0))

        if value <= 44:
            priority_level = 3
        elif value <= 59:
            priority_level = 2
        elif value <= 69:
            priority_level = 1
        else:
            priority_level = 0

        if priority_level > 0:
            scored_rules.append({
                "label": rule["label"],
                "score": value,
                "priority_level": priority_level,
                "ingredients": rule["ingredients"]
            })

    scored_rules = sorted(
        scored_rules,
        key=lambda x: (
            -x["priority_level"],
            x["score"]
        )
    )

    for item in scored_rules[:3]:
        priority_concerns.append(item["label"])

        for ingredient in item["ingredients"]:
            if ingredient not in key_ingredients:
                key_ingredients.append(ingredient)

    if not priority_concerns:
        priority_concerns = existing_plan.get("priority_concerns", []) or ["バリア", "保湿"]

    if not key_ingredients:
        key_ingredients = existing_plan.get("key_ingredients", []) or ["セラミド", "パンテノール"]

    return {
        "priority_concerns": priority_concerns[:3],
        "key_ingredients": key_ingredients[:8],
        "care_direction": "項目別スコアが低い悩みを優先しつつ、刺激を抑えて継続しやすいケアを行う"
    }


def build_improvement_priority(scores):
    """スコア昇順（低い順）で改善優先順位リストを返す。無料ユーザー用。"""
    _labels = [
        ("oil_balance",   "皮脂バランス"),
        ("redness",       "赤み"),
        ("pores",         "毛穴"),
        ("hydration",     "保湿"),
        ("firmness",      "ハリ"),
        ("acne",          "ニキビ"),
        ("dullness",      "くすみ"),
        ("barrier",       "バリア"),
        ("texture",       "キメ"),
        ("tone_evenness", "色ムラ"),
    ]
    items = sorted(
        [{"key": k, "label": lbl, "score": int(scores.get(k, 0) or 0)} for k, lbl in _labels],
        key=lambda x: x["score"]
    )
    return [{"rank": i + 1, **item} for i, item in enumerate(items)]


def infer_improvement_targets(improvement_plan):
    """
    improvement_plan / step / Gemini出力の文章から、
    改善ターゲットを広めに推定する。
    """
    targets = set()

    texts = []

    if isinstance(improvement_plan, dict):

        for value in improvement_plan.values():

            if isinstance(value, list):
                texts.extend(
                    str(v)
                    for v in value
                )

            elif value:
                texts.append(str(value))

    else:
        texts.append(
            str(improvement_plan or "")
        )

    raw_text = " ".join(texts).lower()

    target_keywords = {
        "acne": [
            "ニキビ", "吹き出物", "acne", "breakout", "blemish",
            "肌荒れ", "炎症ニキビ"
        ],
        "acne_marks_red": [
            "赤み", "赤ニキビ跡", "赤いニキビ跡", "炎症後紅斑",
            "post acne redness", "redness", "pie"
        ],
        "pigmentation": [
            "色素沈着", "茶ニキビ跡", "茶色いニキビ跡", "シミ",
            "くすみ", "美白", "透明感", "brightening",
            "pigmentation", "dark spot", "pih", "melasma"
        ],
        "pores": [
            "毛穴", "開き毛穴", "詰まり毛穴", "黒ずみ",
            "角栓", "皮脂", "テカリ", "pores", "sebum",
            "blackhead", "clogged pore"
        ],
        "firmness": [
            "ハリ", "たるみ", "弾力", "小じわ", "しわ",
            "エイジング", "aging", "firmness", "elasticity",
            "wrinkle", "fine line"
        ],
        "barrier": [
            "バリア", "敏感", "ゆらぎ", "鎮静", "刺激",
            "赤みが出やすい", "乾燥しやすい", "barrier",
            "sensitive", "soothing", "calming"
        ],
        "dryness": [
            "乾燥", "保湿", "水分", "つっぱり", "かさつき",
            "dryness", "moisture", "hydration"
        ],
        "oil_control": [
            "皮脂",
            "テカリ",
            "脂性",
            "oily",
            "sebum"
        ],

        "soothing": [
            "鎮静",
            "cica",
            "ドクダミ",
            "赤み",
            "soothing",
            "calming"
        ]
            }

    for target, keywords in target_keywords.items():
        if any(keyword in raw_text for keyword in keywords):
            targets.add(target)

    # ニキビ跡という表現だけの場合は赤・茶どちらも見る
    if "ニキビ跡" in raw_text or "acne scar" in raw_text or "acne marks" in raw_text:
        targets.add("acne_marks_red")
        targets.add("pigmentation")

    if (
        "赤み" in raw_text
        and "ニキビ跡" in raw_text
    ):
        targets.add("acne_marks_red")

    if (
        "色素沈着" in raw_text
        and "ニキビ跡" in raw_text
    ):
        targets.add("pigmentation")

    # 毛穴 + ハリ系はたるみ毛穴対策として扱う
    if "たるみ毛穴" in raw_text:
        targets.add("pores")
        targets.add("firmness")

    # 何も拾えない場合は、最低限バリア・乾燥を評価
    if not targets:
        targets.update(["barrier", "dryness"])

    return targets


def term_matches(terms, keywords):
    for term in terms:
        for keyword in keywords:
            keyword = normalize_text_value(keyword)
            if keyword and keyword in term:
                return True
    return False


def score_improvement(product, improvement_plan=None):
    """
    改善寄与スコア。
    美容液だけでなく、化粧水・乳液・クリーム・洗顔・クレンジング・日焼け止め・パック・ピーリングも評価する。
    """
    if not isinstance(product, dict):
        return 0

    score = 0
    terms = collect_product_terms(product)
    targets = infer_improvement_targets(improvement_plan or {})

    category = str(product.get("category", "")).strip()
    name = str(product.get("name", "")).lower()
    ingredient_strength = product.get("ingredient_strength", {})
    main_functions = product.get("main_functions", [])
    ingredient_focus = product.get("ingredient_focus", [])

    if not isinstance(ingredient_strength, dict):
        ingredient_strength = {}

    if not isinstance(main_functions, list):
        main_functions = []

    if not isinstance(ingredient_focus, list):
        ingredient_focus = []
    score += CATEGORY_IMPROVEMENT_BONUS.get(category, 0)

    for target in targets:
        rule = IMPROVEMENT_KEYWORDS.get(target, {})

        if term_matches(terms, rule.get("strong", [])):
            score += 28

        if term_matches(terms, rule.get("support", [])):
            score += 14
    for ingredient, strength in ingredient_strength.items():
        ingredient = normalize_text(ingredient)
        strength = normalize_text(strength)

        if not ingredient:
            continue

        if ingredient in terms:
            if strength in ["high", "strong"]:
                score += 10
            elif strength in ["medium", "middle"]:
                score += 6
            elif strength in ["low", "mild"]:
                score += 3

            function_text = " ".join(
                str(x)
                for x in main_functions + ingredient_focus
            ).lower()

            function_bonus_keywords = {
                "美白": 8,
                "毛穴": 8,
                "ニキビ": 8,
                "ハリ": 8,
                "バリア": 7,
                "保湿": 6,
                "鎮静": 6,
                "角質": 7,
                "uv": 8,
                "UV": 8,
                "紫外線": 8,
            }

            for keyword, bonus in function_bonus_keywords.items():
                if keyword.lower() in function_text:
                    score += bonus  
    # 商品名からの補正
    name_bonus_keywords = {
        "メラノ": 18,
        "melano": 18,
        "ビタミンc": 18,
        "vitamin c": 18,
        "レチノール": 22,
        "retinol": 22,
        "レチナール": 24,
        "retinal": 24,
        "アゼライン": 22,
        "azelaic": 22,
        "シカ": 12,
        "cica": 12,
        "セラミド": 14,
        "ceramide": 14,
        "ピーリング": 18,
        "peeling": 18,
        "日焼け止め": 18,
        "uv": 18,
        "spf": 18,
    }

    for keyword, bonus in name_bonus_keywords.items():
        if keyword in name:
            score += bonus

    # 日焼け止めは色素沈着・赤み予防として改善寄与を持たせる
    uv_level = product.get("uv_level", {})
    if category == "日焼け止め" or "sunscreen" in terms or "日焼け止め" in terms:
        score += 16
        if isinstance(uv_level, dict):
            spf = uv_level.get("spf")
            pa = str(uv_level.get("pa", ""))
            if isinstance(spf, int) and spf >= 30:
                score += 8
            if "+++" in pa:
                score += 8

    # ピーリングはターンオーバー改善として評価
    if category == "ピーリング" or "peeling" in terms or "ピーリング" in terms:
        score += 15

    # 低刺激・継続性
    sensitive_ok = str(product.get("sensitive_ok", "")).lower()
    if sensitive_ok == "yes":
        score += 8
    elif sensitive_ok == "no":
        score -= 8

    # 上限を設定して暴走防止
    return max(0, min(score, 100))

# =========================================================
# SCORE BLOCK END
# =========================================================
def build_improvement_reason(product, improvement_plan=None):
    if not isinstance(product, dict):
        return ""

    terms = collect_product_terms(product)
    targets = infer_improvement_targets(improvement_plan or {})
    category = str(product.get("category", "")).strip()
    sensitive_ok = str(product.get("sensitive_ok", "")).lower()

    target_labels = {
        "acne": "ニキビ予防",
        "acne_marks_red": "赤み・赤ニキビ跡",
        "pigmentation": "色素沈着・くすみ",
        "pores": "毛穴",
        "firmness": "ハリ",
        "barrier": "バリア",
        "dryness": "乾燥",
        "oil_control": "皮脂",
        "soothing": "鎮静"
    }

    reasons = []

    for target in targets:
        rule = IMPROVEMENT_KEYWORDS.get(target, {})
        label = target_labels.get(target, target)

        if term_matches(terms, rule.get("strong", [])):
            reasons.append(f"{label}に合う主成分を含む")
        elif term_matches(terms, rule.get("support", [])):
            reasons.append(f"{label}を支える補助成分を含む")

    if category == "日焼け止め":
        reasons.append("紫外線対策で赤み・色素沈着の悪化を防ぐ")

    elif category == "ピーリング":
        reasons.append("角質ケアでくすみ・毛穴目立ちを支える")

    elif category in ["洗顔", "洗顔料", "クレンジング"]:
        reasons.append("皮脂や汚れを落とし、ニキビ・毛穴悪化を防ぐ")

    elif category in ["乳液", "クリーム"]:
        reasons.append("バリアを守り、攻め成分を続けやすくする")

    elif category == "パック":
        reasons.append("集中ケアとして保湿・鎮静を補いやすい")

    if sensitive_ok == "yes":
        reasons.append("低刺激で継続しやすい")

    unique = []
    for reason in reasons:
        if reason not in unique:
            unique.append(reason)

    return " / ".join(unique[:3])
def db_has_matching_ingredient(products, ingredient_focus):
    ingredient_tag = normalize_ingredient_tag(ingredient_focus)

    # 正規化できない成分名は「DBにない扱い」にしない
    # ここでFalseにすると、未知成分はAIに流れやすくなる
    if not ingredient_tag:
        return False

    for p in products:
        active_ingredients = p.get("active_ingredients", [])
        support_ingredients = p.get("support_ingredients", [])

        if ingredient_tag in active_ingredients or ingredient_tag in support_ingredients:
            return True

    return False

def score_ai_candidate(step, products):
    score = 0

    ingredient_focus = step.get("ingredient_focus", "")
    candidates = step.get("product_candidates", [])
    selection_reason = step.get("selection_reason", "")
    estimated_price = step.get("estimated_price", 0)
    category = step.get("category", "")

    # 候補がないならAIは戦えない
    if not candidates or not str(candidates[0]).strip():
        return -9999

    # 候補があるだけで基本点
    score += 35

    # 説明があるなら少し加点
    if selection_reason and str(selection_reason).strip():
        score += 10

    # 価格推定があるなら少し加点
    if isinstance(estimated_price, int) and estimated_price > 0:
        score += 5

    # 成分がDBにないならAIをかなり強くする
    if not db_has_matching_ingredient(products, ingredient_focus):
        score += 30

    # ingredient_focus があるなら少し加点
    if ingredient_focus and str(ingredient_focus).strip():
        score += 10

    # カテゴリがあるなら少し加点
    if category and str(category).strip():
        score += 5

    return score
def infer_virtual_product_fields(name, category="", ingredient_focus="", purpose=""):
    # 商品に含まれる成分の推定は、stepの目的や希望成分ではなく商品名だけから行う
    text = str(name or "").lower()

    active = []
    support = []
    functions = []
    focuses = []
    contraindications = []
    ingredient_strength = {}
    retinol_level = 0

    rules = [
        {
            "keywords": ["ビタミンc", "vitamin c", "メラノ", "vc", "c25", "c23", "c10"],
            "active": ["vitamin_c"],
            "functions": ["美白", "くすみケア"],
            "focuses": ["vitamin_c"],
            "strength": {"vitamin_c": "medium"}
        },
        {
            "keywords": ["レチノール", "retinol"],
            "active": ["retinol"],
            "functions": ["ハリ改善", "毛穴ケア"],
            "focuses": ["retinol"],
            "strength": {"retinol": "medium"},
            "retinol_level": 2,
            "contraindications": ["morning_use_caution", "retinol_same_routine"]
        },
        {
            "keywords": ["レチナール", "retinal", "レチa", "レチA"],
            "active": ["retinal"],
            "functions": ["ハリ改善", "毛穴ケア"],
            "focuses": ["retinal"],
            "strength": {"retinal": "medium"},
            "retinol_level": 3,
            "contraindications": ["morning_use_caution", "retinol_same_routine"]
        },
        {
            "keywords": ["アゼライン", "azelaic"],
            "active": ["azelaic_acid"],
            "functions": ["ニキビケア", "皮脂コントロール"],
            "focuses": ["azelaic_acid"],
            "strength": {"azelaic_acid": "medium"}
        },
        {
            "keywords": ["ナイアシンアミド", "niacinamide"],
            "active": ["niacinamide"],
            "functions": ["毛穴ケア", "バリア改善"],
            "focuses": ["niacinamide"],
            "strength": {"niacinamide": "medium"}
        },
        {
            "keywords": ["トラネキサム", "tranexamic"],
            "active": ["tranexamic_acid"],
            "functions": ["美白", "色素沈着ケア"],
            "focuses": ["tranexamic_acid"],
            "strength": {"tranexamic_acid": "medium"}
        },
        {
            "keywords": ["pdrn"],
            "active": ["pdrn"],
            "functions": ["ハリ改善", "肌修復サポート"],
            "focuses": ["pdrn"],
            "strength": {"pdrn": "medium"}
        },
        {
            "keywords": ["ペプチド", "peptide"],
            "active": ["peptide"],
            "functions": ["ハリ改善"],
            "focuses": ["peptide"],
            "strength": {"peptide": "medium"}
        },
        {
            "keywords": ["セラミド", "ceramide"],
            "support": ["ceramide"],
            "functions": ["バリア改善", "保湿"],
            "focuses": ["ceramide"]
        },
        {
            "keywords": ["シカ", "cica", "ツボクサ"],
            "support": ["cica"],
            "functions": ["鎮静", "バリア改善"],
            "focuses": ["cica"]
        },
        {
            "keywords": ["パンテノール", "panthenol"],
            "support": ["panthenol"],
            "functions": ["鎮静", "バリア改善"],
            "focuses": ["panthenol"]
        },
        {
            "keywords": ["aha", "bha", "pha", "ピーリング", "角質"],
            "active": ["aha_bha"],
            "functions": ["角質ケア", "毛穴ケア"],
            "focuses": ["aha_bha"],
            "strength": {"aha_bha": "medium"},
            "contraindications": ["acid_same_routine", "photosensitivity", "morning_use_caution"]
        },
        {
            "keywords": ["uv", "spf", "日焼け止め"],
            "active": ["uv_filter"],
            "functions": ["UV防御", "色素沈着予防"],
            "focuses": ["uv_filter"]
        }
    ]

    for rule in rules:
        if any(keyword.lower() in text for keyword in rule["keywords"]):
            active.extend(rule.get("active", []))
            support.extend(rule.get("support", []))
            functions.extend(rule.get("functions", []))
            focuses.extend(rule.get("focuses", []))
            contraindications.extend(rule.get("contraindications", []))

            for key, value in rule.get("strength", {}).items():
                ingredient_strength[key] = value

            retinol_level = max(
                retinol_level,
                safe_retinol_level(rule.get("retinol_level", 0))
            )

    # sensitive_ok 推論
    sensitive_ok = None
    SENSITIVE_YES_KEYWORDS = [
        "低刺激", "敏感肌", "セラミド", "ceramide", "cica", "シカ", "ツボクサ",
        "ドクダミ", "パンテノール", "panthenol", "ノンコメドジェニック",
        "アレルギーテスト済", "ノンアルコール", "バリアケア", "バリア強化"
    ]
    SENSITIVE_NO_KEYWORDS = [
        "レチノール", "retinol", "レチナール", "retinal",
        "高濃度", "高配合", "ピーリング", "ピール", "aha", "bha", "pha",
        "グリコール酸", "サリチル酸", "マンデル酸"
    ]
    if any(k in text for k in SENSITIVE_NO_KEYWORDS):
        sensitive_ok = "no"
    elif any(k in text for k in SENSITIVE_YES_KEYWORDS):
        sensitive_ok = "yes"

    # availability_japan 推論
    DRUGSTORE_BRAND_KEYWORDS = [
        "資生堂", "shiseido", "花王", "kao", "キュレル", "curel",
        "ビオレ", "コーセー", "kose", "雪肌精", "sekkisei",
        "ニベア", "nivea", "ちふれ", "セザンヌ", "cezanne",
        "なめらか本舗", "haba", "dhc", "ロート", "rohto",
        "メンソレータム", "小林製薬", "ハトムギ", "スキンアクア",
        "アネッサ", "anessa", "マキアージュ", "maquillage",
        "ソフィーナ", "sofina", "エリクシール", "elixir",
        "オルビス", "orbis", "ファンケル", "fancl"
    ]
    availability_japan = ["amazon", "rakuten"]
    if any(k in text for k in DRUGSTORE_BRAND_KEYWORDS):
        availability_japan = ["drugstore", "amazon", "rakuten"]

    return {
        "active_ingredients": list(dict.fromkeys(active)),
        "support_ingredients": list(dict.fromkeys(support)),
        "main_functions": list(dict.fromkeys(functions)),
        "ingredient_focus": list(dict.fromkeys(focuses)),
        "ingredient_strength": ingredient_strength,
        "retinol_level": retinol_level,
        "contraindications": list(dict.fromkeys(contraindications)),
        "sensitive_ok": sensitive_ok,
        "availability_japan": availability_japan,
    }
def normalize_candidate_category(value, fallback=""):
    raw_value = str(value or "").strip()
    raw_fallback = str(fallback or "").strip()

    if not raw_value:
        return raw_fallback

    allowed = {
        "クレンジング",
        "洗顔",
        "化粧水",
        "美容液",
        "導入美容液",
        "乳液",
        "クリーム",
        "日焼け止め",
        "パック",
        "ピーリング"
    }

    if raw_value in allowed:
        return raw_value

    mapped = AI_CATEGORY_MAP.get(raw_value.lower())
    if mapped in allowed:
        return mapped

    text = normalize_text(raw_value)

    if any(w in text for w in ["日焼け止め", "uv", "spf", "pa++++", "pa+++", "サンスクリーン", "サンケア", "suncream", "sun cream", "sunscreen"]):
        return "日焼け止め"

    if any(w in text for w in ["クレンジング", "メイク落とし", "cleansing", "makeup remover", "remover"]):
        return "クレンジング"

    if any(w in text for w in ["洗顔", "洗顔料", "洗顔フォーム", "フォーム", "フェイスウォッシュ", "cleanser", "face wash", "facewash"]):
        return "洗顔"

    if any(w in text for w in ["導入", "ブースター", "先行", "セラムヴェール", "ブースト"]):
        return "導入美容液"

    if any(w in text for w in ["化粧水", "ローション", "トナー", "toner", "lotion"]):
        return "化粧水"

    if any(w in text for w in ["乳液", "ミルク", "エマルジョン", "emulsion", "milk"]):
        return "乳液"

    if any(w in text for w in ["クリーム", "バーム", "moisturizer", "moisturiser", "cream", "balm"]):
        return "クリーム"

    if any(w in text for w in ["美容液", "セラム", "エッセンス", "アンプル", "serum", "essence", "ampoule"]):
        return "美容液"

    if any(w in text for w in [
        "パック",
        "マスク",
        "シートマスク",
        "フェイスマスク",
        "フェイスパック",
        "部分用マスク",
        "集中マスク",
        "mask",
        "sheet mask",
        "face mask"
    ]):
        return "パック"

    if any(w in text for w in ["ピーリング", "角質", "exfoliator", "exfoliating", "peeling"]):
        return "ピーリング"

    fallback_mapped = AI_CATEGORY_MAP.get(raw_fallback.lower())
    if fallback_mapped in allowed:
        return fallback_mapped

    return raw_fallback
    
def safe_bundle_quantity(value):
    if isinstance(value, int) and value > 0:
        return value

    if isinstance(value, str):
        value = value.strip()
        if value.isdigit():
            number = int(value)
            if number > 0:
                return number

    return 1

def build_virtual_product_from_ai_candidate(step, candidate):
    category = step.get("category", "")
    ingredient_focus = step.get("ingredient_focus", "")
    purpose = step.get("purpose", "")

    inferred_fields = infer_virtual_product_fields(
        name=candidate.get("name", "") if isinstance(candidate, dict) else candidate,
        category=category,
        ingredient_focus=ingredient_focus,
        purpose=purpose
    )

    if isinstance(candidate, str):
        candidate = {
            "name": candidate,
            "brand": "",
            "price_ref": 0,
            "raw_price": 0,
            "bundle_quantity": 1,
            "active_ingredients": [],
            "support_ingredients": [],
            "signature_ingredients": [],
            "concerns": purpose_to_concern_tags(purpose),
            "skin_types": [],
            "sensitive_ok": "unknown",
            "retinol_level": 0,
            "main_functions": [],
            "ingredient_focus": [],
            "ingredient_strength": {},
            "formulation": [],
            "technology": [],
            "texture": "",
            "contraindications": [],
            "availability_japan": [],
            "uv_level": {},
            "reason": ""
        }

    if not isinstance(candidate, dict):
        candidate = {}

    active_ingredients = [
        normalize_ingredient_tag(x)
        for x in candidate.get("active_ingredients", [])
    ]
    active_ingredients = [x for x in active_ingredients if x]

    for x in inferred_fields.get("active_ingredients", []):
        x = normalize_ingredient_tag(x)
        if x and x not in active_ingredients:
            active_ingredients.append(x)

    ingredient_tag = normalize_ingredient_tag(ingredient_focus)
    if ingredient_tag and ingredient_tag not in active_ingredients:
        active_ingredients.append(ingredient_tag)

    support_ingredients = [
        normalize_ingredient_tag(x)
        for x in candidate.get("support_ingredients", [])
    ]
    support_ingredients = [x for x in support_ingredients if x]

    for x in inferred_fields.get("support_ingredients", []):
        x = normalize_ingredient_tag(x)
        if x and x not in support_ingredients:
            support_ingredients.append(x)

    signature_ingredients = [
        normalize_ingredient_tag(x)
        for x in candidate.get("signature_ingredients", [])
    ]
    signature_ingredients = [
        x for x in signature_ingredients
        if x in signature_ingredient_effects
    ]

    for x in inferred_fields.get("signature_ingredients", []):
        x = normalize_ingredient_tag(x)
        if x and x in signature_ingredient_effects and x not in signature_ingredients:
            signature_ingredients.append(x)

    concerns = []
    for c in candidate.get("concerns", []):
        c = normalize_text(c)
        if c in [
            "pores",
            "acne",
            "redness",
            "oil_control",
            "dryness",
            "barrier",
            "dullness",
            "whitening",
            "aging"
        ]:
            concerns.append(c)

    if not concerns:
        concerns = purpose_to_concern_tags(purpose)

    skin_types = []
    for s in candidate.get("skin_types", []):
        s = normalize_text(s)
        if s in ["dry", "oily", "mixed", "sensitive", "normal"]:
            skin_types.append(s)

    for s in inferred_fields.get("skin_types", []):
        s = normalize_text(s)
        if s in ["dry", "oily", "mixed", "sensitive", "normal"] and s not in skin_types:
            skin_types.append(s)

    sensitive_ok = normalize_text(candidate.get("sensitive_ok", "unknown"))
    if sensitive_ok not in ["yes", "no", "unknown"]:
        sensitive_ok = "unknown"
    if sensitive_ok == "unknown" and inferred_fields.get("sensitive_ok") in ["yes", "no"]:
        sensitive_ok = inferred_fields["sensitive_ok"]

    texture = normalize_text(candidate.get("texture", ""))
    if not texture:
        texture = normalize_text(inferred_fields.get("texture", ""))

    if texture not in [
        "light",
        "watery",
        "gel",
        "medium",
        "essence",
        "cream",
        "rich",
        "oil",
        "balm",
        "foam",
        "powder"
    ]:
        texture = ""

    main_functions = [
        MAIN_FUNCTION_MAP.get(str(x), str(x))
        for x in candidate.get("main_functions", [])
        if str(x).strip()
    ]

    for x in inferred_fields.get("main_functions", []):
        mapped = MAIN_FUNCTION_MAP.get(str(x), str(x))
        if mapped and mapped not in main_functions:
            main_functions.append(mapped)

    main_functions = [
        x for x in main_functions
        if x in MAIN_FUNCTION_TAGS
    ]

    ingredient_focus_list = []

    raw_focuses = candidate.get("ingredient_focus", [])
    if isinstance(raw_focuses, str):
        raw_focuses = [raw_focuses]

    if isinstance(raw_focuses, list):
        for x in raw_focuses:
            x = normalize_text(x)
            if x:
                ingredient_focus_list.append(x)

    if ingredient_focus:
        ingredient_focus_list.append(str(ingredient_focus))

    for x in inferred_fields.get("ingredient_focus", []):
        x = normalize_text(x)
        if x:
            ingredient_focus_list.append(x)

    formulation = [
        str(x) for x in candidate.get("formulation", [])
        if str(x).strip()
    ]

    for x in inferred_fields.get("formulation", []):
        x = str(x).strip()
        if x and x not in formulation:
            formulation.append(x)

    technology = [
        str(x) for x in candidate.get("technology", [])
        if str(x).strip()
    ]

    for x in inferred_fields.get("technology", []):
        x = str(x).strip()
        if x and x not in technology:
            technology.append(x)

    contraindications = [
        str(x) for x in candidate.get("contraindications", [])
        if str(x).strip()
    ]
    for x in inferred_fields.get("contraindications", []):
        x = str(x).strip()
        if x and x not in contraindications:
            contraindications.append(x)

    ingredient_strength = candidate.get("ingredient_strength", {})
    if not isinstance(ingredient_strength, dict):
        ingredient_strength = {}

    if ingredient_tag and ingredient_tag not in ingredient_strength:
        ingredient_strength[ingredient_tag] = "medium"

    for key, value in inferred_fields.get("ingredient_strength", {}).items():
        key = normalize_ingredient_tag(key)
        if key and key not in ingredient_strength:
            ingredient_strength[key] = value

    availability_japan = candidate.get("availability_japan", [])
    if not isinstance(availability_japan, list):
        availability_japan = []

    for x in inferred_fields.get("availability_japan", []):
        x = str(x).strip()
        if x and x not in availability_japan:
            availability_japan.append(x)

    if not availability_japan:
        availability_japan = ["rakuten"]

    uv_level = candidate.get("uv_level", {})
    if not isinstance(uv_level, dict):
        uv_level = {}

    return {
        "name": candidate.get("name", ""),
        "brand": candidate.get("brand", ""),
        "category": normalize_candidate_category(
            candidate.get("category", category),
            fallback=category
        ),
        "active_ingredients": list(dict.fromkeys(active_ingredients)),
        "support_ingredients": list(dict.fromkeys(support_ingredients)),
        "signature_ingredients": list(dict.fromkeys(signature_ingredients)),
        "concerns": list(dict.fromkeys(concerns)),
        "skin_types": list(dict.fromkeys(skin_types)),
        "sensitive_ok": sensitive_ok,
        "retinol_level": max(
            safe_retinol_level(candidate.get("retinol_level", 0)),
            safe_retinol_level(inferred_fields.get("retinol_level", 0))
        ),
        "price_ref": safe_price(
            candidate.get("normalized_price")
            or candidate.get("price_ref")
            or candidate.get("itemPrice")
            or candidate.get("price")
            or 0
        ),
        "raw_price": safe_price(
            candidate.get("raw_price")
            or candidate.get("itemPrice")
            or candidate.get("price_ref")
            or candidate.get("price")
            or 0
        ),
        "bundle_quantity": safe_bundle_quantity(candidate.get("bundle_quantity")),
        "main_functions": list(dict.fromkeys(main_functions)),
        "ingredient_focus": list(dict.fromkeys(ingredient_focus_list)),
        "ingredient_strength": ingredient_strength,
        "formulation": list(dict.fromkeys(formulation)),
        "technology": list(dict.fromkeys(technology)),
        "texture": texture,
        "contraindications": list(dict.fromkeys(contraindications)),
        "availability_japan": list(dict.fromkeys(availability_japan)),
        "uv_level": uv_level,
        "image": "",
        "_is_virtual": True
    }

def select_best_db_product(step, products, user_data, budget_value, used_brands=None):

    if used_brands is None:
        used_brands = set()

    best = None
    best_score = -9999

    for product in products:

        score = calculate_final_score(product, step, user_data, budget_value)

        if score > best_score:
            best_score = score
            best = product
            best["_final_score"] = score

    return best

DISCONTINUED_KEYWORDS = [
    # 日本語
    "生産終了",
    "販売終了",
    "廃盤",
    "終売",
    "在庫限り",

    # 英語
    "discontinued",
    "no longer available",
    "out of production",
    "end of sale",

    # 問題商品対策（個別）
    "ディープレチノホワイト5",
    "ディープレチノホワイト５",
]



def is_discontinued_or_suspicious_product(product):
    if not isinstance(product, dict):
        return True

    def norm(value):
        return str(value or "").lower().replace("　", " ").strip()

    name = norm(product.get("name") or product.get("product"))
    brand = norm(product.get("brand"))
    release_status = norm(product.get("release_status"))
    status = norm(product.get("status"))

    joined_text = " ".join([
        brand,
        name,
        release_status,
        status,
        norm(product.get("description")),
        norm(product.get("reason")),
    ])

    if release_status in ["old", "discontinued", "ended"]:
        return True

    if status in ["discontinued", "out_of_stock", "ended", "販売終了", "生産終了", "廃盤"]:
        return True

    hard_block_words = [
        "廃盤",
        "廃番",
        "生産終了",
        "販売終了",
        "製造終了",
        "リニューアル前",
        "旧パッケージ",
        "旧処方",
        "旧品",
    ]

    for word in hard_block_words:
        if word in joined_text:
            return True

    return False


def score_routine_balance(step, product, routine_context=None):
    profile = infer_active_profile(product)

    score = 0

    families = set(profile.get("families", []))
    strength = profile.get("strength", "low")
    irritation_risk = profile.get("irritation_risk", "low")

    purpose = normalize_text(step.get("purpose", ""))

    if "ニキビ跡" in purpose or "色素沈着" in purpose:
        if "vitamin_c" in families:
            score += 10
        if "retinoid" in families and strength != "high":
            score += 10
        if "azelaic" in families:
            score += 8
        if "niacinamide" in families:
            score += 6

    if "毛穴" in purpose or "ハリ" in purpose:
        if "retinoid" in families:
            score += 12
        if "peptide" in families:
            score += 10
        if "niacinamide" in families:
            score += 6

    if "barrier" in families:
        score += 6

    if irritation_risk == "high":
        score -= 10
    elif irritation_risk == "medium":
        score -= 4

    if routine_context and families:
        existing_families = set(routine_context.get("families", []))
        # scope:"any" ルール用: 前セクションまでに選ばれた全成分を含む横断コンテキスト
        global_families = set(routine_context.get("global_families", []))
        avoid_rules = routine_context.get("avoid_rules", [])

        for rule in avoid_rules:
            rule_fams = rule.get("families", [])
            if len(rule_fams) < 2:
                continue
            severity = rule.get("severity", "soft")
            scope = rule.get("scope", "same_session")
            # scope:"any" → 前セクション含む全成分で衝突判定、それ以外は同セッション内のみ
            check_against = (global_families | existing_families) if scope == "any" else existing_families
            conflict = False
            for i, fa in enumerate(rule_fams):
                for j, fb in enumerate(rule_fams):
                    if i == j:
                        continue
                    if fa in families and fb in check_against:
                        conflict = True
                        break
                if conflict:
                    break
            if conflict:
                if severity == "hard":
                    return -9999  # hard block: remove from selection entirely
                else:
                    score -= 40  # ① soft 衝突ペナルティ（選定を強く抑制するが除外はしない）

    # -------------------------------------------------------
    # Non-focus active ingredient overlap penalty
    # stepのingredient_focusに指定されていない成分が
    # 他のステップのfocusと重複している場合はスコアを下げる
    # -------------------------------------------------------
    if routine_context:
        assigned_focus_tags = set(routine_context.get("assigned_focus_tags", []))
        if assigned_focus_tags:
            step_focus_tag = normalize_ingredient_tag(step.get("ingredient_focus", "") or "")
            product_active_tags = {
                t for t in (
                    normalize_ingredient_tag(x)
                    for x in (product.get("active_ingredients") or [])
                    if x
                )
                if t
            }
            # focus成分そのものの重複は意図的なので除外
            non_focus_actives = product_active_tags - ({step_focus_tag} if step_focus_tag else set())
            # 他ステップがすでにカバーしている成分との重複
            overlap = non_focus_actives & assigned_focus_tags
            if overlap:
                score -= len(overlap) * 15

    # -------------------------------------------------------
    # Gemini 由来の相乗効果ボーナス
    # -------------------------------------------------------
    if routine_context and families:
        synergy_rules = routine_context.get("synergy_rules", [])
        existing_families = set(routine_context.get("families", []))
        _bonus_map = {"high": 20, "medium": 12, "low": 6}
        for rule in synergy_rules:
            rule_fams = rule.get("families", [])
            if len(rule_fams) < 2:
                continue
            bonus_val = _bonus_map.get(str(rule.get("bonus", "")).lower(), 0)
            if not bonus_val:
                continue
            synergy_found = False
            for fa in rule_fams:
                for fb in rule_fams:
                    if fa == fb:
                        continue
                    if fa in families and fb in existing_families:
                        synergy_found = True
                        break
                if synergy_found:
                    break
            if synergy_found:
                score += bonus_val

    return score

def select_best_market_candidate(step, db_products, user_data, budget_value, improvement_plan=None, exclude_names=None, routine_context=None, verified_products=None):
    if exclude_names is None:
        exclude_names = set()

    category = step.get("category", "")
    candidates = normalize_ai_candidates(step)

    all_candidates = []

    if verified_products is None:
        verified_products = load_verified_products_cache()

    if not isinstance(db_products, list):
        db_products = []

    if not isinstance(verified_products, list):
        verified_products = []

    combined_products = []
    seen_product_keys = set()

    for source_product in db_products:
        if not isinstance(source_product, dict):
            continue

        product_key = make_verified_product_key(source_product)

        if not product_key:
            continue

        if product_key in seen_product_keys:
            continue

        seen_product_keys.add(product_key)
        combined_products.append(source_product)

    for source_product in verified_products:
        if not isinstance(source_product, dict):
            continue

        product_key = make_verified_product_key(source_product)

        if not product_key:
            continue

        if product_key in seen_product_keys:
            continue

        seen_product_keys.add(product_key)
        combined_products.append(source_product)

    # Phase 2: criteria-based Rakuten search（常時実行・最大3クエリ）
    n_db_before = len(combined_products)
    criteria_products = search_rakuten_for_step(step, improvement_plan)
    for cp in criteria_products:
        cp_key = normalize_product_name(cp.get("rakuten_title", "") or cp.get("name", ""))
        if cp_key and cp_key not in seen_product_keys:
            seen_product_keys.add(cp_key)
            combined_products.append(cp)
    n_rakuten_criteria = len(combined_products) - n_db_before
    _step_name = str(step.get("step_name") or step.get("name") or "")
    print(
        f"[SELECT_CANDIDATE] step={_step_name!r} category={category} / "
        f"db+verified={n_db_before} / rakuten_criteria={n_rakuten_criteria} / "
        f"combined_total={len(combined_products)} / ai_candidates={len(candidates)}",
        flush=True
    )
    _scored_db = 0
    _scored_rakuten = 0

    for p in combined_products:
        if not isinstance(p, dict):
            continue

        product_category = normalize_candidate_category(
            p.get("category", ""),
            fallback=p.get("category", "")
        )

        step_category = normalize_candidate_category(
            category,
            fallback=category
        )

        if product_category != step_category:
            continue

        if p.get("name") in exclude_names:
            continue

        product = dict(p)

        # Phase 2: Gemini評価をキャッシュから適用（方針A・enrich の前に実行）
        # Geminiが推定した active_ingredients/concerns/skin_types/sensitive_ok/
        # main_functions/formulation/ingredient_strength をここで上書きすることで
        # 後続の方針A・enrich がより正確なベースデータから動く
        if product.get("_source_hint") == "rakuten_criteria":
            apply_gemini_product_eval(product)

        # -------------------------------------------------------
        # 方針A: Rakuten criteria 商品にステップ由来メタデータを付与
        # enrich_product_metadata_from_ingredients の前に実行することで
        # ingredient_tag → concerns/main_functions/ingredient_focus の
        # 連鎖補完が正しく機能する
        # -------------------------------------------------------
        if product.get("_source_hint") == "rakuten_criteria":
            _itag = normalize_ingredient_tag(step.get("ingredient_focus", "") or "")
            _actives = list(product.get("active_ingredients") or [])
            # ① step の ingredient_tag を active_ingredients に追加
            #    enrich_product_metadata_from_ingredients が後続で concerns/main_functions も
            #    _RAKUTEN_INGREDIENT_METADATA から自然に補完するため、concerns の強制付与は不要
            if _itag and _itag not in _actives:
                product["active_ingredients"] = _actives + [_itag]
            # ② availability_japan: 楽天で見つかった商品には最低限 rakuten を付与
            if not (product.get("availability_japan") or []):
                product["availability_japan"] = ["rakuten"]
            # ③（削除）concerns の強制付与は overcorrection のため廃止
            #    _itag → enrich_product_metadata_from_ingredients → concerns の自然な連鎖で十分
            # ④ contraindications: タイトルから刺激性・使用注意事項を推定
            #    DB商品は products.json の実データを使うので、楽天商品にのみ適用
            if not (product.get("contraindications") or []):
                _contra_title = str(
                    product.get("rakuten_title") or product.get("name") or ""
                )
                _inferred_contra = infer_contraindications_from_title(_contra_title)
                if _inferred_contra:
                    product["contraindications"] = _inferred_contra

        # DB・楽天問わず全商品に同じ基準でメタデータ補完を適用する
        enrich_product_metadata_from_ingredients(product)

        # enrich 後も concerns が空の rakuten_criteria 商品には step の目的から補完する
        # ai_virtual は build_virtual_product_from_ai_candidate で purpose_to_concern_tags を
        # フォールバックとして使うため、公平性のため同じ処理を適用する
        if (
            product.get("_source_hint") == "rakuten_criteria"
            and not (product.get("concerns") or [])
        ):
            product["concerns"] = purpose_to_concern_tags(step.get("purpose", ""))

        # 楽天criteriaのメタデータ確認ログ
        _src_hint = product.get("_source_hint", "")
        if _src_hint == "rakuten_criteria":
            _itag = normalize_ingredient_tag(step.get("ingredient_focus", "") or "")
            _actives = product.get("active_ingredients", []) or []
            _hit = _itag and _itag in _actives
            _contra = product.get("contraindications", []) or []
            print(
                f"[RAKUTEN_META] name={str(product.get('name',''))[:30]} "
                f"focus_hit={_hit} "
                f"concerns={len(product.get('concerns',[]) or [])} "
                f"contra={_contra} "
                f"sens={product.get('sensitive_ok','?')} "
                f"avail={product.get('availability_japan',[])}",
                flush=True
            )

        base_score = score_product(product, step, user_data, budget_value)

        if base_score <= -9000:
            if product.get("_source_hint") == "verified_cache":
                print(
                    "[VERIFIED CACHE REJECTED BY SCORE_PRODUCT]",
                    step.get("_section", ""),
                    step.get("category", ""),
                    {
                        "brand": product.get("brand", ""),
                        "name": product.get("name", ""),
                        "category": product.get("category", ""),
                        "base_score": base_score,
                    },
                    flush=True
                )
            continue

        improve_score = score_improvement(product, improvement_plan or {})
        improvement_reason = build_improvement_reason(product, improvement_plan or {})

        base_weight, improve_weight = get_dynamic_score_weights(step, user_data)

        routine_score = score_routine_balance(
            step,
            product,
            routine_context
        )

        routine_weight = get_routine_score_weight(step)

        final_score = (
            base_score * base_weight
        ) + (
            improve_score * improve_weight
        ) + (
            routine_score * routine_weight
        )

        product["_score"] = round(final_score, 1)
        product["_base_score"] = round(base_score, 1)
        product["_improve_score"] = round(improve_score, 1)
        product["_routine_score"] = round(routine_score, 1)
        product["_improvement_reason"] = improvement_reason
        product["_source"] = product.get("_source_hint", "db")

        if product.get("_source_hint") == "rakuten_criteria":
            _scored_rakuten += 1
        else:
            _scored_db += 1

        all_candidates.append(product)

    # =========================
    # AI候補を採点対象に入れる
    # 楽天APIはここでは呼ばない
    # =========================
    for candidate in candidates:
        if isinstance(candidate, str):
            try:
                parsed_candidate = json.loads(candidate)
                if isinstance(parsed_candidate, dict):
                    candidate = parsed_candidate
            except Exception as e:
                print("[AI CANDIDATE STRING PARSE ERROR]", candidate, e, flush=True)
                continue

        if not isinstance(candidate, dict):
            continue

        fields = extract_ai_candidate_fields(candidate)

        candidate_name = clean_display_product_name(
            str(fields.get("name", "") or "").strip()
        )
        brand = str(fields.get("brand", "") or "").strip()

        if not candidate_name:
            continue

        candidate_key = normalize_product_name(candidate_name)
        exclude_keys = {
            normalize_product_name(name)
            for name in exclude_names
            if name
        }

        if candidate_key in exclude_keys:
            continue

        candidate_for_check = dict(candidate)
        candidate_for_check["name"] = candidate_name
        candidate_for_check["brand"] = brand

        if is_discontinued_or_suspicious_product(candidate_for_check):
            continue

        lookup_names = [candidate_name]

        if brand and candidate_name and not candidate_name.startswith(brand):
            lookup_names.append(f"{brand} {candidate_name}")

        db_match = None

        for lookup_name in lookup_names:
            db_match = find_db_product_by_name(
                db_products,
                lookup_name,
                category
            )

            if db_match:
                break

        if db_match:
            product = dict(db_match)

            if is_discontinued_or_suspicious_product(product):
                continue

            if brand and not product.get("brand"):
                product["brand"] = brand

            enrich_product_metadata_from_ingredients(product)
            base_score = score_product(
                product,
                step,
                user_data,
                budget_value
            )

            if base_score <= -9000:
                continue

            improve_score = score_improvement(
                product,
                improvement_plan or {}
            )

            base_weight, improve_weight = get_dynamic_score_weights(
                step,
                user_data
            )

            routine_score = score_routine_balance(
                step,
                product,
                routine_context
            )

            routine_weight = get_routine_score_weight(step)

            final_score = (
                base_score * base_weight
            ) + (
                improve_score * improve_weight
            ) + (
                routine_score * routine_weight
            )

            product["_score"] = round(final_score, 1)
            product["_base_score"] = round(base_score, 1)
            product["_improve_score"] = round(improve_score, 1)
            product["_routine_score"] = round(routine_score, 1)
            product["_improvement_reason"] = build_improvement_reason(
                product,
                improvement_plan or {}
            )
            product["_source"] = "ai+db"

            all_candidates.append(product)
            continue

        virtual = build_virtual_product_from_ai_candidate(
            step,
            candidate_for_check
        )

        # 商品選定中は楽天APIを呼ばない。
        # 楽天リンク・画像取得は attach_affiliate_links_to_step 側で、選定後の商品だけに行う。
        virtual["brand"] = brand
        virtual["name"] = candidate_name
        virtual["category"] = category
        virtual["image"] = ""
        virtual["rakuten_link"] = ""
        virtual["_source"] = "ai_virtual"

        if is_discontinued_or_suspicious_product(virtual):
            continue

        if is_wrong_cleanser_candidate(virtual, step):
            continue

        if is_non_cosmetic(virtual):
            continue

        enrich_product_metadata_from_ingredients(virtual)
        base_score = score_product(
            virtual,
            step,
            user_data,
            budget_value
        )

        if base_score <= -9000:
            continue

        improve_score = score_improvement(
            virtual,
            improvement_plan or {}
        )

        base_weight, improve_weight = get_dynamic_score_weights(
            step,
            user_data
        )

        routine_score = score_routine_balance(
            step,
            virtual,
            routine_context
        )

        routine_weight = get_routine_score_weight(step)

        final_score = (
            base_score * base_weight
        ) + (
            improve_score * improve_weight
        ) + (
            routine_score * routine_weight
        )

        virtual["_score"] = round(final_score, 1)
        virtual["_base_score"] = round(base_score, 1)
        virtual["_improve_score"] = round(improve_score, 1)
        virtual["_routine_score"] = round(routine_score, 1)
        virtual["_improvement_reason"] = build_improvement_reason(
            virtual,
            improvement_plan or {}
        )

        all_candidates.append(virtual)

    if not all_candidates:
        return None

    all_candidates = [
        c for c in all_candidates
        if isinstance(c, dict)
    ]

    if not all_candidates:
        return None

    sorted_candidates = sorted(
        all_candidates,
        key=lambda x: (
            round(x.get("_score", -9999), 2),
            round(x.get("_base_score", -9999), 2),
            round(x.get("_improve_score", -9999), 2),
            -safe_price(x.get("price_ref", 0)),
            str(x.get("name", "")).strip().lower()
        ),
        reverse=True
    )

    deduped_candidates = []
    seen_names = set()

    for c in sorted_candidates:
        brand_key = normalize_candidate_name_for_merge(c.get("brand", ""))
        name_key = normalize_candidate_name_for_merge(c.get("name", ""))

        if not name_key:
            continue

        name_without_brand_key = name_key

        if brand_key and name_without_brand_key.startswith(brand_key):
            name_without_brand_key = name_without_brand_key[len(brand_key):].strip()

        identity_keys = {
            name_key,
            name_without_brand_key
        }

        if brand_key:
            # normalize_product_identity() と同じ正規化を使う（ブランド+商品名の
            # 連結時にスペースが残って重複排除に失敗するバグの修正を共有するため）
            identity_keys.add(
                normalize_product_identity(c.get("brand", ""), c.get("name", ""))
            )

        identity_keys = {k for k in identity_keys if k}

        if seen_names.intersection(identity_keys):
            continue

        seen_names.update(identity_keys)
        deduped_candidates.append(c)

    sorted_candidates = deduped_candidates


    top_candidates = sorted_candidates[:3]

    if not top_candidates:
        return None

    best = dict(top_candidates[0])

    best["_top_candidates"] = [
        {
            "brand": c.get("brand", ""),
            "name": clean_display_product_name(c.get("name", "")),
            "score": c.get("_score", 0),
            "base_score": c.get("_base_score", 0),
            "improve_score": c.get("_improve_score", 0),
            "routine_score": c.get("_routine_score", 0),
            "source": c.get("_source", ""),
            "price_ref": c.get("price_ref", 0),
            # 比較説明文生成用の成分情報
            "active_ingredients": c.get("active_ingredients", []),
            "main_functions": c.get("main_functions", []),
            "concerns": c.get("concerns", []),
            "sensitive_ok": c.get("sensitive_ok", "unknown"),
            "texture": c.get("texture", ""),
        }
        for c in top_candidates
    ]

    best["base_score"] = best.get("_base_score", 0)
    best["improve_score"] = best.get("_improve_score", 0)
    best["routine_score"] = best.get("_routine_score", 0)
    best["final_score"] = best.get("_score", 0)

    best["score_detail"] = {
        "base": best.get("_base_score", 0),
        "improve": best.get("_improve_score", 0),
        "routine": best.get("_routine_score", 0),
        "final": best.get("_score", 0)
    }

    _scored_ai = sum(1 for c in all_candidates if c.get("_source") in ("ai+db", "ai_virtual"))
    print(
        f"[SCORED] step={_step_name!r} category={category} / "
        f"db={_scored_db} / rakuten={_scored_rakuten} / ai={_scored_ai} / "
        f"total={len(all_candidates)} / winner={best.get('name','')!r} src={best.get('_source','')}",
        flush=True
    )

    log_candidate_battle(
        step,
        sorted_candidates,
        best,
        user_data=user_data,
        budget_value=budget_value,
    )

    return best
   

def apply_moisture_plan(data):
    import json

    if isinstance(data, str):
        data = json.loads(data)

    if not isinstance(data, dict):
        return {}

    moisture_plan = data.get("moisture_plan", {})
    if not isinstance(moisture_plan, dict):
        moisture_plan = {}

    need_emulsion = bool(moisture_plan.get("need_emulsion", False))
    need_cream = bool(moisture_plan.get("need_cream", False))
    need_double_moisture = bool(moisture_plan.get("need_double_moisture", False))

    for section in ["morning", "night"]:
        if section not in data or not isinstance(data.get(section), dict):
            data[section] = {"steps": []}

        steps = data.get(section, {}).get("steps", [])
        if not isinstance(steps, list):
            steps = []

        filtered_steps = []

        for step in steps:
            if not isinstance(step, dict):
                continue

            category = step.get("category", "")

            if section == "morning":
                if need_double_moisture:
                    filtered_steps.append(step)
                    continue

                if category == "乳液" and need_cream:
                    continue

                if category == "クリーム" and need_emulsion:
                    continue

            if section == "night":
                if category == "乳液" and not need_emulsion and not need_double_moisture:
                    continue

            filtered_steps.append(step)

        categories = [
            s.get("category")
            for s in filtered_steps
            if isinstance(s, dict)
        ]

        if section == "morning":
            if need_double_moisture:
                if "乳液" not in categories:
                    filtered_steps.append({
                        "category": "乳液",
                        "role": "main",
                        "purpose": "朝の保湿補助",
                        "ingredient_focus": "セラミド",
                        "risk_note": "",
                        "priority": 5,
                        "product_candidates": []
                    })

                categories = [
                    s.get("category")
                    for s in filtered_steps
                    if isinstance(s, dict)
                ]

                if "クリーム" not in categories:
                    filtered_steps.append({
                        "category": "クリーム",
                        "role": "main",
                        "purpose": "朝のバリア保護",
                        "ingredient_focus": "セラミド",
                        "risk_note": "",
                        "priority": 6,
                        "product_candidates": []
                    })

            else:
                if "乳液" not in categories and "クリーム" not in categories:
                    if need_cream:
                        filtered_steps.append({
                            "category": "クリーム",
                            "role": "main",
                            "purpose": "朝のバリア保護",
                            "ingredient_focus": "セラミド",
                            "risk_note": "",
                            "priority": 6,
                            "product_candidates": []
                        })
                    else:
                        filtered_steps.append({
                            "category": "乳液",
                            "role": "main",
                            "purpose": "朝の保湿補助",
                            "ingredient_focus": "セラミド",
                            "risk_note": "",
                            "priority": 5,
                            "product_candidates": []
                        })

        if section == "night":
            categories = [
                s.get("category")
                for s in filtered_steps
                if isinstance(s, dict)
            ]

            if "クリーム" not in categories:
                filtered_steps.append({
                    "category": "クリーム",
                    "role": "main",
                    "purpose": "夜のバリア保護",
                    "ingredient_focus": "セラミド",
                    "risk_note": "",
                    "priority": 6,
                    "product_candidates": []
                })

            if need_double_moisture and "乳液" not in categories:
                filtered_steps.append({
                    "category": "乳液",
                    "role": "main",
                    "purpose": "保湿強化",
                    "ingredient_focus": "セラミド",
                    "risk_note": "",
                    "priority": 5,
                    "product_candidates": []
                })

        data[section]["steps"] = filtered_steps

    return data

def ensure_required_routine_steps(data):
    if not isinstance(data, dict):
        return {}

    for section in ["morning", "night"]:
        if section not in data or not isinstance(data.get(section), dict):
            data[section] = {"steps": []}
        if not isinstance(data[section].get("steps"), list):
            data[section]["steps"] = []

    morning_steps = data["morning"]["steps"]
    night_steps = data["night"]["steps"]

    def has_category(steps, category):
        normalized_category = normalize_candidate_category(
            category,
            fallback=category
        )

        return any(
            isinstance(s, dict)
            and normalize_candidate_category(
                s.get("category", ""),
                fallback=s.get("category", "")
            ) == normalized_category
            for s in steps
        )

    if not has_category(morning_steps, "洗顔"):
        morning_steps.insert(0, {
            "category": "洗顔",
            "role": "main",
            "purpose": "朝の皮脂・汗を落として、次のスキンケアがなじみやすい状態に整える",
            "ingredient_focus": "低刺激",
            "risk_note": "",
            "priority": 1,
            "product_candidates": []
        })

    if not has_category(morning_steps, "化粧水"):
        morning_steps.append({
            "category": "化粧水",
            "role": "main",
            "purpose": "洗顔後の保湿補給とスキンケアの土台を整える",
            "ingredient_focus": "保湿",
            "risk_note": "",
            "priority": 3,
            "product_candidates": []
        })

    if not has_category(morning_steps, "クリーム") and not has_category(morning_steps, "乳液"):
        morning_steps.append({
            "category": "クリーム",
            "role": "main",
            "purpose": "朝の保湿とバリア保護。日中の乾燥や刺激から肌を守る",
            "ingredient_focus": "セラミド",
            "risk_note": "",
            "priority": 8,
            "product_candidates": []
        })

    if not has_category(morning_steps, "日焼け止め"):
        morning_steps.append({
            "category": "日焼け止め",
            "role": "main",
            "purpose": "紫外線による赤み・色素沈着・毛穴悪化を防ぐ",
            "ingredient_focus": "UV防御",
            "risk_note": "",
            "priority": 9,
            "product_candidates": []
        })

    if not has_category(night_steps, "クレンジング"):
        night_steps.insert(0, {
            "category": "クレンジング",
            "role": "main",
            "purpose": "日焼け止め・皮脂・メイク汚れを落とす",
            "ingredient_focus": "低刺激",
            "risk_note": "",
            "priority": 1,
            "product_candidates": []
        })

    if not has_category(night_steps, "洗顔"):
        night_steps.insert(1, {
            "category": "洗顔",
            "role": "main",
            "purpose": "残った汚れを落として、毛穴・ニキビ悪化を防ぐ",
            "ingredient_focus": "低刺激",
            "risk_note": "",
            "priority": 2,
            "product_candidates": []
        })

    if not has_category(night_steps, "クリーム") and not has_category(night_steps, "乳液"):
        night_steps.append({
            "category": "クリーム",
            "role": "main",
            "purpose": "夜の保湿とバリア保護。攻めのケア後の乾燥や刺激感を抑える",
            "ingredient_focus": "セラミド",
            "risk_note": "",
            "priority": 9,
            "product_candidates": []
        })

    if "weekly_care" not in data or not isinstance(data.get("weekly_care"), list):
        data["weekly_care"] = []

    weekly_care = data["weekly_care"]

    def weekly_has_category(category):
        normalized_category = normalize_candidate_category(
            category,
            fallback=category
        )

        return any(
            isinstance(s, dict)
            and normalize_candidate_category(
                s.get("category", ""),
                fallback=s.get("category", "")
            ) == normalized_category
            for s in weekly_care
        )
    # パック・ピーリングの要否・頻度はAIが判断する。コード側で強制挿入しない。
    # risk_note のデフォルト値のみ設定する。
    for weekly_step in weekly_care:
        if not isinstance(weekly_step, dict):
            continue
        weekly_category = weekly_step.get("category", "")
        if weekly_category == "ピーリング":
            weekly_step.setdefault(
                "risk_note",
                "レチノールや高濃度ビタミンCと同じ夜は避ける"
            )
        elif weekly_category == "パック":
            weekly_step.setdefault("risk_note", "")

    return data

def get_dynamic_score_weights(step, user_data):
    section = step.get("_section", "")
    purpose = normalize_text(step.get("purpose", ""))
    oil = normalize_text(user_data.get("oil", ""))
    sens = normalize_text(user_data.get("sens", ""))

    # 基本値
    if section == "night":
        base_weight = 0.82
        improve_weight = 1.35
    elif section == "weekly_care":
        base_weight = 0.75
        improve_weight = 1.45
    else:
        base_weight = 0.90
        improve_weight = 1.05

    # 改善寄りにしたいケース
    if any(word in purpose for word in ["赤み", "ニキビ", "毛穴", "ハリ", "エイジング", "くすみ", "美白"]):
        improve_weight += 0.08

    # 敏感肌はベース適合も重視
    if sens == "high":
        base_weight += 0.05

    # 脂性肌で毛穴・皮脂系は改善少し強め
    if oil == "oily" and any(word in purpose for word in ["毛穴", "皮脂", "テカリ", "ニキビ"]):
        improve_weight += 0.05

    # 朝は攻めすぎない
    if section == "morning":
        improve_weight = min(improve_weight, 1.12)

    return round(base_weight, 2), round(improve_weight, 2)


def get_routine_score_weight(step):
    section = step.get("_section", "")
    category = step.get("category", "")

    if section == "weekly_care":
        return 1.45

    if section == "night":
        if category in ["美容液", "ピーリング"]:
            return 1.35

        if category in ["乳液", "クリーム"]:
            return 1.25

        return 1.15

    if section == "morning":
        if category in ["美容液", "日焼け止め"]:
            return 1.2

        return 1.1

    return 1.0
def select_best_product(category, step, products, user_data, budget_value, improvement_plan=None, exclude_names=None):

    """
    カテゴリ一致商品の中から最高スコアを選ぶ
    同じsection内での重複回避用に exclude_names を使う
    """
    
    if exclude_names is None:
        exclude_names = set()

    candidates = [
        p for p in products
        if p.get("category") == category and p.get("name") not in exclude_names
    ]

    # 除外した結果ゼロなら、保険で元の候補に戻す
    if not candidates:
        candidates = [p for p in products if p.get("category") == category]

    if not candidates:
        return None

    best_product = None
    best_score = -9999

    for product in candidates:
        base_score = score_product(product, step, user_data, budget_value)
        improve_score = score_improvement(product, improvement_plan or {})
        section = step.get("_section", "")

        base_weight, improve_weight = get_dynamic_score_weights(step, user_data)
        final_score = (base_score * base_weight) + (improve_score * improve_weight)

        if final_score > best_score:
            best_score = final_score
            best_product = dict(product)
            best_product["_score"] = round(final_score, 1)
            best_product["_base_score"] = round(base_score, 1)
            best_product["_improve_score"] = round(improve_score, 1)

    print("section:", step.get("_section"))
    print("selected:", best_product.get("name") if best_product else None)
    print("base_score:", best_product.get("_base_score") if best_product else None)
    print("improve_score:", best_product.get("_improve_score") if best_product else None)
    print("final_score:", best_product.get("_score") if best_product else None)

    return best_product

def get_candidate_collection_schema():
    return {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string"},
                        "role": {"type": "string"},
                        "ingredient_focus": {"type": "string"},
                        "purpose": {"type": "string"},
                        "product_candidates": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "brand": {
                                        "type": "string"
                                    },
                                    "name": {
                                        "type": "string"
                                    },
                                    "confidence": {
                                        "type": "number"
                                    }
                                },
                                "required": [
                                    "name"
                                ]
                            }
                        }
                    },
                    "required": [
                        "category",
                        "role",
                        "ingredient_focus",
                        "purpose",
                        "product_candidates"
                    ]
                }
            }
        },
        "required": ["steps"]
    }


def build_candidate_collection_prompt(user_data, analyzed_data):
    return f"""
あなたは日本で市販されているスキンケア商品を広く比較収集するリサーチ担当です。
このタスクでは最終選定はしません。比較用候補を広く集めることだけを行ってください。

【ユーザー情報】
年齢: {user_data.get("age", "")}
肌質: {user_data.get("oil", "")}
敏感度: {user_data.get("sens", "")}
レチノール経験: {user_data.get("exp", "")}
予算: {user_data.get("budget", "")}
肌の悩み（ユーザー申告）: {', '.join([_CONCERN_LABELS_JA.get(c, c) for c in (user_data.get('concerns') or [])]) or '未回答'}

【診断結果JSON】
{json.dumps(analyzed_data, ensure_ascii=False)}

【目的】
各ステップごとに、日本で市販されているスキンケア商品を幅広く集める。
まだ絞り込まない。比較候補をできるだけ広く集める。

【収集ルール】
・各ステップごとに product_candidates を 3〜5 個出すこと
・最低でも 3 個以上出すこと
・異なるブランドから幅広く出すこと
・同じブランドは最大1個まで
・同じシリーズばかりに偏らないこと
・ドラッグストア、バラエティショップ、韓国スキンケア、デパコス、通販定番商品を混ぜること
・日本で比較的入手しやすい商品を優先すること
・カテゴリ、目的、成分軸、予算に合う候補を優先すること
・ product_candidates には具体的な商品名だけを入れること
・候補が不足する場合でも、なるべくブランド分散を優先すること
・特定ブランドに偏らず、市場を広く探索すること

【出力ルール】
・入力された各ステップに対応する候補を返す
・category, role, ingredient_focus, purpose は入力に合わせる
・JSONのみで返す
・JSONは途中で切れないように最後まで必ず出力すること
・出力が長くなりすぎる場合は候補数を減らしてよい
"""
    

def normalize_candidate_name_for_merge(name):
    return normalize_product_name(name)

def clean_brand_and_product_name(brand, name):
    brand_text = str(brand or "").strip()
    name_text = clean_display_product_name(str(name or "").strip())

    if not name_text:
        return brand_text, ""

    if not brand_text:
        return "", name_text

    brand_key = normalize_candidate_name_for_merge(brand_text)

    if not brand_key:
        return brand_text, name_text

    cleaned_name = name_text

    # ブランド名がフレーズとして商品名の先頭に重複している場合はまるごと除去する。
    # 単語単位の除去を先にやると、"The Ordinary" のように構成語の一部
    # （"The" 等）が正規化で空文字になるブランド名で除去が半端になり、
    # "The Ordinary The AHA..." のような壊れた表示になるため、
    # まずフレーズ単位の先頭一致を優先して剥がす。
    while True:
        name_key = normalize_candidate_name_for_merge(cleaned_name)

        if name_key == brand_key:
            cleaned_name = ""
            break

        if name_key.startswith(brand_key) and cleaned_name.startswith(brand_text):
            cleaned_name = cleaned_name[len(brand_text):].strip()
            continue

        break

    # フレーズ単位で剥がしきれなかった場合の保険として、
    # ブランド名と完全一致する単語が紛れ込んでいれば個別に除去する。
    if normalize_candidate_name_for_merge(cleaned_name) and brand_key in normalize_candidate_name_for_merge(cleaned_name):
        words = cleaned_name.split()
        cleaned_words = [
            word for word in words
            if normalize_candidate_name_for_merge(word) != brand_key
        ]
        candidate_cleaned = " ".join(cleaned_words).strip()
        if candidate_cleaned:
            cleaned_name = candidate_cleaned

    return brand_text, cleaned_name.strip()

def normalize_product_identity(brand="", name=""):
    def to_text(value):
        if isinstance(value, list):
            value = " ".join([
                str(v) for v in value
                if v is not None
            ])
        elif isinstance(value, dict):
            value = " ".join([
                str(v) for v in value.values()
                if v is not None
            ])
        else:
            value = str(value or "")

        return value.strip()

    brand_text, name_text = clean_brand_and_product_name(
        to_text(brand),
        to_text(name)
    )

    brand_key = normalize_candidate_name_for_merge(brand_text)
    name_key = normalize_candidate_name_for_merge(name_text)

    if not name_key:
        return ""

    name_without_brand_key = name_key

    if brand_key and name_without_brand_key.startswith(brand_key):
        name_without_brand_key = name_without_brand_key[len(brand_key):].strip()

    if brand_key:
        # brand_key/name_without_brand_key は既に空白除去済みの正規化文字列だが、
        # f-string で連結すると間に半角スペースが1つ入ってしまう。
        # ブランドが空で「ブランド名+商品名」が1つの文字列として渡ってきた
        # 候補（楽天検索由来など）はスペースなしで正規化されるため、
        # 同一商品なのにキーが一致せず重複排除できないバグになっていた。
        # 連結後にもう一度正規化してスペースを除去し、両者を一致させる。
        return normalize_candidate_name_for_merge(
            f"{brand_key} {name_without_brand_key}".strip()
        )

    return name_without_brand_key

def remove_repeated_brand_from_name(brand, name):
    _, cleaned_name = clean_brand_and_product_name(brand, name)
    return cleaned_name

def merge_candidate_list(original, extra, max_items=20):
    merged = []
    seen = set()

    for item in (original or []) + (extra or []):
        if isinstance(item, dict):
            name = item.get("name", "")
        else:
            name = item

        norm = normalize_candidate_name_for_merge(name)
        if not norm:
            continue
        if norm in seen:
            continue

        seen.add(norm)
        merged.append(name)

        if len(merged) >= max_items:
            break

    return merged

def extract_ai_candidate_fields(candidate):
    if isinstance(candidate, dict):
        brand = str(candidate.get("brand", "") or "").strip()
        name = str(candidate.get("name", "") or "").strip()
        confidence = candidate.get("confidence", "")

        if name:
            return {
                "brand": brand,
                "name": clean_display_product_name(name),
                "confidence": confidence
            }

    if isinstance(candidate, str):
        text = candidate.strip()

        try:
            parsed = json.loads(text)

            if isinstance(parsed, dict):
                return extract_ai_candidate_fields(parsed)

        except Exception:
            pass

        cleaned_name = clean_display_product_name(text)

        return {
            "brand": "",
            "name": cleaned_name,
            "confidence": ""
        }

    return {
        "brand": "",
        "name": "",
        "confidence": ""
    }

def safe_retinol_level(value):
    if value is None:
        return 0

    if isinstance(value, int):
        return value

    text = str(value).strip().lower()

    if text in ["", "none", "null", "unknown", "なし", "不明"]:
        return 0

    try:
        return int(float(text))
    except Exception:
        return 0

def normalize_ai_candidates(step):
    if not isinstance(step, dict):
        return []

    raw_candidates = step.get("product_candidates", [])
    normalized = []
    seen = set()

    if not isinstance(raw_candidates, list):
        return []

    step_category = normalize_candidate_category(
        step.get("category", ""),
        fallback=step.get("category", "")
    )

    step_ingredient_focus = step.get("ingredient_focus", "")
    if isinstance(step_ingredient_focus, list):
        step_ingredient_focus_list = [
            normalize_ingredient_tag(x)
            for x in step_ingredient_focus
            if normalize_ingredient_tag(x)
        ]
    else:
        step_ingredient_focus_list = [
            normalize_ingredient_tag(step_ingredient_focus)
        ] if normalize_ingredient_tag(step_ingredient_focus) else []

    rejected_statuses = {
        "old",
        "discontinued",
        "ended",
        "販売終了",
        "生産終了",
        "廃盤",
    }

    for candidate in raw_candidates:
        if isinstance(candidate, str):
            text = candidate.strip()

            if text.startswith("{") and text.endswith("}"):
                try:
                    candidate = json.loads(text)
                except Exception:
                    candidate = {"name": text}
            else:
                candidate = {"name": text}

        if not isinstance(candidate, dict):
            continue

        brand = str(candidate.get("brand", "") or "").strip()
        name = clean_display_product_name(
            str(candidate.get("name", "") or "").strip()
        )

        if brand and name.startswith(brand):
            name = name[len(brand):].strip()

        if not name:
            continue

        # セット販売・まとめ買い商品を除外（単品のみ選定対象とする）
        if _is_set_product_name(f"{brand} {name}".strip()):
            print(f"[CANDIDATE SKIP set-product] {name}", flush=True)
            continue

        confidence_raw = candidate.get("confidence", None)

        if confidence_raw is not None:
            try:
                confidence = float(confidence_raw or 0)
            except Exception:
                confidence = 0

            if confidence < 70:
                continue
        else:
            confidence = None

        release_status = str(
            candidate.get("release_status", "") or ""
        ).lower().strip()

        if release_status in rejected_statuses:
            continue

        candidate_category = normalize_candidate_category(
            candidate.get("category", ""),
            fallback=step_category
        )

        # AI候補は、stepカテゴリと一致するものだけ採用する。
        # 商品名からカテゴリを推測してstepカテゴリを上書きしない。
        if candidate_category != step_category:
            continue

        active_ingredients = candidate.get("active_ingredients", [])
        if not isinstance(active_ingredients, list):
            active_ingredients = []

        active_ingredients = [
            normalize_ingredient_tag(x)
            for x in active_ingredients
        ]
        active_ingredients = [x for x in active_ingredients if x]

        support_ingredients = candidate.get("support_ingredients", [])
        if not isinstance(support_ingredients, list):
            support_ingredients = []

        support_ingredients = [
            normalize_ingredient_tag(x)
            for x in support_ingredients
        ]
        support_ingredients = [x for x in support_ingredients if x]

        ingredient_focus = candidate.get("ingredient_focus", [])
        if isinstance(ingredient_focus, str):
            ingredient_focus = [ingredient_focus]
        elif not isinstance(ingredient_focus, list):
            ingredient_focus = []

        ingredient_focus = [
            normalize_text(x)
            for x in ingredient_focus
            if normalize_text(x)
        ]


        concerns = candidate.get("concerns", [])
        if not isinstance(concerns, list):
            concerns = purpose_to_concern_tags(step.get("purpose", ""))

        normalized_concerns = []
        for c in concerns:
            c = normalize_text(c)
            if c in [
                "pores",
                "acne",
                "redness",
                "oil_control",
                "dryness",
                "barrier",
                "dullness",
                "whitening",
                "aging"
            ]:
                normalized_concerns.append(c)

        if not normalized_concerns:
            normalized_concerns = purpose_to_concern_tags(step.get("purpose", ""))

        main_functions = candidate.get("main_functions", [])
        if not isinstance(main_functions, list):
            main_functions = []

        normalized_main_functions = []
        for mf in main_functions:
            mapped = MAIN_FUNCTION_MAP.get(str(mf), str(mf))
            if mapped in MAIN_FUNCTION_TAGS:
                normalized_main_functions.append(mapped)

        ingredient_strength = candidate.get("ingredient_strength", {})
        if not isinstance(ingredient_strength, dict):
            ingredient_strength = {}


        item = {
            "brand": brand,
            "name": name,
            "category": step_category,
            "confidence": confidence,
            "price_ref": safe_price(
                candidate.get("normalized_price")
                or candidate.get("price_ref")
                or candidate.get("itemPrice")
                or candidate.get("price")
                or 0
            ),
            "raw_price": safe_price(
                candidate.get("raw_price")
                or candidate.get("itemPrice")
                or candidate.get("price_ref")
                or candidate.get("price")
                or 0
            ),
            "bundle_quantity": safe_bundle_quantity(candidate.get("bundle_quantity")),
            "active_ingredients": list(dict.fromkeys(active_ingredients)),
            "support_ingredients": list(dict.fromkeys(support_ingredients)),
            "signature_ingredients": candidate.get("signature_ingredients", []) if isinstance(candidate.get("signature_ingredients", []), list) else [],
            "concerns": list(dict.fromkeys(normalized_concerns)),
            "skin_types": candidate.get("skin_types", []) if isinstance(candidate.get("skin_types", []), list) else [],
            "sensitive_ok": candidate.get("sensitive_ok", "unknown"),
            "retinol_level": safe_retinol_level(candidate.get("retinol_level", 0)),
            "main_functions": list(dict.fromkeys(normalized_main_functions)),
            "ingredient_focus": list(dict.fromkeys(ingredient_focus)),
            "ingredient_strength": ingredient_strength,
            "formulation": candidate.get("formulation", []) if isinstance(candidate.get("formulation", []), list) else [],
            "technology": candidate.get("technology", []) if isinstance(candidate.get("technology", []), list) else [],
            "texture": str(candidate.get("texture", "") or ""),
            "contraindications": candidate.get("contraindications", []) if isinstance(candidate.get("contraindications", []), list) else [],
            "availability_japan": candidate.get("availability_japan", []) if isinstance(candidate.get("availability_japan", []), list) else [],
            "uv_level": candidate.get("uv_level", {}) if isinstance(candidate.get("uv_level", {}), dict) else {},
            "reason": str(candidate.get("reason", "") or step.get("selection_reason", "") or ""),
            "release_status": release_status or "current",
        }

        norm_name = normalize_candidate_name_for_merge(item["name"])
        if not norm_name or norm_name in seen:
            continue

        seen.add(norm_name)
        normalized.append(item)

    return sorted(
        normalized,
        key=lambda x: (
            -safe_float(x.get("confidence", 0)),
            normalize_candidate_name_for_merge(x.get("name", "")),
            str(x.get("brand", "")).lower()
        )
    )

def enrich_steps_with_market_candidates(data, candidate_data):
    extra_steps = candidate_data.get("steps", [])

    def enrich_step_list(step_list):
        for step in step_list:
            category = step.get("category", "")
            role = step.get("role", "")
            ingredient_focus = step.get("ingredient_focus", "")
            purpose = step.get("purpose", "")

            matched = None
            for extra in extra_steps:
                if (
                    extra.get("category", "") == category
                    and extra.get("role", "") == role
                    and extra.get("ingredient_focus", "") == ingredient_focus
                    and extra.get("purpose", "") == purpose
                ):
                    matched = extra
                    break

            if matched:
                step["product_candidates"] = merge_candidate_list(
                    step.get("product_candidates", []),
                    matched.get("product_candidates", []),
                    max_items=80
                )

    enrich_step_list(data.get("morning", {}).get("steps", []))
    enrich_step_list(data.get("night", {}).get("steps", []))
    enrich_step_list(data.get("weekly_care", []))

    return data

def safe_price(value):
    if isinstance(value, (int, float)):
        return value

    if value is None:
        return 0

    text = str(value).replace("円", "").replace(",", "").strip()

    digits = ""
    for ch in text:
        if ch.isdigit():
            digits += ch

    return int(digits) if digits else 0

def build_price_band(price):
    price = safe_price(price)

    if price <= 0:
        return "価格不明"
    if price <= 1500:
        return "〜1500円"
    if price <= 3000:
        return "1501〜3000円"
    if price <= 5000:
        return "3001〜5000円"
    return "5001円以上"


def normalize_step_price_fields(step):
    if not isinstance(step, dict):
        return step

    price = safe_price(step.get("price", 0))
    estimated_price = safe_price(step.get("estimated_price", 0))

    # 実価格がなければ推定価格を使う
    if price <= 0 and estimated_price > 0:
        price = estimated_price

    # 推定価格がなければ実価格を使う
    if estimated_price <= 0 and price > 0:
        estimated_price = price

    step["price"] = price
    step["estimated_price"] = estimated_price

    # 価格帯を再確定
    if price > 0:
        step["price_band"] = build_price_band(price)
    elif estimated_price > 0:
        step["price_band"] = build_price_band(estimated_price)
    else:
        step["price_band"] = "価格不明"

    return step

def pick_best_db_fallback_product(step, products, user_data, budget_value, exclude_names=None):
    if exclude_names is None:
        exclude_names = set()

    category = str(step.get("category", "") or "").strip()
    if not category:
        return None

    candidates = []

    for product in products:
        if not isinstance(product, dict):
            continue

        if str(product.get("category", "") or "").strip() != category:
            continue

        product_name = str(product.get("name", "") or "").strip()
        if not product_name:
            continue

        if product_name in exclude_names:
            continue

        try:
            base_score = score_product(product, step, user_data, budget_value)
        except Exception:
            base_score = -9999

        if base_score <= -9999:
            continue

        copied = dict(product)
        copied["_base_score"] = base_score
        copied["_improve_score"] = 0
        copied["_score"] = base_score
        copied["_source"] = "fallback"
        candidates.append(copied)

    if not candidates:
        return None

    # 価格がある商品を少し優先、同点なら安すぎず高すぎない順に寄せる
    def sort_key(p):
        price = safe_price(p.get("price_ref", 0))
        has_price = 1 if price > 0 else 0
        return (
            p.get("_score", -9999),
            has_price,
            -abs(price - budget_value) if budget_value > 0 and price > 0 else 0
        )

    candidates.sort(key=sort_key, reverse=True)
    for c in candidates:

        if not isinstance(c, dict):
            continue

        brand = str(
            c.get("brand", "")
        ).strip()

        name = str(
            c.get("name", "")
        ).strip()

        if (
            brand
            and name
            and name.startswith(brand)
        ):
            name = (
                name[len(brand):]
                .strip()
            )

        c["brand"] = brand
        c["name"] = name

    return filtered[0]

def clean_display_product_name(name):
    if not isinstance(name, str):
        return ""

    text = name.strip()

    if not text:
        return ""

    # 内容量表記の除去 (例: 100mL, 50g, 30cc, 150ml×2)
    # 先読みなし: 数字+単位の組み合わせを後続文字に関わらず除去
    text = re.sub(r'\s*\d+(\.\d+)?\s*(mL|ml|ｍｌ|ミリリットル|cc|CC|ℓ|リットル)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*\d+(\.\d+)?\s*g(?=[^a-zA-Z%]|$)', '', text)
    text = re.sub(r'\s*\d+(\.\d+)?\s*(mg|μg|ug)', '', text, flags=re.IGNORECASE)
    # セット・個数表記の除去 (例: 2本組, 3個セット, 30枚入, お得2個)
    text = re.sub(r'\s*\d+\s*(本|個|枚|袋|包|箱)(組|セット|入り?|まとめ)?', '', text)
    text = re.sub(r'\s*\d+(組|セット)', '', text)
    text = re.sub(r'\s*(セット品|まとめ買い|お得セット|お試しセット|2点セット|3点セット)', '', text)
    # 詰め替え・レフィル・トライアル等の除去（括弧あり・なし両対応）
    text = re.sub(r'[\(（【]?(詰め?替え?|詰替|つめかえ|レフィル|リフィル|付け?替え?|つけかえ|お試し|サンプル|ミニサイズ|トライアル|限定品|限定版|新パッケージ|旧品)[\)）】]?', '', text)
    # 括弧内の補足情報の除去 (例: (旧パッケージ), 【限定品】)
    text = re.sub(r'[\(（【]旧[^)）】]*[\)）】]', '', text)
    text = re.sub(r'[\(（【]旧パッケージ[\)）】]', '', text)
    # AIがカテゴリ注記として付与する末尾の括弧書きを除去
    # 例: "〇〇ドリンク (美容液)" と "〇〇ドリンク" が別候補として重複表示されるのを防ぐ
    # 注意: "ピーリング"/"酵素洗顔" は score_product() のピーリング強制判定が
    # 商品名から拾うキーワードでもあるため、ここでは除去対象に含めない
    # （含めると酵素ピーリング候補が誤って強制除外されてしまう）
    text = re.sub(
        r'[\(（](化粧水|美容液|乳液|クリーム|洗顔料?|クレンジング|日焼け止め|パック|'
        r'導入美容液|保湿クリーム|サプリメント?|美容機器|[^\)）]{1,10}代わり)[\)）]\s*$',
        '',
        text
    )
    # 価格・割引・クーポン表記の除去（楽天商品名に混入するパターン）
    # 例: "500円off" "☆クーポンで1980円" "【1000円OFF】" "税込2980円"
    text = re.sub(r'[\(（【]?[^\s（【】）]{0,10}(クーポン|coupon|COUPON)[^\s）】]{0,20}[\)）】]?', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\d[\d,，]*円\s*(off|OFF|引き?|引)|(?:送料)?無料', '', text)
    text = re.sub(r'(税込|税抜|定価|参考価格|通常価格|割引|円OFF|%OFF|%引き?)\s*[\d,，]*円?', '', text, flags=re.IGNORECASE)
    text = re.sub(r'[\d,，]+\s*円', '', text)  # 残った「数字円」をまとめて除去
    text = re.sub(r'(ポイント\d+倍|\d+倍|pt\d+|P\d+倍)', '', text)
    # ☆★等の装飾記号（単独または先頭）
    text = re.sub(r'^[\s　☆★◆◇▼▽△▲●○■□♪♦♥❤✨]+', '', text)
    text = re.sub(r'[\s　☆★◆◇●○■□]+$', '', text)

    text = text.replace("{", " ")
    text = text.replace("}", " ")
    text = text.replace('"', " ")
    text = text.replace("'", "'")
    text = text.replace(":", " ")
    text = text.replace(",", " ")

    parts = text.split()

    if "name" in [p.lower() for p in parts]:
        lowered = [p.lower() for p in parts]

        start = lowered.index("name") + 1
        end = len(parts)

        for stop_word in [
            "confidence",
            "score",
            "category",
            "reason",
            "source"
        ]:
            if stop_word in lowered[start:]:
                end = min(
                    end,
                    lowered.index(stop_word)
                )

        text = " ".join(parts[start:end])

    bad_words = [
        "brand",
        "name",
        "confidence",
        "category",
        "score",
        "product",
        "reason",
        "source"
    ]

    cleaned = []

    for part in text.split():
        if part.lower() in bad_words:
            continue

        if part.isdigit():
            continue

        cleaned.append(part)

    return " ".join(cleaned).strip()

def finalize_step_display_fields(step, best, user_data):
    if not isinstance(step, dict):
        return step

    if not isinstance(best, dict):
        return step

    brand = str(best.get("brand", "") or step.get("brand", "") or "").strip()
    raw_name = clean_display_product_name(
        best.get("name") or step.get("product") or ""
    )

    brand, product_name = clean_brand_and_product_name(brand, raw_name)

    step["brand"] = brand
    step["product"] = product_name

    base_score = best.get("_base_score", best.get("base_score", step.get("base_score", 0))) or 0
    improve_score = best.get("_improve_score", best.get("improve_score", step.get("improve_score", 0))) or 0
    routine_score = best.get("_routine_score", best.get("routine_score", step.get("routine_score", 0))) or 0
    final_score = best.get("_score", best.get("final_score", best.get("match_score", step.get("match_score", 0)))) or 0

    step["base_score"] = base_score
    step["improve_score"] = improve_score
    step["routine_score"] = routine_score
    step["final_score"] = final_score
    step["match_score"] = final_score

    step["improvement_score"] = improve_score
    step["improvement_reason"] = (
        best.get("_improvement_reason")
        or best.get("improvement_reason")
        or best.get("reason")
        or step.get("improvement_reason")
        or ""
    )

    step["score_detail"] = {
        "base": base_score,
        "improve": improve_score,
        "routine": routine_score,
        "final": final_score,
    }

    if isinstance(best.get("_top_candidates"), list) and best.get("_top_candidates"):
        step["top_candidates"] = best.get("_top_candidates", [])
    elif not isinstance(step.get("top_candidates"), list):
        step["top_candidates"] = []

    step["price_ref"] = safe_price(
        best.get("price_ref")
        or best.get("normalized_price")
        or step.get("price_ref")
        or step.get("price")
        or 0
    )

    step["raw_price"] = safe_price(
        best.get("raw_price")
        or step.get("raw_price")
        or step.get("price")
        or 0
    )

    step["bundle_quantity"] = int(
        best.get("bundle_quantity")
        or step.get("bundle_quantity")
        or 1
    )

    invalid_reason = "現在確認できる商品候補が見つかりませんでした。"

    if (
        not step.get("recommend_reason")
        or step.get("recommend_reason") == invalid_reason
    ):
        step["recommend_reason"] = (
            best.get("reason")
            or best.get("_improvement_reason")
            or step.get("selection_reason")
            or build_ai_reason(step, user_data)
        )

    return step

def prefetch_rakuten_for_all_steps(data, improvement_plan, max_workers=3):
    """
    全ステップのRakuten検索を並列実行してキャッシュに載せる。
    スコアリングループはキャッシュヒットで瞬時に返すため、
    並列プリフェッチによって総診断時間を大幅短縮できる。
    """
    global _step_rakuten_results, _rakuten_criteria_cache
    import concurrent.futures

    all_steps = []
    for section in ["morning", "night"]:
        steps = data.get(section, {}).get("steps", [])
        if isinstance(steps, list):
            all_steps.extend(s for s in steps if isinstance(s, dict))
    weekly = data.get("weekly_care", [])
    if isinstance(weekly, list):
        all_steps.extend(s for s in weekly if isinstance(s, dict))

    for key in ["supplements", "beauty_devices"]:
        items = data.get(key, [])
        if isinstance(items, list):
            all_steps.extend(s for s in items if isinstance(s, dict))

    if not all_steps:
        return

    t0 = time.time()
    _log_mem("prefetch-start")
    print(
        f"[PREFETCH START] steps={len(all_steps)} rate_gap={RAKUTEN_RATE_GAP}s workers={max_workers}",
        flush=True
    )

    def fetch(step):
        t_step = time.time()
        try:
            result = search_rakuten_for_step(step, improvement_plan)
            label = str(step.get("step_name") or step.get("category") or "")
            print(
                f"[PREFETCH STEP] {label!r} done in {time.time()-t_step:.2f}s items={len(result)}",
                flush=True
            )
            return result
        except Exception as e:
            print(f"[PREFETCH ERROR] {repr(e)}", flush=True)
            return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch, step): step for step in all_steps}
        for future in concurrent.futures.as_completed(futures):
            future.result()

    t_phase_a = time.time() - t0
    _log_mem("phase-A-done")
    print(f"[PREFETCH] Phase-A(Rakuten) {t_phase_a:.1f}s", flush=True)

    # Phase B: Gemini バッチ商品評価（直列 + ファイルキャッシュ）
    _load_gemini_eval_cache_if_needed()
    t1 = time.time()

    seen_step_keys = set()
    steps_to_eval = []
    for step in all_steps:
        sk = _get_step_rakuten_key(step)
        if sk in seen_step_keys:
            continue
        seen_step_keys.add(sk)
        products_for_step = _step_rakuten_results.get(sk, [])
        if products_for_step:
            steps_to_eval.append((step, products_for_step))

    print(
        f"[PREFETCH] Phase-B start: {len(steps_to_eval)}ステップをGemini評価 workers=1",
        flush=True
    )
    if steps_to_eval:
        # メモリ節約のため直列実行（max_workers=1）
        for s, p in steps_to_eval:
            try:
                gemini_evaluate_rakuten_batch(s, p)
            except Exception as e:
                print(f"[PREFETCH GEMINI ERROR] {repr(e)}", flush=True)

    _save_gemini_eval_cache()
    t_phase_b = time.time() - t1
    t_total = time.time() - t0
    _log_mem("phase-B-done")
    print(
        f"[PREFETCH TIMELINE] Phase-A={t_phase_a:.1f}s Phase-B={t_phase_b:.1f}s Total={t_total:.1f}s",
        flush=True
    )

    # prefetch完了後にセッションキャッシュを解放（Gemini評価はファイルキャッシュに永続化済み）
    _step_rakuten_results = {}
    _rakuten_criteria_cache = {}
    _log_mem("cache-freed")


def assign_products_to_all_steps(data, products, user_data, budget_value):

    ai_image_db = load_ai_product_images()
    improvement_plan = data.get("improvement_plan", {})
    verified_products = load_verified_products_cache()

    # 全ステップのRakuten検索を並列プリフェッチ（スコアリングループはキャッシュヒット）
    prefetch_rakuten_for_all_steps(data, improvement_plan)

    _raw_avoid = data.get("routine_strategy", {}).get("avoid_combinations", [])
    avoid_rules = [r for r in _raw_avoid if isinstance(r, dict) and r.get("families")]

    _raw_synergy = data.get("routine_strategy", {}).get("synergy_combinations", [])
    synergy_rules = [r for r in _raw_synergy if isinstance(r, dict) and r.get("families") and r.get("bonus")]

    routine_context = {
        "families": [],
        "strengths": [],
        "selected_products": [],
        "avoid_rules": avoid_rules,
        "synergy_rules": synergy_rules,
        "assigned_focus_tags": [],
    }

    def assign_one_step(step, used_product_names, section_name,routine_context):
        if not isinstance(step, dict):
            return step

        step["_section"] = section_name
        category = str(step.get("category", "") or "").strip()

        best = select_best_market_candidate(
            step=step,
            db_products=products,
            user_data=user_data,
            budget_value=budget_value,
            improvement_plan=improvement_plan,
            exclude_names=used_product_names,
            routine_context=routine_context,
            verified_products=verified_products,
        )

        if not best:
            print(
                "[BEST CHECK]",
                section_name,
                category,
                best.get("name") if best else "NONE",
                flush=True
            )

            step["top_candidates"] = []
            step["product"] = ""
            step["price"] = 0
            step["estimated_price"] = 0
            step["image"] = get_product_image(step.get("category", ""))
            step["rakuten_link"] = ""
            step["amazon_link"] = ""
            step["product_source"] = "none"
            step["recommend_reason"] = "現在確認できる商品候補が見つかりませんでした。"
            step["product"] = clean_display_product_name(step.get("product", ""))

            return normalize_step_price_fields(step)

        best["name"] = clean_display_product_name(best.get("name", ""))

        product_name = best.get("name", "")
        if product_name:
            used_product_names.add(product_name)

        profile = infer_active_profile(best)

        # routine_context更新前に相乗効果・悩みタグを計算
        _product_families = profile.get("families", set())
        _existing_families = set(routine_context.get("families", []))
        step["synergy_note"] = build_synergy_note(
            _product_families, _existing_families, synergy_rules
        )
        step["concern_tags"] = build_concern_tags(best, step)

        routine_context["families"].extend(profile.get("families", []))
        routine_context["strengths"].append(profile.get("strength", "low"))
        routine_context["selected_products"].append(product_name)

        # このステップのfocus成分タグを記録 — 後続ステップのnon-focus重複検出に使う
        _step_focus_tag = normalize_ingredient_tag(step.get("ingredient_focus", "") or "")
        if _step_focus_tag:
            routine_context["assigned_focus_tags"].append(_step_focus_tag)

        step["top_candidates"] = best.get("_top_candidates", [])
        source = best.get("_source", "")

        if source in ["db", "ai+db", "fallback_db"]:
            apply_db_product_to_step(step, best, user_data)
            step["product_source"] = source or "db"

        elif source in ["ai", "ai_virtual", "ai_rakuten_verified", "rakuten_criteria"]:
            step["product"] = best.get("name", category)
            step["price"] = safe_price(
                best.get("price_ref")
                or best.get("normalized_price")
                or best.get("raw_price")
                or 0
            )
            step["estimated_price"] = step["price"]

            if source in ["ai_rakuten_verified", "rakuten_criteria"]:
                step["image"] = best.get("image", "") or ""
                step["rakuten_link"] = best.get("rakuten_link", "") or ""
                # 整形前の元タイトルを保持（後でGeminiで商品名を整形するために使う）
                step["rakuten_title"] = best.get("rakuten_title", "") or ""
            else:
                image_path = None
                if ai_image_db:
                    lookup_names = []

                    raw_name = clean_display_product_name(best.get("name", ""))
                    brand = str(best.get("brand", "") or "").strip()

                    if raw_name:
                        lookup_names.append(raw_name)

                    if brand and raw_name and not raw_name.startswith(brand):
                        lookup_names.append(f"{brand} {raw_name}")

                    found_price = 0

                    for lookup_name in lookup_names:
                        image_path, found_price = find_ai_candidate_data(
                            lookup_name,
                            ai_image_db
                        )

                        if image_path:
                            break

                    if found_price and step["price"] <= 0:
                        step["price"] = safe_price(found_price)
                        step["estimated_price"] = safe_price(found_price)

                step["image"] = image_path or ""

            step["match_score"] = best.get("_score", 0) or 0
            step["base_score"] = best.get("_base_score", 0) or 0
            step["improve_score"] = best.get("_improve_score", 0) or 0
            step["routine_score"] = best.get("_routine_score", 0) or 0
            step["recommend_reason"] = (
                best.get("reason")
                or step.get("selection_reason")
                or build_ai_reason(step, user_data)
            )
            step["improvement_score"] = best.get("_improve_score", 0)
            step["improvement_reason"] = best.get("_improvement_reason", "")
            step["product_source"] = source or "ai"

            impact = calculate_step_impact(step, best)
            step["impact_scores"] = impact
            step["top_impacts"] = format_top_impacts(impact)

        else:
            apply_db_product_to_step(step, best, user_data)
            step["product_source"] = source or "db"

        invalid_reason = "現在確認できる商品候補が見つかりませんでした。"

        if (
            not step.get("recommend_reason")
            or step.get("recommend_reason") == invalid_reason
        ):
            step["recommend_reason"] = (
                best.get("reason")
                or step.get("selection_reason")
                or build_ai_reason(step, user_data)
            )

        step = finalize_step_display_fields(step, best, user_data)
        step = normalize_step_price_fields(step)

        step["product"] = clean_display_product_name(step.get("product", ""))

        # 画像が取れなかった場合はカテゴリデフォルト画像にフォールバック
        if not step.get("image"):
            step["image"] = get_product_image(step.get("category", ""))

        return step

    # ② scope:"any" 衝突を検出するためにセクション間で成分familiesを引き継ぐ
    global_families: list = []

    for section in ["morning", "night"]:
        used_product_names = set()
        routine_context = {
            "families": [],
            "strengths": [],
            "selected_products": [],
            "avoid_rules": avoid_rules,
            "synergy_rules": synergy_rules,
            "assigned_focus_tags": [],
            "global_families": list(global_families),  # 前セクションまでの確定済み成分
        }

        steps = data.get(section, {}).get("steps", [])

        if not isinstance(steps, list):
            continue

        for idx, step in enumerate(steps):
            steps[idx] = assign_one_step(
                step,
                used_product_names,
                section,
                routine_context
            )

        # このセクションで選ばれた成分を次セクションに引き継ぐ
        global_families.extend(routine_context["families"])

    weekly_used_product_names = set()
    weekly_routine_context = {
        "families": [],
        "strengths": [],
        "selected_products": [],
        "avoid_rules": avoid_rules,
        "synergy_rules": synergy_rules,
        "assigned_focus_tags": [],
        "global_families": list(global_families),  # morning + night 全成分
    }

    weekly_steps = data.get("weekly_care", [])

    if isinstance(weekly_steps, list):
        for idx, step in enumerate(weekly_steps):
            weekly_steps[idx] = assign_one_step(
                step,
                weekly_used_product_names,
                "weekly_care",
                weekly_routine_context
            )

    return data

_SYNERGY_FAMILY_LABELS = {
    "retinoid": "レチノイド",
    "aha_bha": "AHA/BHA",
    "vitamin_c": "ビタミンC",
    "strong_vitamin_c": "高濃度ビタミンC",
    "niacinamide": "ナイアシンアミド",
    "ceramide": "セラミド",
    "barrier": "バリア成分",
    "peptide": "ペプチド",
    "pdrn": "PDRN",
    "azelaic": "アゼライン酸",
    "uv_protection": "紫外線防御",
}

def build_synergy_note(product_families, existing_families, synergy_rules):
    product_fam_set = set(product_families)
    existing_fam_set = set(existing_families)
    for rule in synergy_rules:
        rule_fams = rule.get("families", [])
        if len(rule_fams) < 2:
            continue
        rule_fam_set = set(rule_fams)
        product_contrib = product_fam_set & rule_fam_set
        existing_contrib = existing_fam_set & rule_fam_set
        if not (product_contrib and existing_contrib):
            continue
        reason = str(rule.get("reason", "")).strip()
        labels = list(dict.fromkeys(
            _SYNERGY_FAMILY_LABELS.get(f, f)
            for f in rule_fams
            if f in (product_contrib | existing_contrib)
        ))
        combo = "と".join(labels[:2]) if len(labels) >= 2 else (labels[0] if labels else "成分")
        if reason:
            return f"{combo}の相乗効果が期待できます。{reason}"
        return f"{combo}の組み合わせで相乗効果が期待できます。"
    return ""

_CONCERN_LABEL_MAP = {
    "pores": "毛穴ケア",
    "redness": "赤み鎮静",
    "dryness": "乾燥対策",
    "barrier": "バリア強化",
    "dullness": "くすみ改善",
    "whitening": "美白",
    "pigmentation": "色素沈着",
    "acne": "ニキビ対策",
    "aging": "エイジングケア",
    "firmness": "ハリ補給",
    "texture": "質感改善",
    "oiliness": "皮脂コントロール",
    "acne_marks": "ニキビ跡",
}
_PURPOSE_KEYWORD_LABELS = [
    ("毛穴", "毛穴ケア"), ("赤み", "赤み鎮静"), ("乾燥", "乾燥対策"),
    ("バリア", "バリア強化"), ("くすみ", "くすみ改善"), ("美白", "美白"),
    ("色素沈着", "色素沈着"), ("ニキビ", "ニキビ対策"), ("ハリ", "ハリ補給"),
    ("ざらつき", "質感改善"), ("皮脂", "皮脂コントロール"),
]

def build_concern_tags(product, step):
    concerns = product.get("concerns", []) if isinstance(product, dict) else []
    purpose = str((step.get("purpose") or "") if isinstance(step, dict) else "").strip()
    tags = []
    for key, label in _CONCERN_LABEL_MAP.items():
        if key in concerns:
            tags.append(label)
    for keyword, label in _PURPOSE_KEYWORD_LABELS:
        if keyword in purpose and label not in tags:
            tags.append(label)
    return tags[:4]

def build_selection_reason_from_scores(product, step, user_data):
    if not isinstance(product, dict):
        product = {}

    if not isinstance(step, dict):
        step = {}

    def clean(value):
        return str(value or "").strip()

    def as_list(value):
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    category = clean(step.get("category"))
    purpose = clean(step.get("purpose"))
    ingredient_focus = clean(step.get("ingredient_focus"))
    section = clean(step.get("_section"))

    product_name = clean(product.get("name") or product.get("product") or step.get("product"))
    brand = clean(product.get("brand") or step.get("brand"))

    oil = clean(user_data.get("oil"))
    sens = clean(user_data.get("sens"))

    base_score = safe_price(product.get("_base_score", step.get("base_score", 0)))
    improve_score = safe_price(product.get("_improve_score", step.get("improve_score", 0)))
    routine_score = safe_price(product.get("_routine_score", step.get("routine_score", 0)))

    active_ingredients = as_list(product.get("active_ingredients", []))
    support_ingredients = as_list(product.get("support_ingredients", []))
    main_functions = as_list(product.get("main_functions", []))

    normalized_focus = normalize_ingredient_tag(ingredient_focus)
    focus_label = ingredient_map.get(normalized_focus, ingredient_focus)

    concern_words = []
    for word in ["毛穴", "赤み", "乾燥", "保湿", "バリア", "くすみ", "透明感", "色素沈着", "ニキビ", "ハリ", "ざらつき"]:
        if word and word in purpose:
            concern_words.append(word)

    concern_words = list(dict.fromkeys(concern_words))

    product_label = product_name
    if brand and product_name and not product_name.startswith(brand):
        product_label = f"{brand} {product_name}"

    if concern_words:
        if len(concern_words) >= 2:
            first = f"今の肌では、{concern_words[0]}と{concern_words[1]}を同時に整えることを重視したい状態です。"
        else:
            first = f"今の肌では、{concern_words[0]}を丁寧に整えることを優先したい状態です。"
    elif purpose:
        first = f"今回の{category}では、{purpose}を無理なく支えられることを重視しています。"
    else:
        first = f"今回の{category}では、今の肌に負担をかけにくく、続けやすいことを重視しています。"

    ingredient_points = []

    if focus_label:
        if normalized_focus in active_ingredients or focus_label in active_ingredients:
            ingredient_points.append(f"{focus_label}を主軸にケアできる")
        elif normalized_focus in support_ingredients or focus_label in support_ingredients:
            ingredient_points.append(f"{focus_label}で肌を支えやすい")
        else:
            ingredient_points.append(f"{focus_label}を意識したケアに合わせやすい")

    if main_functions:
        ingredient_points.append(f"{main_functions[0]}の役割も期待できる")

    ingredient_points = list(dict.fromkeys([p for p in ingredient_points if p]))

    score_points = []

    if improve_score >= max(base_score, routine_score):
        score_points.append("改善したい悩みに寄せやすい")
    elif base_score >= max(improve_score, routine_score):
        score_points.append("今の肌状態との相性が良い")
    elif routine_score >= max(base_score, improve_score):
        score_points.append("ルーティン全体のバランスを崩しにくい")

    if oil == "oily":
        score_points.append("皮脂が出やすい肌でも重さが出にくい")
    elif oil == "dry":
        score_points.append("乾燥しやすい肌の保湿を支えやすい")
    elif oil == "mixed":
        score_points.append("乾燥と皮脂の差が出やすい肌でも使いやすい")

    if sens in ["high", "middle"]:
        score_points.append("攻めすぎずに続けやすい")

    score_points = list(dict.fromkeys([p for p in score_points if p]))

    if product_label and ingredient_points:
        second = f"{product_label}は、{ingredient_points[0]}点が今のケア目的に合っています。"
    elif product_label and score_points:
        second = f"{product_label}は、{score_points[0]}点を評価して選んでいます。"
    elif ingredient_points:
        second = f"成分面では、{ingredient_points[0]}点が今の肌に合っています。"
    elif score_points:
        second = f"使用バランスでは、{score_points[0]}点が取り入れやすいです。"
    else:
        second = "肌への負担とケア効果のバランスを見て、今の状態に合わせやすい候補として選んでいます。"

    if len(score_points) >= 2:
        second += f"さらに、{score_points[1]}ところも続けやすさにつながります。"

    if section == "morning":
        third = "朝に使いやすい軽さも見ながら、日中の乾燥や皮脂崩れを防ぐ流れに入れています。"
    elif section == "night":
        third = "夜のケアに入れることで、日中に乱れた肌を落ち着かせながら、翌朝のなめらかさにつなげやすくなります。"
    elif section == "weekly_care":
        third = "毎日使うよりも週単位で取り入れることで、攻めすぎずに肌の底上げを狙いやすい位置づけです。"
    else:
        third = "今のルーティンに無理なく組み込みやすい点も選定理由です。"

    return first + second + third

def build_recommend_reason(product, step, user_data):
    if not isinstance(product, dict):
        product = {}

    if not isinstance(step, dict):
        step = {}

    def to_list(value):
        if isinstance(value, list):
            return value
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    def clean(value):
        return str(value or "").strip()

    category = clean(step.get("category"))
    purpose = clean(step.get("purpose"))
    ingredient_focus = clean(step.get("ingredient_focus"))
    section = clean(step.get("_section"))

    oil = clean(user_data.get("oil"))
    sens = clean(user_data.get("sens"))
    exp = clean(user_data.get("exp"))

    product_name = clean(product.get("name"))
    product_concerns = to_list(product.get("concerns", []))
    active_ingredients = to_list(product.get("active_ingredients", []))
    support_ingredients = to_list(product.get("support_ingredients", []))
    main_functions = to_list(product.get("main_functions", []))
    skin_types = to_list(product.get("skin_types", []))
    sensitive_ok = clean(product.get("sensitive_ok", "unknown"))
    texture = clean(product.get("texture"))
    formulation = to_list(product.get("formulation", []))
    contraindications = to_list(product.get("contraindications", []))

    normalized_ingredient = normalize_ingredient_tag(ingredient_focus)
    ingredient_label = ingredient_map.get(
        normalized_ingredient,
        ingredient_focus
    )

    concern_phrases = []

    if "pores" in product_concerns or "毛穴" in purpose:
        concern_phrases.append("毛穴まわりの目立ち")
    if "redness" in product_concerns or "赤み" in purpose:
        concern_phrases.append("赤み")
    if "dryness" in product_concerns or "保湿" in purpose or "乾燥" in purpose:
        concern_phrases.append("乾燥しやすさ")
    if "barrier" in product_concerns or "バリア" in purpose:
        concern_phrases.append("バリアの弱り")
    if "dullness" in product_concerns or "くすみ" in purpose or "透明感" in purpose:
        concern_phrases.append("くすみ")
    if "whitening" in product_concerns or "色素沈着" in purpose or "美白" in purpose:
        concern_phrases.append("色ムラ・色素沈着")
    if "acne" in product_concerns or "ニキビ" in purpose:
        concern_phrases.append("ニキビができやすい状態")
    if "aging" in product_concerns or "ハリ" in purpose or "エイジング" in purpose:
        concern_phrases.append("ハリ不足")

    concern_phrases = list(dict.fromkeys(concern_phrases))

    if concern_phrases:
        if len(concern_phrases) == 1:
            first_sentence = f"今の肌では、{concern_phrases[0]}への対応を優先したい状態です。"
        else:
            first_sentence = f"今の肌では、{concern_phrases[0]}と{concern_phrases[1]}を同時に見ていきたい状態です。"
    elif purpose:
        first_sentence = f"今回の{category}では、{purpose}を優先して選んでいます。"
    else:
        first_sentence = f"今回の{category}では、肌状態との相性を優先して選んでいます。"

    ingredient_sentence = ""

    if normalized_ingredient and normalized_ingredient in active_ingredients:
        ingredient_sentence = f"この商品は{ingredient_label}を軸に、今回の改善方針に直接合わせやすい点を評価しています。"
    elif normalized_ingredient and normalized_ingredient in support_ingredients:
        ingredient_sentence = f"{ingredient_label}は補助的な位置づけですが、肌を整える方向性と合っています。"
    elif main_functions:
        ingredient_sentence = f"成分面では、{main_functions[0]}を中心に今のケア目的へつなげやすい構成です。"
    else:
        ingredient_sentence = "成分情報が限られるため、カテゴリと目的の一致度を中心に判断しています。"

    skin_sentence_parts = []

    if oil == "oily":
        if "oily" in skin_types or texture in ["light", "watery", "gel", "essence"]:
            skin_sentence_parts.append("脂性肌でも重くなりすぎにくい点")
        else:
            skin_sentence_parts.append("皮脂が出やすい肌でも使い方を調整しやすい点")
    elif oil == "dry":
        if "dry" in skin_types or "dryness" in product_concerns or "barrier" in product_concerns:
            skin_sentence_parts.append("乾燥しやすい肌の支えになりやすい点")
    elif oil == "mixed":
        skin_sentence_parts.append("部分的な乾燥と皮脂の両方を見ながら使いやすい点")

    if sens == "high":
        if sensitive_ok == "yes":
            skin_sentence_parts.append("敏感傾向でも選びやすい点")
        elif sensitive_ok == "unknown":
            skin_sentence_parts.append("刺激感には様子を見ながら取り入れたい点")

    if exp == "beginner" and normalized_ingredient in ["retinol", "retinal", "retinoid"]:
        skin_sentence_parts.append("レチノール経験に合わせて少量から試したい点")

    if skin_sentence_parts:
        skin_sentence = "また、" + "、".join(skin_sentence_parts[:2]) + "も今回の肌状態に合っています。"
    else:
        skin_sentence = ""

    caution_sentence = ""

    caution_labels = []
    for c in contraindications:
        if c in contraindications_labels:
            caution_labels.append(contraindications_labels[c])

    if caution_labels:
        caution_sentence = f"一方で、{caution_labels[0]}には注意して使うのが安心です。"

    sentences = [
        first_sentence,
        ingredient_sentence,
        skin_sentence,
        caution_sentence
    ]

    return "".join([s for s in sentences if s])

def build_ai_reason(step, user_data):
    if not isinstance(step, dict):
        step = {}

    def clean(value):
        return str(value or "").strip()

    category = clean(step.get("category"))
    purpose = clean(step.get("purpose"))
    ingredient_focus = clean(step.get("ingredient_focus"))
    section = clean(step.get("_section"))
    product_name = clean(step.get("product"))
    brand = clean(step.get("brand"))

    oil = clean(user_data.get("oil"))
    sens = clean(user_data.get("sens"))
    exp = clean(user_data.get("exp"))

    product_label = product_name
    if brand and product_name and not product_name.startswith(brand):
        product_label = f"{brand} {product_name}"

    if purpose:
        first = f"今回の{category}では、{purpose}を支えられることを重視しています。"
    else:
        first = f"今回の{category}では、今の肌に負担をかけにくく、続けやすいことを重視しています。"

    focus_sentence = ""
    if product_label and ingredient_focus:
        focus_sentence = f"{product_label}は、{ingredient_focus}を意識したケアに取り入れやすい候補です。"
    elif ingredient_focus:
        focus_sentence = f"成分面では、{ingredient_focus}を軸にしたケアとして組み込んでいます。"

    section_sentence = ""
    if section == "morning":
        section_sentence = "朝は日中の乾燥・皮脂・紫外線の影響を受けやすいため、重さが出にくく使いやすい流れを優先しています。"
    elif section == "night":
        section_sentence = "夜は日中に乱れた肌を整えやすい時間帯なので、保湿と改善ケアのバランスを見て入れています。"
    elif section == "weekly_care":
        section_sentence = "週ケアでは、毎日のケアだけでは補いにくいざらつき・乾燥・くすみを無理のない頻度で整える目的です。"

    skin_sentence_parts = []

    if oil == "oily":
        skin_sentence_parts.append("皮脂が出やすい肌でも重くなりにくいこと")
    elif oil == "dry":
        skin_sentence_parts.append("乾燥しやすい肌の保湿を支えやすいこと")
    elif oil == "mixed":
        skin_sentence_parts.append("乾燥と皮脂の差が出やすい肌でも使いやすいこと")

    if sens == "high":
        skin_sentence_parts.append("刺激感が出にくいこと")
    elif sens == "middle":
        skin_sentence_parts.append("攻めと守りのバランスを取りやすいこと")

    if exp == "beginner" and (
        "レチノール" in ingredient_focus
        or "レチナール" in ingredient_focus
        or "retinol" in ingredient_focus.lower()
    ):
        skin_sentence_parts.append("レチノール経験に合わせて慎重に始めやすいこと")

    skin_sentence = ""
    if skin_sentence_parts:
        skin_sentence = "また、" + "、".join(skin_sentence_parts[:2]) + "も選定理由です。"

    return "".join([
        first,
        focus_sentence,
        section_sentence,
        skin_sentence
    ])

def calculate_step_impact(step, product):
    impact = {
        "oil_balance": 0,
        "redness": 0,
        "pores": 0,
        "hydration": 0,
        "firmness": 0
    }

    purpose = normalize_text(step.get("purpose", ""))
    ingredient_focus = normalize_ingredient_tag(step.get("ingredient_focus", ""))

    active_ingredients = product.get("active_ingredients", []) if product else []
    support_ingredients = product.get("support_ingredients", []) if product else []
    concerns = product.get("concerns", []) if product else []
    main_functions = product.get("main_functions", []) if product else []
    category = step.get("category", "")

    all_ingredients = set(active_ingredients + support_ingredients)

    # =========================
    # purposeベース
    # =========================
    if "毛穴" in purpose or "pores" in purpose:
        impact["pores"] += 10

    if "赤み" in purpose or "redness" in purpose:
        impact["redness"] += 12

    if "乾燥" in purpose or "保湿" in purpose or "うるおい" in purpose:
        impact["hydration"] += 12

    if "ハリ" in purpose or "エイジング" in purpose or "しわ" in purpose:
        impact["firmness"] += 10

    if "皮脂" in purpose or "テカリ" in purpose:
        impact["oil_balance"] += 10
        impact["pores"] += 4

    # =========================
    # concernベース
    # =========================
    if "pores" in concerns:
        impact["pores"] += 8

    if "redness" in concerns:
        impact["redness"] += 10

    if "dryness" in concerns or "barrier" in concerns:
        impact["hydration"] += 10

    if "aging" in concerns:
        impact["firmness"] += 10

    if "oil_control" in concerns:
        impact["oil_balance"] += 8
        impact["pores"] += 4

    # =========================
    # 成分ベース
    # =========================
    if "vitamin_c" in all_ingredients:
        impact["pores"] += 6
        impact["firmness"] += 3

    if "niacinamide" in all_ingredients:
        impact["pores"] += 6
        impact["oil_balance"] += 6
        impact["redness"] += 3

    if "azelaic_acid" in all_ingredients:
        impact["redness"] += 10
        impact["pores"] += 6
        impact["oil_balance"] += 4

    if "retinol" in all_ingredients:
        impact["firmness"] += 12
        impact["pores"] += 5

    if "retinal" in all_ingredients:
        impact["firmness"] += 14
        impact["pores"] += 5

    if "peptide" in all_ingredients:
        impact["firmness"] += 10

    if "pdrn" in all_ingredients:
        impact["firmness"] += 8
        impact["redness"] += 4
        impact["hydration"] += 3

    if "ceramide" in all_ingredients:
        impact["hydration"] += 12

    if "hyaluronic_acid" in all_ingredients:
        impact["hydration"] += 10

    if "panthenol" in all_ingredients:
        impact["hydration"] += 6
        impact["redness"] += 4

    if "beta_glucan" in all_ingredients:
        impact["hydration"] += 5
        impact["redness"] += 3

    if "cica" in all_ingredients:
        impact["redness"] += 10

    if "madecassoside" in all_ingredients:
        impact["redness"] += 8

    if "centella_extract" in all_ingredients:
        impact["redness"] += 6

    if "dipotassium_glycyrrhizate" in all_ingredients:
        impact["redness"] += 6

    if "salicylic_acid" in all_ingredients:
        impact["pores"] += 10
        impact["oil_balance"] += 6

    if "bha" in all_ingredients:
        impact["pores"] += 8
        impact["oil_balance"] += 5

    if "aha" in all_ingredients:
        impact["pores"] += 6
        impact["firmness"] += 2

    if "clay" in all_ingredients:
        impact["oil_balance"] += 8
        impact["pores"] += 4

    if "tranexamic_acid" in all_ingredients:
        impact["dullness"] = impact.get("dullness", 0) + 8

    if "ferulic_acid" in all_ingredients:
        impact["dullness"] = impact.get("dullness", 0) + 5
        impact["firmness"] += 2

    if "bakuchiol" in all_ingredients:
        impact["firmness"] += 6

    if "egf" in all_ingredients or "fgf" in all_ingredients:
        impact["firmness"] += 8

    if "ceramide" in all_ingredients and "cholesterol" in all_ingredients:
        impact["hydration"] += 4

    if "mugwort" in all_ingredients or "azulene" in all_ingredients:
        impact["redness"] += 5

    if "lha" in all_ingredients:
        impact["pores"] += 6
        impact["oil_balance"] += 3

    if "zinc" in all_ingredients:
        impact["oil_balance"] += 4
        impact["pores"] += 2
    # =========================
    # ingredient_focus補正
    # =========================
    if ingredient_focus == "vitamin_c":
        impact["pores"] += 4
    elif ingredient_focus == "azelaic_acid":
        impact["redness"] += 5
        impact["pores"] += 3
    elif ingredient_focus == "retinol":
        impact["firmness"] += 5
    elif ingredient_focus == "retinal":
        impact["firmness"] += 6
    elif ingredient_focus == "niacinamide":
        impact["oil_balance"] += 4
        impact["pores"] += 3
    elif ingredient_focus == "ceramide":
        impact["hydration"] += 5
    elif ingredient_focus == "peptide":
        impact["firmness"] += 4
    elif ingredient_focus == "cica":
        impact["redness"] += 4

    # =========================
    # カテゴリベース
    # =========================
    if category == "洗顔":
        impact["oil_balance"] += 3
        impact["pores"] += 3

    if category == "化粧水":
        impact["hydration"] += 3

    if category == "クリーム" or category == "乳液":
        impact["hydration"] += 4

    if category == "日焼け止め":
        impact["redness"] += 2
        impact["firmness"] += 2

    # 0未満防止
    for k in impact:
        if impact[k] < 0:
            impact[k] = 0

    if "jojoba_oil" in all_ingredients or "argan_oil" in all_ingredients or "olive_oil" in all_ingredients:
        impact["hydration"] += 4

    if "tea_tree_oil" in all_ingredients:
        impact["redness"] += 2
        impact["oil_balance"] += 3
        impact["pores"] += 2

    if "rice_power_no11" in all_ingredients:
        impact["hydration"] += 8

    if "rice_power_no6" in all_ingredients:
        impact["oil_balance"] += 6

    if "multi_ceramide_complex" in all_ingredients or "ceramide_complex_ex" in all_ingredients:
        impact["hydration"] += 8

    if "pore_refining_complex" in all_ingredients or "pore_minimizing_complex" in all_ingredients:
        impact["pores"] += 6

    if "sebum_control_complex" in all_ingredients or "oil_balancing_complex" in all_ingredients:
        impact["oil_balance"] += 6

    if "cica_complex" in all_ingredients or "anti_redness_complex" in all_ingredients:
        impact["redness"] += 6

    if "peptide_complex" in all_ingredients or "firming_complex" in all_ingredients:
        impact["firmness"] += 6

    return impact

def format_top_impacts(impact, top_n=2):
    label_map = {
        "oil_balance": "皮脂",
        "redness": "赤み",
        "pores": "毛穴",
        "hydration": "保湿",
        "firmness": "ハリ"
    }

    pairs = sorted(impact.items(), key=lambda x: x[1], reverse=True)
    pairs = [p for p in pairs if p[1] > 0][:top_n]

    return [
        {
            "key": key,
            "label": label_map.get(key, key),
            "value": value
        }
        for key, value in pairs
    ]

def parse_budget(budget_text):
    if not budget_text:
        return 0

    text = str(budget_text)
    text = text.replace("円", "").replace(",", "").replace(" ", "").strip()

    digits = ""
    for ch in text:
        if ch.isdigit():
            digits += ch

    return int(digits) if digits else 0

def debug_log(label, value=None):
    print(f"\n===== {label} =====")
    if value is not None:
        print(value)
    print("====================\n")

USAGE_LOG = {}

def is_rate_limited(ip, limit=3):
    today = datetime.now().strftime("%Y-%m-%d")

    if ip not in USAGE_LOG:
        USAGE_LOG[ip] = {"date": today, "count": 0}

    if USAGE_LOG[ip]["date"] != today:
        USAGE_LOG[ip] = {"date": today, "count": 0}

    if USAGE_LOG[ip]["count"] >= limit:
        return True

    USAGE_LOG[ip]["count"] += 1
    return False



def debug_step_summary(section_name, steps):
    print(f"\n===== STEP SUMMARY: {section_name} =====")
    if not isinstance(steps, list):
        print("steps is not a list")
        print("==============================\n")
        return

    for i, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            print(f"{i}. invalid step: {step}")
            continue

        print(
            f"{i}. "
            f"category={step.get('category', '')} / "
            f"product={step.get('product', '')} / "
            f"source={step.get('product_source', '')} / "
            f"price={step.get('price', 0)} / "
            f"base={step.get('base_score', 0)} / "
            f"improve={step.get('improve_score', 0)} / "
            f"final={step.get('match_score', 0)}"
        )
    print("====================================\n")

def validate_lab_dependencies():
    required_functions = [
        "extract_user_data",
        "load_uploaded_images",
        "analyze_skin_with_gemini",
        "normalize_ai_labels",
        "normalize_serum_roles",
        "enforce_booster_night_only",
        "apply_moisture_plan",
        "load_products",
        "validate_and_log_products",
        "parse_budget",
        "assign_products_to_all_steps",
        "limit_serum_steps",
        "sort_steps",
        "finalize_result_data",
        "finalize_budget_info",
        "append_result",
        "debug_log",
        "debug_step_summary",
    ]

    missing = []

    for name in required_functions:
        obj = globals().get(name)
        if obj is None or not callable(obj):
            missing.append(name)

    if missing:
        raise RuntimeError("不足している関数: " + ", ".join(missing))

def _score_breakdown(product, step, user_data, budget_value):
    """DB vs Rakuten スコア差分デバッグ用: 主要コンポーネント別の加点/減点を返す"""
    purpose = step.get("purpose", "")
    ingredient_focus = step.get("ingredient_focus", "")
    ingredient_tag = normalize_ingredient_tag(ingredient_focus)
    concern_tags = purpose_to_concern_tags(purpose)
    purpose_norm = normalize_text(purpose)

    actives = list(product.get("active_ingredients", []) or [])
    support = list(product.get("support_ingredients", []) or [])
    concerns = list(product.get("concerns", []) or [])
    functions = list(product.get("main_functions", []) or [])
    focuses = list(product.get("ingredient_focus", []) or [])
    skins = list(product.get("skin_types", []) or [])
    sigs = list(product.get("signature_ingredients", []) or [])
    strength_map = product.get("ingredient_strength", {}) or {}
    sensitive_ok = product.get("sensitive_ok", "unknown")
    avail = list(product.get("availability_japan", []) or [])
    price_ref = safe_price(product.get("price_ref", 0))
    sens = normalize_text((user_data or {}).get("sens", ""))
    oil = normalize_text((user_data or {}).get("oil", ""))

    bd = {}
    bd["category_match"] = 40

    # ingredient_focus active/support/miss
    if ingredient_tag:
        if ingredient_tag in actives:
            strength_bonus = (get_strength_score(strength_map.get(ingredient_tag)) or 0)
            bd["focus_active"] = 25 + strength_bonus
        elif ingredient_tag in support:
            bd["focus_support"] = 10
        else:
            bd["focus_miss"] = -15

    # score_goal_fit: concerns (cap=24) + main_functions×8 + ingredient_focus×6
    concern_hits = [c for c in concern_tags if c in concerns]
    if concern_hits:
        bd["goal_concerns"] = min(len(concern_hits) * 12, 24)
    func_hits = [f for f in functions if normalize_text(f) and (normalize_text(f) in purpose_norm or purpose_norm in normalize_text(f))]
    if func_hits:
        bd["goal_functions"] = len(func_hits) * 8
    focus_hits = [f for f in focuses if normalize_text(f) and (normalize_text(f) in purpose_norm or purpose_norm in normalize_text(f))]
    if focus_hits:
        bd["goal_ing_focus"] = len(focus_hits) * 6

    # apply_common: concerns×8
    if concern_hits:
        bd["common_concerns"] = len(concern_hits) * 8

    # apply_common: functions×6
    if func_hits:
        bd["common_functions"] = len(func_hits) * 6

    # apply_common: ingredient_focus (product's focus list)×6
    if focus_hits:
        bd["common_ing_focus"] = len(focus_hits) * 6

    # skin_types
    user_skins = normalize_skin_type(oil, sens)
    skin_hits = [s for s in user_skins if s in skins]
    if skin_hits:
        bd["skin_types"] = len(skin_hits) * 6

    # sensitive_ok
    if sens == "high":
        if sensitive_ok == "yes":
            bd["sensitive_ok"] = 12
        elif sensitive_ok == "no":
            bd["sensitive_ok"] = -15

    # availability
    avail_score = get_availability_score(avail)
    if avail_score:
        bd["availability"] = avail_score

    # budget
    if budget_value and budget_value > 0:
        bscore = get_budget_fit_score(price_ref, budget_value)
        if bscore:
            bd["budget"] = bscore

    # signature ingredients bonus (approximate: each known sig = +8)
    if sigs:
        bd["signature"] = f"(sigs={sigs})"

    # ingredient_tag extra +15 at end of score_product
    if ingredient_tag and ingredient_tag in actives:
        bd["focus_extra_15"] = 15

    return bd


def log_candidate_battle(step, candidates, selected=None, user_data=None, budget_value=0):
    section = step.get("_section", "")
    category = step.get("category", "")
    purpose = step.get("purpose", "")
    ingredient_focus = step.get("ingredient_focus", "")
    ingredient_tag = normalize_ingredient_tag(ingredient_focus)

    print("\n===== CANDIDATE BATTLE DETAIL =====", flush=True)
    print(f"section={section} / category={category} / ingredient_focus={ingredient_focus}", flush=True)

    if not candidates:
        print("no candidates", flush=True)
        print("===================================\n", flush=True)
        return

    # ソース別の集計
    source_counts = {}
    for c in candidates:
        src = c.get("_source", "unknown")
        source_counts[src] = source_counts.get(src, 0) + 1
    print(f"candidates total={len(candidates)} / by_source={source_counts}", flush=True)

    for idx, c in enumerate(candidates[:8], start=1):
        if not isinstance(c, dict):
            continue
        src = c.get("_source", "?")
        actives = c.get("active_ingredients", []) or []
        has_focus = ingredient_tag and ingredient_tag in actives
        sensitive_ok = c.get("sensitive_ok", "?")
        avail = c.get("availability_japan", []) or []
        concerns_count = len(c.get("concerns", []) or [])
        functions_count = len(c.get("main_functions", []) or [])

        print(
            f"  [{idx}] src={src:<14} final={c.get('_score',0):>6.1f} "
            f"base={c.get('_base_score',0):>5.1f} "
            f"imp={c.get('_improve_score',0):>5.1f} "
            f"routine={c.get('_routine_score',0):>5.1f} | "
            f"focus_hit={'YES' if has_focus else 'NO '} "
            f"sens={sensitive_ok:<7} "
            f"avail={len(avail)} "
            f"concerns={concerns_count} funcs={functions_count} | "
            f"name={str(c.get('name',''))[:30]}",
            flush=True
        )

    if selected:
        print(f">>> WINNER: {selected.get('name','')} (src={selected.get('_source','')}, score={selected.get('_score',0)})", flush=True)

    # --- DB上位 vs 楽天上位の詳細比較 ---
    _db_top = next((c for c in candidates if c.get("_source") not in ("rakuten_criteria", "ai_virtual", "ai+db")), None)
    _rak_top = next((c for c in candidates if c.get("_source") == "rakuten_criteria"), None)

    if _db_top or _rak_top:
        print("  --- DB vs Rakuten 詳細比較 ---", flush=True)
        for label, c in [("DB_TOP ", _db_top), ("RAK_TOP", _rak_top)]:
            if not c:
                continue
            actives = c.get("active_ingredients", []) or []
            concerns = c.get("concerns", []) or []
            functions = c.get("main_functions", []) or []
            focuses = c.get("ingredient_focus", []) or []
            skins = c.get("skin_types", []) or []
            strength_map = c.get("ingredient_strength", {}) or {}
            avail = c.get("availability_japan", []) or []
            sens_val = c.get("sensitive_ok", "?")
            price = safe_price(c.get("price_ref", 0))
            print(f"  [{label}] {str(c.get('name',''))[:35]} (base={c.get('_base_score',0):.1f})", flush=True)
            print(f"    actives={actives}", flush=True)
            print(f"    strength={strength_map}", flush=True)
            print(f"    concerns={concerns}", flush=True)
            print(f"    functions={functions}", flush=True)
            print(f"    ing_focus={focuses}", flush=True)
            print(f"    skin_types={skins} / sensitive_ok={sens_val}", flush=True)
            print(f"    avail={avail} / price={price}", flush=True)
            if user_data is not None:
                bd = _score_breakdown(c, step, user_data, budget_value)
                bd_str = " | ".join(f"{k}={v:+d}" if isinstance(v, int) else f"{k}={v}" for k, v in bd.items())
                print(f"    breakdown: {bd_str}", flush=True)

    print("===================================\n", flush=True)


def count_selected_sources(data):
    counts = {
        "db": 0,
        "ai": 0,
        "ai+db": 0,
        "fallback": 0,
        "other": 0
    }

    for section in ["morning", "night"]:
        for step in data.get(section, {}).get("steps", []):
            source = str(step.get("product_source", "") or "").strip()
            if source in counts:
                counts[source] += 1
            else:
                counts["other"] += 1

    for step in data.get("weekly_care", []):
        source = str(step.get("product_source", "") or "").strip()
        if source in counts:
            counts[source] += 1
        else:
            counts["other"] += 1

    return counts
def calculate_final_score(product, step, user_data, budget_value):
    """
    完全統一スコア（DB / AI 全て共通）
    """

    base_score = score_product(product, step, user_data, budget_value)
    improve_score = score_improvement(product, step)

    weights = get_dynamic_score_weights(step, user_data)

    final_score = (
        base_score * weights.get("base_weight", 1.0) +
        improve_score * weights.get("improve_weight", 1.0)
    )

    return final_score

def calculate_total_price(data):
    total = 0

    for step in data.get("morning", {}).get("steps", []):
        step["_section"] = "morning"
        price = step.get("price", 0)
        if isinstance(price, (int, float)):
            total += price

    for step in data.get("night", {}).get("steps", []):
        step["_section"] = "night"
        price = step.get("price", 0)
        if isinstance(price, (int, float)):
            total += price

    for step in data.get("weekly_care", []):
        step["_section"] = "weekly_care"
        price = step.get("price", 0)
        if isinstance(price, (int, float)):
            total += price

    return total

def build_budget_fit_plan(data, budget_value):
    data = normalize_result_sections(data)

    all_steps = []

    # 朝
    for step in data.get("morning", {}).get("steps", []):
        copied = dict(step)
        copied["_section"] = "morning"
        all_steps.append(copied)

    # 夜
    for step in data.get("night", {}).get("steps", []):
        copied = dict(step)
        copied["_section"] = "night"
        all_steps.append(copied)

    # 週ケア
    for step in data.get("weekly_care", []):
        copied = dict(step)
        copied["_section"] = "weekly_care"
        all_steps.append(copied)

    # ステップ整形
    for step in all_steps:
        normalize_step_price_fields(step)

    # 優先順位
    # 1. priorityが小さい
    # 2. 最終スコアが高い
    # 3. 価格が低い
    all_steps.sort(
        key=lambda x: (
            x.get("priority", 999),
            -(x.get("match_score", 0) if isinstance(x.get("match_score", 0), (int, float)) else 0),
            safe_price(x.get("price", 0))
        )
    )

    selected = []
    total = 0
    selected_keys = set()

    for step in all_steps:
        section = step.get("_section", "")
        category = step.get("category", "")
        role = step.get("role", "")
        key = (section, category, role)

        price = safe_price(step.get("price", 0))

        # 予算未入力なら高優先だけ整えて全部採用
        if budget_value == 0:
            if key not in selected_keys:
                selected.append(step)
                selected_keys.add(key)
            continue

        # 同じセクション・同カテゴリ・同roleの重複を避ける
        if key in selected_keys:
            continue

        # 価格不明は最後に回したいので、予算あり時は基本スキップ
        if price <= 0:
            continue

        if total + price <= budget_value:
            selected.append(step)
            selected_keys.add(key)
            total += price

    # もし何も入らなかったら、最低限 priority上位を価格無視で補う
    if not selected and all_steps:
        for step in all_steps[:3]:
            key = (step.get("_section", ""), step.get("category", ""), step.get("role", ""))
            if key in selected_keys:
                continue
            selected.append(step)
            selected_keys.add(key)

    result = {
        "morning": {"steps": []},
        "night": {"steps": []},
        "weekly_care": [],
        "total_price": total
    }

    for step in selected:
        section = step.get("_section")
        clean_step = dict(step)
        clean_step.pop("_section", None)

        if section == "morning":
            result["morning"]["steps"].append(clean_step)
        elif section == "night":
            result["night"]["steps"].append(clean_step)
        elif section == "weekly_care":
            result["weekly_care"].append(clean_step)

    result["total_price"] = calculate_total_price(result)
    sort_steps(result)  # 予算選定後もカテゴリ正規順に並べ直す
    return result



def pick_product(category, products):
    candidates = [p for p in products if p.get("category", "") == category]
    if not candidates:
        return None
    return max(candidates, key=lambda x: x.get("score", 0))
# 履歴読み込み
def load_results(user_id=None, client_ip=None):
    """
    user_id（UUIDクッキー）でフィルタする。
    user_id がない場合は全件返す（append_result 内の全件ロード用）。
    """
    conn = None
    cur = None

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        if user_id:
            cur.execute("""
            SELECT payload
            FROM results
            WHERE payload->>'user_id' = %s
            ORDER BY saved_at DESC
            """, (user_id,))
        else:
            cur.execute("""
            SELECT payload
            FROM results
            ORDER BY saved_at DESC
            """)

        rows = cur.fetchall()

        results = []
        for row in rows:
            payload = row[0]
            if isinstance(payload, dict):
                results.append(payload)
            elif isinstance(payload, str):
                try:
                    results.append(json.loads(payload))
                except Exception:
                    pass

        if user_id:
            cur.execute("SELECT COUNT(*) FROM results")
            total = cur.fetchone()[0]
            print(f"[HISTORY LOAD] user_id={user_id!r} matched={len(results)} total_in_db={total}", flush=True)
        else:
            print("[RESULTS LOADED FROM DB]", len(results), flush=True)

        return results

    except Exception as e:
        print("[RESULTS LOAD ERROR]", e, flush=True)
        return []

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


def trim_results_by_user_id(user_id, keep=5):
    """無料ユーザーの診断履歴をkeep件のみ残して古いものを削除する"""
    if not user_id:
        return
    conn = None
    cur = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
        DELETE FROM results
        WHERE payload->>'user_id' = %s
        AND id NOT IN (
            SELECT id FROM results
            WHERE payload->>'user_id' = %s
            ORDER BY saved_at DESC
            LIMIT %s
        )
        """, (user_id, user_id, keep))
        deleted = cur.rowcount
        conn.commit()
        if deleted > 0:
            print(f"[TRIM RESULTS] user_id={user_id} deleted={deleted} kept={keep}", flush=True)
    except Exception as e:
        print(f"[TRIM ERROR] {repr(e)}", flush=True)
        if conn:
            conn.rollback()
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
# 履歴保存
def save_results(data):
    if not isinstance(data, list):
        raise ValueError("save_results: data must be a list")

    conn = None
    cur = None

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        for item in data:
            if not isinstance(item, dict):
                continue

            record_id = str(item.get("id", "")).strip()
            if not record_id:
                continue

            saved_at = item.get("saved_at")

            cur.execute(
                """
                INSERT INTO results (id, saved_at, payload)
                VALUES (%s, %s, %s::jsonb)
                ON CONFLICT (id)
                DO UPDATE SET
                    saved_at = EXCLUDED.saved_at,
                    payload = EXCLUDED.payload;
                """,
                (
                    record_id,
                    saved_at,
                    json.dumps(lightweight_result_payload(item),
                                ensure_ascii=False)
                )
            )

        conn.commit()

        print("[RESULTS SAVED TO DB]", len(data), flush=True)

        return True

    except Exception as e:
        if conn:
            conn.rollback()

        print("[RESULTS DB SAVE ERROR]", e, flush=True)

        raise

    finally:
        if cur:
            cur.close()

        if conn:
            conn.close()

# 診断ID生成
def generate_result_id(history):
    existing_ids = set()

    if isinstance(history, list):
        for item in history:
            if isinstance(item, dict):
                item_id = str(item.get("id", "") or "").strip()
                if item_id:
                    existing_ids.add(item_id)

    while True:
        new_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
        if new_id not in existing_ids:
            return new_id

def get_step_display_role(step):
    category = step.get("category", "")
    role = step.get("role", "")

    if category != "美容液":
        return category

    if role == "booster":
        return "導入美容液"

    return "美容液"

def finalize_step_data(step, user_data):
    if not isinstance(step, dict):
        step = {}

    def to_number(value, default=0):
        if isinstance(value, (int, float)):
            return value
        try:
            return safe_price(value)
        except Exception:
            return default

    def clean_text(value):
        return "" if value is None else str(value).strip()

    def normalize_candidate(c):
        if not isinstance(c, dict):
            return None

        name = clean_text(
            c.get("name")
            or c.get("product")
            or c.get("product_name")
        )

        if not name:
            return None

        brand, name = clean_brand_and_product_name(
            c.get("brand", ""),
            name
        )

        base = to_number(
            c.get("base_score", c.get("base", c.get("fit_score", 0)))
        )
        improve = to_number(
            c.get("improve_score", c.get("improve", c.get("improvement_score", 0)))
        )
        routine = to_number(
            c.get("routine_score", c.get("routine", 0))
        )
        final = to_number(
            c.get("score", c.get("final_score", c.get("match_score", c.get("final", 0))))
        )

        if final <= 0:
            final = base + improve + routine

        return {
            "brand": brand,
            "name": name,
            "score": final,
            "base_score": base,
            "improve_score": improve,
            "routine_score": routine,
            "source": clean_text(c.get("source", c.get("product_source", ""))),
            "price_ref": to_number(c.get("price_ref", c.get("price", 0))),
        }

    def build_candidate_identity_keys(candidate):
        brand = candidate.get("brand", "")
        name = candidate.get("name", "")

        identity = normalize_product_identity(brand, name)

        brand_text, name_text = clean_brand_and_product_name(brand, name)
        name_only_identity = normalize_candidate_name_for_merge(name_text)

        keys = {identity, name_only_identity}
        return {k for k in keys if k}

    def preserve_ranked_top_candidates(step):
        """
        select_best_market_candidate 側で確定した順位を壊さずに、
        表示用の型・キーだけを整える。
        ここでは再ソートしない。
        """
        raw_candidates = step.get("top_candidates", [])

        if not isinstance(raw_candidates, list):
            raw_candidates = []

        # カテゴリ名（またはその同義語、例:「エマルジョン」=乳液）そのままの候補を除外
        # 「セラミド 乳液」「ナイアシンアミド 美容液」等は正当な候補名のため除外しない
        # (score_product() のメイン候補選定と同じ _is_generic_candidate_name を共有し、
        #  判定基準がずれないようにする)
        _step_cat = str(step.get("category", "") or "")

        def _is_generic_name(brand: str, name: str) -> bool:
            return _is_generic_candidate_name(brand, name, _step_cat)

        normalized_candidates = []
        seen_keys = set()
        seen_product_names = set()  # 製品名レベルの追加重複チェック

        for c in raw_candidates:
            normalized = normalize_candidate(c)
            if not normalized:
                continue

            # 汎用名はスキップ（ブランドあり/なし問わず）
            _cand_name = normalized.get("name", "")
            if _is_generic_name(normalized.get("brand", ""), _cand_name):
                print(f"[CANDIDATE FILTER] generic name skipped: cat={_step_cat} name={_cand_name!r}", flush=True)
                continue

            # カテゴリ横断バリデーション（モジュールレベル関数で全カテゴリ対応）
            if is_candidate_wrong_for_category(_step_cat, _cand_name):
                print(f"[CANDIDATE FILTER] wrong category skipped: cat={_step_cat} name={_cand_name!r}", flush=True)
                continue

            identity_keys = build_candidate_identity_keys(normalized)

            if not identity_keys:
                continue

            if seen_keys.intersection(identity_keys):
                continue

            # 製品名が同じなら異なるブランドでも重複扱い
            _name_key = normalize_product_name(normalized.get("name", ""))
            if _name_key and _name_key in seen_product_names:
                continue

            seen_keys.update(identity_keys)
            if _name_key:
                seen_product_names.add(_name_key)
            normalized_candidates.append(normalized)

            if len(normalized_candidates) >= 3:
                break

        if normalized_candidates:
            # 楽天検索由来の候補は仕様上ブランド欄が常に空文字になる。
            # 商品名自体は具体的（汎用名チェックを通過済み）なので、除外せず
            # 商品名からブランド名をGeminiに補完させる（最大3件・診断1回あたり
            # ステップ数×3件が上限のため呼び出し回数は抑えられている）。
            for cand in normalized_candidates:
                if cand.get("brand"):
                    continue
                _inferred_brand = infer_brand_from_title(
                    cand.get("name", ""), _step_cat
                )
                if _inferred_brand:
                    cand["brand"] = _inferred_brand
            return normalized_candidates

        selected_candidate = normalize_candidate({
            "brand": step.get("brand", ""),
            "name": step.get("product", ""),
            "score": step.get("match_score", 0),
            "base_score": step.get("base_score", 0),
            "improve_score": step.get("improve_score", 0),
            "routine_score": step.get("routine_score", 0),
            "source": step.get("product_source", ""),
            "price_ref": step.get("price", step.get("price_ref", 0)),
        })

        # fallback候補も汎用名なら捨てる（1位2位に同名表示を防ぐ）
        if selected_candidate and _is_generic_name(
            selected_candidate.get("brand", ""), selected_candidate.get("name", "")
        ):
            return []

        return [selected_candidate] if selected_candidate else []

    category = clean_text(step.get("category")) or "美容液"
    ingredient_focus = clean_text(step.get("ingredient_focus"))
    purpose = clean_text(step.get("purpose")) or "肌状態に合わせた基本ケア"

    step["category"] = category
    step["ingredient_focus"] = ingredient_focus
    step["purpose"] = purpose

    step = normalize_step_price_fields(step)

    current_product = clean_text(step.get("product"))

    if not current_product:
        if step.get("product_source") in ["none", "missing"]:
            step["product"] = ""
            step["product_source"] = "none"
        else:
            candidate_name = get_first_concrete_candidate(step)

            if candidate_name:
                step["product"] = clean_text(candidate_name)
                step["product_source"] = step.get("product_source") or "ai_fallback"
            else:
                step["product"] = ""
                step["product_source"] = "missing"
    else:
        step["product"] = current_product

    if not step.get("product_source"):
        step["product_source"] = "selected"

    if not step.get("image"):
        step["image"] = ""

    invalid_reasons = [
        "現在確認できる商品候補が見つかりませんでした。",
        "確認できる商品候補が見つかりませんでした。",
    ]

    if (
        not step.get("recommend_reason")
        or str(step.get("recommend_reason", "")).strip() in invalid_reasons
    ):
        if step.get("product"):
            step["recommend_reason"] = build_ai_reason(step, user_data)
        else:
            step["recommend_reason"] = "現在確認できる商品候補が見つかりませんでした。"

    price = to_number(step.get("price", step.get("price_ref", 0)))
    step["price"] = price

    if not step.get("price_band"):
        if price > 0:
            if price <= 1500:
                step["price_band"] = "〜1500円"
            elif price <= 3000:
                step["price_band"] = "1501〜3000円"
            elif price <= 5000:
                step["price_band"] = "3001〜5000円"
            else:
                step["price_band"] = "5001円以上"
        else:
            step["price_band"] = "価格不明"

    for key in ["match_score", "base_score", "improve_score", "routine_score", "priority"]:
        step[key] = to_number(step.get(key, 0))

    step["top_candidates"] = preserve_ranked_top_candidates(step)

    if step["top_candidates"]:
        best = step["top_candidates"][0]

        if not step.get("product"):
            step["product"] = best.get("name", "")

        if not step.get("brand") and best.get("brand"):
            step["brand"] = best.get("brand", "")

        if step.get("match_score", 0) <= 0:
            step["match_score"] = best.get("score", 0)

        if step.get("base_score", 0) <= 0:
            step["base_score"] = best.get("base_score", 0)

        if step.get("improve_score", 0) <= 0:
            step["improve_score"] = best.get("improve_score", 0)

        if step.get("routine_score", 0) <= 0:
            step["routine_score"] = best.get("routine_score", 0)

    step["product_candidates"] = [
        c for c in step.get("product_candidates", [])
        if isinstance(c, dict)
    ]

    existing_score_detail = step.get("score_detail")

    if isinstance(existing_score_detail, dict):
        base = to_number(existing_score_detail.get("base", step.get("base_score", 0)))
        improve = to_number(existing_score_detail.get("improve", step.get("improve_score", 0)))
        routine = to_number(existing_score_detail.get("routine", step.get("routine_score", 0)))
        final = to_number(existing_score_detail.get("final", step.get("match_score", 0)))
    else:
        base = step.get("base_score", 0)
        improve = step.get("improve_score", 0)
        routine = step.get("routine_score", 0)
        final = step.get("match_score", 0)

    if final <= 0:
        final = base + improve + routine

    step["score_detail"] = {
        "base": base,
        "improve": improve,
        "routine": routine,
        "final": final,
    }

    step["base_score"] = base
    step["improve_score"] = improve
    step["routine_score"] = routine
    step["match_score"] = final

    if not isinstance(step.get("impact_scores"), dict):
        impact = calculate_step_impact(step, None)
        step["impact_scores"] = impact
        step["top_impacts"] = format_top_impacts(impact)
    else:
        if not isinstance(step.get("top_impacts"), list):
            step["top_impacts"] = format_top_impacts(step["impact_scores"])

    step["display_role"] = get_step_display_role(step)

    for key in [
        "category",
        "role",
        "purpose",
        "ingredient_focus",
        "risk_note",
        "product",
        "brand",
        "recommend_reason",
        "product_source",
        "frequency",
        "display_role",
    ]:
        if key in step:
            step[key] = clean_text(step[key])

    return step

def build_rule_based_warnings(data, user_data):
    warnings = []
    sens = normalize_text(user_data.get("sens", ""))
    exp = normalize_text(user_data.get("exp", ""))

    all_steps = []
    all_steps += data.get("morning", {}).get("steps", [])
    all_steps += data.get("night", {}).get("steps", [])
    all_steps += data.get("weekly_care", [])

    ingredient_tags = []
    for step in all_steps:
        ing = normalize_ingredient_tag(step.get("ingredient_focus", ""))
        if ing:
            ingredient_tags.append(ing)

    ingredient_tags = list(dict.fromkeys(ingredient_tags))

    # Gemini-derived combination warnings (soft severity) and residual violations (hard that slipped through)
    avoid_rules = data.get("routine_strategy", {}).get("avoid_combinations", [])
    if avoid_rules:
        # ③ scope:"same_session" はセクション単位で判定、"any" は全セクション横断で判定
        _section_families: dict = {}
        for _sec, _steps in [
            ("morning", data.get("morning", {}).get("steps", [])),
            ("night",   data.get("night",   {}).get("steps", [])),
            ("weekly_care", data.get("weekly_care", [])),
        ]:
            _fams: set = set()
            for _step in (_steps if isinstance(_steps, list) else []):
                _fams.update(infer_active_profile(_step).get("families", set()))
            _section_families[_sec] = _fams

        _all_families = set().union(*_section_families.values())

        for rule in avoid_rules:
            if not isinstance(rule, dict):
                continue
            rule_fams = set(rule.get("families", []))
            if not rule_fams:
                continue
            reason = str(rule.get("reason", "")).strip()
            if not reason:
                continue
            scope = rule.get("scope", "any")
            if scope == "same_session":
                # 単一セクション内で全familiesが揃っている場合のみ警告
                conflict = any(rule_fams.issubset(fams) for fams in _section_families.values() if fams)
            else:
                # scope:"any" — セクション横断で全familiesが存在すれば警告
                conflict = rule_fams.issubset(_all_families)
            if conflict:
                warnings.append(reason)

    # 朝の角質ケア × 日焼け止め注意（成分組み合わせではなくUV対策の注意）
    for step in data.get("morning", {}).get("steps", []):
        ing = normalize_ingredient_tag(step.get("ingredient_focus", ""))
        if ing in ["aha_bha", "pha"]:
            warnings.append("朝に角質ケア系を使う場合は、日焼け止めを丁寧に使うのがおすすめです")
            break

    # 日焼け止め未提案
    morning_categories = [s.get("category", "") for s in data.get("morning", {}).get("steps", [])]
    if "日焼け止め" not in morning_categories:
        warnings.append("日中の肌負担を抑えるため、朝は日焼け止めを取り入れるのがおすすめです")

    # 重複削除
    cleaned = []
    seen = set()
    for w in warnings + data.get("warnings", []):
        text = str(w).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)

    return cleaned

def finalize_result_data(data, user_data):
    if not isinstance(data, dict):
        data = {}

    data = normalize_result_sections(data)
    if "morning" not in data or not isinstance(data.get("morning"), dict):
        data["morning"] = {"steps": []}
    if "night" not in data or not isinstance(data.get("night"), dict):
        data["night"] = {"steps": []}
    if "weekly_care" not in data or not isinstance(data.get("weekly_care"), list):
        data["weekly_care"] = []

    if not isinstance(data["morning"].get("steps"), list):
        data["morning"]["steps"] = []
    if not isinstance(data["night"].get("steps"), list):
        data["night"]["steps"] = []

    data["morning"]["steps"] = [
        finalize_step_data(step, user_data)
        for step in data["morning"]["steps"]
    ]

    data["night"]["steps"] = [
        finalize_step_data(step, user_data)
        for step in data["night"]["steps"]
    ]

    data["weekly_care"] = [
        finalize_step_data(step, user_data)
        for step in data["weekly_care"]
    ]

    # scores
    if not isinstance(data.get("scores"), dict):
        data["scores"] = {}

    score_keys = [
        "oil_balance", "redness", "pores", "hydration", "firmness",
        "acne", "dullness", "barrier", "texture", "tone_evenness"
    ]
    for key in score_keys:
        value = data["scores"].get(key, 0)
        if not isinstance(value, (int, float)):
            value = safe_price(value)
        data["scores"][key] = value

    # 全体スコア
    if not isinstance(data.get("skin_score", 0), (int, float)):
        data["skin_score"] = safe_price(data.get("skin_score", 0))

    # observation
    if not isinstance(data.get("observation"), dict):
        data["observation"] = {}

    obs = data["observation"]

    default_part = {
        "redness": "",
        "pores": "",
        "oiliness": "",
        "dryness": "",
        "texture": "",
        "tone": "",
        "note": ""
    }

    default_cheek = {
        "redness": "",
        "pores": "",
        "acne_marks": "",
        "pigmentation": "",
        "texture": "",
        "note": ""
    }

    if not isinstance(obs.get("front"), dict):
        obs["front"] = dict(default_part)

    if not isinstance(obs.get("left_cheek"), dict):
        obs["left_cheek"] = dict(default_cheek)

    if not isinstance(obs.get("right_cheek"), dict):
        obs["right_cheek"] = dict(default_cheek)

    for key, default_value in default_part.items():
        obs["front"][key] = str(obs["front"].get(key, default_value) or "")

    for cheek_key in ["left_cheek", "right_cheek"]:
        for key, default_value in default_cheek.items():
            obs[cheek_key][key] = str(obs[cheek_key].get(key, default_value) or "")

    obs["symmetry"] = str(obs.get("symmetry", "") or "")
    obs["image_confidence"] = safe_price(obs.get("image_confidence", 0))

    data["observation"] = obs

    # root_causes
    if not isinstance(data.get("root_causes"), list):
        data["root_causes"] = []

    cleaned_causes = []
    for item in data.get("root_causes", []):
        if not isinstance(item, dict):
            continue

        cleaned_causes.append({
            "cause": str(item.get("cause", "") or ""),
            "evidence": str(item.get("evidence", "") or ""),
            "priority": safe_price(item.get("priority", 0)),
            "care_direction": str(item.get("care_direction", "") or "")
        })

    data["root_causes"] = cleaned_causes

    # use_timing 注意書きをステップに付与
    # 美容液の after_serum は同一セクションに他の美容液がある場合のみ表示する
    _morning_serum_count = sum(
        1 for s in data.get("morning", {}).get("steps", [])
        if isinstance(s, dict)
        and normalize_candidate_category(s.get("category", ""), fallback=s.get("category", "")) == "美容液"
    )
    _night_serum_count = sum(
        1 for s in data.get("night", {}).get("steps", [])
        if isinstance(s, dict)
        and normalize_candidate_category(s.get("category", ""), fallback=s.get("category", "")) == "美容液"
    )

    for _section_name, _section_steps in [
        ("morning", data.get("morning", {}).get("steps", [])),
        ("night",   data.get("night",   {}).get("steps", [])),
        ("weekly",  data.get("weekly_care", [])),
    ]:
        _serum_count = _morning_serum_count if _section_name == "morning" else (
            _night_serum_count if _section_name == "night" else 0
        )
        for _step in _section_steps:
            if not isinstance(_step, dict):
                continue
            _timing = _step.get("use_timing", "standard")
            _cat = normalize_candidate_category(_step.get("category", ""), fallback=_step.get("category", ""))
            _default = _CATEGORY_DEFAULT_TIMING.get(_cat, "standard")
            if _timing not in ("standard", "", None) and _timing != _default:
                if _cat == "美容液" and _timing == "after_serum":
                    # 同セクションに複数美容液がある場合のみ「他の美容液の後に」を表示
                    if _serum_count >= 2:
                        _step["timing_note"] = "💡 他の美容液の後にご使用ください"
                    else:
                        _step.pop("timing_note", None)
                else:
                    _step["timing_note"] = _TIMING_NOTES.get(_timing, "")
            else:
                _step.pop("timing_note", None)

    # warnings
    if not isinstance(data.get("warnings"), list):
        data["warnings"] = []
    data["warnings"] = build_rule_based_warnings(data, user_data)
    # budget / age
    data["input_budget"] = safe_price(data.get("input_budget", 0))
    data["total_price"] = safe_price(data.get("total_price", 0))
    data["budget_fit_total"] = safe_price(data.get("budget_fit_total", 0))
    # input_age: フォームの年齢入力を保存（肌年齢との差分表示に使用）
    if not data.get("input_age"):
        try:
            data["input_age"] = int(user_data.get("age") or 0)
        except (ValueError, TypeError):
            data["input_age"] = 0

    if not isinstance(data.get("budget_fit_plan"), dict):
        data["budget_fit_plan"] = {
            "morning": {"steps": []},
            "night": {"steps": []},
            "weekly_care": []
        }
    else:
        budget_plan = data["budget_fit_plan"]

        if not isinstance(budget_plan.get("morning"), dict):
            budget_plan["morning"] = {"steps": []}

        if not isinstance(budget_plan.get("night"), dict):
            budget_plan["night"] = {"steps": []}

        budget_plan["morning"]["steps"] = safe_section_steps(
            budget_plan.get("morning")
        )
        budget_plan["night"]["steps"] = safe_section_steps(
            budget_plan.get("night")
        )
        budget_plan["weekly_care"] = safe_step_list(
            budget_plan.get("weekly_care", [])
        )

        data["budget_fit_plan"] = budget_plan

    # 文字列系
    for key in ["record_date", "analysis_date", "skin_summary", "budget_status"]:
        if key not in data or data[key] is None:
            data[key] = ""
        else:
            data[key] = str(data[key])

    return data

def calculate_skin_score(scores):

    if not isinstance(scores, dict):
        return 0

    weights = {
        "oil_balance": 1.0,
        "redness": 1.1,
        "pores": 1.0,
        "hydration": 1.3,
        "firmness": 1.1,
        "acne": 1.4,
        "dullness": 0.9,
        "barrier": 1.3,
        "texture": 1.0,
        "tone_evenness": 0.9
    }

    total = 0
    total_weight = 0

    for key, weight in weights.items():

        value = safe_int(
            scores.get(
                key,
                0
            )
        )

        value = max(
            0,
            min(
                value,
                100
            )
        )

        total += value * weight
        total_weight += weight

    if total_weight == 0:
        return 0

    return round(
        total / total_weight
    )
def calculate_premium_scores(scores):
    if not isinstance(scores, dict):
        scores = {}

    oil_balance = safe_int(scores.get("oil_balance", 0))
    redness = safe_int(scores.get("redness", 0))
    pores = safe_int(scores.get("pores", 0))
    hydration = safe_int(scores.get("hydration", 0))
    acne = safe_int(scores.get("acne", 0))
    dullness = safe_int(scores.get("dullness", 0))
    barrier = safe_int(scores.get("barrier", 0))
    texture = safe_int(scores.get("texture", 0))
    tone_evenness = safe_int(scores.get("tone_evenness", 0))

    premium_scores = {
        "acne_marks_red": round(
            (redness * 0.65) +
            (acne * 0.35)
        ),
        "pigmentation": round(
            (tone_evenness * 0.55) +
            (dullness * 0.45)
        ),
        "enlarged_pores": round(
            (pores * 0.65) +
            (oil_balance * 0.35)
        ),
        "blackhead_pores": round(
            (pores * 0.45) +
            (oil_balance * 0.35) +
            (dullness * 0.20)
        ),
        "translucency": round(
            (dullness * 0.45) +
            (tone_evenness * 0.35) +
            (hydration * 0.20)
        ),
        "tone_uniformity": round(
            (tone_evenness * 0.55) +
            (redness * 0.25) +
            (dullness * 0.20)
        ),
        "skin_balance": round(
            (hydration * 0.35) +
            (barrier * 0.30) +
            (texture * 0.20) +
            (oil_balance * 0.15)
        )
    }

    return {
        key: max(0, min(value, 100))
        for key, value in premium_scores.items()
    }
# Gemini結果を保存用フォーマットに変換
def normalize_result(raw_data, image_path=""):
    return {
        "record_date": raw_data.get("record_date", datetime.today().strftime("%Y-%m-%d")),
        "analysis_date": raw_data.get("analysis_date", datetime.today().strftime("%Y-%m-%d")),
        "skin_score": calculate_skin_score(
            raw_data.get(
                "scores",
                {}
            )
        ),
        "scores": {
            "oil_balance": raw_data.get("scores", {}).get("oil_balance", 0),
            "redness": raw_data.get("scores", {}).get("redness", 0),
            "pores": raw_data.get("scores", {}).get("pores", 0),
            "hydration": raw_data.get("scores", {}).get("hydration", 0),
            "firmness": raw_data.get("scores", {}).get("firmness", 0),
            "acne": raw_data.get("scores", {}).get("acne", 0),
            "dullness": raw_data.get("scores", {}).get("dullness", 0),
            "barrier": raw_data.get("scores", {}).get("barrier", 0),
            "texture": raw_data.get("scores", {}).get("texture", 0),
            "tone_evenness": raw_data.get("scores", {}).get("tone_evenness", 0)
        },
        "skin_summary": raw_data.get("skin_summary", ""),
        "observation": raw_data.get("observation", {}),
        "root_causes": raw_data.get("root_causes", []),
        "morning": {
            "steps": [
                {
                    **step,
                    "display_role": get_step_display_role(step),
                    "image": step.get("image", "")
                }
                for step in raw_data.get("morning", {}).get("steps", [])
            ]
        },
        "night": {
            "steps": [
                {
                    **step,
                    "display_role": get_step_display_role(step),
                    "image": step.get("image", "")
                }
                for step in raw_data.get("night", {}).get("steps", [])
            ]
        },
        "weekly_care": [
            {
                **step,
                "display_role": get_step_display_role(step),
                "image": step.get("image", "")
            }
            for step in raw_data.get("weekly_care", [])
        ],
        "supplements": [
            {**item, "image": item.get("image", "")}
            for item in raw_data.get("supplements", [])
            if isinstance(item, dict)
        ],
        "beauty_devices": [
            {**item, "image": item.get("image", "")}
            for item in raw_data.get("beauty_devices", [])
            if isinstance(item, dict)
        ],
        "warnings": raw_data.get("warnings", []),
        "improvement_plan": raw_data.get("improvement_plan", {}),
        "routine_strategy": raw_data.get("routine_strategy", {}),
        "weekly_usage_plan": raw_data.get("weekly_usage_plan", []),
        "input_budget": raw_data.get("input_budget", 0),
        "input_age": raw_data.get("input_age", 0),
        "total_price": raw_data.get("total_price", 0),
        "budget_fit_plan": raw_data.get("budget_fit_plan", {}),
        "budget_fit_total": raw_data.get("budget_fit_total", 0),
        "budget_status": raw_data.get("budget_status", "未判定"),
        "score_reasons": raw_data.get("score_reasons", {}),
        "premium_scores": raw_data.get("premium_scores", {}),
        "symmetry_analysis": raw_data.get("symmetry_analysis", {}),
        "skin_age_estimate": raw_data.get("skin_age_estimate", 0),
        "user_id": raw_data.get("user_id", ""),
        "client_ip": raw_data.get("client_ip", ""),
        "image_path": image_path,
        "model": ANALYSIS_MODEL,
        "version": "1.0"
    }
    


# 診断結果を履歴に追加
FREE_HISTORY_LIMIT = 5  # 無料ユーザーの保存上限件数

def append_result(raw_data, image_path="", is_premium=False):
    history = load_results()

    if not isinstance(history, list):
        history = []

    normalized = normalize_result(raw_data, image_path=image_path)

    if not isinstance(normalized, dict):
        normalized = {}

    record_id = normalized.get("id")

    if not record_id:
        record_id = generate_result_id(history)

    record = {
        **normalized,
        "id": record_id,
        "saved_at": (
            datetime.now(
                ZoneInfo("Asia/Tokyo")
            )
            .strftime("%Y-%m-%d %H:%M:%S")
        ),
    }

    history.append(record)
    save_results(history)

    print(f"[RESULT SAVED] id={record_id} user_id={record.get('user_id')!r} client_ip={record.get('client_ip')!r}", flush=True)

    # 無料ユーザーは直近5件のみ保持
    if not is_premium:
        uid = normalized.get("user_id", "")
        trim_results_by_user_id(uid, keep=FREE_HISTORY_LIMIT)

    return record

def update_result_deep_analysis(result_id, deep):

    records = load_results()

    for r in records:

        if r.get("id") == result_id:

            r["deep_analysis"] = deep

            break

    save_results(records)
def safe_int(value, default=0):
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def safe_dict(value):
    return value if isinstance(value, dict) else {}


def safe_list(value):
    return value if isinstance(value, list) else []


def get_score_snapshot(result):
    result = result if isinstance(result, dict) else {}
    scores = safe_dict(result.get("scores"))

    return {
        "skin_score": safe_int(result.get("skin_score", 0)),
        "oil_balance": safe_int(scores.get("oil_balance", 0)),
        "redness": safe_int(scores.get("redness", 0)),
        "pores": safe_int(scores.get("pores", 0)),
        "hydration": safe_int(scores.get("hydration", 0)),
        "firmness": safe_int(scores.get("firmness", 0)),
        "acne": safe_int(scores.get("acne", 0)),
        "dullness": safe_int(scores.get("dullness", 0)),
        "barrier": safe_int(scores.get("barrier", 0)),
        "texture": safe_int(scores.get("texture", 0)),
        "tone_evenness": safe_int(scores.get("tone_evenness", 0)),
    }


def prepare_result_for_view(result):
    if not isinstance(result, dict):
        result = {}

    result = dict(result)

    result["id"] = str(result.get("id", "") or "")
    result["record_date"] = str(result.get("record_date", "") or "")
    result["analysis_date"] = str(result.get("analysis_date", "") or "")
    result["saved_at"] = str(result.get("saved_at", "") or "")
    result["skin_summary"] = str(result.get("skin_summary", "") or "")

    scores = safe_dict(result.get("scores"))
    result["scores"] = {
        "oil_balance": safe_int(scores.get("oil_balance", 0)),
        "redness": safe_int(scores.get("redness", 0)),
        "pores": safe_int(scores.get("pores", 0)),
        "hydration": safe_int(scores.get("hydration", 0)),
        "firmness": safe_int(scores.get("firmness", 0)),
        "acne": safe_int(scores.get("acne", 0)),
        "dullness": safe_int(scores.get("dullness", 0)),
        "barrier": safe_int(scores.get("barrier", 0)),
        "texture": safe_int(scores.get("texture", 0)),
        "tone_evenness": safe_int(scores.get("tone_evenness", 0)),
    }

    result["skin_score"] = safe_int(result.get("skin_score", 0))

    # supplements / beauty_devices が欠落している古いレコードでも安全に扱えるよう正規化
    result["supplements"] = [s for s in (result.get("supplements") or []) if isinstance(s, dict)]
    result["beauty_devices"] = [s for s in (result.get("beauty_devices") or []) if isinstance(s, dict)]

    # improvement_priority が未設定のレコード（機能追加前の旧データ含む）でもスコアから再計算
    if not result.get("improvement_priority"):
        result["improvement_priority"] = build_improvement_priority(result.get("scores", {}))

    return result

def lightweight_result_payload(item):
    """
    DB保存前に、表示に不要な一時データだけ削る。
    result.html / history_detail 表示に必要な情報は残す。
    """
    if not isinstance(item, dict):
        return item

    data = copy.deepcopy(item)

    remove_keys = [
        "raw_response",
        "gemini_response",
        "prompt",
        "debug",
        "debug_info",
        "logs",
        "product_log",
        "market_candidates_raw",
        "rakuten_raw",
        "api_raw",
        "trace",
        "traceback",
        "images",
        "image_data",
        "uploaded_images",
        "image_base64",
        "raw_images",
        "combined_products",
    ]

    for key in remove_keys:
        data.pop(key, None)

    for section in ["morning", "night"]:
        section_data = data.get(section, {})
        if not isinstance(section_data, dict):
            continue

        steps = section_data.get("steps", [])
        if not isinstance(steps, list):
            continue

        for step in steps:
            if isinstance(step, dict):
                step.pop("product_candidates", None)

    weekly_care = data.get("weekly_care", [])
    if isinstance(weekly_care, list):
        for step in weekly_care:
            if isinstance(step, dict):
                step.pop("product_candidates", None)

    for key in ["supplements", "beauty_devices"]:
        for step in (data.get(key) or []):
            if isinstance(step, dict):
                step.pop("product_candidates", None)

    return data

def prepare_step(step):
    if not isinstance(step, dict):
        return {}

    step = dict(step)

    step["product"] = str(step.get("product", "") or "")
    step["category"] = str(step.get("category", "") or "")
    step["purpose"] = str(step.get("purpose", "") or "")
    step["recommend_reason"] = str(step.get("recommend_reason", "") or "")
    step["display_role"] = step.get("display_role") or get_step_display_role(step)

    image = str(step.get("image", "") or "")

    if "/static/images/products/" in image:
        image = ""

    step["image"] = image
    step["price"] = safe_price(step.get("price", step.get("price_ref", 0)))
    step["price_ref"] = safe_price(step.get("price_ref", step["price"]))
    step["estimated_price"] = safe_price(step.get("estimated_price", 0))

    step["rakuten_link"] = str(step.get("rakuten_link", "") or "")
    step["amazon_link"] = str(step.get("amazon_link", "") or "")
    step["product_source"] = str(step.get("product_source", "") or "")
    step["top_candidates"] = safe_list(step.get("top_candidates"))
    step["top_impacts"] = safe_list(step.get("top_impacts"))

    return step


def prepare_result(result):
    if not isinstance(result, dict):
        return {}

    result = dict(result)

    morning = safe_dict(result.get("morning"))
    night = safe_dict(result.get("night"))

    result["morning"] = {
        "steps": [
            prepare_step(step)
            for step in safe_list(morning.get("steps"))
        ]
    }

    result["night"] = {
        "steps": [
            prepare_step(step)
            for step in safe_list(night.get("steps"))
        ]
    }

    result["weekly_care"] = [
        prepare_step(step)
        for step in safe_list(result.get("weekly_care"))
    ]

    return result

from collections import Counter


def iter_selected_products_from_result(result):
    if not isinstance(result, dict):
        return

    sections = [
        ("morning", result.get("morning", {}).get("steps", [])),
        ("night", result.get("night", {}).get("steps", [])),
        ("weekly_care", result.get("weekly_care", [])),
    ]

    for section_name, steps in sections:
        if not isinstance(steps, list):
            continue

        for step in steps:
            if not isinstance(step, dict):
                continue

            product = str(step.get("product", "") or "").strip()
            brand = str(step.get("brand", "") or "").strip()
            category = str(step.get("category", "") or "").strip()

            if not product:
                continue

            yield {
                "product": product,
                "brand": brand,
                "category": category,
                "section": section_name
            }


_RANKING_ALLOWED_CATEGORIES = {
    "クレンジング", "洗顔", "洗顔料", "化粧水", "美容液", "乳液", "クリーム",
    "日焼け止め", "パック", "ピーリング", "サプリメント", "美容機器",
}

def build_product_ranking(results, user_id=None, client_ip=None, limit=20):
    counter = Counter()

    for result in results:
        if not isinstance(result, dict):
            continue

        if user_id and result.get("user_id") != user_id:
            continue

        for item in iter_selected_products_from_result(result):
            cat = item["category"]
            if cat == "導入美容液":
                cat = "美容液"
            if cat == "洗顔":
                cat = "洗顔料"
            if cat not in _RANKING_ALLOWED_CATEGORIES:
                continue
            key = (
                item["brand"],
                item["product"],
                cat
            )
            counter[key] += 1

    ranking = []

    for (brand, product, category), count in counter.most_common(limit):
        ranking.append({
            "brand": brand,
            "product": product,
            "category": category,
            "count": count
        })

    return ranking


def group_ranking_by_category(ranking, per_category_limit=10):
    """ランキングをカテゴリ別にグループ化して返す"""
    groups: dict[str, list] = {}
    for item in ranking:
        cat = item["category"] or "その他"
        groups.setdefault(cat, []).append(item)
    result = []
    for cat in sorted(groups.keys(), key=lambda c: CATEGORY_ORDER.get(c, 99)):
        products = sorted(groups[cat], key=lambda x: -x["count"])[:per_category_limit]
        result.append({"category": cat, "products": products})
    return result


# トップページ
@app.route("/", methods=["GET"])
def home():
    return redirect("/lab")


def translate_to_japanese(data):
   

    return data

def normalize_ai_labels(data):

    def normalize_focus_list(raw_focus):
        if isinstance(raw_focus, str):
            raw_focus = [raw_focus]
        if not isinstance(raw_focus, list):
            return raw_focus

        normalized_focus = []
        for item in raw_focus:
            tag = translate_value(item, AI_INGREDIENT_MAP)
            normalized_focus.append(tag)
        return normalized_focus

    for step in data.get("morning", {}).get("steps", []):
        step["category"] = translate_value(
            step.get("category", ""),
            AI_CATEGORY_MAP
        )
        step["ingredient_focus"] = normalize_focus_list(
            step.get("ingredient_focus", [])
        )

    for step in data.get("night", {}).get("steps", []):
        step["category"] = translate_value(
            step.get("category", ""),
            AI_CATEGORY_MAP
        )
        step["ingredient_focus"] = normalize_focus_list(
            step.get("ingredient_focus", [])
        )

    for step in data.get("weekly_care", []):
        step["category"] = translate_value(
            step.get("category", ""),
            AI_CATEGORY_MAP
        )
        step["ingredient_focus"] = normalize_focus_list(
            step.get("ingredient_focus", [])
        )

    return data
    
def translate_value(text, mapping):
    if not isinstance(text, str):
        return text
    lowered = text.strip().lower()
    return mapping.get(lowered, text)

CATEGORY_ORDER = {
    "クレンジング": 1,
    "洗顔": 2,
    "ピーリング": 2.3,   # 洗顔直後・before_toner(2.5)ゾーン前
    "化粧水": 3,
    "美容液": 4,
    "乳液": 5,
    "クリーム": 6,
    "日焼け止め": 7,
    "パック": 8,
}
def normalize_serum_roles(data):
    booster_keywords = [
        "浸透", "導入", "土台", "なじみ", "ブースト"
    ]

    main_keywords = [
        "毛穴", "赤み", "ニキビ", "美白", "くすみ",
        "ハリ", "エイジング", "シミ", "改善"
    ]

    for section in ["morning", "night"]:
        for step in data.get(section, {}).get("steps", []):
            if step.get("category") != "美容液":
                continue

            purpose = str(step.get("purpose", ""))
            role = step.get("role", "")

            # boosterはかなり厳しく判定
            if any(word in purpose for word in booster_keywords) and not any(word in purpose for word in main_keywords):
                step["role"] = "booster"
            else:
                step["role"] = "main"

    return data

def enforce_booster_night_only(data):
    for step in data.get("morning", {}).get("steps", []):
        if step.get("category") == "美容液" and step.get("role") == "booster":
            # 朝のboosterは削除
            step["remove_flag"] = True

    data["morning"]["steps"] = [
        s for s in data.get("morning", {}).get("steps", [])
        if not s.get("remove_flag")
    ]

    return data

from itertools import combinations


def score_serum_pair_compatibility(a, b):
    profile_a = infer_active_profile(a)
    profile_b = infer_active_profile(b)

    families_a = set(profile_a.get("families", []))
    families_b = set(profile_b.get("families", []))

    score = 0

    # 改善軸が分散しているペアを評価
    if families_a != families_b:
        score += 12

    # VC + レチノイドは朝夜で役割分担しやすい
    if (
        ("vitamin_c" in families_a and "retinoid" in families_b)
        or ("retinoid" in families_a and "vitamin_c" in families_b)
    ):
        score += 15

    # レチノイド + バリア補完
    if (
        ("retinoid" in families_a and "barrier" in families_b)
        or ("barrier" in families_a and "retinoid" in families_b)
    ):
        score += 14

    # 酸 + バリア補完
    if (
        ("aha_bha" in families_a and "barrier" in families_b)
        or ("barrier" in families_a and "aha_bha" in families_b)
    ):
        score += 10

    # 同系統重複は減点
    overlap = families_a & families_b
    score -= len(overlap) * 8

    # レチノイド重複
    if "retinoid" in overlap:
        score -= 18

    # VC重複
    if "vitamin_c" in overlap:
        score -= 10

    # 酸重複
    if "aha_bha" in overlap:
        score -= 18

    # レチノイド × 酸は刺激リスク
    if (
        ("retinoid" in families_a and "aha_bha" in families_b)
        or ("aha_bha" in families_a and "retinoid" in families_b)
    ):
        score -= 25

    # 強刺激同士
    if (
        profile_a.get("irritation_risk") == "high"
        and profile_b.get("irritation_risk") == "high"
    ):
        score -= 25

    return score


def limit_serum_steps(data):
    for section in ["morning", "night"]:
        steps = data.get(section, {}).get("steps", [])

        if not isinstance(steps, list):
            continue

        serum_steps = [
            s for s in steps
            if (
                s.get("category") == "美容液"
                and s.get("role") != "booster"
            )
        ]

        if len(serum_steps) <= 2:
            continue

        best_pair = None
        best_pair_score = -999999

        for a, b in combinations(serum_steps, 2):
            pair_score = (
                safe_float(a.get("final_score", 0))
                + safe_float(b.get("final_score", 0))
                + safe_float(a.get("improve_score", 0))
                + safe_float(b.get("improve_score", 0))
                + score_serum_pair_compatibility(a, b)
            )

            if pair_score > best_pair_score:
                best_pair_score = pair_score
                best_pair = (a, b)

        if not best_pair:
            continue

        keep = set(id(s) for s in best_pair)

        data[section]["steps"] = [
            s for s in steps
            if (
                s.get("category") != "美容液"
                or s.get("role") == "booster"
                or id(s) in keep
            )
        ]

    return data

def limit_booster_steps(data):
    for section in ["morning", "night"]:
        steps = data.get(section, {}).get("steps", [])

        if not isinstance(steps, list):
            continue

        booster_steps = [
            s for s in steps
            if (
                s.get("role") == "booster"
                or s.get("category") in ["ブースター", "導入美容液"]
            )
        ]

        if len(booster_steps) <= 1:
            continue

        booster_sorted = sorted(
            booster_steps,
            key=lambda x: (
                safe_float(x.get("final_score", 0)),
                safe_float(x.get("improve_score", 0)),
                safe_float(x.get("base_score", 0))
            ),
            reverse=True
        )

        keep = id(booster_sorted[0])

        data[section]["steps"] = [
            s for s in steps
            if (
                s not in booster_steps
                or id(s) == keep
            )
        ]

    return data

def safe_float(value):
    try:
        return float(value)
    except Exception:
        return 0.0
def validate_products(products):
    errors = []
    valid_categories = {"クレンジング","洗顔", "化粧水", "美容液", "乳液", "クリーム", "日焼け止め", "パック", "ピーリング"}
    valid_concerns = {"pores", "acne", "redness", "oil_control", "dryness", "barrier", "dullness", "whitening", "aging"}

    for i, p in enumerate(products):
        name = p.get("name", f"index:{i}")

        if p.get("category") not in valid_categories:
            errors.append(f"{name}: category不正 -> {p.get('category')}")

        if not isinstance(p.get("price_ref", 0), (int, float)):
            errors.append(f"{name}: price_refが数値ではない")

        if not isinstance(p.get("active_ingredients", []), list):
            errors.append(f"{name}: active_ingredientsがlistではない")

        if not isinstance(p.get("support_ingredients", []), list):
            errors.append(f"{name}: support_ingredientsがlistではない")

        for c in p.get("concerns", []):
            if c not in valid_concerns:
                errors.append(f"{name}: concern不正 -> {c}")

        for mf in p.get("main_functions", []):
            if mf not in MAIN_FUNCTION_TAGS:
                errors.append(f"{name}: main_function不正 -> {mf}")

    return errors


def validate_and_log_products(products):
    validation_errors = validate_products(products)
    if validation_errors:
        print("=== PRODUCTS VALIDATION ERROR ===")
        for err in validation_errors:
            print(err)

    # 確認用：DBのカテゴリ件数を確認したら削除
    from collections import Counter

    category_counter = Counter()
    normalized_category_counter = Counter()

    for p in products:
        if not isinstance(p, dict):
            continue

        raw_category = p.get("category", "")
        normalized_category = normalize_candidate_category(
            raw_category,
            fallback=raw_category
        )

        category_counter[raw_category] += 1
        normalized_category_counter[normalized_category] += 1

    print("[PRODUCT DB COUNT]", len(products), flush=True)
    print("[PRODUCT RAW CATEGORY COUNT]", dict(category_counter), flush=True)
    print("[PRODUCT NORMALIZED CATEGORY COUNT]", dict(normalized_category_counter), flush=True)

CATEGORY_ORDER = {
    "クレンジング": 1,
    "洗顔": 2,
    "ピーリング": 2.3,   # 洗顔直後・before_toner(2.5)ゾーン前が自然位置
    "化粧水": 3,
    "美容液": 4,
    "乳液": 5,
    "クリーム": 6,
    "日焼け止め": 7,
    "パック": 8,
}

# use_timing → 並び順オフセット（CATEGORY_ORDERを上書き）
# 複数の特殊タイミング商品が共存する場合の位置関係:
#   洗顔(2) → ピーリング標準(2.3) → before_toner(2.5) → 化粧水(3)
#   → after_toner(3.5) → 美容液(4) → after_serum(4.5) → 乳液(5)
#   → クリーム(6) → last(6.9) → 日焼け止め(7) → パック(8)
_TIMING_ORDER_OVERRIDE = {
    "before_toner": 2.5,   # 洗顔後ピーリング(2.3)の次・化粧水(3)の前
    "after_toner":  3.5,   # 化粧水(3)と美容液(4)の間
    "after_serum":  4.5,   # 美容液(4)と乳液(5)の間
    "last":         6.9,   # 日焼け止め(7)の直前
}

# カテゴリのデフォルトタイミング（注意書き表示判定に使用）
_CATEGORY_DEFAULT_TIMING = {
    "クレンジング": "standard",
    "洗顔":       "standard",
    "化粧水":     "standard",
    "美容液":     "after_toner",
    "乳液":       "standard",
    "クリーム":   "standard",
    "日焼け止め": "last",
    "パック":     "standard",
    "ピーリング": "standard",
}

# use_timing → ユーザー向け注意書き
_TIMING_NOTES = {
    "before_toner": "💡 この商品は洗顔後・化粧水の前にご使用ください",
    "after_toner":  "💡 この商品は化粧水の直後にご使用ください",
    "after_serum":  "💡 この商品は美容液の後・乳液の前にご使用ください",
    "last":         "💡 この商品はスキンケアの最後にご使用ください",
}

# 美容液内サブ並び順で使う成分セット
_SERUM_IRRITANT_FOCUS  = {"retinoid", "retinol", "retinal", "aha_bha", "aha", "bha", "pha"}
_SERUM_HYDRATING_FOCUS = {
    "hyaluronic_acid", "hyaluronic", "ceramide", "centella", "cica",
    "panthenol", "squalane", "amino_acid", "glycerin", "collagen",
}
_SERUM_TEXTURE_LIGHT   = {"watery", "light", "essence"}
_SERUM_TEXTURE_HEAVY   = {"cream", "rich", "oil", "balm"}


def _serum_sub_sort_key(step):
    """
    美容液が複数ある場合の同一タイミング内サブ並び順。
    優先ルール（番号が小さいほど優先度高 = 先に使う）:
      ① メーカー推奨 → use_timing で上位解決済み（呼び出し元で処理）
      ② 刺激対策    → irritant_rank: 0=非刺激, 1=刺激系（後回し）
      ③ 成分特性    → ingredient_rank: 0=保湿/導入, 1=機能系, 2=刺激系
      ④ テクスチャ  → texture_rank: 0=軽い, 1=中間, 2=重い
    """
    focuses = set(as_list(step.get("ingredient_focus") or []))

    # ② 刺激成分フラグ
    is_irritant = bool(focuses & _SERUM_IRRITANT_FOCUS)
    irritant_rank = 1 if is_irritant else 0

    # ③ 成分特性
    if is_irritant:
        ingredient_rank = 2
    elif focuses & _SERUM_HYDRATING_FOCUS:
        ingredient_rank = 0
    else:
        ingredient_rank = 1

    # ④ テクスチャ
    texture = str(step.get("texture", "") or "").lower()
    if texture in _SERUM_TEXTURE_LIGHT:
        texture_rank = 0
    elif texture in _SERUM_TEXTURE_HEAVY:
        texture_rank = 2
    else:
        texture_rank = 1

    return (irritant_rank, ingredient_rank, texture_rank)


def step_sort_key(step):
    """
    全ステップの並び順キーを返す。
    タプルは常に 5 要素: (position, irritant_rank, ingredient_rank, texture_rank, priority)
    美容液のみ ②③④ にサブキーが入り、他カテゴリは (0, 0, 0) で埋める。
    """
    if not isinstance(step, dict):
        return (99, 0, 0, 0, 999)

    category = normalize_candidate_category(
        step.get("category", ""),
        fallback=step.get("category", "")
    )

    role = step.get("role")
    priority = step.get("priority", 999)
    use_timing = step.get("use_timing", "standard")

    # クレンジング・洗顔は use_timing に関わらず常に先頭順序を維持する
    if category in ("クレンジング", "洗顔"):
        return (CATEGORY_ORDER.get(category, 99), 0, 0, 0, priority)

    # use_timing による明示的な順序上書き
    if use_timing in _TIMING_ORDER_OVERRIDE:
        # 美容液に before_toner が付いていても化粧水の後（after_toner 相当）に配置
        pos = (_TIMING_ORDER_OVERRIDE["after_toner"]
               if (category == "美容液" and use_timing == "before_toner")
               else _TIMING_ORDER_OVERRIDE[use_timing])
        if category == "美容液":
            sub = _serum_sub_sort_key(step)
            return (pos, *sub, priority)
        return (pos, 0, 0, 0, priority)

    # 後方互換: role=booster は after_toner 相当
    if role == "booster":
        return (3.5, 0, 0, 0, priority)

    base_order = CATEGORY_ORDER.get(category, 99)

    # 美容液はサブキーで詳細ソート
    if category == "美容液":
        sub = _serum_sub_sort_key(step)
        return (base_order, *sub, priority)

    return (base_order, 0, 0, 0, priority)

def sort_steps(data):
    if "morning" in data and "steps" in data["morning"]:
        data["morning"]["steps"].sort(key=step_sort_key)

    if "night" in data and "steps" in data["night"]:
        data["night"]["steps"].sort(key=step_sort_key)

    if "weekly_care" in data and isinstance(data["weekly_care"], list):
        data["weekly_care"].sort(key=step_sort_key)

    return data


def extract_user_data(request):
    return {
        "age": request.form.get("age", ""),
        "oil": request.form.get("oil_status", ""),
        "sens": request.form.get("sensitivity", ""),
        "exp": request.form.get("retinol_exp", ""),
        "budget": request.form.get("budget", ""),
        "concerns": request.form.getlist("concerns"),
        "record_date": datetime.today().strftime("%Y-%m-%d")
    }


def resize_for_gemini(file, max_size=512):
    img = Image.open(io.BytesIO(file.read()))
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")

    img.thumbnail((max_size, max_size))

    return img.copy()


def check_image_quality(file_storage):
    """(ok: bool, message: str) を返す。エラー時は (True, "") でスキップ。"""
    try:
        file_storage.seek(0)
        img = Image.open(io.BytesIO(file_storage.read())).convert("L")
        file_storage.seek(0)

        gray = np.array(img, dtype=np.float32)

        brightness = float(gray.mean())
        if brightness < 35:
            return False, "画像が暗すぎます。明るい場所で撮り直してください。"
        if brightness > 240:
            return False, "画像が明るすぎます。直射日光を避けて撮り直してください。"

        edges = np.array(img.filter(ImageFilter.FIND_EDGES), dtype=np.float32)
        sharpness = float(edges.mean())
        if sharpness < 4.0:
            return False, "画像がぼやけています。カメラを安定させて撮り直してください。"

        return True, ""
    except Exception:
        return True, ""


def load_uploaded_images(request):
    front_file = request.files.get("front_photo")
    left_file = request.files.get("left_photo")
    right_file = request.files.get("right_photo")

    if not front_file or front_file.filename == "":
        raise ValueError("正面画像を選択してください")

    if not left_file or left_file.filename == "":
        raise ValueError("左頬画像を選択してください")

    if not right_file or right_file.filename == "":
        raise ValueError("右頬画像を選択してください")

    for file, label in [(front_file, "正面"), (left_file, "左頬"), (right_file, "右頬")]:
        ok, msg = check_image_quality(file)
        if not ok:
            raise ValueError(f"【{label}画像】{msg}")

    front_img = resize_for_gemini(front_file)
    left_img = resize_for_gemini(left_file)
    right_img = resize_for_gemini(right_file)

    return front_img, left_img, right_img

def pick_uploaded_file(request, normal_name, camera_name):
    normal_file = request.files.get(normal_name)
    camera_file = request.files.get(camera_name)

    if camera_file and camera_file.filename != "":
        return camera_file

    if normal_file and normal_file.filename != "":
        return normal_file

    return None

def get_analysis_schema_phase1():
    """Phase 1: 肌スコア・分析・改善方針のみ（画像3枚で呼ぶ）出力は小さい"""
    _score_keys = ["oil_balance","redness","pores","hydration","firmness","acne","dullness","barrier","texture","tone_evenness"]
    score_obj = {
        "type": "object",
        "properties": {k: {"type": "integer"} for k in _score_keys},
        "required": _score_keys
    }
    reason_obj = {
        "type": "object",
        "properties": {k: {"type": "string"} for k in _score_keys},
        "required": _score_keys
    }
    return {
        "type": "object",
        "properties": {
            "skin_summary": {"type": "string"},
            "scores": score_obj,
            "score_reasons": reason_obj,
            "symmetry_analysis": {
                "type": "object",
                "properties": {
                    "score": {"type": "integer"},
                    "summary": {"type": "string"},
                    "left_tendency": {"type": "string"},
                    "right_tendency": {"type": "string"}
                },
                "required": ["score","summary","left_tendency","right_tendency"]
            },
            "skin_age_estimate": {"type": "integer"},
            "improvement_plan": {
                "type": "object",
                "properties": {
                    "priority_concerns": {"type": "array", "items": {"type": "string"}},
                    "key_ingredients": {"type": "array", "items": {"type": "string"}},
                    "care_direction": {"type": "string"}
                },
                "required": ["priority_concerns","key_ingredients","care_direction"]
            },
            "moisture_plan": {
                "type": "object",
                "properties": {
                    "moisture_level": {"type": "string"},
                    "need_emulsion": {"type": "boolean"},
                    "need_cream": {"type": "boolean"},
                    "need_double_moisture": {"type": "boolean"},
                    "reason": {"type": "string"}
                },
                "required": ["moisture_level","need_emulsion","need_cream","need_double_moisture","reason"]
            },
            "ai_improvement_strategy": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "rank":   {"type": "integer"},
                        "item":   {"type": "string"},
                        "score":  {"type": "integer"},
                        "reason": {"type": "string"}
                    },
                    "required": ["rank","item","score","reason"]
                }
            }
        },
        "required": ["skin_summary","scores","score_reasons","symmetry_analysis","skin_age_estimate","improvement_plan","moisture_plan","ai_improvement_strategy"]
    }


def get_analysis_schema_phase2():
    """Phase 2: ルーティン・商品候補のみ（テキストのみで呼ぶ）"""
    product_candidate_schema = {
        "type": "object",
        "properties": {
            "brand": {"type": "string"},
            "name": {"type": "string"},
            "category": {"type": "string"},
            "confidence": {"type": "integer"},
            "release_status": {"type": "string"},
            "active_ingredients": {"type": "array", "items": {"type": "string"}},
            "support_ingredients": {"type": "array", "items": {"type": "string"}},
            "concerns": {"type": "array", "items": {"type": "string"}},
            "skin_types": {"type": "array", "items": {"type": "string"}},
            "sensitive_ok": {"type": "string"},
            "retinol_level": {"type": "integer"},
            "main_functions": {"type": "array", "items": {"type": "string"}},
            "ingredient_focus": {"type": "array", "items": {"type": "string"}},
            "ingredient_strength": {"type": "object"},
            "formulation": {"type": "array", "items": {"type": "string"}},
            "technology": {"type": "array", "items": {"type": "string"}},
            "texture": {"type": "string"},
            "contraindications": {"type": "array", "items": {"type": "string"}},
            "availability_japan": {"type": "array", "items": {"type": "string"}},
            "uv_level": {"type": "object"},
            "reason": {"type": "string"}
        },
        "required": [
            "brand","name","category","confidence","release_status",
            "active_ingredients","support_ingredients","concerns","skin_types",
            "sensitive_ok","retinol_level","main_functions","ingredient_focus",
            "ingredient_strength","formulation","technology","texture",
            "contraindications","availability_japan","uv_level","reason"
        ]
    }
    step_schema = {
        "type": "object",
        "properties": {
            "category": {"type": "string"},
            "role": {"type": "string"},
            "purpose": {"type": "string"},
            "ingredient_focus": {"type": "string"},
            "risk_note": {"type": "string"},
            "priority": {"type": "integer"},
            "use_days": {"type": "array", "items": {"type": "string"}},
            "use_timing": {"type": "string"},
            "product_candidates": {"type": "array", "items": product_candidate_schema},
            "selection_reason": {"type": "string"}
        },
        "required": ["category","role","purpose","ingredient_focus","risk_note","priority","use_days","use_timing","product_candidates"]
    }
    routine_strategy_schema = {
        "type": "object",
        "properties": {
            "strategy_type": {"type": "string"},
            "overall_policy": {"type": "string"},
            "morning_policy": {"type": "string"},
            "night_policy": {"type": "string"},
            "weekly_policy": {"type": "string"},
            "active_care_frequency": {"type": "string"},
            "recovery_care_frequency": {"type": "string"},
            "rotation_targets": {"type": "array", "items": {"type": "string"}},
            "avoid_combinations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "families": {"type": "array", "items": {"type": "string"}},
                        "scope": {"type": "string"},
                        "reason": {"type": "string"},
                        "severity": {"type": "string"}
                    },
                    "required": ["families","reason","severity"]
                }
            },
            "synergy_combinations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "families": {"type": "array", "items": {"type": "string"}},
                        "reason": {"type": "string"},
                        "bonus": {"type": "string"}
                    },
                    "required": ["families","reason","bonus"]
                }
            },
            "morning_order": {"type": "array", "items": {"type": "string"}},
            "night_order": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"}
        },
        "required": [
            "strategy_type","overall_policy","morning_policy","night_policy","weekly_policy",
            "active_care_frequency","recovery_care_frequency","rotation_targets",
            "avoid_combinations","synergy_combinations","morning_order","night_order","reason"
        ]
    }
    supplement_schema = {
        "type": "object",
        "properties": {
            "category":         {"type": "string"},
            "product":          {"type": "string"},
            "brand":            {"type": "string"},
            "purpose":          {"type": "string"},
            "ingredient_focus": {"type": "array", "items": {"type": "string"}},
            "reason":           {"type": "string"},
            "timing":           {"type": "string"},
            "caution":          {"type": "string"},
            "priority":         {"type": "integer"}
        },
        "required": ["category","product","purpose","ingredient_focus","reason","timing","caution","priority"]
    }
    device_schema = {
        "type": "object",
        "properties": {
            "category":        {"type": "string"},
            "product":         {"type": "string"},
            "brand":           {"type": "string"},
            "purpose":         {"type": "string"},
            "device_function": {"type": "string"},
            "frequency":       {"type": "string"},
            "reason":          {"type": "string"},
            "priority":        {"type": "integer"}
        },
        "required": ["category","product","purpose","device_function","frequency","reason","priority"]
    }
    return {
        "type": "object",
        "properties": {
            "morning": {
                "type": "object",
                "properties": {"steps": {"type": "array", "items": step_schema}},
                "required": ["steps"]
            },
            "night": {
                "type": "object",
                "properties": {"steps": {"type": "array", "items": step_schema}},
                "required": ["steps"]
            },
            "weekly_care":    {"type": "array", "items": step_schema},
            "warnings":       {"type": "array", "items": {"type": "string"}},
            "routine_strategy": routine_strategy_schema,
            "supplements":    {"type": "array", "items": supplement_schema},
            "beauty_devices": {"type": "array", "items": device_schema}
        },
        "required": ["morning","night","weekly_care","warnings","routine_strategy","supplements","beauty_devices"]
    }


def get_analysis_schema():
    product_candidate_schema = {
        "type": "object",
        "properties": {
            "brand": {"type": "string"},
            "name": {"type": "string"},
            "category": {"type": "string"},
            "confidence": {"type": "integer"},
            "release_status": {"type": "string"},
            "active_ingredients": {
                "type": "array",
                "items": {"type": "string"}
            },
            "support_ingredients": {
                "type": "array",
                "items": {"type": "string"}
            },
            "concerns": {
                "type": "array",
                "items": {"type": "string"}
            },
            "skin_types": {
                "type": "array",
                "items": {"type": "string"}
            },
            "sensitive_ok": {"type": "string"},
            "retinol_level": {"type": "integer"},
            "main_functions": {
                "type": "array",
                "items": {"type": "string"}
            },
            "ingredient_focus": {
                "type": "array",
                "items": {"type": "string"}
            },
            "ingredient_strength": {
                "type": "object"
            },
            "formulation": {
                "type": "array",
                "items": {"type": "string"}
            },
            "technology": {
                "type": "array",
                "items": {"type": "string"}
            },
            "texture": {"type": "string"},
            "contraindications": {
                "type": "array",
                "items": {"type": "string"}
            },
            "availability_japan": {
                "type": "array",
                "items": {"type": "string"}
            },
            "uv_level": {
                "type": "object"
            },
            "reason": {"type": "string"}
        },
        "required": [
            "brand",
            "name",
            "category",
            "confidence",
            "release_status",
            "active_ingredients",
            "support_ingredients",
            "concerns",
            "skin_types",
            "sensitive_ok",
            "retinol_level",
            "main_functions",
            "ingredient_focus",
            "ingredient_strength",
            "formulation",
            "technology",
            "texture",
            "contraindications",
            "availability_japan",
            "uv_level",
            "reason"
        ]
    }

    step_schema = {
        "type": "object",
        "properties": {
            "category": {"type": "string"},
            "role": {"type": "string"},
            "purpose": {"type": "string"},
            "ingredient_focus": {"type": "string"},
            "risk_note": {"type": "string"},
            "priority": {"type": "integer"},
            "use_days": {
                "type": "array",
                "items": {"type": "string"}
            },
            "product_candidates": {
                "type": "array",
                "items": product_candidate_schema
            },
            "selection_reason": {"type": "string"}
        },
        "required": [
            "category",
            "role",
            "purpose",
            "ingredient_focus",
            "risk_note",
            "priority",
            "use_days",
            "product_candidates"
        ]
    }

    return {
        "type": "object",
        "properties": {
            "skin_score": {"type": "integer"},
            "skin_summary": {"type": "string"},
            "scores": {
                "type": "object",
                "properties": {
                    "oil_balance": {"type": "integer"},
                    "redness": {"type": "integer"},
                    "pores": {"type": "integer"},
                    "hydration": {"type": "integer"},
                    "firmness": {"type": "integer"},
                    "acne": {"type": "integer"},
                    "dullness": {"type": "integer"},
                    "barrier": {"type": "integer"},
                    "texture": {"type": "integer"},
                    "tone_evenness": {"type": "integer"},
                },
                "required": [
                    "oil_balance",
                    "redness",
                    "pores",
                    "hydration",
                    "firmness",
                    "acne",
                    "dullness",
                    "barrier",
                    "texture",
                    "tone_evenness",
                ]
            },
            "skin_summary": {"type": "string"},
            "morning": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": step_schema
                    }
                },
                "required": ["steps"]
            },
            "night": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": step_schema
                    }
                },
                "required": ["steps"]
            },
            "weekly_care": {
                "type": "array",
                "items": step_schema
            },
            "warnings": {
                "type": "array",
                "items": {"type": "string"}
            },
            "improvement_plan": {
                "type": "object",
                "properties": {
                    "priority_concerns": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "key_ingredients": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "care_direction": {"type": "string"}
                },
                "required": [
                    "priority_concerns",
                    "key_ingredients",
                    "care_direction"
                ]
            },
            "moisture_plan": {
                "type": "object",
                "properties": {
                    "moisture_level": {"type": "string"},
                    "need_emulsion": {"type": "boolean"},
                    "need_cream": {"type": "boolean"},
                    "need_double_moisture": {"type": "boolean"},
                    "reason": {"type": "string"}
                },
                "required": [
                    "moisture_level",
                    "need_emulsion",
                    "need_cream",
                    "need_double_moisture",
                    "reason"
                ]
            },

            "routine_strategy": {
                "type": "object",
                "properties": {
                    "strategy_type": {"type": "string"},
                    "overall_policy": {"type": "string"},
                    "morning_policy": {"type": "string"},
                    "night_policy": {"type": "string"},
                    "weekly_policy": {"type": "string"},
                    "active_care_frequency": {"type": "string"},
                    "recovery_care_frequency": {"type": "string"},
                    "rotation_targets": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "avoid_combinations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "families": {
                                    "type": "array",
                                    "items": {"type": "string"}
                                },
                                "scope": {"type": "string"},
                                "reason": {"type": "string"},
                                "severity": {"type": "string"}
                            },
                            "required": ["families", "reason", "severity"]
                        }
                    },
                    "synergy_combinations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "families": {
                                    "type": "array",
                                    "items": {"type": "string"}
                                },
                                "reason": {"type": "string"},
                                "bonus": {"type": "string"}
                            },
                            "required": ["families", "reason", "bonus"]
                        }
                    },
                    "morning_order": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "night_order": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "reason": {"type": "string"}
                },
                "required": [
                    "strategy_type",
                    "overall_policy",
                    "morning_policy",
                    "night_policy",
                    "weekly_policy",
                    "active_care_frequency",
                    "recovery_care_frequency",
                    "rotation_targets",
                    "avoid_combinations",
                    "synergy_combinations",
                    "morning_order",
                    "night_order",
                    "reason"
                ]
            },

            "symmetry_analysis": {
                "type": "object",
                "properties": {
                    "score": {"type": "integer"},
                    "summary": {"type": "string"},
                    "left_tendency": {"type": "string"},
                    "right_tendency": {"type": "string"}
                },
                "required": [
                    "score",
                    "summary",
                    "left_tendency",
                    "right_tendency"
                ]
            },
            "skin_age_estimate": {"type": "integer"},
        },
        "required": [
            "skin_score",
            "skin_summary",
            "scores",
            "morning",
            "night",
            "weekly_care",
            "warnings",
            "improvement_plan",
            "moisture_plan",
            "routine_strategy",
            "symmetry_analysis",
            "skin_age_estimate",
        ]
    }

def build_analysis_prompt_phase1(user_data):
    """Phase 1: 肌スコア・分析・改善方針のみ。画像3枚で呼ぶ。出力は小さい。"""
    _concerns_ja = ', '.join([_CONCERN_LABELS_JA.get(c, c) for c in (user_data.get('concerns') or [])]) or '未回答'
    return f"""肌分析AIです。3枚の肌画像（1:正面 2:左頬 3:右頬）を分析し、肌スコアと改善方針のみJSONで返す。ルーティン・商品候補は出力しない。

【ユーザー情報】
年齢:{user_data['age']} 皮脂:{user_data['oil']} 敏感度:{user_data['sens']} 予算:{user_data['budget']}
悩み:{_concerns_ja}

【スコア(0-100)】画像の視覚的事実のみ採点。推測・主観禁止。同一画像→必ず同一値。
各スコアは下記の視覚的アンカーに忠実に対応させること。

acne(ニキビ・炎症丘疹数):
  90:丘疹・膿疱皆無 75:1-2個の小さな赤み 55:3-5個または範囲広め 35:6個以上/広域炎症 20以下:多発・化膿状態

redness(赤みの面積・濃さ):
  90:均一肌色/赤みなし 75:頬に薄い部分的赤み 55:両頬に明確な赤み 35:広域/濃い赤み 20以下:全面的に強い赤み

pores(毛穴の開き・詰まり):
  90:ほぼ不可視 75:鼻周りのみ軽微 55:頬・鼻に明確な開き/詰まりあり 35:広範囲・黒ずみあり 20以下:著しく目立つ

hydration(ツヤ・水分感・光反射):
  90:全体に均一なツヤ/ぷりっとした水分感 75:ツヤあるが部分的にくすみ 55:つや消し/皮脂テカりのみ 35:乾燥感/粉ふき 20以下:著しい乾燥/ごわつき

oil_balance(皮脂の均一さ・テカり):
  90:全体均一/過剰テカりなし 75:Tゾーンに軽いテカり 55:顔全体にテカり/部分的乾燥 35:著しい皮脂分泌 20以下:全面油分過多

firmness(ハリ・弾力・たるみ): 年齢基準=10代後半〜20代前半:85-95、30代:65-80、40代:50-70、50代以上:35-60
  90:頬ふっくら/輪郭くっきり 75:若干の緩み感 55:頬たるみ/フェイスライン乱れ 35:明確なたるみ 20以下:深刻なたるみ/深いしわ

dullness(くすみ・透明感の欠如):
  90:透明感あり/顔色明るい 75:部分的なくすみ 55:全体的にくすんだ印象 35:強いくすみ/黄ばみ 20以下:暗く沈んだ顔色

barrier(バリア・肌表面の荒れ・ザラつき):
  90:滑らか/荒れなし 75:部分的ざらつき/軽い乾燥感 55:広域荒れ/キメ崩れ 35:皮むけ・赤みを伴う荒れ 20以下:深刻な荒れ/炎症性荒れ

texture(キメの細かさ・均一さ):
  90:均一なきめ細かさ 75:部分的なキメの乱れ 55:キメが荒い 35:ざらつき・産毛が目立つ 20以下:著しくキメが荒い

tone_evenness(色ムラ・シミ・色素沈着):
  90:均一な肌色 75:薄いシミ/軽い色ムラ 55:目立つシミが複数/広域色ムラ 35:多数のシミ/色素沈着 20以下:著しいシミ/広域の濃い色ムラ

【スコア間の整合性ルール】
hydration≤50→barrier≤65が自然 / acne≤50→oil_balance≤65が自然 / dullness≤50→tone_evenness≤70が自然
全スコア75以上になる場合は整合性を再確認すること。

score_reasons:各スコアの具体的観察を1文(15-30字)で。部位・数量を含めること(例:「右頬に3個の赤丘疹」「Tゾーンのみ軽テカり」「両頬全体にくすみ」)。

symmetry_analysis: score(0-100,左右差少=高)/summary/left_tendency/right_tendency。不明は控えめに。
skin_age_estimate: 画像から15-70の整数で推定。
skin_summary: 肌状態の総合コメント(30-60字)。
improvement_plan: priority_concerns(配列)/key_ingredients(配列)/care_direction(短文)
moisture_plan: moisture_level/need_emulsion(bool)/need_cream(bool)/need_double_moisture(bool)/reason
ai_improvement_strategy: 改善優先順位を戦略的に10項目分出力。スコアの低さだけでなく以下を考慮して順位を決定すること。
  ・項目間の関係性（例: バリアが低いと他の刺激成分が全て逆効果になる→バリアを最優先）
  ・刺激リスク（バリア・保湿が低い状態でレチノール・AHA等を使うとリスクが高い）
  ・相乗効果（例: 保湿改善→化粧水の浸透性向上→後続の美容液の効果増幅）
  ・ユーザーの悩みとの一致度
  各itemはスコアラベル名(日本語)、scoreはそのスコア値、reason は「なぜその順番か」を30〜50字で具体的に記述。

【supplements】
スキンケアのトータルコーディネートとして、内側からの補完が有効な場合に提案。不要なら[]。
提案数は肌状態・悩みに応じて柔軟に決める（件数制限なし）。
提案条件: ①外用スキンケアだけでは補完困難な悩み(コラーゲン生成・抗酸化・抗糖化等) ②既存ルーティンと成分が重複せず相乗効果が見込める ③肌スコアや悩みに明確な根拠がある
禁止: スキンケアと全く同じ成分の重複提案・根拠のない漠然とした提案

【サプリメント安全基準（複数提案時は必ず全項目確認）】
①成分重複チェック: 複数サプリにビタミンC/B群/亜鉛/ビタミンA/Eが重複しないか確認。
  過剰摂取リスクが特に高い成分（ビタミンA・E・亜鉛・セレン・鉄）はサプリ間で重複提案禁止。
②脂溶性ビタミン注意: ビタミンA・D・E・Kは体内蓄積リスクあり。
  複数サプリからの重複摂取になる場合は提案を絞ること。
③ミネラル吸収干渉: 鉄×亜鉛、鉄×カルシウム、亜鉛×銅は同時摂取で吸収率が低下。
  これらを同時提案する場合は必ずtimingを分ける（例: 亜鉛→朝食後、鉄→就寝前）。
④薬との相互作用: 代表的な禁忌（抗凝固薬×ビタミンK、多くの医薬品×セントジョーンズワート等）を
  cautionフィールドに明記。一般ユーザーへの注意喚起として、該当する場合は医師への相談を促す文を含める。

- category: "サプリメント" (固定)
- product: 楽天で検索可能な具体名 (例: "ビタミンC 1000mg", "コラーゲンペプチド", "ナイアシンアミド サプリ", "亜鉛 サプリ", "アスタキサンチン")
- brand: ブランド名 (不明なら "")
- purpose: 肌への期待効果 (30字以内)
- ingredient_focus: 主要成分タグ配列 (例: ["vitamin_c"] ["collagen"] ["niacinamide"] ["zinc"] ["astaxanthin"])
- reason: 既存スキンケアルーティンとの相性・補完価値を具体的に (50字以内)
- timing: 摂取タイミング (例: "朝食後", "就寝前") ※ミネラル干渉がある場合は必ず分ける
- caution: このサプリ固有の注意点（過剰摂取リスク・吸収干渉・薬との相互作用等、なければ ""）(40字以内)
- priority: 1=最優先 2=次点以降

【beauty_devices】
肌スコアや悩みから判断して本当に必要な場合のみ提案。不要なら[]。
【提案基準（すべて考慮して判断）】
①スキンケア単独では改善が遅い項目（毛穴・ハリ・キメ等）に機器の物理的効果が明確に有効
②現在のルーティン（美容液・クリーム等）の効果を技術的に増強できる根拠がある
③肌スコアの該当項目への改善効果が裏付けられた機器のみ
【禁止】
・相性の悪い組み合わせ（例: レチノール使用直後の摩擦系機器・EMS等の刺激系）
・1つのスコア課題に対して複数の機器を重複提案（1課題=1機器が原則）
・根拠のない漠然とした提案
- category: "美容機器" (固定)
- product: 楽天で検索可能な機器タイプ名 (例: "LEDマスク", "超音波美顔器", "EMSフェイスケア", "RF美顔器", "イオン導入器", "ローラー美顔器")
- brand: ブランド名 (不明なら "")
- purpose: 期待される肌効果 (30字以内)
- device_function: 機能説明 (例: "赤色LED照射", "超音波振動", "EMS微電流", "RF高周波", "イオン導入")
- reason: 使用中スキンケアとの相性・増強効果の根拠を具体的に (50字以内)
- frequency: 推奨頻度 (例: "週3〜4回・スキンケア後", "毎日・朝スキンケア後")
- priority: 1=最優先 2=次点以降

JSONのみ返す。説明・Markdown・前置き禁止。JSONキーは英語、値は日本語。"""


def build_analysis_prompt_phase2(user_data, phase1):
    """Phase 2: ルーティン・商品候補。画像不要、phase1結果をコンテキストに使う。"""
    _concerns_ja = ', '.join([_CONCERN_LABELS_JA.get(c, c) for c in (user_data.get('concerns') or [])]) or '未回答'
    scores = phase1.get("scores", {})
    ip = phase1.get("improvement_plan", {})
    mp = phase1.get("moisture_plan", {})
    ai_strategy = phase1.get("ai_improvement_strategy") or []
    _sk = ["oil_balance","redness","pores","hydration","firmness","acne","dullness","barrier","texture","tone_evenness"]
    _score_line = " ".join(f"{k}={scores.get(k,'?')}" for k in _sk)
    _pc = "/".join(ip.get("priority_concerns") or []) or "未設定"
    _ki = "/".join(ip.get("key_ingredients") or []) or "未設定"
    _cd = ip.get("care_direction") or "未設定"
    _ml = mp.get("moisture_level","?")
    _em = mp.get("need_emulsion","?")
    _cr = mp.get("need_cream","?")
    # ai_improvement_strategy を番号付きリストに整形
    if ai_strategy:
        _strategy_lines = "\n".join(
            f"  {s.get('rank','?')}. {s.get('item','?')}(スコア{s.get('score','?')}) — {s.get('reason','')}"
            for s in ai_strategy[:10]
        )
    else:
        _strategy_lines = "  （データなし）"

    # スコアレベル判定（サプリ・美容機器の提案入口）
    def _score_level(v):
        if v is None: return "不明"
        v = int(v)
        if v >= 80: return "原則不要(80以上)"
        if v >= 60: return "必要性が高い場合のみ検討(60-79)"
        if v >= 40: return "積極的に検討(40-59)"
        return "優先的に検討(39以下)"

    _all_score_vals = [scores.get(k) for k in _sk if scores.get(k) is not None]
    _min_score = min(_all_score_vals) if _all_score_vals else None
    _device_keys = ["pores", "firmness", "texture", "acne", "tone_evenness", "dullness"]
    _device_vals = [scores.get(k) for k in _device_keys if scores.get(k) is not None]
    _min_device_score = min(_device_vals) if _device_vals else None

    _supp_level  = _score_level(_min_score)
    _device_level = _score_level(_min_device_score)
    return f"""日本の市販スキンケアと肌分析に詳しい美容アドバイザーです。
肌分析済みの結果をもとに、ルーティン構成と商品候補のみJSONで返す。画像分析は完了済み。

【ユーザー情報】
年齢:{user_data['age']} 皮脂:{user_data['oil']} 敏感度:{user_data['sens']} レチノール経験:{user_data['exp']} 予算:{user_data['budget']}
悩み:{_concerns_ja}
※悩みを優先してステップ構成する。

【肌スコア(画像分析済)】
{_score_line}

【改善優先順位（AI戦略分析・この順に商品・成分を優先して構成すること）】
{_strategy_lines}

【改善方針(分析済)】
優先事項:{_pc}
主要成分:{_ki}
ケア方針:{_cd}
保湿:moisture_level={_ml} need_emulsion={_em} need_cream={_cr}

【カテゴリ固定】
クレンジング/洗顔/化粧水/美容液/乳液/クリーム/日焼け止め/パック/ピーリング のみ。
role: main/booster のみ。

【morning必須カテゴリ】
洗顔・化粧水・日焼け止めは必ずmorning stepsに含める（省略禁止）。
美容液は原則morning stepsに含める（有効成分による日中ケアが基本）。肌悩みが特になく成分が全く不要な場合のみ省略可。
乳液・クリームは保湿必要度（moisture_plan）に応じて追加する。

【ingredient_focus候補】
ビタミンC/ナイアシンアミド/レチノール/レチナール/アゼライン酸/トラネキサム酸/PDRN/ペプチド/セラミド/ヒアルロン酸/CICA/ドクダミ/AHA/BHA/PHA/UV防御/低刺激

【use_timing（重要）】
各stepに必ず設定。その商品カテゴリの一般的な使用順序と異なる場合に正しいタイミングを指定する。
- "standard": カテゴリ標準順序（大半のケース）
- "before_toner": 洗顔後・化粧水前（例: ブースター美容液、ワンバイコーセーセラムヴェール等）
- "after_toner": 化粧水の直後（標準的な美容液はこれ、または standard でよい）
- "after_serum": 美容液の後・乳液前（特殊クリーム等）
- "last": ルーティン最後（日焼け止めは通常これ）
美容液を化粧水前に使う商品、化粧水後だが乳液より先に使うクリーム等、商品固有の指示がある場合はstandardを選ばず正確なタイミングを指定すること。

【美容液が複数ある場合のuse_timing（重要）】
同じ朝/夜に美容液が2つ以上ある場合、以下のルールで use_timing を指定して使用順を明確にすること:
・保湿・導入系（ヒアルロン酸・セラミド・CICA・パンテノール等）→ "after_toner"（化粧水直後）
・機能・美白系（ビタミンC・ナイアシンアミド・トラネキサム酸・ペプチド等）→ "standard"
・刺激系（レチノール・レチナール・AHA・BHA・PHA等）→ "after_serum"（他の美容液の後）
→ 同じ use_timing の美容液は、テクスチャが軽い順（watery < essence < gel < cream）に自動整列される。

【use_days】
night・weekly_careの各stepに必ず設定。morning=[]固定。
基本方針: use_days=[]（毎日）が原則。頻度制限は成分の種類だけで機械的に決めず、以下の要素を総合的に判断すること。

【頻度制限の判断基準（個別評価）】
以下をすべて考慮した上で、毎日使用可能か・制限が必要かを商品ごとに個別判断する:
① 濃度: 低濃度（レチノール0.025〜0.1%相当、低刺激処方）→ 毎日可能なケースが多い
② 緩和成分: セラミド・ナイアシンアミド・ヒアルロン酸・スクワラン・パンテノール等のバリア補修・保湿成分が主体または十分配合 → 刺激が緩和され毎日使用可能になりやすい
③ ルーティン全体の刺激バランス: 同じ夜ルーティンに高濃度AHA/BHA・強力ビタミンC等の刺激成分が重なる場合は制限を検討
④ ユーザーのレチノール経験（retinol_exp）: 慣れている→毎日可能なケース増。初心者→初期は慎重に

【判断例】
- レチノール低濃度＋セラミド豊富な処方 → use_days=[]（毎日）
- レチノール中〜高濃度＋緩和成分少ない → use_days=["月","水","金"]等
- 高濃度AHA/BHA単独 → use_days=["火","金"]等（連続禁止）
- 低濃度AHA＋保湿成分豊富 → use_days=[]または["月","水","金","日"]等
- 化粧水・乳液・クリーム・保湿系美容液 → 原則use_days=[]

weekly_care例（use_daysは種別・濃度で異なる・曜日は固定しない）:
  パック→["日"]等（週1回・任意の一日）
  ピーリング（酵素系・低刺激）→[]（毎日可）または["火","木"]や["月","水","金"]等
  ピーリング（低濃度AHA/BHA）→["火","木"]または["水","金"]等（週2〜3回）
  ピーリング（中〜高濃度AHA/BHA・物理スクラブ）→["木"]または["火"]等（週1〜2回・任意の一日）

【ピーリングの使用頻度個別評価】
ピーリング商品もレチノールと同様に、種別・濃度・配合成分によって使用頻度を個別判断すること。一律週1固定にしない。

■ 酵素系（パパイン酵素・ブロメライン・プロテアーゼ等）
  → 刺激が最も穏やか。保湿成分豊富なら毎日〜週4回可。
  → use_days=[]（毎日）または["火","木","土","日"]等

■ PHA（グルコノラクトン・ラクトビオン酸等）
  → AHAより穏やか。週3〜5回または毎日可。
  → use_days=[]または["月","水","金","日"]等

■ 低濃度AHA（グリコール酸・乳酸 ＜5%）＋保湿成分豊富
  → 週2〜4回。敏感肌は週2〜3回。
  → use_days=["火","木"]または["月","水","金"]等（曜日は任意、連続禁止）

■ 低濃度BHA（サリチル酸 ＜2%）＋保湿成分あり
  → 週2〜3回。
  → use_days=["火","木"]または["水","金"]等（曜日は任意）

■ 中濃度AHA（5〜15%）／高濃度BHA
  → 週1〜2回。
  → use_days=["木"]または["火","木"]等（週1〜2回・任意の曜日）

■ 物理スクラブ（スクラブ粒子・摩擦タイプ）
  → 週1〜2回（過剰摩擦でバリア損傷リスク）。
  → use_days=["木"]または["月","木"]等（週1〜2回・任意の曜日）

■ 高濃度AHA/BHA（15%超）・強力ケミカルピール
  → 週1回以下。
  → use_days=["木"]等（週1回・任意の一日、土曜に固定しない）

【刺激累積管理ルール（最重要・必ず遵守）】

■ 高刺激成分の定義（以下を「高刺激成分」として扱う）
  A. 高濃度ビタミンC: L-アスコルビン酸 10%以上（オバジC20/C25等の医薬部外品含む）
  B. レチノール・レチナール（濃度問わず）
  C. AHA/BHA/SA: グリコール酸・乳酸・サリチル酸含有製品（ピーリング・洗顔料を問わない）
  D. アゼライン酸（15%以上または医薬品グレード）
  E. 高濃度ナイアシンアミド（20%超）

■ 洗顔料・クレンザーに含まれるBHA/SA（サリチル酸）の扱い
  BHA/SA含有洗顔料（例: セタフィルジェントルSAクレンザー）は週ケアのピーリングと
  同等の刺激源として必ず扱うこと。
  → 夜の他ステップに高刺激成分（上記A〜D）がある場合、
    その洗顔料のuse_daysを必ずずらして高刺激成分と重ならない曜日にすること。

■ 同一夜に高刺激成分を2種以上組み合わせることは禁止
  下記の組み合わせが同一夜に重なる場合、どちらか一方のuse_daysを必ず変更すること:
  × 高濃度ビタミンC (A) × アゼライン酸 (D) → 両方毎日は禁止
  × 高濃度ビタミンC (A) × BHA/SA洗顔 (C) → 同じ曜日に使用禁止
  × アゼライン酸 (D) × BHA/SA洗顔 (C) → 同じ曜日に使用禁止
  × レチノール (B) × 高濃度ビタミンC (A) → 同じ夜は禁止
  × レチノール (B) × BHA/SA (C) → 同じ夜は禁止

■ 具体例（厳守）
  NG: アゼライン酸化粧水[夜毎日] + オバジC25[夜毎日] + SA洗顔[夜火木土]
       → 火木土に3種（A+D+C）が重複 → 高刺激過多で肌ダメージリスク
  OK: アゼライン酸化粧水[夜毎日] + オバジC25[夜月水金日] + SA洗顔[夜火木土]
       → 高濃度ビタミンCとSA洗顔が重ならない（アゼライン酸は毎日でもOK）

■ 商品選定時の刺激配慮（use_days設定前の段階）
  ・バリア(barrier)スコア≤50 または 敏感度が高い場合:
    高濃度L-アスコルビン酸は選ばず、ビタミンC誘導体（3-O-エチルアスコルビン酸・APPSなど）を推奨
    アゼライン酸とBHA/SAの同一ルーティンへの両採用は避ける
  ・hydration≤50 かつ barrier≤50の場合:
    上記A〜Eの高刺激成分は1種のみに絞る（複数選定禁止）

【週ケアとnight刺激成分の衝突防止 — 3段ルール】

【ルール①】use_days=[]（毎日）はそのまま尊重する
AIが商品の濃度・成分を判断してuse_days=[]（毎日可）とした場合は変更しない。
ただし上記「刺激累積管理ルール」に違反する場合は優先して修正すること。

【ルール②】ピーリングとnight刺激成分を「両方毎日」にすることは禁止
ピーリング（weekly_care）およびBHA/SA洗顔料とnight刺激成分（レチノール・AHA・高濃度ビタミンC・アゼライン酸等）が
同一夜に重なる場合、必ずどちらか一方のuse_daysを変更すること。
- ピーリングを["火","木"]等の特定曜日にして、他の刺激成分はその曜日を外す（推奨）
- 判断基準: より刺激の強い方に曜日制限を設けるか、週ケアに具体的な曜日を設定して他を調整

【ルール③】特定曜日同士が重複する場合は別の曜日へ移動
nightステップのuse_days=["月","水","金"]で高刺激成分を使用する場合
→ BHA洗顔・ピーリングはそれらと重ならない曜日（例: "火"や"木"や"日"等）に設定すること
同一夜に複数の高刺激成分（刺激累積管理ルール記載のA〜E）を重ねることは肌ダメージのリスクがあるため厳禁

【ピーリング当日ルール】
ピーリング使用日（use_daysで指定した曜日）は、攻め系成分（レチノール・高濃度ビタミンC・AHA/BHA・アゼライン酸等）を
原則休止し、保湿・バリアケアを優先する。
ただし以下の条件をすべて満たす場合のみ、一部成分の併用を許可する:
  ①ピーリングの種別が低刺激（酵素系・PHA・低濃度AHA5%未満）
  ②組み合わせる成分がナイアシンアミド・セラミド・ヒアルロン酸等の低刺激成分のみ
  ③ユーザーの敏感度が低く、barrierスコアが60以上
この許可はAIが上記条件を判断した場合のみ適用し、高刺激ピーリングとの併用は条件に関わらず禁止。

【weekly_care】
パック・ピーリングのみ。不要なら[]。
ピーリング条件: texture/pores/dullness低スコアで角質ケア必要時のみ。
パック条件: 乾燥・バリア低下顕著または刺激成分使用後の回復ケアが必要な時のみ。

【product_candidates】
各stepに必ず3件出力(2件以下禁止・4件可)。現行販売中の正式名称が確実な商品のみ。stepのcategoryと完全一致必須。

【カテゴリ別 商品種別ルール（必読・厳守）】
- 美容液スロット → セラム/美容液/エッセンス/アンプル/ブースター系のみ。洗顔料・ジェルウォッシュ・クレンジング・化粧水は絶対禁止。
- 化粧水スロット → 化粧水/トナー/ローション系のみ。洗顔料・乳液・クリーム・日焼け止めは絶対禁止。
- 乳液スロット   → 乳液/ミルク/エマルジョン系のみ。洗顔料・化粧水・日焼け止めは絶対禁止。
- クリームスロット→ クリーム/バーム/ジェルクリーム系のみ。洗顔料・化粧水・日焼け止めは絶対禁止。
- 洗顔スロット   → 洗顔料/フォーム/ウォッシュ系のみ。化粧水・美容液・乳液・クリームは絶対禁止。
- クレンジングスロット→ クレンジング/メイク落とし系のみ。化粧水・美容液は絶対禁止。
- 日焼け止めスロット→ SPF/PA製品のみ。洗顔料・美容液・乳液・クリーム単体は絶対禁止。
商品名からカテゴリが判断できない場合は、そのstepをスキップせず「ブランド+カテゴリ名」で検索した確実な商品を選ぶこと。

- brand: ブランド名（必須・空文字禁止）例: "COSRX" "イニスフリー" "花王" "資生堂"
- name: 正式製品名（必須）例: "スネイルムチン96エッセンス" "グリーンティーヒアルロン酸セラム"
  ※「日焼け止め」「化粧水」「保湿クリーム」などカテゴリ名のみ・成分名+カテゴリ名のみは禁止
  ※必ずブランドと製品名のペアで出力すること
- confidence: 70未満出力禁止(90+:確実 80+:名称確実 70+:成分不確か)
- release_status: current のみ
- active_ingredients: 英語タグ(retinol/retinal/vitamin_c/niacinamide/azelaic_acid/ceramide/hyaluronic等)
- concerns: pores/acne/redness/oil_control/dryness/barrier/dullness/whitening/aging から選択
- skin_types: dry/oily/mixed/sensitive/normal(空配列禁止。ニキビ系→oily,mixed必須。セラミド系→dry,sensitive必須)
- sensitive_ok: yes(低刺激・セラミドCICA主体)/no(レチノール・高濃度AHA/BHA主体)/unknown
- retinol_level: レチノール系以外=0。低=1 中=2 高=3
- main_functions: 保湿/バリア強化/鎮静ケア/毛穴改善/ニキビ予防/皮脂抑制/美白ケア/透明感向上/ハリ改善/エイジングケア/紫外線防御/キメ改善 から選択
- ingredient_strength: {{成分: high/medium/low}}(高濃度・医薬部外品=high 標準=medium 補助=low)
- formulation: low_irritation/barrier_formula/light_texture/rich_texture/fragrance_free/alcohol_free/oil_free/non_comedogenic/water_based/oil_based から選択
- texture: light/watery/gel/medium/essence/cream/rich/oil/balm/foam/powder から選択
- availability_japan: drugstore/amazon/rakuten/official_jp(必ず1つ以上。不明でもamazon/rakuten含める)
- uv_level: 日焼け止めのみ{{spf,pa}}、他={{}}
- reason: このstepに合う短い理由
禁止: 架空商品/旧名称/廃盤品/カテゴリ違い/自信ない商品/ブランド空文字/カテゴリ名のみの製品名/セット販売・まとめ買い商品

【routine_strategy】
strategy_type: fixed/rotation(攻め成分分散が必要な場合のみrotation)
overall_policy/morning_policy/night_policy/weekly_policy
active_care_frequency/recovery_care_frequency
rotation_targets: ローテーション対象成分配列
morning_order: 朝の使用順序配列(ブースターは化粧水前)
night_order: 夜の使用順序配列
reason: この肌状態に合う理由

avoid_combinations(3件以上必須):
[{{families:[タグA,タグB], scope:"same_session"/"any", reason:"この肌スコアに言及した理由", severity:"hard"/"soft"}}]
タグ: retinoid/aha_bha/strong_vitamin_c/vitamin_c/azelaic/niacinamide/ceramide/barrier/peptide/pdrn
同系統重複["retinoid","retinoid"]=「同種2製品禁止」。敏感度高・バリア低→hard多用。レチノール未経験→retinoidペアはhard。
【必須組み合わせ（ルーティンに含まれている場合は必ずavoid_combinationsに追加）】
- strong_vitamin_c + azelaic → same_session → hard（両者を同一夜に使うと酸性刺激が過剰）
- strong_vitamin_c + aha_bha → same_session → hard（高濃度ビタミンC×BHA/SAは刺激過多）
- azelaic + aha_bha → same_session → hard（アゼライン酸×BHA/SA洗顔料を同一夜に使用禁止）
- retinoid + strong_vitamin_c → same_session → hard（レチノールと高濃度ビタミンCの同夜使用禁止）

synergy_combinations(3件以上必須):
[{{families:[タグA,タグB], reason:"この肌状態に基づく相乗理由", bonus:"high"/"medium"/"low"}}]
タグはavoidと同じ+uv_protection

【supplements】
スキンケアのトータルコーディネートとして、内側からの補完が有効な場合に提案。不要なら[]。
提案数は肌状態・悩みに応じて柔軟に決める（件数制限なし）。

【サプリ提案の入口判断（全スコアの最低値: {_min_score} → {_supp_level}）】
スコアが低いほど提案の必要性は高くなるが、必ず提案する必要はない。
スキンケアのみで十分改善可能と判断した場合は[]とする。
・80以上: 原則不要
・60〜79: 必要性が高い場合のみ検討
・40〜59: 積極的に検討
・39以下: 優先的に検討し、改善効果が期待できるなら提案

提案条件: ①外用スキンケアだけでは補完困難な悩み(コラーゲン生成・抗酸化・抗糖化等) ②既存ルーティンと成分が重複せず相乗効果が見込める ③肌スコアや悩みに明確な根拠がある
禁止: スキンケアと全く同じ成分の重複提案・根拠のない漠然とした提案

【サプリメント安全基準（複数提案時は必ず全項目確認）】
①成分重複チェック: 複数サプリにビタミンC/B群/亜鉛/ビタミンA/Eが重複しないか確認。
  過剰摂取リスクが特に高い成分（ビタミンA・E・亜鉛・セレン・鉄）はサプリ間で重複提案禁止。
②脂溶性ビタミン注意: ビタミンA・D・E・Kは体内蓄積リスクあり。
  複数サプリからの重複摂取になる場合は提案を絞ること。
③ミネラル吸収干渉: 鉄×亜鉛、鉄×カルシウム、亜鉛×銅は同時摂取で吸収率が低下。
  これらを同時提案する場合は必ずtimingを分ける（例: 亜鉛→朝食後、鉄→就寝前）。
④薬との相互作用: 代表的な禁忌（抗凝固薬×ビタミンK、多くの医薬品×セントジョーンズワート等）を
  cautionフィールドに明記。一般ユーザーへの注意喚起として、該当する場合は医師への相談を促す文を含める。

- category: "サプリメント" (固定)
- product: 楽天で検索可能な具体名 (例: "ビタミンC 1000mg", "コラーゲンペプチド", "ナイアシンアミド サプリ", "亜鉛 サプリ", "アスタキサンチン")
- brand: ブランド名 (不明なら "")
- purpose: 肌への期待効果 (30字以内)
- ingredient_focus: 主要成分タグ配列 (例: ["vitamin_c"] ["collagen"] ["niacinamide"] ["zinc"] ["astaxanthin"])
- reason: 既存スキンケアルーティンとの相性・補完価値を具体的に (50字以内)
- timing: 摂取タイミング (例: "朝食後", "就寝前") ※ミネラル干渉がある場合は必ず分ける
- caution: このサプリ固有の注意点（過剰摂取リスク・吸収干渉・薬との相互作用等、なければ ""）(40字以内)
- priority: 1=最優先 2=次点以降

【beauty_devices】
肌スコアや悩みから判断して提案。不要なら[]。

【美容機器提案の入口判断（毛穴/ハリ/キメ/ニキビ等の最低値: {_min_device_score} → {_device_level}）】
スコアが低いほど提案の必要性は高くなるが、必ず提案する必要はない。
スキンケアのみで十分改善可能と判断した場合は[]とする。
・80以上: 原則不要
・60〜79: 必要性が高い場合のみ検討
・40〜59: 積極的に検討
・39以下: 優先的に検討し、改善効果が期待できるなら提案

【提案基準（すべて考慮して判断）】
①スキンケア単独では改善が遅い項目（毛穴・ハリ・キメ等）に機器の物理的効果が明確に有効
②現在のルーティン（美容液・クリーム等）の効果を技術的に増強できる根拠がある
③肌スコアの該当項目への改善効果が裏付けられた機器のみ
【禁止】
・相性の悪い組み合わせ（例: レチノール使用直後の摩擦系機器・EMS等の刺激系）
・1つのスコア課題に対して複数の機器を重複提案（1課題=1機器が原則）
・根拠のない漠然とした提案
- category: "美容機器" (固定)
- product: 楽天で検索可能な機器タイプ名 (例: "LEDマスク", "超音波美顔器", "EMSフェイスケア", "RF美顔器", "イオン導入器", "ローラー美顔器")
- brand: ブランド名 (不明なら "")
- purpose: 期待される肌効果 (30字以内)
- device_function: 機能説明 (例: "赤色LED照射", "超音波振動", "EMS微電流", "RF高周波", "イオン導入")
- reason: 使用中スキンケアとの相性・増強効果の根拠を具体的に (50字以内)
- frequency: 推奨頻度 (例: "週3〜4回・スキンケア後", "毎日・朝スキンケア後")
- priority: 1=最優先 2=次点以降

JSONのみ返す。説明・Markdown・前置き禁止。JSONキーは英語、値は日本語。"""


def build_analysis_prompt(user_data):
    _concerns_ja = ', '.join([_CONCERN_LABELS_JA.get(c, c) for c in (user_data.get('concerns') or [])]) or '未回答'
    return f"""あなたは日本の市販スキンケアと肌分析に詳しい美容アドバイザーです。
肌画像（1:正面 2:左頬 3:右頬）とユーザー情報を分析し、JSONのみ返す。

【ユーザー情報】
年齢:{user_data['age']} 皮脂:{user_data['oil']} 敏感度:{user_data['sens']} レチノール経験:{user_data['exp']} 予算:{user_data['budget']}
悩み:{_concerns_ja}
※悩みをスコア低項目より優先してステップ構成する。

【スコア(0-100)】画像の視覚的事実のみ。同じ画像・情報では必ず同じ値。
85+:良好 65-84:軽微課題 45-64:改善余地 25-44:要改善 0-24:深刻
oil_balance:テカリ均一さ redness:赤み面積 pores:毛穴の目立ち hydration:ツヤ水分感
firmness:ハリたるみ acne:ニキビ炎症 dullness:くすみ barrier:肌表面の荒れ
texture:キメ tone_evenness:色ムラシミ
score_reasons:各スコアの画像的根拠を1文(15-30字)で記述。

【分析】
symmetry_analysis: score(0-100,左右差少=高)/summary/left_tendency/right_tendency。不明は控えめに。
skin_age_estimate: 画像から肌年齢を15-70の整数で推定。

【カテゴリ固定】
クレンジング/洗顔/化粧水/美容液/乳液/クリーム/日焼け止め/パック/ピーリング のみ。
role: main/booster のみ。

【morning必須カテゴリ】
洗顔・化粧水・日焼け止めは必ずmorning stepsに含める（省略禁止）。
美容液は原則morning stepsに含める（有効成分による日中ケアが基本）。肌悩みが特になく成分が全く不要な場合のみ省略可。
乳液・クリームは保湿必要度（moisture_plan）に応じて追加する。

【ingredient_focus候補】
ビタミンC/ナイアシンアミド/レチノール/レチナール/アゼライン酸/トラネキサム酸/PDRN/ペプチド/セラミド/ヒアルロン酸/CICA/ドクダミ/AHA/BHA/PHA/UV防御/低刺激

【use_days】
night・weekly_careの各stepに必ず設定。morning=[]固定。
基本方針: use_days=[]（毎日）が原則。頻度制限は成分の種類だけで機械的に決めず、以下の要素を総合的に判断すること。

【頻度制限の判断基準（個別評価）】
以下をすべて考慮した上で、毎日使用可能か・制限が必要かを商品ごとに個別判断する:
① 濃度: 低濃度（レチノール0.025〜0.1%相当、低刺激処方）→ 毎日可能なケースが多い
② 緩和成分: セラミド・ナイアシンアミド・ヒアルロン酸・スクワラン・パンテノール等のバリア補修・保湿成分が主体または十分配合 → 刺激が緩和され毎日使用可能になりやすい
③ ルーティン全体の刺激バランス: 同じ夜ルーティンに高濃度AHA/BHA・強力ビタミンC等の刺激成分が重なる場合は制限を検討
④ ユーザーのレチノール経験（retinol_exp）: 慣れている→毎日可能なケース増。初心者→初期は慎重に

【判断例】
- レチノール低濃度＋セラミド豊富な処方 → use_days=[]（毎日）
- レチノール中〜高濃度＋緩和成分少ない → use_days=["月","水","金"]等
- 高濃度AHA/BHA単独 → use_days=["火","金"]等（連続禁止）
- 低濃度AHA＋保湿成分豊富 → use_days=[]または["月","水","金","日"]等
- 化粧水・乳液・クリーム・保湿系美容液 → 原則use_days=[]

weekly_care例（use_daysは種別・濃度で異なる・曜日は固定しない）:
  パック→["日"]等（週1回・任意の一日）
  ピーリング（酵素系・低刺激）→[]（毎日可）または["火","木"]や["月","水","金"]等
  ピーリング（低濃度AHA/BHA）→["火","木"]または["水","金"]等（週2〜3回）
  ピーリング（中〜高濃度AHA/BHA・物理スクラブ）→["木"]または["火"]等（週1〜2回・任意の一日）

【ピーリングの使用頻度個別評価】
ピーリング商品もレチノールと同様に、種別・濃度・配合成分によって使用頻度を個別判断すること。一律週1固定にしない。

■ 酵素系（パパイン酵素・ブロメライン・プロテアーゼ等）
  → 刺激が最も穏やか。保湿成分豊富なら毎日〜週4回可。
  → use_days=[]（毎日）または["火","木","土","日"]等

■ PHA（グルコノラクトン・ラクトビオン酸等）
  → AHAより穏やか。週3〜5回または毎日可。
  → use_days=[]または["月","水","金","日"]等

■ 低濃度AHA（グリコール酸・乳酸 ＜5%）＋保湿成分豊富
  → 週2〜4回。敏感肌は週2〜3回。
  → use_days=["火","木"]または["月","水","金"]等（曜日は任意、連続禁止）

■ 低濃度BHA（サリチル酸 ＜2%）＋保湿成分あり
  → 週2〜3回。
  → use_days=["火","木"]または["水","金"]等（曜日は任意）

■ 中濃度AHA（5〜15%）／高濃度BHA
  → 週1〜2回。
  → use_days=["木"]または["火","木"]等（週1〜2回・任意の曜日）

■ 物理スクラブ（スクラブ粒子・摩擦タイプ）
  → 週1〜2回（過剰摩擦でバリア損傷リスク）。
  → use_days=["木"]または["月","木"]等（週1〜2回・任意の曜日）

■ 高濃度AHA/BHA（15%超）・強力ケミカルピール
  → 週1回以下。
  → use_days=["木"]等（週1回・任意の一日、土曜に固定しない）

【刺激累積管理ルール（最重要・必ず遵守）】

■ 高刺激成分の定義（以下を「高刺激成分」として扱う）
  A. 高濃度ビタミンC: L-アスコルビン酸 10%以上（オバジC20/C25等の医薬部外品含む）
  B. レチノール・レチナール（濃度問わず）
  C. AHA/BHA/SA: グリコール酸・乳酸・サリチル酸含有製品（ピーリング・洗顔料を問わない）
  D. アゼライン酸（15%以上または医薬品グレード）
  E. 高濃度ナイアシンアミド（20%超）

■ 洗顔料・クレンザーに含まれるBHA/SA（サリチル酸）の扱い
  BHA/SA含有洗顔料（例: セタフィルジェントルSAクレンザー）は週ケアのピーリングと
  同等の刺激源として必ず扱うこと。
  → 夜の他ステップに高刺激成分（上記A〜D）がある場合、
    その洗顔料のuse_daysを必ずずらして高刺激成分と重ならない曜日にすること。

■ 同一夜に高刺激成分を2種以上組み合わせることは禁止
  下記の組み合わせが同一夜に重なる場合、どちらか一方のuse_daysを必ず変更すること:
  × 高濃度ビタミンC (A) × アゼライン酸 (D) → 両方毎日は禁止
  × 高濃度ビタミンC (A) × BHA/SA洗顔 (C) → 同じ曜日に使用禁止
  × アゼライン酸 (D) × BHA/SA洗顔 (C) → 同じ曜日に使用禁止
  × レチノール (B) × 高濃度ビタミンC (A) → 同じ夜は禁止
  × レチノール (B) × BHA/SA (C) → 同じ夜は禁止

■ 具体例（厳守）
  NG: アゼライン酸化粧水[夜毎日] + オバジC25[夜毎日] + SA洗顔[夜火木土]
       → 火木土に3種（A+D+C）が重複 → 高刺激過多で肌ダメージリスク
  OK: アゼライン酸化粧水[夜毎日] + オバジC25[夜月水金日] + SA洗顔[夜火木土]
       → 高濃度ビタミンCとSA洗顔が重ならない（アゼライン酸は毎日でもOK）

■ 商品選定時の刺激配慮（use_days設定前の段階）
  ・バリア(barrier)スコア≤50 または 敏感度が高い場合:
    高濃度L-アスコルビン酸は選ばず、ビタミンC誘導体（3-O-エチルアスコルビン酸・APPSなど）を推奨
    アゼライン酸とBHA/SAの同一ルーティンへの両採用は避ける
  ・hydration≤50 かつ barrier≤50の場合:
    上記A〜Eの高刺激成分は1種のみに絞る（複数選定禁止）

【週ケアとnight刺激成分の衝突防止 — 3段ルール】

【ルール①】use_days=[]（毎日）はそのまま尊重する
AIが商品の濃度・成分を判断してuse_days=[]（毎日可）とした場合は変更しない。
ただし上記「刺激累積管理ルール」に違反する場合は優先して修正すること。

【ルール②】ピーリングとnight刺激成分を「両方毎日」にすることは禁止
ピーリング（weekly_care）およびBHA/SA洗顔料とnight刺激成分（レチノール・AHA・高濃度ビタミンC・アゼライン酸等）が
同一夜に重なる場合、必ずどちらか一方のuse_daysを変更すること。
- ピーリングを["火","木"]等の特定曜日にして、他の刺激成分はその曜日を外す（推奨）
- 判断基準: より刺激の強い方に曜日制限を設けるか、週ケアに具体的な曜日を設定して他を調整

【ルール③】特定曜日同士が重複する場合は別の曜日へ移動
nightステップのuse_days=["月","水","金"]で高刺激成分を使用する場合
→ BHA洗顔・ピーリングはそれらと重ならない曜日（例: "火"や"木"や"日"等）に設定すること
同一夜に複数の高刺激成分（刺激累積管理ルール記載のA〜E）を重ねることは肌ダメージのリスクがあるため厳禁

【ピーリング当日ルール】
ピーリング使用日（use_daysで指定した曜日）は、攻め系成分（レチノール・高濃度ビタミンC・AHA/BHA・アゼライン酸等）を
原則休止し、保湿・バリアケアを優先する。
ただし以下の条件をすべて満たす場合のみ、一部成分の併用を許可する:
  ①ピーリングの種別が低刺激（酵素系・PHA・低濃度AHA5%未満）
  ②組み合わせる成分がナイアシンアミド・セラミド・ヒアルロン酸等の低刺激成分のみ
  ③ユーザーの敏感度が低く、barrierスコアが60以上
この許可はAIが上記条件を判断した場合のみ適用し、高刺激ピーリングとの併用は条件に関わらず禁止。

【weekly_care】
パック・ピーリングのみ。不要なら[]。
ピーリング条件: texture/pores/dullness低スコアで角質ケア必要時のみ。
パック条件: 乾燥・バリア低下顕著または刺激成分使用後の回復ケアが必要な時のみ。

【product_candidates】
各stepに3-4件必須(0-2件禁止)。現行販売中の正式名称確実な商品のみ。stepのcategoryと完全一致必須。

【カテゴリ別 商品種別ルール（必読・厳守）】
- 美容液スロット → セラム/美容液/エッセンス/アンプル系のみ。洗顔料・ジェルウォッシュ・クレンジング・化粧水は絶対禁止。
- 化粧水スロット → 化粧水/トナー/ローション系のみ。洗顔料・乳液・クリーム・日焼け止めは絶対禁止。
- 乳液スロット   → 乳液/ミルク/エマルジョン系のみ。洗顔料・化粧水・日焼け止めは絶対禁止。
- クリームスロット→ クリーム/バーム/ジェルクリーム系のみ。洗顔料・化粧水・日焼け止めは絶対禁止。
- 洗顔スロット   → 洗顔料/フォーム/ウォッシュ系のみ。化粧水・美容液・乳液・クリームは絶対禁止。
- クレンジングスロット→ クレンジング/メイク落とし系のみ。化粧水・美容液は絶対禁止。
- 日焼け止めスロット→ SPF/PA製品のみ。洗顔料・乳液・クリーム単体は絶対禁止。

各候補のフィールドルール:
- confidence: 70未満出力禁止(90+:確実 80+:名称確実 70+:成分不確か)
- release_status: current のみ
- active_ingredients: 英語タグ(retinol/retinal/vitamin_c/niacinamide/azelaic_acid/ceramide/hyaluronic等)
- concerns: pores/acne/redness/oil_control/dryness/barrier/dullness/whitening/aging から選択
- skin_types: dry/oily/mixed/sensitive/normal(空配列禁止。全肌タイプ→全4種。ニキビ系→oily,mixed必須。セラミド系→dry,sensitive必須)
- sensitive_ok: yes(低刺激・セラミドCICA主体)/no(レチノール・高濃度AHA/BHA主体)/unknown
- retinol_level: レチノール系以外=0。低=1 中=2 高=3
- main_functions: 保湿/バリア強化/鎮静ケア/毛穴改善/ニキビ予防/皮脂抑制/美白ケア/透明感向上/ハリ改善/エイジングケア/紫外線防御/キメ改善 から選択
- ingredient_strength: {{成分: high/medium/low}}(高濃度・医薬部外品=high 標準=medium 補助=low)
- formulation: low_irritation/barrier_formula/light_texture/rich_texture/fragrance_free/alcohol_free/oil_free/non_comedogenic/water_based/oil_based から選択
- texture: light/watery/gel/medium/essence/cream/rich/oil/balm/foam/powder から選択
- availability_japan: drugstore/amazon/rakuten/official_jp(必ず1つ以上。不明でもamazon/rakuten含める)
- uv_level: 日焼け止めのみ{{spf,pa}}、他={{}}
- reason: このstepに合う短い理由
禁止: 架空商品/旧名称/廃盤品/カテゴリ違い/自信ない商品/抽象名/セット販売・まとめ買い商品(「○個セット」「○本セット」「まとめ買い」「詰め合わせ」等は除外、単品のみ提案すること)

【improvement_plan】
priority_concerns(配列)/key_ingredients(配列)/care_direction(短文)

【routine_strategy】
strategy_type: fixed(固定型)/rotation(ローテーション型、攻め成分分散が必要な場合のみ)
overall_policy/morning_policy/night_policy/weekly_policy
active_care_frequency/recovery_care_frequency
rotation_targets: ローテーション対象成分配列
morning_order: 朝の使用順序配列(ブースターは化粧水前)
night_order: 夜の使用順序配列(役割・テクスチャーに基づく順序)
reason: この肌状態に合う理由

avoid_combinations(3件以上必須):
[{{families:[タグA,タグB], scope:"same_session"/"any", reason:"この肌スコア・経験値に言及した理由", severity:"hard"/"soft"}}]
タグ: retinoid/aha_bha/strong_vitamin_c/vitamin_c/azelaic/niacinamide/ceramide/barrier/peptide/pdrn
同系統重複["retinoid","retinoid"]=「同種2製品禁止」。敏感度高・バリア低→hard多用。レチノール未経験→retinoidペアはhard。

synergy_combinations(3件以上必須):
[{{families:[タグA,タグB], reason:"この肌状態に基づく相乗理由", bonus:"high"/"medium"/"low"}}]
タグはavoidと同じ+uv_protection

【moisture_plan】
moisture_level/need_emulsion/need_cream/need_double_moisture/reason

【supplements】
スキンケアのトータルコーディネートとして、内側からの補完が有効な場合に提案。不要なら[]。
提案数は肌状態・悩みに応じて柔軟に決める（件数制限なし）。
提案条件: ①外用スキンケアだけでは補完困難な悩み(コラーゲン生成・抗酸化・抗糖化等) ②既存ルーティンと成分が重複せず相乗効果が見込める ③肌スコアや悩みに明確な根拠がある
禁止: スキンケアと全く同じ成分の重複提案・根拠のない漠然とした提案

【サプリメント安全基準（複数提案時は必ず全項目確認）】
①成分重複チェック: 複数サプリにビタミンC/B群/亜鉛/ビタミンA/Eが重複しないか確認。
  過剰摂取リスクが特に高い成分（ビタミンA・E・亜鉛・セレン・鉄）はサプリ間で重複提案禁止。
②脂溶性ビタミン注意: ビタミンA・D・E・Kは体内蓄積リスクあり。
  複数サプリからの重複摂取になる場合は提案を絞ること。
③ミネラル吸収干渉: 鉄×亜鉛、鉄×カルシウム、亜鉛×銅は同時摂取で吸収率が低下。
  これらを同時提案する場合は必ずtimingを分ける（例: 亜鉛→朝食後、鉄→就寝前）。
④薬との相互作用: 代表的な禁忌（抗凝固薬×ビタミンK、多くの医薬品×セントジョーンズワート等）を
  cautionフィールドに明記。一般ユーザーへの注意喚起として、該当する場合は医師への相談を促す文を含める。

- category: "サプリメント" (固定)
- product: 楽天で検索可能な具体名 (例: "ビタミンC 1000mg", "コラーゲンペプチド", "ナイアシンアミド サプリ", "亜鉛 サプリ", "アスタキサンチン")
- brand: ブランド名 (不明なら "")
- purpose: 肌への期待効果 (30字以内)
- ingredient_focus: 主要成分タグ配列 (例: ["vitamin_c"] ["collagen"] ["niacinamide"] ["zinc"] ["astaxanthin"])
- reason: 既存スキンケアルーティンとの相性・補完価値を具体的に (50字以内)
- timing: 摂取タイミング (例: "朝食後", "就寝前") ※ミネラル干渉がある場合は必ず分ける
- caution: このサプリ固有の注意点（過剰摂取リスク・吸収干渉・薬との相互作用等、なければ ""）(40字以内)
- priority: 1=最優先 2=次点以降

【beauty_devices】
肌スコアや悩みから判断して本当に必要な場合のみ提案。不要なら[]。
【提案基準（すべて考慮して判断）】
①スキンケア単独では改善が遅い項目（毛穴・ハリ・キメ等）に機器の物理的効果が明確に有効
②現在のルーティン（美容液・クリーム等）の効果を技術的に増強できる根拠がある
③肌スコアの該当項目への改善効果が裏付けられた機器のみ
【禁止】
・相性の悪い組み合わせ（例: レチノール使用直後の摩擦系機器・EMS等の刺激系）
・1つのスコア課題に対して複数の機器を重複提案（1課題=1機器が原則）
・根拠のない漠然とした提案
- category: "美容機器" (固定)
- product: 楽天で検索可能な機器タイプ名 (例: "LEDマスク", "超音波美顔器", "EMSフェイスケア", "RF美顔器", "イオン導入器", "ローラー美顔器")
- brand: ブランド名 (不明なら "")
- purpose: 期待される肌効果 (30字以内)
- device_function: 機能説明 (例: "赤色LED照射", "超音波振動", "EMS微電流", "RF高周波", "イオン導入")
- reason: 使用中スキンケアとの相性・増強効果の根拠を具体的に (50字以内)
- frequency: 推奨頻度 (例: "週3〜4回・スキンケア後", "毎日・朝スキンケア後")
- priority: 1=最優先 2=次点以降

JSONのみ返す。説明・Markdown・前置き禁止。JSONキーは英語、値は日本語。"""

def extract_image_bytes_for_hash(image):
    if image is None:
        return b""

    if isinstance(image, bytes):
        return image

    if isinstance(image, bytearray):
        return bytes(image)

    # PIL Image → ピクセルをそのままバイト列化（決定論的、メモリアドレス不使用）
    try:
        from PIL import Image as _PILImage
        if isinstance(image, _PILImage.Image):
            return image.tobytes()
    except Exception:
        pass

    # Gemini Part オブジェクト
    inline_data = getattr(image, "inline_data", None)
    if inline_data is not None:
        data = getattr(inline_data, "data", None)
        if data:
            return data

    data = getattr(image, "data", None)
    if data:
        return data

    # フォールバック: str(image) はメモリアドレスを含むため使用禁止
    # 到達した場合は空バイトを返してキャッシュ無効化より再現性を優先
    return b""


def make_analysis_cache_key(user_data, front_img, left_img, right_img):
    h = hashlib.sha256()

    h.update(ANALYSIS_CACHE_VERSION.encode("utf-8"))

    user_key = json.dumps(
        user_data,
        ensure_ascii=False,
        sort_keys=True,
        default=str
    )

    h.update(user_key.encode("utf-8"))
    h.update(extract_image_bytes_for_hash(front_img))
    h.update(extract_image_bytes_for_hash(left_img))
    h.update(extract_image_bytes_for_hash(right_img))

    return h.hexdigest()

GEMINI_ANALYSIS_CACHE_FILE = "gemini_analysis_cache.json"


def load_gemini_analysis_file_cache():
    try:
        with open(GEMINI_ANALYSIS_CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print("[GEMINI FILE CACHE LOAD ERROR]", e, flush=True)
        return {}

    if not isinstance(cache, dict):
        return {}

    return cache


def save_gemini_analysis_file_cache(cache):
    if not isinstance(cache, dict):
        return

    try:
        with open(GEMINI_ANALYSIS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception as e:
        print("[GEMINI FILE CACHE SAVE ERROR]", e, flush=True)


def get_gemini_cached_analysis(cache_keys):
    if not isinstance(cache_keys, list):
        return None

    # 1. メモリキャッシュ（最速）
    for key in cache_keys:
        if key in GEMINI_ANALYSIS_CACHE:
            print("[GEMINI MEMORY CACHE HIT]", key, flush=True)
            return copy.deepcopy(GEMINI_ANALYSIS_CACHE[key])

    # 2. PostgreSQLキャッシュ（デプロイ後も永続）
    db_result = get_analysis_cache_from_db(cache_keys)
    if isinstance(db_result, dict):
        GEMINI_ANALYSIS_CACHE[cache_keys[0]] = copy.deepcopy(db_result)
        return copy.deepcopy(db_result)

    # 3. ファイルキャッシュ（フォールバック）
    file_cache = load_gemini_analysis_file_cache()
    for key in cache_keys:
        item = file_cache.get(key)
        if isinstance(item, dict):
            print("[GEMINI FILE CACHE HIT]", key, flush=True)
            GEMINI_ANALYSIS_CACHE[key] = copy.deepcopy(item)
            return copy.deepcopy(item)

    return None


def set_gemini_cached_analysis(cache_key, data):
    if not cache_key or not isinstance(data, dict):
        return

    # メモリ・ファイル・DBすべてに保存
    GEMINI_ANALYSIS_CACHE[cache_key] = copy.deepcopy(data)

    file_cache = load_gemini_analysis_file_cache()
    file_cache[cache_key] = copy.deepcopy(data)
    save_gemini_analysis_file_cache(file_cache)

    save_analysis_cache_to_db(cache_key, data)

def analyze_skin_with_gemini(user_data, front_img, left_img, right_img):

    if DEV_MODE:
        print("DEV_MODE: analyze_skin_with_gemini ダミー返却")
        return {
            "skin_score": 65,
            "skin_summary": "テストモードのダミー診断結果です",
            "morning": {"steps": [{"category": "化粧水", "purpose": "保湿"}]},
            "night": {"steps": [{"category": "美容液", "purpose": "毛穴ケア"}]},
            "weekly_care": [{"category": "パック", "purpose": "集中ケア"}],
            "scores": {}
        }

    base_cache_key = make_analysis_cache_key(
        user_data,
        front_img,
        left_img,
        right_img
    )

    cache_key = f"ai_candidate_schema_v5:{base_cache_key}"

    fallback_cache_keys = [
        cache_key,
        f"ai_candidate_schema_v4:{base_cache_key}",
        f"ai_candidate_schema_v3:{base_cache_key}",
    ]

    cached_analysis = get_gemini_cached_analysis(fallback_cache_keys)

    if cached_analysis:
        cached_analysis.setdefault("warnings", [])
        return cached_analysis

    print("[GEMINI ANALYSIS CACHE MISS]", cache_key, flush=True)

    # ===== Phase 1: 肌スコア・分析・改善方針（画像3枚、小出力） =====
    _t1 = time.time()
    print("[GEMINI PHASE1 START] 肌スコア・分析", flush=True)
    try:
        response1 = call_gemini_with_retry(
            client,
            ANALYSIS_MODEL,
            contents=[build_analysis_prompt_phase1(user_data), front_img, left_img, right_img],
            config=types.GenerateContentConfig(
                temperature=0,
                top_p=0.05,
                seed=42,
                thinking_config=types.ThinkingConfig(thinking_budget=0),  # スコアの一貫性のためthinking無効
                response_mime_type="application/json",
                response_schema=get_analysis_schema_phase1()
            ),
            max_retries=1,
            timeout=60  # 画像3枚+小出力: 60s
        )
        _raw1 = response1.text.strip()
        phase1 = json.loads(_raw1)
        print(f"[GEMINI PHASE1 DONE] elapsed={time.time()-_t1:.1f}s scores={phase1.get('scores',{})}", flush=True)
    except Exception as e:
        error_text = str(e)
        if "503" in error_text or "UNAVAILABLE" in error_text:
            for fallback_key in fallback_cache_keys:
                if fallback_key in GEMINI_ANALYSIS_CACHE:
                    print("[GEMINI FALLBACK CACHE HIT]", fallback_key, flush=True)
                    cached = copy.deepcopy(GEMINI_ANALYSIS_CACHE[fallback_key])
                    cached.setdefault("warnings", [])
                    cached["warnings"].append("現在AI診断が混み合っているため、同じ入力の前回診断結果をもとに表示しています。")
                    return cached
        raise

    # ===== Phase 2: ルーティン・商品候補（テキストのみ、大出力） =====
    _t2 = time.time()
    print("[GEMINI PHASE2 START] ルーティン・商品候補", flush=True)
    try:
        response2 = call_gemini_with_retry(
            client,
            ANALYSIS_MODEL,
            contents=[build_analysis_prompt_phase2(user_data, phase1)],
            config=types.GenerateContentConfig(
                temperature=0,
                top_p=0.05,
                seed=42,
                thinking_config=types.ThinkingConfig(thinking_budget=0),  # スコアの一貫性のためthinking無効
                response_mime_type="application/json",
                response_schema=get_analysis_schema_phase2()
            ),
            max_retries=1,
            timeout=90  # テキストのみ+大出力: 90s
        )
        _raw2 = response2.text.strip()
        phase2 = json.loads(_raw2)
        print(f"[GEMINI PHASE2 DONE] elapsed={time.time()-_t2:.1f}s", flush=True)
    except Exception as e:
        raise

    # ===== マージ =====
    data = {**phase1, **phase2}
    data.setdefault("warnings", [])

    # 無料ユーザー用: スコア昇順の改善優先順位リストを生成
    data["improvement_priority"] = build_improvement_priority(data.get("scores", {}))

    # ルーティンログ
    def _fmt_steps(steps):
        if not isinstance(steps, list):
            return []
        return [
            {"category": s.get("category",""), "role": s.get("role",""),
             "ingredient_focus": s.get("ingredient_focus",""), "use_days": s.get("use_days",[])}
            for s in steps if isinstance(s, dict)
        ]
    print("[ROUTINE PLAN] morning:", json.dumps(_fmt_steps(data.get("morning",{}).get("steps",[])), ensure_ascii=False), flush=True)
    print("[ROUTINE PLAN] night:", json.dumps(_fmt_steps(data.get("night",{}).get("steps",[])), ensure_ascii=False), flush=True)
    print("[ROUTINE PLAN] weekly_care:", json.dumps(_fmt_steps(data.get("weekly_care",[])), ensure_ascii=False), flush=True)

    # レチノール × アゼライン酸チェック
    _night_ingredients = [s.get("ingredient_focus","") for s in data.get("night",{}).get("steps",[]) if isinstance(s, dict)]
    _has_retinol = any("retinol" in str(i).lower() or "レチノール" in str(i) for i in _night_ingredients)
    _has_azelaic = any("azelaic" in str(i).lower() or "アゼライン" in str(i) for i in _night_ingredients)
    if _has_retinol and _has_azelaic:
        print("[ROUTINE WARN] ⚠ Night routine contains BOTH retinol AND azelaic acid — check use_days separation", flush=True)
    else:
        print(f"[ROUTINE CHECK] retinol={'YES' if _has_retinol else 'no'}  azelaic_acid={'YES' if _has_azelaic else 'no'}", flush=True)

    set_gemini_cached_analysis(cache_key, data)
    return data

def detailed_analysis_with_gemini(client, user_data, result_data):

    prompt = f"""
あなたは皮膚分析AIです。

以下の既存診断結果をもとに、
より詳細な分析だけ行ってください。

【基本情報】
{user_data}

【既存診断】
{result_data}

返却JSON:

{{
"deep_analysis": {{
"root_causes": [],
"priority_concerns": [],
"skin_age_comment": "",
"barrier_score": 0,
"acne_score": 0,
"pigment_score": 0,
"wrinkle_score": 0,
"pore_type": "",
"improvement_plan": {{
"immediate": [],
"short_term": [],
"long_term": []
}},
"extra_advice": []
}}
}}
"""

    response = call_gemini_with_retry(
        client=client,
        model=CANDIDATE_MODEL,
        contents=prompt,
        config={
            "temperature": 0
        }
    )

    return extract_json(response.text)

def get_rich_candidate_collection_schema():
    return {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string"},
                        "role": {"type": "string"},
                        "ingredient_focus": {"type": "string"},
                        "purpose": {"type": "string"},
                        "candidates": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "price_ref": {"type": "integer"},
                                    "active_ingredients": {
                                        "type": "array",
                                        "items": {"type": "string"}
                                    },
                                    "support_ingredients": {
                                        "type": "array",
                                        "items": {"type": "string"}
                                    },
                                    "signature_ingredients": {
                                        "type": "array",
                                        "items": {"type": "string"}
                                    },
                                    "concerns": {
                                        "type": "array",
                                        "items": {"type": "string"}
                                    },
                                    "skin_types": {
                                        "type": "array",
                                        "items": {"type": "string"}
                                    },
                                    "sensitive_ok": {"type": "string"},
                                    "retinol_level": {"type": "integer"},
                                    "main_functions": {
                                        "type": "array",
                                        "items": {"type": "string"}
                                    },
                                    "formulation": {
                                        "type": "array",
                                        "items": {"type": "string"}
                                    },
                                    "technology": {
                                        "type": "array",
                                        "items": {"type": "string"}
                                    },
                                    "texture": {"type": "string"},
                                    "contraindications": {
                                        "type": "array",
                                        "items": {"type": "string"}
                                    },
                                    "reason": {"type": "string"}
                                },
                                "required": [
                                    "name",
                                    "price_ref",
                                    "active_ingredients",
                                    "support_ingredients",
                                    "signature_ingredients",
                                    "concerns",
                                    "skin_types",
                                    "sensitive_ok",
                                    "retinol_level",
                                    "main_functions",
                                    "formulation",
                                    "technology",
                                    "texture",
                                    "contraindications",
                                    "reason"
                                ]
                            }
                        }
                    },
                    "required": [
                        "category",
                        "role",
                        "ingredient_focus",
                        "purpose",
                        "candidates"
                    ]
                }
            }
        },
        "required": ["steps"]
    }

def build_rich_candidate_collection_prompt(user_data, analyzed_data):
    return f"""
あなたは日本で市販されているスキンケア商品を広く比較収集するリサーチ担当です。
このタスクでは最終選定はしません。比較用候補を広く集めることだけを行ってください。

【ユーザー情報】
年齢: {user_data.get("age", "")}
肌質: {user_data.get("oil", "")}
敏感度: {user_data.get("sens", "")}
レチノール経験: {user_data.get("exp", "")}
予算: {user_data.get("budget", "")}
肌の悩み（ユーザー申告）: {', '.join([_CONCERN_LABELS_JA.get(c, c) for c in (user_data.get('concerns') or [])]) or '未回答'}

【診断結果JSON】
{json.dumps(analyzed_data, ensure_ascii=False)}

【目的】
各ステップごとに、DB商品と同じ基準で比較できるだけの情報を持った候補を返してください。

【出力必須項目】
各候補には必ず以下を入れてください
- name
- price_ref
- active_ingredients
- support_ingredients
- signature_ingredients
- concerns
- skin_types
- sensitive_ok
- retinol_level
- main_functions
- formulation
- technology
- texture
- contraindications
- reason

【重要ルール】
・signature_ingredients はブランド独自成分や独自複合体を入れる
・共通成分は active_ingredients / support_ingredients に入れる
・DB商品と同じ基準で比較できるように、情報不足の商品にしない
・曖昧な場合でも、現実的に推定して埋める
・日本で比較的入手しやすい商品を優先する
・3〜5個の候補を返す
・JSONのみで返す

【concerns候補】
pores, acne, redness, oil_control, dryness, barrier, dullness, whitening, aging

【skin_types候補】
dry, oily, mixed, sensitive

【sensitive_ok候補】
yes, no, unknown

【texture候補】
light, watery, gel, medium, essence, cream, rich

【独自成分例】
rice_power_no11, rice_power_no6, madewhite, melazero, melazero_v2,
cica_reedle_complex, pore_refining_complex, sebum_control_complex,
white_tranex_complex, peptide_complex_5, bifida_complex, galactomyces_complex など

【共通成分タグ例】
vitamin_c, niacinamide, retinol, retinal, azelaic_acid, tranexamic_acid, pdrn, peptide, bakuchiol,
ceramide, hyaluronic_acid, polyglutamic_acid, beta_glucan, panthenol, allantoin, squalane, cholesterol,
amino_acid, urea, cica, teca, madecassoside, centella_extract, heartleaf, dipotassium_glycyrrhizate,
propolis, alpha_arbutin, arbutin, adenosine, glutathione, kojic_acid, aha, bha, pha, salicylic_acid,
glycolic_acid, lactic_acid, mandelic_acid, enzyme, clay, tocopherol, uv_filter, probiotic_ferment,
ferulic_acid, mugwort, lha, zinc_oxide, titanium_dioxide, bifida, galactomyces
"""

def enrich_steps_with_rich_market_candidates(data, candidate_data):
    extra_steps = candidate_data.get("steps", [])

    def enrich_step_list(step_list):
        for step in step_list:
            category = step.get("category", "")
            role = step.get("role", "")
            ingredient_focus = step.get("ingredient_focus", "")
            purpose = step.get("purpose", "")

            matched = None
            for extra in extra_steps:
                if (
                    extra.get("category", "") == category
                    and extra.get("role", "") == role
                    and extra.get("ingredient_focus", "") == ingredient_focus
                    and extra.get("purpose", "") == purpose
                ):
                    matched = extra
                    break

            if matched:
                original = step.get("product_candidates", [])
                original_objs = [{"name": x} for x in original if isinstance(x, str)]
                extra_objs = matched.get("candidates", [])

                merged = []
                seen = set()

                for item in original_objs + extra_objs:
                    name = item.get("name", "") if isinstance(item, dict) else str(item)
                    norm = normalize_candidate_name_for_merge(name)
                    if not norm:
                        continue
                    if norm in seen:
                        continue
                    seen.add(norm)
                    merged.append(item)

                    if len(merged) >= 80:
                        break

                step["product_candidates"] = merged

    enrich_step_list(data.get("morning", {}).get("steps", []))
    enrich_step_list(data.get("night", {}).get("steps", []))
    enrich_step_list(data.get("weekly_care", []))

    return data

def build_buy_lead(step):
    impacts = step.get("top_impacts", [])

    if not impacts:
        return "今の肌に合う基本ケア"

    top = impacts[0]

    label = top.get("label", "")
    value = top.get("value", 0)

    ingredient = step.get("ingredient_focus", "")

    return f"{label} +{value} → {ingredient}中心ケア"

def apply_db_product_to_step(step, product, user_data):
    if product is None:
        apply_category_fallback_to_step(step, user_data)
        return

    category = step.get("category", "")

    invalid_reasons = {
        "",
        "現在確認できる商品候補が見つかりませんでした。",
        "確認できる商品候補が見つかりませんでした。",
    }

    brand = str(product.get("brand", "") or "").strip()
    name = clean_display_product_name(
        str(product.get("name", category) or "").strip()
    )

    step["brand"] = brand
    step["product"] = name
    step["price"] = safe_price(product.get("price_ref", 0))
    step["estimated_price"] = step["price"]

    image_file = (
        product.get("image")
        or product.get("image_url")
        or product.get("image_path")
        or product.get("thumbnail")
        or product.get("thumbnail_url")
        or ""
    )

    if image_file:
        image_file = str(image_file).strip()
        if image_file.startswith("http://") or image_file.startswith("https://"):
            step["image"] = image_file.replace("http://", "https://")
        elif image_file.startswith("/static/"):
            step["image"] = image_file
        else:
            step["image"] = f"/static/images/products/{image_file}"
    else:
        step["image"] = ""

    step["affiliate_provider"] = product.get("affiliate_provider", "")
    step["affiliate_item_id"] = product.get("affiliate_item_id", "")

    affiliate_item_id = str(product.get("affiliate_item_id", "") or "").strip()
    rakuten_link = str(product.get("rakuten_link", "") or "").strip()

    if rakuten_link:
        step["rakuten_link"] = rakuten_link
    elif affiliate_item_id.startswith("http://") or affiliate_item_id.startswith("https://"):
        step["rakuten_link"] = affiliate_item_id
    else:
        step["rakuten_link"] = ""

    base_score = product.get("_base_score", product.get("base_score", 0)) or 0
    improve_score = product.get("_improve_score", product.get("improve_score", 0)) or 0
    routine_score = product.get("_routine_score", product.get("routine_score", 0)) or 0
    final_score = product.get("_score", product.get("final_score", product.get("match_score", 0))) or 0

    step["base_score"] = base_score
    step["improve_score"] = improve_score
    step["routine_score"] = routine_score
    step["final_score"] = final_score
    step["match_score"] = final_score

    step["improvement_score"] = improve_score
    step["improvement_reason"] = (
        product.get("_improvement_reason")
        or product.get("improvement_reason")
        or product.get("reason")
        or ""
    )

    step["score_detail"] = {
        "base": base_score,
        "improve": improve_score,
        "routine": routine_score,
        "final": final_score,
    }

    generated_reason = build_selection_reason_from_scores(
        product,
        step,
        user_data
    )

    fallback_reason = (
        build_recommend_reason(product, step, user_data)
        or product.get("_improvement_reason")
        or product.get("improvement_reason")
        or product.get("reason")
        or ""
    )

    if str(fallback_reason).strip() in invalid_reasons:
        fallback_reason = ""

    step["recommend_reason"] = (
        generated_reason
        or fallback_reason
        or build_ai_reason(step, user_data)
    )    

    step["product_source"] = product.get("_source", "db") or "db"

    impact = calculate_step_impact(step, product)
    step["impact_scores"] = impact
    step["top_impacts"] = format_top_impacts(impact)

    step["buy_lead"] = build_buy_lead(step)

def apply_ai_candidate_to_step(step, user_data, ai_image_db=None):
    category = step.get("category", "")
    candidates = step.get("product_candidates", [])

    product_name = candidates[0] if candidates else category

    step["product"] = product_name
    step["price"] = safe_price(step.get("estimated_price"))
    step["price_band"] = step.get("price_band", "")

    image_path = None
    if ai_image_db:
        image_path, price = find_ai_candidate_data(best.get("name"), ai_image_db)

    step["image"] = image_path if image_path else ""
    step["price"] = price if price else best.get("price_ref", 0)

    step["match_score"] = 0
    step["base_score"] = 0
    step["improve_score"] = 0
    step["recommend_reason"] = step.get("selection_reason") or build_ai_reason(step, user_data)
    step["product_source"] = "ai"

    impact = calculate_step_impact(step, None)
    step["impact_scores"] = impact
    step["top_impacts"] = format_top_impacts(impact)

    step["buy_lead"] = build_buy_lead(step)

def get_first_concrete_candidate(step):
    candidates = step.get("product_candidates", [])

    if not isinstance(candidates, list):
        return ""

    ng_words = ["おすすめ", "候補"]

    for c in candidates:
        if isinstance(c, dict):
            name = str(c.get("name", "") or "").strip()
        else:
            name = str(c or "").strip()

        if not name:
            continue

        if any(w in name for w in ng_words):
            continue

        return name

    return ""


def apply_category_fallback_to_step(step, user_data):
    category = str(step.get("category", "") or "美容液").strip()
    purpose = str(step.get("purpose", "") or "肌状態に合わせた基本ケア").strip()
    ingredient_focus = str(step.get("ingredient_focus", "") or "").strip()

    estimated_price = safe_price(step.get("estimated_price", 0))
    price = safe_price(step.get("price", 0))
    final_price = price if price > 0 else estimated_price

    candidate_name = get_first_concrete_candidate(step)

    if candidate_name:
        step["product"] = candidate_name
        step["product_source"] = "ai_fallback"
    else:
        step["product"] = ""
        step["product_source"] = "missing"

    step["price"] = final_price
    step["estimated_price"] = final_price
    step["price_band"] = build_price_band(final_price) if final_price > 0 else "価格不明"
    step["image"] = ""

    if not step.get("recommend_reason"):
        if ingredient_focus:
            step["recommend_reason"] = f"{purpose}を目的に、{ingredient_focus}を意識した{category}として提案しています。"
        else:
            step["recommend_reason"] = f"{purpose}を目的に、肌状態に合わせやすい{category}として提案しています。"

    step["match_score"] = step.get("match_score", 0) or 0
    step["base_score"] = step.get("base_score", 0) or 0
    step["improve_score"] = step.get("improve_score", 0) or 0

    impact = calculate_step_impact(step, None)
    step["impact_scores"] = impact
    step["top_impacts"] = format_top_impacts(impact)

    if not isinstance(step.get("top_candidates"), list):
        step["top_candidates"] = []

    return normalize_step_price_fields(step)

def supplement_night_steps_from_morning(data):
    """
    化粧水・美容液がnight_stepsにない場合、morning_stepsから複製して補完する。
    AIが朝のみに設定した場合でも夜のスキンケアセクションに表示されるようにする。
    attach_affiliate_links_to_all_stepsの後に呼ぶこと（コピー元にリンクが付いている状態）。
    """
    ALWAYS_NIGHTLY = {"化粧水", "美容液"}
    CLEANSER_CATS  = {"洗顔", "クレンジング"}
    CAT_ORDER      = {"化粧水": 0, "美容液": 1}

    morning_steps = data.get("morning", {}).get("steps", [])
    night = data.get("night", {})
    if not isinstance(night, dict):
        return data
    night_steps = night.get("steps", [])
    if not isinstance(night_steps, list):
        return data

    night_cats = {str(s.get("category") or "").strip() for s in night_steps if isinstance(s, dict)}

    to_add = sorted(
        [
            copy.deepcopy(s) for s in morning_steps
            if isinstance(s, dict)
            and str(s.get("category") or "").strip() in ALWAYS_NIGHTLY
            and str(s.get("category") or "").strip() not in night_cats
        ],
        key=lambda s: CAT_ORDER.get(str(s.get("category") or "").strip(), 99)
    )

    if not to_add:
        return data

    # 補完ステップを末尾に追加してから再ソート（挿入位置を計算するより確実）
    for step in to_add:
        night_steps.append(step)

    # CATEGORY_ORDER / use_timing に従って正しい順序に並び替え
    night_steps.sort(key=step_sort_key)

    return data


def build_weekly_usage_plan(data):
    """
    各stepのuse_daysフィールドに従って週間スケジュールを組み立てる。
    成分名・刺激性・スコア閾値の判断はGeminiに委譲し、コードは一切行わない。
    """
    if not isinstance(data, dict):
        return []

    routine_strategy = data.get("routine_strategy", {})
    if not isinstance(routine_strategy, dict):
        routine_strategy = {}

    morning_steps = data.get("morning", {}).get("steps", [])
    night_steps   = data.get("night",   {}).get("steps", [])
    weekly_steps  = data.get("weekly_care", [])

    if not isinstance(morning_steps, list): morning_steps = []
    if not isinstance(night_steps, list):   night_steps   = []
    if not isinstance(weekly_steps, list):  weekly_steps  = []

    days = ["月", "火", "水", "木", "金", "土", "日"]

    # 朝の週間ルーティンに表示するカテゴリ（特別ケアのみ・日焼け止めは毎日固定なので除外）
    MORNING_DISPLAY_CATEGORIES = {"化粧水", "美容液", "パック", "ピーリング", "ブースター"}
    # 夜の週間ルーティンから除外するカテゴリ（毎日必須なので省略）
    NIGHT_EXCLUDE_CATEGORIES = {"洗顔", "クレンジング"}
    # 夜: AIがnight_stepsに含めた場合、use_daysに関わらず毎日表示するカテゴリ
    # （AIがルーティンに入れた=毎日必要と判断したとみなす。入れていなければそのまま非表示）
    NIGHT_ALWAYS_DAILY = {"化粧水", "美容液"}
    # 夜: use_days=[]（毎日同じ）なら週間ビューに出さないカテゴリ
    NIGHT_SUPPRESS_IF_DAILY = {"乳液", "クリーム"}

    def step_label(step):
        if not isinstance(step, dict):
            return ""
        category = str(step.get("category") or "").strip()
        product  = str(step.get("product")  or "").strip()
        brand    = str(step.get("brand")    or "").strip()
        if brand and product and not product.startswith(brand):
            product = f"{brand} {product}"
        return f"{category}: {product}" if product else category

    def get_use_days(step):
        if not isinstance(step, dict):
            return []
        v = step.get("use_days", [])
        if isinstance(v, list) and v:
            return v
        # use_days=[] はAIが「毎日使用可」と判断した結果 → 全曜日で表示
        return _ALL_DAYS

    def is_morning_display(step):
        return str(step.get("category") or "").strip() in MORNING_DISPLAY_CATEGORIES if isinstance(step, dict) else False

    def is_night_display(step):
        return str(step.get("category") or "").strip() not in NIGHT_EXCLUDE_CATEGORIES if isinstance(step, dict) else False

    # 朝は毎日固定（use_daysに関わらず全表示カテゴリを並べる）
    fixed_morning = [
        step_label(s) for s in morning_steps
        if isinstance(s, dict) and is_morning_display(s) and step_label(s)
    ]

    overall_note = str(routine_strategy.get("overall_policy") or "")

    # 乳液/クリームで use_days=[]（毎日同じ）のstepは週間ビューから除外
    suppress_daily_ids = {
        id(s) for s in night_steps
        if isinstance(s, dict)
        and str(s.get("category") or "").strip() in NIGHT_SUPPRESS_IF_DAILY
        and not s.get("use_days")
    }

    # NIGHT_ALWAYS_DAILYのうちnight_stepsに存在しないカテゴリをmorning_stepsから補完
    # （AIが化粧水を朝のみに設定した場合でも夜に表示させる）
    night_daily_cats_covered = {
        str(s.get("category") or "").strip()
        for s in night_steps
        if isinstance(s, dict) and str(s.get("category") or "").strip() in NIGHT_ALWAYS_DAILY
    }
    morning_supplement_night = [
        step_label(s)
        for s in morning_steps
        if isinstance(s, dict)
        and str(s.get("category") or "").strip() in NIGHT_ALWAYS_DAILY
        and str(s.get("category") or "").strip() not in night_daily_cats_covered
        and step_label(s)
    ]

    usage_plan = []
    for day in days:
        # 夜: 洗顔・クレンジング以外を表示
        # - 化粧水・美容液: use_days無視で毎日表示。night_stepsになければmorning_stepsから補完
        # - 乳液/クリームでuse_days=[]（毎日同じ）のものは除外（冗長なので省略）
        # - その他: use_days=[]は毎日、指定曜日のみその日に表示
        night_items = morning_supplement_night + [
            step_label(s)
            for s in night_steps
            if isinstance(s, dict)
            and is_night_display(s)
            and step_label(s)
            and id(s) not in suppress_daily_ids
            and (
                str(s.get("category") or "").strip() in NIGHT_ALWAYS_DAILY  # 化粧水・美容液は常時
                or not s.get("use_days")
                or day in (s.get("use_days") or [])
            )
        ]

        # 週ケア: use_daysにこの曜日が含まれるstepのみ表示
        special_care = [
            step_label(s)
            for s in weekly_steps
            if isinstance(s, dict)
            and step_label(s)
            and day in get_use_days(s)
        ]

        usage_plan.append({
            "day": day,
            "morning": fixed_morning,
            "night": night_items,
            "special_care": special_care,
            "note": overall_note,
        })

    return usage_plan

def finalize_budget_info(data, budget_value):
    if not isinstance(data, dict):
        data = {}

    data["input_budget"] = safe_price(budget_value)
    data["total_price"] = calculate_total_price(data)

    budget_fit_plan = build_budget_fit_plan(data, budget_value)
    data["budget_fit_plan"] = budget_fit_plan
    data["budget_fit_total"] = safe_price(budget_fit_plan.get("total_price", 0))

    if budget_value > 0:
        if data["total_price"] <= budget_value:
            data["budget_status"] = "予算内"
        else:
            data["budget_status"] = "予算オーバー"
    else:
        data["budget_status"] = "予算未入力"

    return data


def debug_candidate_counts(data):
    print("===== CANDIDATE COUNTS =====")
    for section in ["morning", "night"]:
        for step in data.get(section, {}).get("steps", []):
            print(section, step.get("category"), step.get("ingredient_focus"), len(step.get("product_candidates", [])))
    for step in data.get("weekly_care", []):
        print("weekly_care", step.get("category"), step.get("ingredient_focus"), len(step.get("product_candidates", [])))
    print("============================")
# # AI肌診断ページ
@app.route("/lab", methods=["GET", "POST"])
def lab_test_function():
    print("[LAB ENTER]", request.method, flush=True)
    if request.method == "POST":
        lab_t0 = time.time()
        print("[LAB TIME] start 0.0", flush=True)
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        try:
            client_ip = get_client_ip()
            _is_creator = is_creator()
            _is_premium = is_premium_user()
            _premium_key = request.args.get("premium_key", "")

            if _is_creator:
                pass  # 作成者は制限なし
            elif _is_premium:
                if not can_use_premium_diagnosis(_premium_key):
                    return render_template(
                        "lab.html",
                        error=f"今月の診断回数（月{PREMIUM_MONTHLY_LIMIT}回）に達しました。来月また利用できます。",
                        is_premium=True,
                        premium_key=_premium_key,
                        remaining_premium_count=0,
                        DISABLE_USAGE_LIMIT=DISABLE_USAGE_LIMIT
                    )
            elif not can_use_free_diagnosis(client_ip):
                return render_template(
                    "lab.html",
                    error=f"無料診断は月{FREE_MONTHLY_LIMIT}回までです。続けて利用するには有料プランをご利用ください。",
                    remaining_free_count=0,
                    DISABLE_USAGE_LIMIT=DISABLE_USAGE_LIMIT
                )

            ip = request.remote_addr

            if not _is_creator and is_rate_limited(ip):
                if is_ajax:
                    return jsonify({"success": False, "error": "本日の診断回数の上限に達しました。明日またお試しください。"}), 429
                return render_template("lab.html", error="本日の診断回数の上限に達しました。明日またお試しください。", is_premium=_is_premium, premium_key=_premium_key, DISABLE_USAGE_LIMIT=DISABLE_USAGE_LIMIT)
            validate_lab_dependencies()
            # =========================
            # ① 入力取得
            # =========================
            user_data = extract_user_data(request)
            print("===== USER DATA DEBUG =====", flush=True)

            for k, v in user_data.items():
                print(k, "=", repr(v), flush=True)

            print("===========================", flush=True)
            debug_log("START LAB")
            debug_log("USER DATA", user_data)

            # =========================
            # ② 画像取得
            # =========================
            front_img, left_img, right_img = load_uploaded_images(request)
            if not can_use_global_diagnosis():
                message = "現在、今月の診断上限に達しています。来月以降に再度お試しください。"

                if is_ajax:
                    return jsonify({
                        "success": False,
                        "message": message
                    }), 429

                return render_template(
                    "error.html",
                    error_message=message
                )
            global_used = get_global_usage_count()
            global_remaining = GLOBAL_MONTHLY_LIMIT - global_used
            # =========================
            # ③ AI分析
            # =========================

            # 診断ごとにセッションキャッシュをリセット（メモリリーク防止）
            global _rakuten_criteria_cache, _rakuten_criteria_call_count, _step_rakuten_results
            _rakuten_criteria_cache = {}
            _rakuten_criteria_call_count = 0
            _step_rakuten_results = {}

            _log_mem("diag-start")

            try:
                print("[LAB CHECK] before Gemini", flush=True)

                data = analyze_skin_with_gemini(
                    user_data,
                    front_img,
                    left_img,
                    right_img
                )

                print("[LAB CHECK] after Gemini", flush=True)
                _log_mem("after-gemini-analysis")

            except Exception as e:
                print("===== LAB ERROR =====")
                print(e)
                traceback.print_exc()
                print("=====================")

                error_text = str(e)

                _quota_keywords = ["RESOURCE_EXHAUSTED", "quota", "RATE_LIMIT_EXCEEDED"]
                _is_quota_error = (
                    any(kw.lower() in error_text.lower() for kw in _quota_keywords)
                    or ("429" in error_text and "RESOURCE_EXHAUSTED" in error_text)
                )

                message = "診断中にエラーが発生しました。時間をおいて再度お試しください。"

                if _is_quota_error:
                    message = "現在、診断サービスを一時停止しています。ご不便をおかけして申し訳ありません。復旧までしばらくお待ちください。"

                elif "503" in error_text or "UNAVAILABLE" in error_text:
                    message = "現在診断が混み合っています。少し時間をおいて再度お試しください。"

                elif "429" in error_text:
                    message = "現在診断利用が集中しています。しばらくしてから再度お試しください。"

                if is_ajax:
                    return jsonify({
                        "success": False,
                        "message": message
                    }), 503
                    
                return render_template(
                    "lab.html",
                    error_message=str(e),
                    remaining_free_count=get_remaining_free_count(get_client_ip()),
                    global_used=get_global_usage_count(),
                    global_remaining=GLOBAL_MONTHLY_LIMIT - get_global_usage_count(),
                    DISABLE_USAGE_LIMIT=DISABLE_USAGE_LIMIT
                  )

            if not isinstance(data, dict):
                raise RuntimeError("analyze_skin_with_gemini の戻り値が dict ではありません")

            data = ensure_result_structure(data)
            data["skin_score"] = calculate_skin_score(data.get("scores", {}))
            data["premium_scores"] = calculate_premium_scores(
                data.get("scores", {})
            )
            symmetry_analysis = data.get("symmetry_analysis", {})

            if not isinstance(symmetry_analysis, dict):
                symmetry_analysis = {}

            data["premium_scores"]["symmetry"] = safe_int(
                symmetry_analysis.get("score", 0)
            )

            data["symmetry_analysis"] = {
                "score": safe_int(symmetry_analysis.get("score", 0)),
                "summary": str(symmetry_analysis.get("summary", "") or ""),
                "left_tendency": str(symmetry_analysis.get("left_tendency", "") or ""),
                "right_tendency": str(symmetry_analysis.get("right_tendency", "") or "")
            }
            debug_log("AFTER ANALYZE", {
                "skin_score": data.get("skin_score"),
                "summary": data.get("skin_summary"),
                "morning_steps": len(data.get("morning", {}).get("steps", [])),
                "night_steps": len(data.get("night", {}).get("steps", [])),
                "weekly_steps": len(data.get("weekly_care", [])),
            })
            # ===== DEV_MODE_START =====
            if DEV_MODE:
                debug_log("DEV MODE ACTIVE")
            # ===== DEV_MODE_END =====

           

            if "morning" not in data or not isinstance(data.get("morning"), dict):
                data["morning"] = {"steps": []}

            if "night" not in data or not isinstance(data.get("night"), dict):
                data["night"] = {"steps": []}

            if "weekly_care" not in data or not isinstance(data.get("weekly_care"), list):
                data["weekly_care"] = []

            if "steps" not in data["morning"] or not isinstance(data["morning"].get("steps"), list):
                data["morning"]["steps"] = []

            if "steps" not in data["night"] or not isinstance(data["night"].get("steps"), list):
                data["night"]["steps"] = []

            # =========================
            # ④ AI候補拡張
            # =========================
            # 高速化のため、別Gemini呼び出しは停止。
            # product_candidates は analyze_skin_with_gemini の1回目の診断結果で返させる。
            debug_log("SKIP CANDIDATE ENRICH", "product_candidates are generated in analyze_skin_with_gemini")

            debug_log("AFTER CANDIDATE ENRICH")
            debug_step_summary("morning enriched", data.get("morning", {}).get("steps", []))
            debug_step_summary("night enriched", data.get("night", {}).get("steps", []))
            debug_step_summary("weekly enriched", data.get("weekly_care", []))

            if not isinstance(data.get("morning", {}).get("steps"), list):
                data["morning"]["steps"] = []

            if not isinstance(data.get("night", {}).get("steps"), list):
                data["night"]["steps"] = []

            if not isinstance(data.get("weekly_care"), list):
                data["weekly_care"] = []

            # =========================
            # ⑤ ラベル正規化・構成補正
            # =========================
            data = normalize_ai_labels(data)
            data = normalize_serum_roles(data)
            data = enforce_booster_night_only(data)
            data["improvement_plan"] = build_score_based_improvement_plan(
                data.get("scores", {}),
                data.get("improvement_plan", {})
            )
            data = apply_moisture_plan(data)
            data = ensure_required_routine_steps(data)

            print("[FLOW AFTER ENSURE]", {
                "night": [
                    {
                        "category": s.get("category", ""),
                        "product": s.get("product", ""),
                        "source": s.get("product_source", "")
                    }
                    for s in data.get("night", {}).get("steps", [])
                    if isinstance(s, dict) and s.get("category") in ["クリーム", "乳液"]
                ],
                "weekly": [
                    {
                        "category": s.get("category", ""),
                        "product": s.get("product", ""),
                        "source": s.get("product_source", "")
                    }
                    for s in data.get("weekly_care", [])
                    if isinstance(s, dict) and s.get("category") in ["パック", "ピーリング"]
                ],
            }, flush=True)
            # serum制限は product選定後のほうが安全
            # ここではまだやらない

            # =========================
            # ⑥ DB読み込み（Phase3: 固定DB廃止 → 空リストを渡し楽天+Geminiのみで選定）
            # =========================
            products = []

            # =========================
            # ⑦ 商品割当
            # =========================
            print(
                "[IMPROVEMENT PLAN BEFORE ASSIGN]",
                data.get("improvement_plan", {}),
                flush=True
            )
            budget_value = parse_budget(user_data.get("budget", ""))
            debug_log("BUDGET VALUE", budget_value)
            _log_mem("before-assign-products")

            data = assign_products_to_all_steps(data, products, user_data, budget_value)
            _log_mem("after-assign-products")

            print("[FLOW AFTER ASSIGN]", {
                "night": [
                    {
                        "category": s.get("category", ""),
                        "product": s.get("product", ""),
                        "source": s.get("product_source", ""),
                        "reason": s.get("recommend_reason", "")
                    }
                    for s in data.get("night", {}).get("steps", [])
                    if isinstance(s, dict) and s.get("category") in ["クリーム", "乳液"]
                ],
                "weekly": [
                    {
                        "category": s.get("category", ""),
                        "product": s.get("product", ""),
                        "source": s.get("product_source", ""),
                        "reason": s.get("recommend_reason", "")
                    }
                    for s in data.get("weekly_care", [])
                    if isinstance(s, dict) and s.get("category") in ["パック", "ピーリング"]
                ],
            }, flush=True)
            
            affiliate_ai_db = load_affiliate_links_ai()
            
            debug_log("AFTER ASSIGN PRODUCTS")
            debug_step_summary("morning assigned", data.get("morning", {}).get("steps", []))
            debug_step_summary("night assigned", data.get("night", {}).get("steps", []))
            debug_step_summary("weekly assigned", data.get("weekly_care", []))

            # =========================
            # ⑧ 選定後の調整
            # =========================
            data = limit_serum_steps(data)
            data = limit_booster_steps(data)
            data = sort_steps(data)

            # =========================
            # ⑨ 最終整形
            # =========================
            data = finalize_result_data(data, user_data)

            print("[FLOW AFTER FINALIZE]", {
                "night": [
                    {
                        "category": s.get("category", ""),
                        "product": s.get("product", ""),
                        "image": bool(s.get("image")),
                        "rakuten": bool(s.get("rakuten_link")),
                        "source": s.get("product_source", "")
                    }
                    for s in data.get("night", {}).get("steps", [])
                    if isinstance(s, dict) and s.get("category") in ["クリーム", "乳液"]
                ],
                "weekly": [
                    {
                        "category": s.get("category", ""),
                        "product": s.get("product", ""),
                        "image": bool(s.get("image")),
                        "rakuten": bool(s.get("rakuten_link")),
                        "source": s.get("product_source", "")
                    }
                    for s in data.get("weekly_care", [])
                    if isinstance(s, dict) and s.get("category") in ["パック", "ピーリング"]
                ],
            }, flush=True)

            data = attach_affiliate_links_to_all_steps(data, affiliate_ai_db)

            # 化粧水・美容液がnight_stepsにない場合はmorning_stepsから補完
            data = supplement_night_steps_from_morning(data)

            # 週ケアとnight刺激成分の曜日衝突を強制解消
            data = resolve_weekly_care_day_conflicts(data)
            # 夜ルーティン内のレチノイド×BHA/SA洗顔料の曜日衝突を解消
            data = resolve_night_irritant_conflicts(data)

            # 楽天商品名をGeminiで短く整形（rakuten_criteria / ai_rakuten_verified のみ対象）
            data = gemini_clean_rakuten_product_names(data)

            # Geminiによる商品選定理由・1位vs2位比較文を生成
            data = gemini_generate_selection_reasons(data, user_data)

            print("[FLOW AFTER AFFILIATE]", {
                "night": [
                    {
                        "category": s.get("category", ""),
                        "product": s.get("product", ""),
                        "image": bool(s.get("image")),
                        "rakuten": bool(s.get("rakuten_link")),
                        "source": s.get("product_source", "")
                    }
                    for s in data.get("night", {}).get("steps", [])
                    if isinstance(s, dict) and s.get("category") in ["クリーム", "乳液"]
                ],
                "weekly": [
                    {
                        "category": s.get("category", ""),
                        "product": s.get("product", ""),
                        "image": bool(s.get("image")),
                        "rakuten": bool(s.get("rakuten_link")),
                        "source": s.get("product_source", "")
                    }
                    for s in data.get("weekly_care", [])
                    if isinstance(s, dict) and s.get("category") in ["パック", "ピーリング"]
                ],
            }, flush=True)

            debug_log("AFTER FINALIZE")
            debug_step_summary("morning finalized", data.get("morning", {}).get("steps", []))
            debug_step_summary("night finalized", data.get("night", {}).get("steps", []))
            debug_step_summary("weekly finalized", data.get("weekly_care", []))

            # =========================
            # ⑩ 予算情報
            # =========================
            data = finalize_budget_info(data, budget_value)
            data["weekly_usage_plan"] = build_weekly_usage_plan(data)
            print("[LAB TIME] after build_weekly_usage_plan", round(time.time() - lab_t0, 2), flush=True)
            print(
                "[WEEKLY USAGE PLAN]",
                json.dumps(
                    data.get("weekly_usage_plan", []),
                    ensure_ascii=False
                ),
                flush=True
            )

            debug_log("PRICE SUMMARY", {
                "total_price": data.get("total_price", 0),
                "budget_fit_total": data.get("budget_fit_total", 0),
                "budget_status": data.get("budget_status", "")
            })

            # =========================
            # ⑪ 保存
            # =========================
            debug_log("SAVE READY", {
                "skin_score": data.get("skin_score", 0),
                "record_date": data.get("record_date", ""),
                "analysis_date": data.get("analysis_date", "")
            })

            try:
                if _is_creator:
                    pass  # 作成者はカウントしない
                elif _is_premium:
                    increment_premium_usage(_premium_key)
                else:
                    increment_free_usage(client_ip)
                increment_global_usage()
            except Exception as e:
                print("===== USAGE SAVE ERROR =====")
                print(e)

            data["client_ip"] = client_ip
            data["user_id"] = get_or_create_user_id()
            flask_session["client_ip"] = client_ip
            saved_record = None
            try:
                saved_record = append_result(lightweight_result_payload(data), is_premium=bool(_is_premium or _is_creator))
                if isinstance(saved_record, dict) and saved_record.get("id"):
                    data["id"] = saved_record["id"]
            except Exception as e:
                print("===== RESULT SAVE ERROR =====")
                print(e)
                traceback.print_exc()
                print("=============================")
                # 保存に失敗しても結果表示は止めない

            # =========================
            # ⑫ 表示
            # =========================

            data = lightweight_result_payload(data)
            data["is_premium"] = is_premium_user()
            data["is_dev_mode"] = DEV_MODE or DEV_PREMIUM_MODE
            print("[LAB TIME] before render_template", round(time.time() - lab_t0, 2), flush=True)
            _log_mem("before-render")
            html = render_template(
                "result.html",
                data=data,
                result_id=data.get("id", "")
            )
            if is_ajax:
                return jsonify({
                    "success": True,
                    "html": html
                })

            return html

        except ValueError as e:
            print("\n===== LAB VALUE ERROR =====")
            print(str(e))
            traceback.print_exc()
            print("===========================\n")
            return f"<pre>{traceback.format_exc()}</pre>"

        except Exception as e:
            print("\n===== LAB ERROR =====")
            print("ERROR:", e)
            traceback.print_exc()
            print("=====================\n")

            user_error = build_user_friendly_error_message(
                    str(e)
                )

            if is_ajax:
                return jsonify({
                    "success": False,
                    "message": user_error
                }), 500

            return render_template(
                "error.html",
                error_message=user_error
            )
    client_ip = get_client_ip()
    _is_creator = is_creator()
    is_premium = is_premium_user()
    premium_key = request.args.get("premium_key", "")
    remaining_free_count = get_remaining_free_count(client_ip)
    remaining_premium_count = get_remaining_premium_count(premium_key) if (is_premium and not _is_creator) else None
    gemini_usage = get_gemini_usage_status()
    return render_template(
        "lab.html",
        remaining_free_count=remaining_free_count,
        remaining_premium_count=remaining_premium_count,
        gemini_usage=gemini_usage,
        DISABLE_USAGE_LIMIT=DISABLE_USAGE_LIMIT,
        is_premium=is_premium,
        is_creator=_is_creator,
        premium_key=premium_key
    )

@app.route("/creator-auth")
def creator_auth():
    key = request.args.get("key", "")
    if not CREATOR_KEY or key != CREATOR_KEY:
        return "403 Forbidden", 403
    token = _creator_token()
    resp = make_response(redirect("/lab"))
    resp.set_cookie(
        "creator_token", token,
        max_age=365 * 24 * 3600,
        httponly=True,
        samesite="Lax"
    )
    return resp


@app.route("/creator-login", methods=["GET", "POST"])
def creator_login():
    error = ""
    if request.method == "POST":
        key = request.form.get("key", "")
        if CREATOR_KEY and key == CREATOR_KEY:
            token = _creator_token()
            resp = make_response(redirect("/lab"))
            resp.set_cookie(
                "creator_token", token,
                max_age=365 * 24 * 3600,
                httponly=True,
                samesite="Lax"
            )
            return resp
        error = "キーが正しくありません"
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Creator ログイン</title>
<style>
  body {{ font-family: sans-serif; background: #fff5f8; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }}
  .box {{ background: #fff; border-radius: 16px; padding: 32px 24px; box-shadow: 0 4px 20px rgba(199,91,122,0.12); width: 100%; max-width: 360px; text-align: center; }}
  h2 {{ color: #c75b7a; margin: 0 0 24px; font-size: 18px; }}
  input {{ width: 100%; box-sizing: border-box; padding: 12px 14px; border: 1.5px solid #e8a5bc; border-radius: 10px; font-size: 15px; margin-bottom: 14px; outline: none; }}
  input:focus {{ border-color: #c75b7a; }}
  button {{ width: 100%; padding: 12px; background: linear-gradient(135deg, #e07a9a, #c75b7a); color: #fff; font-size: 15px; font-weight: bold; border: none; border-radius: 10px; cursor: pointer; }}
  .error {{ color: #c75b7a; font-size: 13px; margin-bottom: 10px; }}
</style>
</head>
<body>
<div class="box">
  <h2>✦ Creator ログイン</h2>
  {'<p class="error">' + error + '</p>' if error else ''}
  <form method="POST">
    <input type="password" name="key" placeholder="Creator キーを入力" autofocus>
    <button type="submit">ログイン</button>
  </form>
</div>
</body>
</html>"""

def _admin_authorized():
    return request.args.get("key") == os.getenv("ADMIN_KEY", "") and os.getenv("ADMIN_KEY", "")

@app.route("/admin/premium-keys", methods=["GET"])
def admin_premium_keys():
    if not _admin_authorized():
        return "403 Forbidden", 403
    keys = load_premium_keys()
    admin_key = request.args.get("key", "")
    from datetime import datetime
    return render_template("admin_premium.html", keys=keys, admin_key=admin_key,
                           site_url=SITE_URL.rstrip("/") if SITE_URL else request.host_url.rstrip("/"),
                           now_str=datetime.utcnow().isoformat())

@app.route("/admin/premium-keys/issue", methods=["POST"])
def admin_issue_key():
    if request.args.get("key") != os.getenv("ADMIN_KEY", "") or not os.getenv("ADMIN_KEY", ""):
        return "403 Forbidden", 403
    email = request.form.get("email", "").strip()
    days  = int(request.form.get("days", 35))
    send  = request.form.get("send_email") == "1"
    admin_key = request.args.get("key", "")
    if not email:
        return redirect(f"/admin/premium-keys?key={admin_key}&error=email_required")
    key = issue_premium_key_manual(email, days)
    if send:
        send_premium_email(email, key)
    return redirect(f"/admin/premium-keys?key={admin_key}&issued={key}&email={email}")

@app.route("/admin/premium-keys/revoke", methods=["POST"])
def admin_revoke_key():
    if request.args.get("key") != os.getenv("ADMIN_KEY", "") or not os.getenv("ADMIN_KEY", ""):
        return "403 Forbidden", 403
    target_key = request.form.get("target_key", "")
    admin_key  = request.args.get("key", "")
    revoke_premium_key_direct(target_key)
    return redirect(f"/admin/premium-keys?key={admin_key}&revoked=1")

@app.route("/admin/db-stats")
def db_stats():
    admin_key = request.args.get("key", "")

    _required_key = os.getenv("ADMIN_KEY", "")
    if not _required_key or admin_key != _required_key:
        return jsonify({
            "error": "unauthorized"
        }), 403

    products = load_products()

    from collections import Counter, defaultdict

    category_counter = Counter()
    focus_counter = defaultdict(Counter)

    for p in products:
        if not isinstance(p, dict):
            continue

        category = p.get("category", "未分類")
        category_counter[category] += 1

        focuses = p.get("ingredient_focus", [])

        if isinstance(focuses, str):
            focuses = [focuses]

        if not isinstance(focuses, list):
            focuses = []

        if not focuses:
            focus_counter[category]["未設定"] += 1
            continue

        for focus in focuses:
            focus = str(focus).strip()
            if focus:
                focus_counter[category][focus] += 1

    return jsonify({
        "category_counts": dict(category_counter),
        "ingredient_focus_counts": {
            category: dict(counter)
            for category, counter in focus_counter.items()
        }
    })

@app.route("/premium")
def premium():
    is_premium = is_premium_user()
    premium_key = request.args.get("premium_key", "")
    return render_template("premium.html", is_premium=is_premium, premium_key=premium_key)

# ==========================================
# Stripe決済
# ==========================================

@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    try:
        if not stripe.api_key or not STRIPE_PRICE_ID:
            return jsonify({"error": "Stripe未設定"}), 500

        base_url = SITE_URL.rstrip("/") if SITE_URL else request.host_url.rstrip("/")
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            success_url=f"{base_url}/premium-success",
            cancel_url=f"{base_url}/premium",
            customer_email=request.form.get("email") or None,
            locale="ja",
        )
        return redirect(session.url, code=303)
    except Exception as e:
        print(f"[STRIPE ERROR] {repr(e)}", flush=True)
        return jsonify({"error": "決済処理中にエラーが発生しました。しばらくしてから再度お試しください。"}), 500


@app.route("/stripe-webhook", methods=["POST"], strict_slashes=False)
def stripe_webhook():
    print("[STRIPE WEBHOOK] リクエスト受信", flush=True)
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        print("[STRIPE WEBHOOK] 署名検証失敗", flush=True)
        return jsonify({"error": "invalid signature"}), 400
    except Exception as e:
        print(f"[STRIPE WEBHOOK ERROR] {repr(e)}", flush=True)
        return jsonify({"error": str(e)}), 400

    event_type = event.get("type", "") if isinstance(event, dict) else getattr(event, "type", "")
    print(f"[STRIPE WEBHOOK] event={event_type}", flush=True)

    try:
        obj = event["data"]["object"]

        def sg(o, key, default=""):
            """StripeObject / dict 両対応の安全な値取得"""
            if isinstance(o, dict):
                return o.get(key, default)
            return getattr(o, key, default) or default

        # 支払い完了（新規）
        if event_type == "checkout.session.completed":
            customer_details = sg(obj, "customer_details", None)
            email = sg(obj, "customer_email") or sg(customer_details, "email")
            customer_id = str(sg(obj, "customer"))
            subscription_id = str(sg(obj, "subscription"))
            print(f"[STRIPE] checkout completed: email={email} sub={subscription_id}", flush=True)
            if email:
                key = issue_premium_key(email, customer_id, subscription_id)
                send_premium_email(email, key)
                print(f"[STRIPE] キー発行完了: {email}", flush=True)

        # 請求成功（更新時のキー延長）
        elif event_type == "invoice.payment_succeeded":
            email = str(sg(obj, "customer_email"))
            customer_id = str(sg(obj, "customer"))
            sub = sg(obj, "subscription", None)
            # subscription は文字列IDまたは展開StripeObjectの両方に対応
            if sub is None or sub == "":
                subscription_id = ""
            elif isinstance(sub, str):
                subscription_id = sub
            else:
                subscription_id = str(sg(sub, "id"))
            print(f"[STRIPE] invoice succeeded: email={email} sub={subscription_id}", flush=True)
            if email and subscription_id:
                issue_premium_key(email, customer_id, subscription_id)
                print(f"[STRIPE] キー延長完了: {email}", flush=True)

        # サブスク解約
        elif event_type == "customer.subscription.deleted":
            subscription_id = str(sg(obj, "id"))
            print(f"[STRIPE] subscription deleted: sub={subscription_id}", flush=True)
            if subscription_id:
                revoke_premium_key_by_subscription(subscription_id)
                print(f"[STRIPE] キー失効完了: sub={subscription_id}", flush=True)

        else:
            print(f"[STRIPE WEBHOOK] 未処理イベント: {event_type}", flush=True)

    except Exception as e:
        print(f"[STRIPE WEBHOOK 処理エラー] event={event_type} error={repr(e)}", flush=True)
        traceback.print_exc()
        return jsonify({"error": "internal processing error"}), 500

    return jsonify({"status": "ok"})


@app.route("/premium-success")
def premium_success():
    return render_template("premium_success.html")


@app.route("/privacy-policy")
def privacy_policy():
    return render_template("privacy_policy.html")


@app.route("/terms-of-service")
def terms_of_service():
    return render_template("terms_of_service.html")


@app.route("/portal-by-email", methods=["GET", "POST"])
def portal_by_email():
    if request.method == "GET":
        return render_template("portal_by_email.html")

    email = (request.form.get("email") or "").strip().lower()
    if not email:
        return render_template("portal_by_email.html", error="メールアドレスを入力してください。")

    # premium_keys からメールで一致するエントリを検索
    keys = load_premium_keys()
    customer_id = None
    matched_key = None
    for key, entry in keys.items():
        if (entry.get("email") or "").strip().lower() == email and not entry.get("revoked", False):
            customer_id = entry.get("stripe_customer_id", "")
            matched_key = key
            break

    if not customer_id:
        return render_template(
            "portal_by_email.html",
            submitted_email=email,
            error="入力されたメールアドレスに紐づくプレミアム会員が見つかりませんでした。ご登録時のアドレスをご確認ください。"
        )

    if not stripe.api_key:
        return render_template("error.html", error_message="決済システムへの接続に失敗しました。")

    try:
        base_url = SITE_URL.rstrip("/") if SITE_URL else request.host_url.rstrip("/")
        return_url = f"{base_url}/lab?premium_key={matched_key}" if matched_key else f"{base_url}/lab"
        portal_session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=return_url
        )
        return redirect(portal_session.url)
    except Exception as e:
        print(f"[PORTAL BY EMAIL ERROR] {repr(e)}", flush=True)
        return render_template("error.html", error_message="カスタマーポータルへの接続に失敗しました。しばらく経ってからお試しください。")


@app.route("/customer-portal")
def customer_portal():
    premium_key = request.args.get("premium_key", "")
    if not premium_key:
        return redirect("/premium")

    keys = load_premium_keys()
    entry = keys.get(premium_key)
    if not entry or entry.get("revoked", False):
        return redirect("/premium")

    customer_id = entry.get("stripe_customer_id", "")
    if not customer_id or not stripe.api_key:
        return render_template("error.html", error_message="カスタマー情報が見つかりません。サポートにお問い合わせください。")

    try:
        base_url = SITE_URL.rstrip("/") if SITE_URL else request.host_url.rstrip("/")
        portal_session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{base_url}/lab?premium_key={premium_key}"
        )
        return redirect(portal_session.url)
    except Exception as e:
        print(f"[PORTAL ERROR] {repr(e)}", flush=True)
        return render_template("error.html", error_message="カスタマーポータルへの接続に失敗しました。しばらく経ってからお試しください。")

@app.route("/admin/product-ranking")
def admin_product_ranking():
    admin_key = request.args.get("key", "")

    _required_key = os.getenv("ADMIN_KEY", "")
    if not _required_key or admin_key != _required_key:
        return jsonify({
            "error": "unauthorized"
        }), 403

    results = load_results()
    ranking = build_product_ranking(results, limit=30)

    return render_template(
        "product_ranking.html",
        title="全ユーザー 商品出力ランキング",
        ranking=ranking,
        ranking_by_category=group_ranking_by_category(ranking)
    )


@app.route("/my-product-ranking")
def my_product_ranking():
    _is_creator = is_creator()
    results = load_results()

    if _is_creator:
        # 全ユーザー集計・カテゴリ別20件
        ranking = build_product_ranking(results, user_id=None, limit=300)
        ranking_by_category = group_ranking_by_category(ranking, per_category_limit=20)
        title = "よく提案される商品（全ユーザー集計）"
    else:
        uid = get_or_create_user_id()
        ranking = build_product_ranking(results, user_id=uid, limit=200)
        ranking_by_category = group_ranking_by_category(ranking, per_category_limit=10)
        title = "よく提案される商品"

    return render_template(
        "product_ranking.html",
        title=title,
        ranking=ranking,
        ranking_by_category=ranking_by_category
    )
@app.route("/log-click")
def log_click():
    source = request.args.get("source", "unknown")
    product = request.args.get("product", "")
    category = request.args.get("category", "")
    log_product_click(source, product, category)
    return "", 204


@app.route("/click")
def product_click():
    source = request.args.get("source", "unknown")
    product = request.args.get("product", "")
    category = request.args.get("category", "")
    url = request.args.get("url", "")

    log_product_click(source, product, category)

    if not url:
        return "リンクがありません", 400

    allowed_domains = [
        "rakuten.co.jp",
        "hb.afl.rakuten.co.jp",
        "amazon.co.jp",
        "amzn.to"
    ]

    try:
        parsed = urllib.parse.urlparse(url)
        netloc = parsed.netloc.lower().lstrip("www.")
        if not any(netloc == d or netloc.endswith("." + d) for d in allowed_domains):
            return "許可されていないリンクです", 400
    except Exception:
        return "無効なURLです", 400

    return redirect(url)

@app.route("/pricing")
def pricing():
    source = request.args.get("source", "unknown")
    log_pricing_view(source)
    return redirect("/premium", code=301)

@app.route("/debug/db")
def debug_db():
    """クリエイター限定 DB診断エンドポイント"""
    if not is_creator():
        return "Unauthorized", 403
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM results")
        total = cur.fetchone()[0]
        cur.execute("SELECT id, saved_at, payload->>'user_id', payload->>'client_ip' FROM results ORDER BY saved_at DESC LIMIT 20")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        current_uid = get_or_create_user_id()
        lines = [
            "<pre>",
            f"current user_id    : {current_uid}",
            f"total records in DB: {total}",
            "",
            "最新20件 (id, saved_at, user_id, client_ip):"
        ]
        for r in rows:
            lines.append(f"  {r[0]}  {r[1]}  {r[2]!r}  {r[3]!r}")
        lines.append("</pre>")
        return "\n".join(lines)
    except Exception as e:
        return f"<pre>ERROR: {e}</pre>", 500

@app.route("/admin/stats")
def admin_stats():
    """管理者向け統計ページ。ADMIN_KEY クエリパラメータで保護。"""
    if not ADMIN_KEY or request.args.get("key") != ADMIN_KEY:
        return "403 Forbidden", 403

    jst = ZoneInfo("Asia/Tokyo")
    now_jst = datetime.now(jst)
    today_str  = now_jst.strftime("%Y-%m-%d")
    month_key  = now_jst.strftime("%Y-%m")
    # DB クエリ用: JST 今日の 00:00 〜 23:59:59 をそのまま文字列比較
    today_start = today_str + " 00:00:00"
    today_end   = today_str + " 23:59:59"

    errors = []

    # ① 有料会員数（revoked=False かつ valid_until が未来）
    premium_count = 0
    premium_detail = []
    try:
        keys = load_premium_keys()
        for key, entry in keys.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("revoked", False):
                continue
            valid_until = entry.get("valid_until", "")
            if valid_until:
                try:
                    expiry = datetime.fromisoformat(valid_until)
                    if expiry.tzinfo is None:
                        expiry = expiry.replace(tzinfo=jst)
                    if expiry > now_jst:
                        premium_count += 1
                        premium_detail.append({
                            "email": entry.get("email", "—"),
                            "valid_until": valid_until[:10],
                        })
                except Exception:
                    pass
            else:
                # valid_until なし = 無期限扱い
                premium_count += 1
                premium_detail.append({
                    "email": entry.get("email", "—"),
                    "valid_until": "無期限",
                })
    except Exception as e:
        errors.append(f"premium_keys 読み込みエラー: {e}")

    # ② 無料会員数（ユニークな user_id）・本日／今月の診断回数
    free_user_count  = 0
    diag_today       = 0
    diag_month       = 0
    total_records    = 0
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur  = conn.cursor()

        # ユニーク user_id（空・NULL 除外）
        cur.execute("""
            SELECT COUNT(DISTINCT payload->>'user_id')
            FROM results
            WHERE payload->>'user_id' IS NOT NULL
              AND payload->>'user_id' != ''
        """)
        free_user_count = cur.fetchone()[0] or 0

        # 本日の診断件数（saved_at は JST 文字列で保存）
        cur.execute("""
            SELECT COUNT(*) FROM results
            WHERE saved_at::text >= %s AND saved_at::text <= %s
        """, (today_start, today_end))
        diag_today = cur.fetchone()[0] or 0

        # 今月の診断件数
        cur.execute("""
            SELECT COUNT(*) FROM results
            WHERE to_char(saved_at, 'YYYY-MM') = %s
        """, (month_key,))
        diag_month = cur.fetchone()[0] or 0

        # 総レコード数
        cur.execute("SELECT COUNT(*) FROM results")
        total_records = cur.fetchone()[0] or 0

        cur.close()
        conn.close()
    except Exception as e:
        errors.append(f"DB クエリエラー: {e}")

    # global_usage.json から今月の累計（参考値）
    global_month_count = 0
    try:
        gdata = load_global_usage()
        global_month_count = int((gdata.get(month_key) or {}).get("count", 0))
    except Exception as e:
        errors.append(f"global_usage 読み込みエラー: {e}")

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>管理者統計</title>
  <style>
    body{{font-family:"Hiragino Sans",sans-serif;background:#f8f4f7;color:#444;margin:0;padding:24px}}
    h1{{color:#7a2942;font-size:22px;margin-bottom:20px}}
    h2{{color:#9b3156;font-size:15px;margin:24px 0 8px;border-left:4px solid #e07a9a;padding-left:10px}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:16px;margin-bottom:16px}}
    .card{{background:#fff;border:1px solid #f0d5df;border-radius:14px;padding:16px 18px;box-shadow:0 2px 8px rgba(180,90,120,.08)}}
    .card .num{{font-size:32px;font-weight:bold;color:#c75b7a;line-height:1.2}}
    .card .label{{font-size:12px;color:#9a7080;margin-top:4px}}
    table{{border-collapse:collapse;width:100%;max-width:540px;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(180,90,120,.06)}}
    th,td{{padding:8px 14px;font-size:13px;text-align:left;border-bottom:1px solid #f5e0e8}}
    th{{background:#fdf0f5;color:#7a2942;font-weight:bold}}
    .err{{background:#fff3f3;border:1px solid #ffbbbb;border-radius:8px;padding:10px 14px;margin-bottom:12px;font-size:13px;color:#c00}}
    .note{{font-size:12px;color:#b09098;margin-top:6px}}
    .ts{{font-size:12px;color:#b09098;margin-top:24px}}
  </style>
</head>
<body>
  <h1>📊 管理者統計</h1>
  {''.join(f'<div class="err">⚠ {e}</div>' for e in errors)}

  <h2>会員数</h2>
  <div class="grid">
    <div class="card">
      <div class="num">{premium_count}</div>
      <div class="label">有料会員（有効期限内）</div>
    </div>
    <div class="card">
      <div class="num">{free_user_count}</div>
      <div class="label">無料会員（ユニーク UUID）</div>
    </div>
  </div>

  <h2>診断実行回数</h2>
  <div class="grid">
    <div class="card">
      <div class="num">{diag_today}</div>
      <div class="label">本日（{today_str}）</div>
    </div>
    <div class="card">
      <div class="num">{diag_month}</div>
      <div class="label">今月（{month_key}）</div>
    </div>
    <div class="card">
      <div class="num">{total_records}</div>
      <div class="label">累計保存件数</div>
    </div>
    <div class="card">
      <div class="num">{global_month_count}</div>
      <div class="label">今月（global_usage.json）</div>
    </div>
  </div>

  <h2>有料会員一覧</h2>
  {'<p style="color:#999;font-size:13px">有料会員はいません。</p>' if not premium_detail else f"""
  <table>
    <tr><th>メールアドレス</th><th>有効期限</th></tr>
    {''.join(f'<tr><td>{d["email"]}</td><td>{d["valid_until"]}</td></tr>' for d in premium_detail)}
  </table>"""}

  <div class="ts">生成日時: {now_jst.strftime('%Y-%m-%d %H:%M:%S')} JST</div>
</body>
</html>"""
    return html


# ==========================================
# マジックリンク ログイン
# ==========================================

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        if not email or "@" not in email or "." not in email.split("@")[-1]:
            return render_template("login.html", error="メールアドレスを正しく入力してください")
        token = create_magic_token(email)
        if not token:
            return render_template("login.html", error="エラーが発生しました。しばらく後でお試しください")
        send_magic_link_email(email, token)
        return render_template("login.html", sent=True, email=email)
    return render_template("login.html")


@app.route("/auth/<token>")
def auth_verify(token):
    email = verify_magic_token(token)
    if not email:
        return render_template("login.html", error="このリンクは無効または期限切れです。再度ログインしてください")
    new_user_id = get_or_create_user_for_email(email)
    if not new_user_id:
        return render_template("login.html", error="ログインに失敗しました。再度お試しください")
    # 現在のデバイスに既存の診断結果があれば email アカウントに移行
    old_user_id = request.cookies.get(RUMILOG_UID_COOKIE, "") or flask_session.get("user_id", "")
    if old_user_id and old_user_id != new_user_id:
        migrate_results_to_email_user(old_user_id, new_user_id)
    flask_session["user_id"] = new_user_id
    flask_session["email"] = email
    flask_session.permanent = True
    resp = make_response(redirect("/history"))
    resp.set_cookie(RUMILOG_UID_COOKIE, new_user_id, max_age=365 * 24 * 3600, httponly=True, samesite="Lax")
    return resp


@app.route("/logout")
def logout():
    flask_session.clear()
    resp = make_response(redirect("/"))
    resp.delete_cookie(RUMILOG_UID_COOKIE)
    return resp


# 診断履歴ページ
@app.route("/history")
def history():
    try:
        user_id = get_or_create_user_id()
        _is_premium = is_premium_user()
        _is_cre = is_creator()

        print(f"[HISTORY ROUTE] user_id={user_id!r}", flush=True)

        # 自分のuser_idの診断結果のみ取得
        history_data = load_results(user_id=user_id)

        if not isinstance(history_data, list):
            history_data = []

        # 表示制限前に全件から日付・スコア系列を収集（ストリーク・改善サマリー・プレミアムグラフに使用）
        _all_score_keys = [
            "oil_balance", "redness", "pores", "hydration", "firmness",
            "acne", "dullness", "barrier", "texture", "tone_evenness"
        ]
        _all_premium_score_keys = [
            "acne_marks_red", "pigmentation", "enlarged_pores", "blackhead_pores",
            "translucency", "tone_uniformity", "skin_balance", "symmetry"
        ]
        all_diag_dates = set()
        all_labels = []
        all_skin_scores = []
        all_score_series = {key: [] for key in _all_score_keys}
        all_premium_score_series = {key: [] for key in _all_premium_score_keys}

        for item in history_data:
            if not isinstance(item, dict):
                continue
            date_str = (item.get("record_date") or item.get("saved_at") or "")[:10]
            for fmt in ["%Y/%m/%d", "%Y-%m-%d"]:
                try:
                    all_diag_dates.add(datetime.strptime(date_str, fmt).date())
                    break
                except ValueError:
                    continue
            all_labels.append(item.get("record_date") or item.get("saved_at") or "")
            all_skin_scores.append(safe_int(item.get("skin_score", 0)))
            scores_dict = item.get("scores", {}) if isinstance(item.get("scores"), dict) else {}
            for key in _all_score_keys:
                all_score_series[key].append(safe_int(scores_dict.get(key, 0)))
            premium_scores_dict = item.get("premium_scores", {}) if isinstance(item.get("premium_scores"), dict) else {}
            for key in _all_premium_score_keys:
                all_premium_score_series[key].append(safe_int(premium_scores_dict.get(key, 0)))

        # 無料ユーザーは表示を直近5件に制限
        if not _is_premium and not _is_cre:
            history_data = history_data[:FREE_HISTORY_LIMIT]

        prepared = []

        for item in history_data:
            if not isinstance(item, dict):
                continue

            prepared.append({
                "id": item.get("id", ""),
                "record_date": item.get("record_date", ""),
                "analysis_date": item.get("analysis_date", ""),
                "saved_at": item.get("saved_at", ""),
                "skin_score": item.get("skin_score", 0),
                "skin_summary": item.get("skin_summary", ""),
                "scores": item.get("scores", {}),
                "input_budget": item.get("input_budget", 0),
                "total_price": item.get("total_price", 0),
                "budget_status": item.get("budget_status", ""),
                "premium_scores": item.get("premium_scores", {}),
                "symmetry_analysis": item.get("symmetry_analysis", {}),
                "skin_age_estimate": item.get("skin_age_estimate", 0),
                "input_age": item.get("input_age", 0),
                "score_diff": {},
            })

        labels = []
        skin_scores = []

        score_keys = [
            "oil_balance",
            "redness",
            "pores",
            "hydration",
            "firmness",
            "acne",
            "dullness",
            "barrier",
            "texture",
            "tone_evenness"
        ]

        score_labels = {
            "oil_balance": "皮脂バランス",
            "redness": "赤み",
            "pores": "毛穴",
            "hydration": "水分",
            "firmness": "ハリ",
            "acne": "ニキビ",
            "dullness": "くすみ",
            "barrier": "バリア",
            "texture": "キメ",
            "tone_evenness": "色ムラ"
        }

        score_series = {
            key: []
            for key in score_keys
        }

        premium_score_keys = [
            "acne_marks_red",
            "pigmentation",
            "enlarged_pores",
            "blackhead_pores",
            "translucency",
            "tone_uniformity",
            "skin_balance",
            "symmetry"
        ]

        premium_score_series = {
            key: []
            for key in premium_score_keys
        }

        for item in prepared:
            labels.append(
                item.get("record_date")
                or item.get("saved_at")
                or ""
            )

            skin_scores.append(
                safe_int(item.get("skin_score", 0))
            )

            scores_dict = item.get("scores", {})
            if not isinstance(scores_dict, dict):
                scores_dict = {}

            for key in score_keys:
                score_series[key].append(
                    safe_int(scores_dict.get(key, 0))
                )

            premium_scores_dict = item.get("premium_scores", {})
            if not isinstance(premium_scores_dict, dict):
                premium_scores_dict = {}

            for key in premium_score_keys:
                premium_score_series[key].append(
                    safe_int(premium_scores_dict.get(key, 0))
                )

        # プレミアム・クリエイターは全件データでグラフを構築
        if _is_premium or _is_cre:
            labels = all_labels
            skin_scores = all_skin_scores
            score_series = all_score_series
            premium_score_series = all_premium_score_series

        # improvement_summary: 全件スコア系列で 最新(values[0]) - 最古(values[-1]) を計算
        improvement_summary = []

        for key, values in all_score_series.items():
            if len(values) >= 2:
                diff = values[0] - values[-1]  # 最新 - 最古

                if diff != 0:
                    improvement_summary.append({
                        "label": score_labels.get(key, key),
                        "diff": diff
                    })

        improvement_summary = sorted(
            improvement_summary,
            key=lambda x: x["diff"],
            reverse=True
        )[:5]

        # ① 改善ハイライト: 各診断に前回比スコア差分と期間ラベルを付与
        # prepared は DESC 順: prepared[0]=最新, prepared[1]=その前, ...
        # → prepared[i-1]=新しい, prepared[i]=古い
        # → 新しい診断(prepared[i-1])の改善量 = 新 - 旧
        def _parse_date(item):
            date_str = (item.get("record_date") or item.get("saved_at") or "")[:10]
            for fmt in ["%Y/%m/%d", "%Y-%m-%d"]:
                try:
                    return datetime.strptime(date_str, fmt).date()
                except ValueError:
                    pass
            return None

        for i in range(1, len(prepared)):
            old_scores = prepared[i].get("scores", {}) or {}      # 古い診断
            new_scores = prepared[i - 1].get("scores", {}) or {}  # 新しい診断
            diff_map = {}
            for key in score_keys:
                d = safe_int(new_scores.get(key, 0)) - safe_int(old_scores.get(key, 0))
                if d != 0:
                    diff_map[score_labels.get(key, key)] = d
            prepared[i - 1]["score_diff"] = diff_map  # 新しい診断カードに付与

            # 改善ハイライト: ±3点以上の変化を抽出（改善も下落も表示）
            highlights = sorted(
                [(label, d) for label, d in diff_map.items() if abs(d) >= 3],
                key=lambda x: abs(x[1]),
                reverse=True
            )[:5]
            prepared[i - 1]["improvement_highlights"] = highlights

            # 前回診断からの日数ラベル（新しい日付 - 古い日付 = 正の値）
            try:
                d_new = _parse_date(prepared[i - 1])
                d_old = _parse_date(prepared[i])
                if d_new and d_old:
                    days = (d_new - d_old).days
                    if days <= 7:
                        period_label = "先週比"
                    elif days <= 14:
                        period_label = "2週間前比"
                    elif days <= 31:
                        period_label = "先月比"
                    else:
                        period_label = "前回比"
                    prepared[i - 1]["period_label"] = period_label
                else:
                    prepared[i - 1]["period_label"] = "前回比"
            except Exception:
                prepared[i - 1]["period_label"] = "前回比"

        # ⑥ 診断ストリーク計算（全件日付 all_diag_dates を使用）
        streak = 0
        try:
            if all_diag_dates:
                today = datetime.now().date()
                check = today
                if check not in all_diag_dates:
                    check = today - timedelta(days=1)
                while check in all_diag_dates:
                    streak += 1
                    check -= timedelta(days=1)
        except Exception:
            streak = 0

        monthly_report = None
        try:
            now = datetime.now()
            thirty_days_ago = now - timedelta(days=30)

            monthly_items = []
            for item in prepared:
                date_str = (item.get("record_date") or item.get("saved_at") or "")[:10]
                if not date_str:
                    continue
                for fmt in ["%Y/%m/%d", "%Y-%m-%d"]:
                    try:
                        item_date = datetime.strptime(date_str, fmt)
                        if item_date >= thirty_days_ago:
                            monthly_items.append(item)
                        break
                    except ValueError:
                        continue

            if monthly_items:
                monthly_scores = [safe_int(item.get("skin_score", 0)) for item in monthly_items]
                avg_score = round(sum(monthly_scores) / len(monthly_scores))

                most_improved = None
                needs_attention = None

                if len(monthly_items) >= 2:
                    first_scores = monthly_items[0].get("scores", {}) or {}
                    last_scores = monthly_items[-1].get("scores", {}) or {}

                    best_diff = None
                    best_key = None
                    worst_score = None
                    worst_key = None

                    for key in score_keys:
                        first_val = safe_int(first_scores.get(key, 0))
                        last_val = safe_int(last_scores.get(key, 0))
                        diff = last_val - first_val

                        if best_diff is None or diff > best_diff:
                            best_diff = diff
                            best_key = key

                        if worst_score is None or last_val < worst_score:
                            worst_score = last_val
                            worst_key = key

                    if best_key and best_diff is not None and best_diff > 0:
                        most_improved = {
                            "label": score_labels.get(best_key, best_key),
                            "diff": best_diff
                        }

                    if worst_key and worst_score is not None:
                        needs_attention = {
                            "label": score_labels.get(worst_key, worst_key),
                            "score": worst_score
                        }

                monthly_report = {
                    "count": len(monthly_items),
                    "avg_score": avg_score,
                    "most_improved": most_improved,
                    "needs_attention": needs_attention
                }
        except Exception as _e:
            print(f"monthly_report error: {_e}")
            monthly_report = None

        return render_template(
            "history.html",
            history=prepared,
            labels=labels,
            scores=skin_scores,
            score_series=score_series,
            improvement_summary=improvement_summary,
            premium_score_series=premium_score_series,
            is_premium=is_premium_user(),
            monthly_report=monthly_report,
            streak=streak,
            email=flask_session.get("email", "")
        )

    except Exception as e:
        print("===== HISTORY ROUTE ERROR =====")
        print(e)
        traceback.print_exc()
        print("===============================")

        return render_template(
            "history.html",
            history=[],
            labels=[],
            scores=[],
            score_series={},
            improvement_summary=[],
            premium_score_series={},
            is_premium=is_premium_user(),
            monthly_report=None,
            streak=0,
            email=flask_session.get("email", "")
        )

@app.route("/history/<result_id>")
def result_detail(result_id):
    try:
        uid = get_or_create_user_id()
        history_data = load_results(user_id=uid)

        if not isinstance(history_data, list):
            history_data = []

        for item in history_data:
            if not isinstance(item, dict):
                continue

            if str(item.get("id", "")) == str(result_id):
                data = prepare_result_for_view(item)

                if not isinstance(data, dict):
                    data = item

                data["is_premium"] = is_premium_user()
                data["is_dev_mode"] = DEV_MODE or DEV_PREMIUM_MODE

                if not isinstance(data.get("symmetry_analysis"), dict):
                    data["symmetry_analysis"] = {
                        "summary": "",
                        "left_tendency": "",
                        "right_tendency": ""
                    }
                return render_template("result.html", data=data, result_id=result_id)

        return "結果が見つかりません", 404

    except Exception as e:
        print("===== HISTORY DETAIL ERROR =====", flush=True)
        print(e, flush=True)
        traceback.print_exc()
        print("================================", flush=True)
        return "エラーが発生しました", 500

def build_user_friendly_error_message(error_text=""):
    text = str(error_text).lower()

    if "429" in text:
        return (
            "現在アクセスが集中しているため、少し時間を空けてから再度お試しください。"
        )

    if "503" in text:
        return (
            "現在診断が混み合っています。"
            "少し時間を空けてから再度お試しください。"
        )

    if "timeout" in text:
        return (
            "診断処理に時間がかかっています。"
            "通信環境を確認して、再度お試しください。"
        )

    if "ssl" in text:
        return (
            "通信エラーが発生しました。"
            "時間を空けて再度お試しください。"
        )

    return (
        "診断中に一時的なエラーが発生しました。"
        "少し時間を空けて再度お試しください。"
    )

@app.route("/result/<result_id>")
def history_detail(result_id):
    try:
        history_data = load_results()

        if not isinstance(history_data, list):
            history_data = []

        prepared_history = []
        for item in history_data:
            if isinstance(item, dict):
                prepared_history.append(prepare_result_for_view(item))

        # 対象データ
        current = None
        for item in prepared_history:
            if str(item.get("id", "")) == str(result_id):
                current = item
                break

        if not current:
            return "履歴が見つかりません", 404

        # 日付順に並べて、ひとつ前を探す
        sorted_history = sorted(
            prepared_history,
            key=lambda x: (
                str(x.get("record_date", "") or ""),
                str(x.get("saved_at", "") or ""),
                str(x.get("id", "") or "")
            )
        )

        previous = None
        for idx, item in enumerate(sorted_history):
            if str(item.get("id", "")) == str(result_id):
                if idx > 0:
                    previous = sorted_history[idx - 1]
                break

        current_scores = get_score_snapshot(current)
        previous_scores = get_score_snapshot(previous) if previous else None

        score_diff = {}
        comparison = None
        if previous_scores:
            for key, current_value in current_scores.items():
                prev_value = safe_int(previous_scores.get(key, 0))
                score_diff[key] = current_value - prev_value
            # テンプレートが期待する comparison 形式（xxx_diff キー）
            comparison = {f"{k}_diff": v for k, v in score_diff.items()}
        else:
            for key in current_scores.keys():
                score_diff[key] = None
        return render_template(
            "history_detail.html",
            data=current,
            prev_data=previous,
            previous=previous,       # テンプレートの {{ previous.id }} に対応
            score_diff=score_diff,
            comparison=comparison,   # テンプレートの {% if comparison %} に対応
        )

    except Exception as e:
        print("===== HISTORY DETAIL ERROR =====")
        print(e)
        traceback.print_exc()
        print("================================")
        return "履歴詳細の読み込みに失敗しました", 500

def deep_analysis(result_id):

    results = load_results()

    target = next(
        (
            r for r in results
            if r.get("id") == result_id
        ),
        None
    )

    if not target:
        return "not found"

    if target.get("deep_analysis"):

        return render_template(
            "deep_analysis.html",
            data=target
        )

    deep = detailed_analysis_with_gemini(
        client,
        target,
        target
    )

    target["deep_analysis"] = deep

    update_result_deep_analysis(
        result_id,
        deep
    )

    return render_template(
        "deep_analysis.html",
        data=target
    )
@app.route("/api/verify-product")
def api_verify_product():
    product_name = request.args.get("product", "").strip()
    category = request.args.get("category", "").strip()
    brand = request.args.get("brand", "").strip()

    if not product_name:
        return jsonify({
            "ok": False,
            "error": "product is required"
        })

    cache_key = (
        product_name,
        category,
        brand
    )

    if cache_key in VERIFY_PRODUCT_CACHE:
        return jsonify(
            VERIFY_PRODUCT_CACHE[cache_key]
        )

    item = fetch_rakuten_item(
        product_name=product_name,
        category=category,
        brand=brand
    )

    if not item:
        result = {
            "ok": False,
            "error": "not found",
            "price": 0,
            "image": "",
            "rakuten_link": ""
        }

        VERIFY_PRODUCT_CACHE[cache_key] = result

        return jsonify(result)
    
    
    result = {
        "ok": True,
        "name": item.get("name", ""),
        "price": item.get("price", 0),
        "image": item.get("image", ""),
        "rakuten_link": item.get("rakuten_link", "")
    }

    VERIFY_PRODUCT_CACHE[cache_key] = result

    return jsonify(result)

# ==========================================
# Flaskサーバー起動
# ==========================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)