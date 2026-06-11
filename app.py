import os
import psycopg2
from dotenv import load_dotenv
load_dotenv()

RAKUTEN_APP_ID = os.environ.get("RAKUTEN_APP_ID", "")
RAKUTEN_ACCESS_KEY = os.environ.get("RAKUTEN_ACCESS_KEY", "")
RAKUTEN_AFFILIATE_ID = os.environ.get("RAKUTEN_AFFILIATE_ID", "")
print("ENV CHECK")
print("APP_ID:", RAKUTEN_APP_ID)
print("ACCESS:", RAKUTEN_ACCESS_KEY)
print("AFF:", RAKUTEN_AFFILIATE_ID)
# ==========================================
# rumilog - AI肌診断アプリ
# Flaskメインサーバー
# Gemini APIを使った肌診断 + 履歴管理
# ==========================================

import io
import json
import traceback
import urllib.parse
import requests
import re
import copy
import time
from psycopg2.pool import SimpleConnectionPool
import hashlib
GEMINI_ANALYSIS_CACHE = {}
ANALYSIS_CACHE_VERSION = "v1"
DATABASE_URL = os.getenv("DATABASE_URL")
RAKUTEN_COOLDOWN_UNTIL = 0
_rakuten_item_cache = {}
_rakuten_criteria_cache = {}
_rakuten_criteria_call_count = 0
MAX_RAKUTEN_CRITERIA_CALLS = 5
VERIFIED_PRODUCTS_CACHE_FILE = "verified_products_cache.json"
VERIFIED_PRODUCTS_CACHE_TTL_SECONDS = 60 * 60 * 24 * 45

# ===== Gemini Models =====

ANALYSIS_MODEL = "gemini-3.5-flash"

CANDIDATE_MODEL = "gemini-3.5-flash"

ROUTINE_MODEL = "gemini-3.5-flash"

DETAIL_MODEL = "gemini-3.1-flash-lite"

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
init_results_table()
init_gemini_usage_table()
VERIFY_PRODUCT_CACHE = {}
# ===== DEV_MODE_START =====
DEV_MODE = False  # ← 開発中はTrue / 公開時はFalseにするか削除
# ===== DEV_MODE_END =====
USE_RICH_CANDIDATE = False
DISABLE_USAGE_LIMIT = True 
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
def call_gemini_with_retry(client, model, contents, config=None, max_retries=2):
    import random
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

    for attempt in range(max_retries):
        try:
            print(
                f"[GEMINI CALL START] model={model} attempt={attempt + 1}/{max_retries}",
                flush=True
            )

            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config
            )

            current_count = increment_gemini_usage()
            if current_count is not None:
                print(
                    "[GEMINI REQUEST COUNT]",
                    current_count,
                    "/",
                    GEMINI_DAILY_LIMIT,
                    flush=True
                )

            print(
                f"[GEMINI CALL SUCCESS] model={model} attempt={attempt + 1}/{max_retries}",
                flush=True
            )

            return response

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

from datetime import datetime, date
from zoneinfo import ZoneInfo
from PIL import Image,ImageOps
from flask import Flask, render_template, request,jsonify,redirect
from google import genai
from google.genai import types
# ==========================================
# Flask初期設定
# ==========================================
app = Flask(__name__)

CLICK_LOG_FILE = "product_clicks.json"

# ===== 有料会員設定 =====
ENABLE_SUBSCRIPTION = False  # 決済導入前はFalse
DEV_PREMIUM_MODE = True      # 開発中に有料表示を確認したい時だけTrue


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
FREE_MONTHLY_LIMIT = 3
GLOBAL_MONTHLY_LIMIT = 1000
GLOBAL_USAGE_FILE = "global_usage.json"
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

def is_premium_user():
    """
    有料会員判定をここに集約する。
    将来、ログイン・決済・DB管理に移行しても、
    各画面側のコードは変更しない。
    """
    if DEV_PREMIUM_MODE:
        return True

    premium_key = request.args.get("premium_key", "")
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
    return "https://www.amazon.co.jp/s?k=" + urllib.parse.quote(name) + "&tag=あなたのAmazonアソシエイトID"

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

def wait_for_rakuten_rate_limit():
    global _last_rakuten_request_time

    now = time.time()
    elapsed = now - _last_rakuten_request_time

    if elapsed < 1.2:
        time.sleep(1.2 - elapsed)

    _last_rakuten_request_time = time.time()

    
def build_rakuten_search_keywords(product_name, brand="", category="", ingredient_focus="", purpose=""):
    name = clean_rakuten_keyword(product_name)
    brand = clean_rakuten_keyword(brand)

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

    if brand and meaningful_parts:
        add(f"{brand} {' '.join(meaningful_parts)}")

    if meaningful_parts:
        add(" ".join(meaningful_parts))

    if brand and len(meaningful_parts) >= 2:
        add(f"{brand} {' '.join(meaningful_parts[-2:])}")

    if len(meaningful_parts) >= 2:
        add(" ".join(meaningful_parts[-2:]))

    print("[RAKUTEN KEYWORDS]", keywords, flush=True)

    return keywords[:5]

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
            return -9999

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
    ]

    if any(word in title for word in hard_reject_words):
        return -9999

    if infer_bundle_quantity_from_title(title) > 1:
        return -9999

    if category == "美容液":
        if any(word in title for word in [
            "化粧水",
            "ローション",
            "トナー",
            "toner",
            "lotion",
            "乳液",
            "ミルク",
            "クリーム",
            "ジェルクリーム",
            "オールインワン",
            "シートマスク",
            "フェイスマスク",
            "薬用マスク",
            "パック"
        ]):
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

    else:
        score_category_penalty = 0

    score = name_match_score
    score += locals().get("score_category_penalty", 0)

    if brand:
        brand_compact = compact_text(brand)
        if brand_compact and brand_compact in title_compact:
            score += 25

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
        score += 60
    elif review_count >= 3000:
        score += 50
    elif review_count >= 1000:
        score += 40
    elif review_count >= 500:
        score += 30
    elif review_count >= 300:
        score += 22
    elif review_count >= 100:
        score += 14
    elif review_count >= 30:
        score += 6
    elif review_count > 0:
        score += 1

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
        "'", "’", '"', "“", "”", "%", "％"
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

    # ブランド一致しない場合はトークン一致数の閾値を1つ上げて誤照合を防ぐ
    if brand_ok:
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
        "Referer": "https://rumilog.onrender.com",
        "Origin": "https://rumilog.onrender.com",
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

    print("[RAKUTEN KEYWORDS]", keywords, flush=True)

    MAX_RAKUTEN_KEYWORDS = 2

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

                    RAKUTEN_COOLDOWN_UNTIL = time.time() + retry_seconds

                    print(
                        "[RAKUTEN RATE LIMIT COOLDOWN]",
                        keyword,
                        retry_seconds,
                        "seconds",
                        flush=True
                    )

                    return None

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

            if not items:
                continue

            scored_items = []

            for raw_item in items:
                item = raw_item.get("Item", raw_item) if isinstance(raw_item, dict) else raw_item

                if not isinstance(item, dict):
                    continue

                rakuten_title = str(item.get("itemName", "") or "").strip()

                if not is_same_verified_rakuten_product(
                    product_name=product_name,
                    rakuten_title=rakuten_title,
                    brand=brand
                ):
                    print(
                        "[RAKUTEN REJECT TITLE MISMATCH]",
                        {
                            "product": product_name,
                            "brand": brand,
                            "rakuten_title": rakuten_title
                        },
                        flush=True
                    )
                    continue

                score = score_rakuten_item(
                    item,
                    product_name=product_name,
                    brand=brand,
                    category=category
                )

                if score < 20:
                    continue

                scored_items.append((score, item))

            if not scored_items:
                continue

            scored_items.sort(
                key=lambda pair: (
                    pair[0],
                    1 if (pair[1].get("mediumImageUrls") or pair[1].get("smallImageUrls")) else 0,
                    safe_price(pair[1].get("reviewCount", 0)),
                    safe_price(pair[1].get("reviewAverage", 0)),
                    -safe_price(pair[1].get("itemPrice", 0))
                ),
                reverse=True
            )

            best_score, best = scored_items[0]

            

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


def search_rakuten_by_criteria(category, improvement_plan):
    """
    improvement_plan の key_ingredients + category で楽天検索し候補商品リストを返す。
    セッション内でキャッシュ、MAX_RAKUTEN_CRITERIA_CALLS 回を上限とする。
    """
    global _rakuten_criteria_cache, _rakuten_criteria_call_count, RAKUTEN_COOLDOWN_UNTIL

    if not RAKUTEN_APP_ID or not RAKUTEN_ACCESS_KEY:
        return []

    if time.time() < RAKUTEN_COOLDOWN_UNTIL:
        return []

    if _rakuten_criteria_call_count >= MAX_RAKUTEN_CRITERIA_CALLS:
        print("[RAKUTEN CRITERIA] session call limit reached", flush=True)
        return []

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

    keyword = f"{category_clean} {top_ingredient}"
    norm_cat = normalize_candidate_category(category, fallback=category)
    cache_key = (norm_cat, top_ingredient)

    if cache_key in _rakuten_criteria_cache:
        print(f"[RAKUTEN CRITERIA CACHE HIT] {cache_key}", flush=True)
        return _rakuten_criteria_cache[cache_key]

    print(f"[RAKUTEN CRITERIA SEARCH] keyword={keyword}", flush=True)
    _rakuten_criteria_call_count += 1

    endpoint = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260401"
    headers = {
        "Referer": "https://rumilog.onrender.com",
        "Origin": "https://rumilog.onrender.com",
        "User-Agent": "Mozilla/5.0",
    }

    try:
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

        if RAKUTEN_AFFILIATE_ID:
            params["affiliateId"] = RAKUTEN_AFFILIATE_ID

        res = requests.get(endpoint, params=params, headers=headers, timeout=(2, 4))
        print(f"[RAKUTEN CRITERIA STATUS] {res.status_code}", flush=True)

        if res.status_code == 429:
            retry_seconds = 10
            retry_match = re.search(
                r"Try again in ([0-9\.]+) seconds?", res.text, re.IGNORECASE
            )
            if retry_match:
                try:
                    retry_seconds = max(3, float(retry_match.group(1)) + 2)
                except Exception:
                    pass
            RAKUTEN_COOLDOWN_UNTIL = time.time() + retry_seconds
            _rakuten_criteria_cache[cache_key] = []
            return []

        if res.status_code != 200:
            _rakuten_criteria_cache[cache_key] = []
            return []

        payload = res.json()
        items = payload.get("items") or payload.get("Items") or []

        results = []

        for raw_item in items:
            item = raw_item.get("Item", raw_item) if isinstance(raw_item, dict) else raw_item
            if not isinstance(item, dict):
                continue

            item_name = str(item.get("itemName", "") or "").strip()
            if not item_name:
                continue

            item = normalize_rakuten_item_price(item)
            price = safe_price(item.get("raw_price") or item.get("itemPrice") or 0)
            image_url = extract_rakuten_image_url(item)
            if not image_url:
                continue  # 画像なしは候補から除外

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

        print(f"[RAKUTEN CRITERIA] {len(results)} items for '{keyword}'", flush=True)
        _rakuten_criteria_cache[cache_key] = results
        return results

    except Exception as e:
        print(f"[RAKUTEN CRITERIA ERROR] {repr(e)}", flush=True)
        _rakuten_criteria_cache[cache_key] = []
        return []


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

    if existing_image and existing_rakuten_link:
        step["amazon_link"] = build_amazon_link(product_name)
        return normalize_step_price_fields(step)


    if product_source not in ["db", "ai+db", "fallback_db"]:
        if "affiliate_links" in step and isinstance(step["affiliate_links"], dict):
            step["amazon_link"] = step["affiliate_links"].get("amazon", "")
            step["rakuten_link"] = step["affiliate_links"].get("rakuten", "")
            existing_rakuten_link = step["rakuten_link"]

            if existing_image:
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

            if existing_image:
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

   
def attach_affiliate_links_to_all_steps(data, affiliate_ai_db):
    for section in ["morning", "night"]:
        for step in data.get(section, {}).get("steps", []):
            if isinstance(step, dict):
                attach_affiliate_links_to_step(step, affiliate_ai_db)

    for step in data.get("weekly_care", []):
        if isinstance(step, dict):
            attach_affiliate_links_to_step(step, affiliate_ai_db)

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

    product_concerns = product.get("concerns", [])
    product_actives = product.get("active_ingredients", [])
    product_support = product.get("support_ingredients", [])
    product_skin_types = product.get("skin_types", [])
    sensitive_ok = product.get("sensitive_ok", "unknown")
    retinol_level = safe_retinol_level(
        product.get("retinol_level", 0)
    )
    price_ref = safe_price(product.get("price_ref", 0))
    availability = product.get("availability_japan", [])
    product_functions = product.get("main_functions", [])
    product_focuses = product.get("ingredient_focus", [])
    product_formulation = product.get("formulation", [])
    product_technology = product.get("technology", [])
    product_texture = normalize_text(product.get("texture", ""))
    product_contra = product.get("contraindications", [])
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

    # -------------------------------------------------
    # 1. ingredient_focus（step側）と active/support 一致
    # -------------------------------------------------
    if ingredient_tag:
        if ingredient_tag in product_actives:
            score += 25
            score += get_strength_score(ingredient_strength_map.get(ingredient_tag))

        elif ingredient_tag in product_support:
            score += 10

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

    if "morning_use_caution" in product_contra and step.get("_section") == "morning":
        score -= 8

    if "photosensitivity" in product_contra and step.get("_section") == "morning":
        score -= 6

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
    product_support = product.get("support_ingredients", [])
    formulation = product.get("formulation", [])
    texture = normalize_text(product.get("texture", ""))
    sensitive_ok = product.get("sensitive_ok", "unknown")
    functions = product.get("main_functions", [])
    technology = product.get("technology", [])
    contraindications = product.get("contraindications", [])

    skin = normalize_text(user_data.get("oil", ""))
    sens = normalize_text(user_data.get("sens", ""))
    makeup_level = normalize_text(user_data.get("makeup_level", "medium"))
    morning_cleanse = normalize_text(user_data.get("morning_cleanse", "no"))

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

    # 👇ここに追加
    uv_info = product.get("uv_level", {})
    spf_raw = str(uv_info.get("spf", 0) or "0").strip()
    spf = int(re.sub(r"[^0-9]", "", spf_raw) or 0)
    pa = str(uv_info.get("pa", "") or "")

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
    "クレンザー",
    "ウォッシュ",
    "ジェルウォッシュ",
    "泡",
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

    toner_keywords = [
        "化粧水", "トナー", "ローション",
        "toner", "lotion", "ampoule toner", "アンプルトナー"
    ]

    cleanser_keywords = [
        "洗顔", "フォーム", "クレンザー", "ウォッシュ",
        "ジェルウォッシュ", "泡",
        "soap", "cleanser", "cleansing foam",
        "face wash", "facial wash"
    ]

    if any(k.lower() in name for k in toner_keywords):
        return True

    if not any(k.lower() in name for k in cleanser_keywords):
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
            "mandelic_acid"
        ]

        PEELING_NAME_KEYWORDS = [
            "ピーリング",
            "ピール",
            "スキンピール",
            "ゴマージュ",
            "角質",
            "角質ケア",
            "スクラブ",
            "アクアジェル"
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
            "contraindications": ["acid_same_routine"]
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

    if routine_context:
        existing_families = set(routine_context.get("families", []))
        existing_strengths = routine_context.get("strengths", [])

        profile_pair = set(profile.get("pair_well_with", set()))
        profile_avoid = set(profile.get("avoid_with", set()))

        for family in existing_families:
            if family in profile_pair:
                score += 10
            if family in profile_avoid:
                score -= 18

        if "vitamin_c" in families and "vitamin_c" in existing_families:
            score -= 8

        if "retinoid" in families and "retinoid" in existing_families:
            score -= 16

        if "aha_bha" in families and "aha_bha" in existing_families:
            score -= 14

        if (
            "retinoid" in families and "aha_bha" in existing_families
        ) or (
            "aha_bha" in families and "retinoid" in existing_families
        ):
            score -= 24

        if (
            "vitamin_c" in families and "retinoid" in existing_families
        ) or (
            "retinoid" in families and "vitamin_c" in existing_families
        ):
            score += 8

        if (
            "barrier" in families
            and (
                "retinoid" in existing_families
                or "aha_bha" in existing_families
                or "vitamin_c" in existing_families
            )
        ):
            score += 12

        if strength == "high" and "high" in existing_strengths:
            score -= 16

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

    # Phase 2: criteria-based Rakuten search
    if improvement_plan and improvement_plan.get("priority_concerns"):
        criteria_products = search_rakuten_by_criteria(category, improvement_plan)
        for cp in criteria_products:
            cp_key = normalize_product_name(cp.get("rakuten_title", "") or cp.get("name", ""))
            if cp_key and cp_key not in seen_product_keys:
                seen_product_keys.add(cp_key)
                combined_products.append(cp)

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
            identity_keys.add(f"{brand_key} {name_without_brand_key}".strip())

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
            "name": c.get("name", ""),
            "score": c.get("_score", 0),
            "base_score": c.get("_base_score", 0),
            "improve_score": c.get("_improve_score", 0),
            "routine_score": c.get("_routine_score", 0),
            "source": c.get("_source", ""),
            "price_ref": c.get("price_ref", 0),
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

    log_candidate_battle(
        step,
        sorted_candidates,
        best
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
    scores = data.get("scores", {})
    if not isinstance(scores, dict):
        scores = {}

    hydration_score = safe_price(scores.get("hydration", 0))
    barrier_score = safe_price(scores.get("barrier", 0))
    redness_score = safe_price(scores.get("redness", 0))
    texture_score = safe_price(scores.get("texture", 0))
    pores_score = safe_price(scores.get("pores", 0))
    dullness_score = safe_price(scores.get("dullness", 0))

    needs_recovery_pack = (
        hydration_score <= 65
        or barrier_score <= 65
        or redness_score <= 65
    )

    needs_peeling = (
        texture_score <= 65
        or pores_score <= 65
        or dullness_score <= 65
    )

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

    if needs_peeling and not weekly_has_category("ピーリング"):
        weekly_care.append({
            "category": "ピーリング",
            "role": "main",
            "purpose": "毛穴詰まり・ざらつき・くすみを週1回の角質ケアで整える",
            "ingredient_focus": "PHA",
            "risk_note": "レチノールや高濃度ビタミンCと同じ夜は避ける",
            "priority": 7,
            "product_candidates": []
        })

    if needs_recovery_pack and not weekly_has_category("パック"):
        weekly_care.append({
            "category": "パック",
            "role": "main",
            "purpose": "乾燥・赤み・バリア低下を集中保湿で整える",
            "ingredient_focus": "CICA",
            "risk_note": "",
            "priority": 8,
            "product_candidates": []
        })

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
    

def collect_market_candidates_with_gemini(user_data, analyzed_data):
    schema = get_candidate_collection_schema()
    prompt = build_candidate_collection_prompt(user_data, analyzed_data)

    response = call_gemini_with_quota_guard(
        model=CANDIDATE_MODEL,
        contents=[prompt],
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=schema
        )
    )

    if response is None:
        return {"steps": []}

    raw_text = (response.text or "").strip()

    print("===== RAW AI RESPONSE =====")
    print(raw_text)
    print("===========================")

    if raw_text.startswith("```json"):
        raw_text = raw_text.replace("```json", "", 1).strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.replace("```", "", 1).strip()
    if raw_text.endswith("```"):
        raw_text = raw_text[:-3].strip()

    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start != -1 and end != -1 and end > start:
        raw_text = raw_text[start:end + 1]

    try:
        parsed = json.loads(raw_text)
        if not isinstance(parsed, dict):
            return {"steps": []}
        if not isinstance(parsed.get("steps"), list):
            parsed["steps"] = []
        return parsed
    except Exception as e:
        print("JSON ERROR:", e)
        print("BROKEN JSON ↓")
        print(raw_text)
        return {"steps": []}

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

    words = name_text.split()
    cleaned_words = []

    for word in words:
        word_key = normalize_candidate_name_for_merge(word)

        if word_key and word_key == brand_key:
            continue

        cleaned_words.append(word)

    cleaned_name = " ".join(cleaned_words).strip()

    name_key = normalize_candidate_name_for_merge(cleaned_name)

    while name_key.startswith(f"{brand_key} {brand_key}"):
        cleaned_name = cleaned_name[len(brand_text):].strip()
        name_key = normalize_candidate_name_for_merge(cleaned_name)

    if normalize_candidate_name_for_merge(cleaned_name).startswith(brand_key):
        if cleaned_name.startswith(brand_text):
            cleaned_name = cleaned_name[len(brand_text):].strip()

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
        return f"{brand_key} {name_without_brand_key}".strip()

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

    text = text.replace("{", " ")
    text = text.replace("}", " ")
    text = text.replace('"', " ")
    text = text.replace("’", "'")
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

def assign_products_to_all_steps(data, products, user_data, budget_value):
    

    ai_image_db = load_ai_product_images()
    improvement_plan = data.get("improvement_plan", {})
    verified_products = load_verified_products_cache()

    routine_context = {
        "families": [],
        "strengths": [],
        "selected_products": []
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
            step["image"] = ""
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

        routine_context["families"].extend(profile.get("families", []))
        routine_context["strengths"].append(profile.get("strength", "low"))
        routine_context["selected_products"].append(product_name)

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

        return step

    for section in ["morning", "night"]:
        used_product_names = set()
        routine_context = {
            "families": [],
            "strengths": [],
            "selected_products": []
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

    weekly_used_product_names = set()
    weekly_routine_context = {
        "families": [],
        "strengths": [],
        "selected_products": []
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



def call_gemini_with_quota_guard(**kwargs):
    return call_gemini_with_retry(
        client=client,
        model=kwargs.get("model"),
        contents=kwargs.get("contents"),
        config=kwargs.get("config"),
        max_retries=3
    )
    

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

def log_candidate_battle(step, candidates, selected=None):
    section = step.get("_section", "")
    category = step.get("category", "")
    purpose = step.get("purpose", "")
    ingredient_focus = step.get("ingredient_focus", "")

    print("\n===== CANDIDATE BATTLE DETAIL =====")
    print(f"section: {section}")
    print(f"category: {category}")
    print(f"purpose: {purpose}")
    print(f"ingredient_focus: {ingredient_focus}")

    if not candidates:
        print("no candidates")
        print("===================================\n")
        return

    for idx, c in enumerate(candidates[:5], start=1):
        if not isinstance(c, dict):
            continue

        print(
            f"{idx}. "
            f"name={c.get('name', '')} / "
            f"source={c.get('_source', '')} / "
            f"final={c.get('_score', 0)} / "
            f"base={c.get('_base_score', 0)} / "
            f"improve={c.get('_improve_score', 0)} / "
            f"price={c.get('price_ref', 0)}"
        )

    if selected:
        print(f"WINNER: {selected.get('name', '')} ({selected.get('_source', '')})")

    print("===================================\n")


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
    return result



def pick_product(category, products):
    candidates = [p for p in products if p.get("category", "") == category]
    if not candidates:
        return None
    return max(candidates, key=lambda x: x.get("score", 0))
# 履歴読み込み
def load_results():
    conn = None
    cur = None

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

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

        normalized_candidates = []
        seen_keys = set()

        for c in raw_candidates:
            normalized = normalize_candidate(c)
            if not normalized:
                continue

            identity_keys = build_candidate_identity_keys(normalized)

            if not identity_keys:
                continue

            if seen_keys.intersection(identity_keys):
                continue

            seen_keys.update(identity_keys)
            normalized_candidates.append(normalized)

            if len(normalized_candidates) >= 3:
                break

        if normalized_candidates:
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

    # 敏感肌 × 攻め成分
    if sens == "high":
        if any(tag in ingredient_tags for tag in ["retinol", "retinal", "aha", "bha", "pha", "salicylic_acid", "glycolic_acid", "lactic_acid", "mandelic_acid"]):
            warnings.append("敏感傾向があるため、攻めの成分は少量から様子を見て使うのがおすすめです")

    # レチノール初心者
    if exp == "beginner":
        if any(tag in ingredient_tags for tag in ["retinol", "retinal"]):
            warnings.append("レチノール系は初心者のため、使用頻度を低めから始めるのがおすすめです")

    # 酸系とレチノールの併用注意
    if any(tag in ingredient_tags for tag in ["retinol", "retinal"]) and any(tag in ingredient_tags for tag in ["aha", "bha", "pha", "salicylic_acid", "glycolic_acid", "lactic_acid", "mandelic_acid"]):
        warnings.append("レチノール系と角質ケア系を同じタイミングで重ねると刺激が出やすいため注意してください")

    # 朝の強い角質ケア注意
    for step in data.get("morning", {}).get("steps", []):
        ing = normalize_ingredient_tag(step.get("ingredient_focus", ""))
        if ing in ["aha", "bha", "pha", "glycolic_acid", "salicylic_acid", "lactic_acid", "mandelic_acid"]:
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
    # warnings
    if not isinstance(data.get("warnings"), list):
        data["warnings"] = []
    data["warnings"] = build_rule_based_warnings(data, user_data)
    # budget
    data["input_budget"] = safe_price(data.get("input_budget", 0))
    data["total_price"] = safe_price(data.get("total_price", 0))
    data["budget_fit_total"] = safe_price(data.get("budget_fit_total", 0))

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
        "warnings": raw_data.get("warnings", []),
        "improvement_plan": raw_data.get("improvement_plan", {}),
        "input_budget": raw_data.get("input_budget", 0),
        "total_price": raw_data.get("total_price", 0),
        "budget_fit_plan": raw_data.get("budget_fit_plan", {}),
        "budget_fit_total": raw_data.get("budget_fit_total", 0),
        "budget_status": raw_data.get("budget_status", "未判定"),
        "image_path": image_path,
        "model": ANALYSIS_MODEL,
        "version": "1.0"
    }
    


# 診断結果を履歴に追加
def append_result(raw_data, image_path=""):
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

    print("[RESULT SAVED]", record_id, flush=True)

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
            category = str(step.get("category", "") or "").strip()

            if not product:
                continue

            yield {
                "product": product,
                "category": category,
                "section": section_name
            }


def build_product_ranking(results, client_ip=None, limit=20):
    counter = Counter()

    for result in results:
        if not isinstance(result, dict):
            continue

        if client_ip and result.get("client_ip") != client_ip:
            continue

        for item in iter_selected_products_from_result(result):
            key = (
                item["product"],
                item["category"]
            )
            counter[key] += 1

    ranking = []

    for (product, category), count in counter.most_common(limit):
        ranking.append({
            "product": product,
            "category": category,
            "count": count
        })

    return ranking

# トップページ
@app.route("/", methods=["GET", "POST"])
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
    "化粧水": 3,
    "美容液": 4,
    "乳液": 5,
    "クリーム": 6,
    "日焼け止め": 7,
    "パック": 8,
    "ピーリング": 9,
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
    "化粧水": 3,
    "美容液": 4,
    "乳液": 5,
    "クリーム": 6,
    "日焼け止め": 7,
    "パック": 8,
    "ピーリング": 9
}


def step_sort_key(step):
    if not isinstance(step, dict):
        return (99, 999)

    category = normalize_candidate_category(
        step.get("category", ""),
        fallback=step.get("category", "")
    )

    role = step.get("role")
    priority = step.get("priority", 999)

    if role == "booster":
        return (2.5, priority)

    base_order = CATEGORY_ORDER.get(category, 99)

    return (base_order, priority)

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
        "record_date": datetime.today().strftime("%Y-%m-%d")
    }


def resize_for_gemini(file, max_size=640):
    img = Image.open(io.BytesIO(file.read()))
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")

    img.thumbnail((max_size, max_size))

    return img.copy()


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
        ]
    }

def build_analysis_prompt(user_data):
    return f"""
あなたは日本の市販スキンケアと肌分析に詳しい美容アドバイザーです。
肌画像とユーザー情報から、客観的分析、原因推定、改善計画、商品候補作成を行ってください。

【ユーザー情報】
記録日: {user_data['record_date']}
年齢: {user_data['age']}
皮脂: {user_data['oil']}
敏感度: {user_data['sens']}
レチノール経験: {user_data['exp']}
予算: {user_data['budget']}

【画像情報】
1枚目: 正面
2枚目: 左頬
3枚目: 右頬

【左右差分析】
正面、左頬、右頬の画像から左右差を評価する。

symmetry_analysis:
score:
左右差が少ないほど高スコア。
0〜100の整数。

summary:
左右差の全体要約。

left_tendency:
左頬に見られる傾向。
例: 赤みがやや強い / 毛穴が目立つ / 色ムラが少ない

right_tendency:
右頬に見られる傾向。
例: 毛穴がやや目立つ / 赤みが少ない / 色素沈着が目立つ

画像から分からない場合は断定せず、控えめに記載する。

【診断方針】
・画像から確認できる事実を優先する。
・画像から分からないことは断定しない。
・同じ画像、同じユーザー情報では、できるだけ同じ評価と同じ候補を返す。
・人気順、売れ筋順、流行順ではなく、悩みと成分適合を優先する。

【評価項目】
scores は0〜100の整数。
以下を必ず出力する。

oil_balance
redness
pores
hydration
firmness
acne
dullness
barrier
texture
tone_evenness

【カテゴリ固定】
category は必ず以下のみ。

クレンジング
洗顔
化粧水
美容液
乳液
クリーム
日焼け止め
パック
ピーリング

【role固定】
role は main または booster のみ。

【ingredient_focus候補】
ビタミンC
ナイアシンアミド
レチノール
レチナール
アゼライン酸
トラネキサム酸
PDRN
ペプチド
セラミド
ヒアルロン酸
CICA
ドクダミ
AHA
BHA
PHA
UV防御
低刺激

【週ケアルール】
weekly_care は空配列にしない。
肌状態に応じて、ピーリングまたはパックを1〜2件出す。

ピーリングを出す条件:
毛穴詰まり、ざらつき、くすみ、キメ乱れが目立つ場合。

パックを出す条件:
乾燥、赤み、バリア低下、刺激リスク、レチノール使用中の回復ケアが必要な場合。

ただし、肌状態から週ケアが不要と判断できる場合のみ、weekly_care は最小限にする。
カテゴリは必ず「ピーリング」または「パック」。
通常の美容液・化粧水・クリームを weekly_care に入れることは禁止。

【週間運用方針】

routine_strategy を出力する。

overall_policy:
今週の改善方針。

reason:
なぜその方針にしたか。

rotation_needed:
毎日同じ構成ではなく、
使い分けを行うべきなら true。

weekly_focus:
今週重点的に改善する項目。

例:
[
 "毛穴",
 "赤み",
 "色素沈着"
]

【商品候補ルール】
product_candidates は候補収集のみ。
最終選定、順位付け、点数付けは行わない。

各 step の product_candidates は object 配列にする。
各stepのproduct_candidatesは必ず4件以上、最大5件出す。
1件だけ、2件だけ、3件だけは禁止。
0件は禁止。
候補が少ない場合でも、同じcategoryとingredient_focusに合う現行品を6件以上出す。
ただし、数合わせのためにカテゴリ違い・目的違い・旧品・廃盤品・正式名称に自信がない商品を出すことは禁止。
必ずその step の category と同じカテゴリの商品だけを出す。
カテゴリ違いの商品は禁止。

例:
category が「乳液」のstepに「クリーム」は出力禁止。
category が「美容液」のstepに「クリーム」「化粧水」「パック」は出力禁止。
ingredient_focus が「レチノール」のstepに、レチノール系ではない美白美容液だけを出すのは禁止。

各候補は以下を必ず出力する。

{{
  "brand": "",
  "name": "",
  "category": "",
  "confidence": 0,
  "release_status": "current",
  "active_ingredients": [],
  "support_ingredients": [],
  "concerns": [],
  "skin_types": [],
  "sensitive_ok": "unknown",
  "retinol_level": 0,
  "main_functions": [],
  "ingredient_focus": [],
  "ingredient_strength": {{}},
  "formulation": [],
  "technology": [],
  "texture": "",
  "contraindications": [],
  "availability_japan": [],
  "uv_level": {{}},
  "reason": ""
}}

category:
必ずstepのcategoryと完全一致させる。
許可カテゴリ以外は禁止。

active_ingredients:
stepのingredient_focusに対応する主要成分を英語タグで出す。
例:
レチノール -> retinol
レチナール -> retinal
ビタミンC -> vitamin_c
ナイアシンアミド -> niacinamide
アゼライン酸 -> azelaic_acid
セラミド -> ceramide

concerns:
以下から選ぶ。
pores / acne / redness / oil_control / dryness / barrier / dullness / whitening / aging

skin_types:
以下から選ぶ。
dry / oily / mixed / sensitive / normal

sensitive_ok:
yes: 敏感肌向け処方・低刺激・バリアケア系（セラミド・CICA・ドクダミ・パンテノール主体、ノンコメドジェニック等）
no: 刺激のある成分が主体（レチノール・レチナール・高濃度AHA/BHA/PHAなど）
unknown: 上記どちらでもない場合のみ。不明でもできる限り yes / no を判断する。

skin_types:
以下から1つ以上必ず選ぶ（空配列は禁止）。
dry / oily / mixed / sensitive / normal
全肌タイプ向けなら ["normal", "dry", "oily", "mixed"] を出す。
敏感肌向けなら必ず "sensitive" を含める。
目安：
ニキビケア・皮脂抑制系 → oily, mixed を必ず含める
セラミド・バリアケア系 → dry, sensitive を必ず含める
乾燥ケア・高保湿系 → dry を必ず含める

retinol_level:
レチノール・レチナール系でなければ0。
低刺激なら1、標準なら2、高濃度・強めなら3。

main_functions:
以下の値のみ使用する（それ以外は禁止）。
保湿 / バリア強化 / 鎮静ケア / 毛穴改善 / ニキビ予防 / 皮脂抑制 / 美白ケア / 透明感向上 / ハリ改善 / エイジングケア / 紫外線防御 / キメ改善
成分との対応目安：
セラミド・CICA → バリア強化, 鎮静ケア
ビタミンC・トラネキサム酸 → 美白ケア, 透明感向上
ナイアシンアミド → 美白ケア, 毛穴改善, 皮脂抑制
レチノール・ペプチド → エイジングケア, ハリ改善
AHA/BHA/PHA → 毛穴改善, キメ改善
ヒアルロン酸 → 保湿

ingredient_focus:
stepのingredient_focusと一致する成分・目的を配列で出す。

ingredient_strength:
主要成分の強さを high / medium / low で出す。
目安：高濃度・医薬部外品 → high / 一般的な配合量 → medium / 微量配合・補助的 → low
不明なら {{}}。

formulation:
以下の値から選ぶ（複数可）。
low_irritation / barrier_formula / light_texture / rich_texture /
fragrance_free / alcohol_free / oil_free / non_comedogenic / water_based / oil_based
不明なら []。
目安：セラミド・CICA系 → low_irritation, barrier_formula / ニキビ・毛穴系 → oil_free, non_comedogenic

technology:
リポソーム、ナノカプセル、安定化ビタミンCなど、分かる範囲で出す。
不明なら []。

texture:
light / watery / gel / medium / essence / cream / rich / oil / balm / foam / powder から選ぶ。
不明なら ""。

contraindications:
敏感肌注意、朝使用注意、レチノール併用注意などがあれば出す。
不明なら []。

availability_japan:
日本で買える販路を分かる範囲で出す。必ず1つ以上含める。
drugstore: ドラッグストア・スーパーで購入可能（日本大手ブランド・プチプラ系）
amazon: Amazon.co.jpで販売
rakuten: 楽天市場で販売
official_jp: ブランド公式サイト・百貨店専売
不明な場合でも amazon / rakuten は含める。

uv_level:
日焼け止めのみspfとpaを出す。
それ以外は {{}}。

reason:
そのstepのcategory・ingredient_focus・purposeに合う理由を短く出す。

brand:
ブランド名のみ。

name:
商品名のみ。
ブランド名を含めない。

confidence:
0〜100の整数。
90以上: 現行品確実、成分・名称とも高確信
80以上: 名称は確実、代表成分は分かる
70以上: 名称は確実だが成分詳細は不確か
70未満: 出力禁止

release_status:
current のみ。
old / unknown は出力禁止。

【現行品ルール】
現行販売中の商品名のみ出力する。
リニューアル済み商品の場合は、必ず最新の正式名称を使う。
旧名称、旧処方名、旧パッケージ名、リニューアル前の商品名は禁止。
現行品か確信できない商品は出力しない。

【候補選定ルール】
候補は以下の固定優先順位で選ぶ。

1. 目的成分とカテゴリが一致する
2. 日本で継続購入しやすい
3. 正式名称に高い確信がある
4. 刺激リスクが過剰ではない
5. 予算帯から大きく外れない

【禁止】
・架空商品
・旧名称
・リニューアル前商品
・廃盤商品
・正式名称に自信がない商品
・カテゴリ名だけ
・「おすすめ美容液」のような抽象名
・シリーズ名だけ
・推測価格
・ランキングや流行だけを理由にした候補

【改善計画】
improvement_plan は以下だけを簡潔に出す。

priority_concerns:
改善優先度の高い悩みを配列で出す。

key_ingredients:
改善に重要な成分を配列で出す。

care_direction:
全体のケア方針を短く出す。


【ルーティン戦略】
routine_strategy は必ず出力する。

目的:
現在の肌状態から、毎日同じ商品を使う固定型が良いのか、成分や商品を日ごとに分けるローテーション型が良いのかを判断する。

肌スコア改善を最優先にする。
無理にローテーションにしない。
固定が最適なら strategy_type は fixed。
攻め成分を分散した方が良い場合のみ strategy_type は rotation。

routine_strategy:
strategy_type:
fixed / rotation

overall_policy:
全体方針。

morning_policy:
朝の方針。

night_policy:
夜の方針。

weekly_policy:
週ケア方針。

active_care_frequency:
攻めケアの頻度方針。
例: 週2回 / 週2〜3回 / 2週間に1回 / 今は控える

recovery_care_frequency:
回復ケアの頻度方針。

rotation_targets:
ローテーション対象の成分や目的。
例: ["レチノール", "アゼライン酸", "ビタミンC", "保湿回復"]

avoid_combinations:
避ける組み合わせ。
例: ["レチノールとピーリングを同じ夜に使わない"]

reason:
なぜその戦略が今の肌状態に合うか。

【保湿計画】
moisture_plan は必ず以下を出す。

moisture_level
need_emulsion
need_cream
need_double_moisture
reason

【最重要】
JSONのみ返す。
説明禁止。
Markdown禁止。
前置き禁止。
JSONキーは英語。
値は日本語。
"""

def extract_image_bytes_for_hash(image):
    if image is None:
        return b""

    if isinstance(image, bytes):
        return image

    if isinstance(image, bytearray):
        return bytes(image)

    inline_data = getattr(image, "inline_data", None)
    if inline_data is not None:
        data = getattr(inline_data, "data", None)
        if data:
            return data

    data = getattr(image, "data", None)
    if data:
        return data

    return str(image).encode("utf-8")


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

    for key in cache_keys:
        if key in GEMINI_ANALYSIS_CACHE:
            print("[GEMINI MEMORY CACHE HIT]", key, flush=True)
            return copy.deepcopy(GEMINI_ANALYSIS_CACHE[key])

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

    GEMINI_ANALYSIS_CACHE[cache_key] = copy.deepcopy(data)

    file_cache = load_gemini_analysis_file_cache()
    file_cache[cache_key] = copy.deepcopy(data)
    save_gemini_analysis_file_cache(file_cache)

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

    cache_key = f"ai_candidate_schema_v4:{base_cache_key}"

    fallback_cache_keys = [
        cache_key,
        f"ai_candidate_schema_v3:{base_cache_key}",
        f"ai_candidate_schema_v2:{base_cache_key}",
    ]

    cached_analysis = get_gemini_cached_analysis(fallback_cache_keys)

    if cached_analysis:
        cached_analysis.setdefault("warnings", [])
        return cached_analysis

    print("[GEMINI ANALYSIS CACHE MISS]", cache_key, flush=True)
    schema = get_analysis_schema()
    prompt = build_analysis_prompt(user_data)

    try:
        response = call_gemini_with_retry(
            client,
            ANALYSIS_MODEL,
            contents=[prompt, front_img, left_img, right_img],
            config=types.GenerateContentConfig(
                temperature=0,
                top_p=0.1,
                response_mime_type="application/json",
                response_schema=schema
            ),
            max_retries=1
        )

        raw_text = response.text.strip()

    except Exception as e:
        error_text = str(e)

        if "503" in error_text or "UNAVAILABLE" in error_text:
            for fallback_key in fallback_cache_keys:
                if fallback_key in GEMINI_ANALYSIS_CACHE:
                    print(
                        "[GEMINI FALLBACK CACHE HIT]",
                        fallback_key,
                        flush=True
                    )
                    cached = copy.deepcopy(GEMINI_ANALYSIS_CACHE[fallback_key])
                    cached.setdefault("warnings", [])
                    cached["warnings"].append(
                        "現在AI診断が混み合っているため、同じ入力の前回診断結果をもとに表示しています。"
                    )
                    return cached

        raise
    print("=== Gemini raw response ===")
    print(raw_text)

    if raw_text.startswith("```json"):
        raw_text = raw_text.replace("```json", "", 1).strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.replace("```", "", 1).strip()
    if raw_text.endswith("```"):
        raw_text = raw_text[:-3].strip()

    start = raw_text.find("{")
    end = raw_text.rfind("}")

    if start != -1 and end != -1 and end > start:
        raw_text = raw_text[start:end + 1]

    try:
        data = json.loads(raw_text)

        print(
            "[ROUTINE STRATEGY]",
            json.dumps(
                data.get("routine_strategy", {}),
                ensure_ascii=False
            ),
            flush=True
        )
        set_gemini_cached_analysis(cache_key, data)

        return data

    except json.JSONDecodeError as e:
        print("===== GEMINI JSON ERROR =====")
        print(e)
        print("===== RAW TEXT =====")
        print(raw_text)
        print("====================")

        raise ValueError("AIの診断結果JSONが壊れています。もう一度診断してください。")

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

def collect_rich_market_candidates_with_gemini(user_data, analyzed_data):
    schema = get_rich_candidate_collection_schema()
    prompt = build_rich_candidate_collection_prompt(user_data, analyzed_data)

    response = call_gemini_with_quota_guard(
        model=DETAIL_MODEL,
        contents=[prompt],
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=schema
        )
    )

    if response is None:
        return {"steps": []}

    raw_text = (response.text or "").strip()

    if raw_text.startswith("```json"):
        raw_text = raw_text.replace("```json", "", 1).strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.replace("```", "", 1).strip()
    if raw_text.endswith("```"):
        raw_text = raw_text[:-3].strip()

    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start != -1 and end != -1 and end > start:
        raw_text = raw_text[start:end + 1]

    try:
        parsed = json.loads(raw_text)
        if not isinstance(parsed, dict):
            return {"steps": []}
        if not isinstance(parsed.get("steps"), list):
            parsed["steps"] = []
        return parsed
    except Exception as e:
        print("RICH JSON ERROR:", e)
        print("BROKEN JSON ↓")
        print(raw_text)
        return {"steps": []}

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

def build_weekly_usage_plan(data):
    if not isinstance(data, dict):
        return []

    routine_strategy = data.get("routine_strategy", {})
    if not isinstance(routine_strategy, dict):
        routine_strategy = {}

    strategy_type = str(
        routine_strategy.get("strategy_type")
        or routine_strategy.get("type")
        or "fixed"
    ).strip()

    rotation_needed = routine_strategy.get("rotation_needed", None)
    if rotation_needed is True:
        strategy_type = "rotation"

    morning_steps = data.get("morning", {}).get("steps", [])
    night_steps = data.get("night", {}).get("steps", [])
    weekly_steps = data.get("weekly_care", [])

    if not isinstance(morning_steps, list):
        morning_steps = []

    if not isinstance(night_steps, list):
        night_steps = []

    if not isinstance(weekly_steps, list):
        weekly_steps = []

    scores = data.get("scores", {})
    if not isinstance(scores, dict):
        scores = {}

    days = ["月", "火", "水", "木", "金", "土", "日"]

    def clean(value):
        return str(value or "").strip()

    def score_value(key):
        try:
            return safe_price(scores.get(key, 0))
        except Exception:
            return 0

    redness_score = score_value("redness")
    barrier_score = score_value("barrier")
    hydration_score = score_value("hydration")
    texture_score = score_value("texture")
    pores_score = score_value("pores")
    dullness_score = score_value("dullness")

    is_sensitive_week = (
        redness_score <= 65
        or barrier_score <= 65
        or hydration_score <= 60
    )

    needs_texture_care = (
        texture_score <= 65
        or pores_score <= 65
        or dullness_score <= 65
    )

    def step_text(step):
        if not isinstance(step, dict):
            return ""

        return " ".join([
            clean(step.get("category")),
            clean(step.get("ingredient_focus")),
            clean(step.get("purpose")),
            clean(step.get("product")),
            clean(step.get("brand")),
        ])

    def step_label(step):
        if not isinstance(step, dict):
            return ""

        category = clean(step.get("category"))
        product = clean(step.get("product"))
        brand = clean(step.get("brand"))

        if brand and product and not product.startswith(brand):
            product = f"{brand} {product}"

        if product:
            return f"{category}: {product}"

        return category

    def has_any(text, words):
        return any(word in text for word in words)

    def is_strong_active_step(step):
        text = step_text(step)

        strong_words = [
            "レチノール",
            "レチナール",
            "AHA",
            "BHA",
            "ピーリング",
        ]

        return has_any(text, strong_words)

    def is_mild_active_step(step):
        text = step_text(step)

        mild_words = [
            "ビタミンC",
            "アゼライン酸",
            "ナイアシンアミド",
            "トラネキサム酸",
            "PDRN",
            "ペプチド",
        ]

        return has_any(text, mild_words) and not is_strong_active_step(step)

    def is_recovery_step(step):
        text = step_text(step)

        recovery_words = [
            "保湿",
            "セラミド",
            "CICA",
            "ヒアルロン酸",
            "バリア",
            "鎮静",
            "ドクダミ",
            "パック",
        ]

        return has_any(text, recovery_words)

    def is_peeling_step(step):
        return "ピーリング" in step_text(step)

    def is_pack_step(step):
        text = step_text(step)
        return has_any(text, ["パック", "マスク", "シートマスク", "フェイスマスク","フェイスパック"])

    WEEKLY_SHOW_CATEGORIES = {"化粧水", "美容液", "パック", "ピーリング", "ブースター"}
    WEEKLY_BASE_CATEGORIES = {"洗顔", "洗顔料", "クレンジング", "乳液", "クリーム", "日焼け止め"}

    def step_category(step):
        return clean(step.get("category")) if isinstance(step, dict) else ""

    def is_show_step(step):
        return step_category(step) in WEEKLY_SHOW_CATEGORIES

    def is_base_step(step):
        return step_category(step) in WEEKLY_BASE_CATEGORIES

    def filter_for_weekly(steps, changed_base_labels=None):
        result = []
        for step in steps:
            label = step_label(step)
            if not label:
                continue
            cat = step_category(step)
            if cat in WEEKLY_SHOW_CATEGORIES:
                result.append(label)
            elif cat in WEEKLY_BASE_CATEGORIES and changed_base_labels and label in changed_base_labels:
                result.append(label)
        return result

    # ベースカテゴリの変化検出（現状は固定なので空集合、将来のローテーション対応）
    morning_base_by_cat = {}
    for s in morning_steps:
        cat = step_category(s)
        if cat in WEEKLY_BASE_CATEGORIES:
            morning_base_by_cat.setdefault(cat, set()).add(step_label(s))
    changed_base_morning = {
        step_label(s)
        for s in morning_steps
        if step_category(s) in WEEKLY_BASE_CATEGORIES
        and len(morning_base_by_cat.get(step_category(s), set())) > 1
    }

    night_base_by_cat = {}
    for s in night_steps:
        cat = step_category(s)
        if cat in WEEKLY_BASE_CATEGORIES:
            night_base_by_cat.setdefault(cat, set()).add(step_label(s))
    changed_base_night = {
        step_label(s)
        for s in night_steps
        if step_category(s) in WEEKLY_BASE_CATEGORIES
        and len(night_base_by_cat.get(step_category(s), set())) > 1
    }

    fixed_morning = filter_for_weekly(morning_steps, changed_base_morning)

    base_night_steps = [
        step
        for step in night_steps
        if isinstance(step, dict) and not is_strong_active_step(step) and not is_mild_active_step(step)
    ]

    strong_active_steps = [
        step
        for step in night_steps
        if isinstance(step, dict) and is_strong_active_step(step)
    ]

    mild_active_steps = [
        step
        for step in night_steps
        if isinstance(step, dict) and is_mild_active_step(step)
    ]

    recovery_steps = [
        step
        for step in night_steps
        if isinstance(step, dict) and is_recovery_step(step)
    ]

    peeling_steps = [
        step
        for step in weekly_steps
        if isinstance(step, dict) and is_peeling_step(step)
    ]

    pack_steps = [
        step
        for step in weekly_steps
        if isinstance(step, dict) and is_pack_step(step)
    ]

    base_night = filter_for_weekly(base_night_steps, changed_base_night)

    def labels(steps):
        return filter_for_weekly(steps, changed_base_night)

    def make_day(day, morning=None, night=None, special_care=None, note=""):
        return {
            "day": day,
            "morning": morning or [],
            "night": night or [],
            "special_care": special_care or [],
            "note": note
        }

    def first_label(steps):
        if not steps:
            return ""
        return step_label(steps[0])

    usage_plan = []

    if strategy_type == "rotation":
        for index, day in enumerate(days):
            night = list(base_night)
            special_care = []
            note = ""

            if is_sensitive_week:
                if index in [1, 4] and mild_active_steps:
                    night += [first_label(mild_active_steps)]
                    note = "肌を大きく攻めすぎず、赤みや乾燥を見ながら穏やかな改善成分を入れる日です。"
                elif index == 6 and pack_steps:
                    special_care = [first_label(pack_steps)]
                    note = "回復ケアの日。乾燥や赤みを落ち着かせる目的で入れます。"
                else:
                    night += labels(recovery_steps)
                    note = "回復寄りの日。バリアを整えて、次の改善ケアに備えます。"

            else:
                if index in [0, 3] and strong_active_steps:
                    night += [first_label(strong_active_steps)]
                    note = "攻めケアの日。翌日は肌を休ませる前提で入れます。"
                elif index in [2, 5] and mild_active_steps:
                    selected = mild_active_steps[index % len(mild_active_steps)]
                    night += [step_label(selected)]
                    note = "穏やかな改善成分を入れる日。攻めすぎずに悩みへ寄せます。"
                elif index == 6 and pack_steps:
                    special_care = [first_label(pack_steps)]
                    note = "週の最後は回復ケアで整える日です。"
                elif needs_texture_care and index == 5 and peeling_steps:
                    special_care = [first_label(peeling_steps)]
                    note = "角質ケアの日。レチノールや高刺激成分とは同じ夜に重ねません。"
                else:
                    night += labels(recovery_steps)
                    note = "回復寄りの日。肌を休ませて、次のケアの効きやすさを整えます。"

            usage_plan.append(
                make_day(
                    day=day,
                    morning=fixed_morning,
                    night=night,
                    special_care=special_care,
                    note=note
                )
            )

        return usage_plan

    for index, day in enumerate(days):
        night = filter_for_weekly(night_steps, changed_base_night)

        special_care = []
        note = routine_strategy.get("overall_policy") or "固定ルーティンを軸に、肌を安定させる方針です。"

        if index == 5 and peeling_steps and needs_texture_care and not is_sensitive_week:
            special_care = [first_label(peeling_steps)]
            note = "肌状態に余裕がある週だけ、角質ケアを入れる想定です。赤みや乾燥が強い場合は休みます。"

        elif index == 6 and pack_steps:
            special_care = [first_label(pack_steps)]
            note = "週の最後に回復ケアを入れて、乾燥や赤みを整えます。"

        usage_plan.append(
            make_day(
                day=day,
                morning=fixed_morning,
                night=night,
                special_care=special_care,
                note=note
            )
        )

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

            if not can_use_free_diagnosis(client_ip):
                return render_template(
                    "lab.html",
                    error=f"無料診断は月{FREE_MONTHLY_LIMIT}回までです。続けて利用するには有料プランをご利用ください。",
                    DISABLE_USAGE_LIMIT=DISABLE_USAGE_LIMIT
                )

            ip = request.remote_addr

            if is_rate_limited(ip):
                return "<h2>本日の診断回数の上限に達しました</h2>"
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

            # 診断ごとにcriteria検索キャッシュ/カウンターをリセット
            global _rakuten_criteria_cache, _rakuten_criteria_call_count
            _rakuten_criteria_cache = {}
            _rakuten_criteria_call_count = 0

            try:
                print("[LAB CHECK] before Gemini", flush=True)

                data = analyze_skin_with_gemini(
                    user_data,
                    front_img,
                    left_img,
                    right_img
                )

                print("[LAB CHECK] after Gemini", flush=True)

            except Exception as e:
                print("===== LAB ERROR =====")
                print(e)
                traceback.print_exc()
                print("=====================")

                error_text = str(e)

                message = "診断中にエラーが発生しました。時間をおいて再度お試しください。"

                if "503" in error_text or "UNAVAILABLE" in error_text:
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
            # ⑥ DB読み込み
            # =========================
            products = load_products()
            

            validate_and_log_products(products)

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

            data = assign_products_to_all_steps(data, products, user_data, budget_value)
            
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
                increment_free_usage(client_ip)
                increment_global_usage()
            except Exception as e:
                print("===== USAGE SAVE ERROR =====")
                print(e)

            data["client_ip"] = client_ip
            saved_record = None
            try:
                saved_record = append_result(lightweight_result_payload(data))
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
            html = render_template(
                "result.html",
                data=data
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
    remaining_free_count = get_remaining_free_count(client_ip)
    gemini_usage = get_gemini_usage_status()
    return render_template(
        "lab.html",
        remaining_free_count=remaining_free_count,
        gemini_usage=gemini_usage,
        DISABLE_USAGE_LIMIT=DISABLE_USAGE_LIMIT
    )

@app.route("/admin/db-stats")
def db_stats():
    admin_key = request.args.get("key", "")

    if admin_key != os.getenv("ADMIN_KEY", ""):
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
    return render_template("premium.html")

@app.route("/admin/product-ranking")
def admin_product_ranking():
    admin_key = request.args.get("key", "")

    if admin_key != os.getenv("ADMIN_KEY", ""):
        return jsonify({
            "error": "unauthorized"
        }), 403

    results = load_results()
    ranking = build_product_ranking(results, limit=30)

    return render_template(
        "product_ranking.html",
        title="全ユーザー 商品出力ランキング",
        ranking=ranking
    )


@app.route("/my-product-ranking")
def my_product_ranking():
    client_ip = get_client_ip()
    results = load_results()

    ranking = build_product_ranking(
        results,
        client_ip=client_ip,
        limit=20
    )

    return render_template(
        "product_ranking.html",
        title="よく提案される商品",
        ranking=ranking
    )
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

    if not any(domain in url for domain in allowed_domains):
        return "許可されていないリンクです", 400

    return redirect(url)
@app.route("/pricing")
def pricing():
    source = request.args.get("source", "unknown")
    log_pricing_view(source)
    return render_template("pricing.html")
# 診断履歴ページ
@app.route("/history")
def history():
    try:
        history_data = load_results()

        if not isinstance(history_data, list):
            history_data = []

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

        improvement_summary = []

        for key, values in score_series.items():
            if len(values) >= 2:
                diff = values[-1] - values[0]

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

        return render_template(
            "history.html",
            history=prepared,
            labels=labels,
            scores=skin_scores,
            score_series=score_series,
            improvement_summary=improvement_summary,
            premium_score_series=premium_score_series,
            is_premium=False
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
            premium_score_series={}
        )

@app.route("/history/<result_id>")
def result_detail(result_id):
    try:
        history_data = load_results()

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
                data["is_dev_mode"] = True

                if not isinstance(data.get("symmetry_analysis"), dict):
                    data["symmetry_analysis"] = {
                        "summary": "",
                        "left_tendency": "",
                        "right_tendency": ""
                    }
                return render_template("result.html", data=data)

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
        if previous_scores:
            for key, current_value in current_scores.items():
                prev_value = safe_int(previous_scores.get(key, 0))
                score_diff[key] = current_value - prev_value
        else:
            for key in current_scores.keys():
                score_diff[key] = None
        return render_template(
            "history_detail.html",
            data=current,
            prev_data=previous,
            score_diff=score_diff
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