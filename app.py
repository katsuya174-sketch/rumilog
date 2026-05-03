import os
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
def call_gemini_with_retry(client, model, contents, config=None, max_retries=5):
    import time
    from google.genai import errors

    last_error = None

    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config
            )
            return response

        except (errors.ServerError, errors.APIError) as e:
            last_error = e
            msg = str(e)

            print(f"[Gemini retry] attempt={attempt+1}/{max_retries} error={msg}")

            retryable = (
                "503" in msg or
                "UNAVAILABLE" in msg or
                "429" in msg or
                "RESOURCE_EXHAUSTED" in msg
            )

            if not retryable:
                raise

            if attempt == max_retries - 1:
                raise

            wait_seconds = min(12, 2 * (attempt + 1))
            time.sleep(wait_seconds)

        except Exception as e:
            print(f"[Gemini fatal] {e}")
            raise

    raise last_error

from datetime import datetime, date
from PIL import Image
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
DEV_PREMIUM_MODE = False     # 開発中に有料表示を確認したい時だけTrue

def is_premium_user():
    if DEV_PREMIUM_MODE:
        return True

    # 後でログイン/決済と接続する
    # 例: session.get("is_premium") == True
    return False

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
        products = json.load(f)

    # Noneや壊れたデータを除去
    products = [p for p in products if isinstance(p, dict)]

   
    for p in products:
        # category正規化
        category = p.get("category", "")
        p["category"] = AI_CATEGORY_MAP.get(category, category)

        # concerns正規化
        concerns = p.get("concerns", [])
        new_concerns = []

        for c in concerns:
            mapped = CONCERN_MAP.get(c, c)
            if mapped is None:
                continue
            new_concerns.append(mapped)

        p["concerns"] = list(dict.fromkeys(new_concerns))

        # main_functions正規化
        main_functions = p.get("main_functions", [])
        new_main_functions = []

        for mf in main_functions:
            mapped = MAIN_FUNCTION_MAP.get(mf, mf)
            if mapped:
                new_main_functions.append(mapped)

        p["main_functions"] = list(dict.fromkeys(new_main_functions))

    return products


AFFILIATE_LINKS_AI_FILE = "affiliate_links_ai.json"

print("[RAKUTEN APP ID RAW]", repr(RAKUTEN_APP_ID))
print("[RAKUTEN APP ID LENGTH]", len(RAKUTEN_APP_ID))

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

def clean_rakuten_image_url(url):
    if not url:
        return ""
    url = str(url).strip()
    if url.startswith("//"):
        url = "https:" + url
    return url


def fetch_rakuten_item(product_name, category=""):
    if not product_name:
        print("[RAKUTEN API] product_name empty")
        return None

    if not RAKUTEN_APP_ID:
        print("[RAKUTEN API] RAKUTEN_APP_ID is empty")
        return None

    if not RAKUTEN_ACCESS_KEY:
        print("[RAKUTEN API] RAKUTEN_ACCESS_KEY is empty")
        return None

    keyword_parts = [str(product_name).strip()]
    if category:
        keyword_parts.append(str(category).strip())

    keyword = " ".join([p for p in keyword_parts if p])

    endpoint = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20220601"

    params = {
        "applicationId": RAKUTEN_APP_ID,
        "accessKey": RAKUTEN_ACCESS_KEY,
        "keyword": keyword,
        "hits": 3,
        "format": "json",
        "formatVersion": 2,
        "imageFlag": 1,
    }

    if RAKUTEN_AFFILIATE_ID:
        params["affiliateId"] = RAKUTEN_AFFILIATE_ID

    try:
        print(f"[RAKUTEN API REQUEST] keyword={keyword}")

        headers = {
            "Referer": "http://example.com/",
            "Origin": "http://example.com/",
            "User-Agent": "Mozilla/5.0"
        }

        res = requests.get(endpoint, params=params, headers=headers, timeout=10)
        print(f"[RAKUTEN API STATUS] {res.status_code}")
        print(f"[RAKUTEN API URL] {res.url}")

        # 400 の中身を見やすくする
        if res.status_code != 200:
            print("[RAKUTEN API ERROR BODY]", res.text)

        res.raise_for_status()

        payload = res.json()
        items = payload.get("items", [])

        print(f"[RAKUTEN API ITEMS] count={len(items)}")

        if not items:
            print(f"[RAKUTEN API] no items for keyword={keyword}")
            return None

        best = items[0]

        image_url = ""
        medium_images = best.get("mediumImageUrls", [])
        if isinstance(medium_images, list) and medium_images:
            first_image = medium_images[0]
            if isinstance(first_image, str):
                image_url = first_image
            elif isinstance(first_image, dict):
                image_url = first_image.get("imageUrl", "")

        result = {
            "name": best.get("itemName", ""),
            "price": best.get("itemPrice", 0),
            "rakuten_link": best.get("affiliateUrl") or best.get("itemUrl") or "#",
            "image": clean_rakuten_image_url(image_url),
        }

        print("[RAKUTEN API BEST ITEM]", {
            "name": result["name"],
            "price": result["price"],
            "image": result["image"],
            "rakuten_link_exists": result["rakuten_link"] != "#"
        })

        return result

    except requests.exceptions.RequestException as e:
        print("[RAKUTEN API REQUEST ERROR]", e)
        return None
    except Exception as e:
        print("[RAKUTEN API UNKNOWN ERROR]", repr(e))
        return None


def apply_rakuten_image_and_link(step):
    if not isinstance(step, dict):
        return step

    product_name = step.get("product", "")
    category = step.get("category", "")

    rakuten_item = fetch_rakuten_item(product_name, category)
    if not rakuten_item:
        print(f"[RAKUTEN IMAGE] no rakuten item: product={product_name}, category={category}")
        return step

    if rakuten_item.get("rakuten_link"):
        step["rakuten_link"] = rakuten_item["rakuten_link"]

    current_image = step.get("image", "")
    if (not current_image) or ("/static/images/products/" in str(current_image)):
        if rakuten_item.get("image"):
            step["image"] = rakuten_item["image"]
            print(f"[RAKUTEN IMAGE APPLIED] {product_name} -> {rakuten_item['image']}")
        else:
            print(f"[RAKUTEN IMAGE EMPTY] {product_name}")

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
    product_name = step.get("product", "")
    category = step.get("category", "")

    # 1. step自体に直リンクがあるなら最優先
    if "affiliate_links" in step and isinstance(step["affiliate_links"], dict):
        step["amazon_link"] = step["affiliate_links"].get("amazon", "#")
        step["rakuten_link"] = step["affiliate_links"].get("rakuten", "#")
        step = apply_rakuten_image_and_link(step)
        return step

    # 2. AI候補専用DBで照合
    matched_links = find_affiliate_links_for_ai_product(product_name, category, affiliate_ai_db)
    if matched_links:
        step["amazon_link"] = matched_links.get("amazon", "#")
        step["rakuten_link"] = matched_links.get("rakuten", "#")
        step = apply_rakuten_image_and_link(step)
        return step

    # 3. 見つからなければ検索リンク
    step["amazon_link"] = build_amazon_link(product_name)
    step["rakuten_link"] = build_rakuten_link(product_name)

    # 4. 画像だけは楽天APIから補完
    step = apply_rakuten_image_and_link(step)

    return step

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
    text = text.replace("　", " ")
    text = text.replace("・", "")
    text = text.replace("-", "")
    text = text.replace("（", "")
    text = text.replace("）", "")
    text = text.replace("(", "")
    text = text.replace(")", "")
    text = text.replace("  ", " ")
    text = text.replace("the ", "")
    text = text.replace("serum", "セラム")
    text = text.replace("ampoule", "アンプル")

    return text
def find_db_product_by_name(products, product_name, category=None):
    target = normalize_product_name(product_name)

    if not target:
        return None

    # まず完全一致
    for p in products:
        if not isinstance(p, dict):
            continue

        db_name = normalize_product_name(p.get("name", ""))
        if category and p.get("category") != category:
            continue

        if target == db_name:
            return p

    # 次に部分一致
    for p in products:
        if not isinstance(p, dict):
            continue

        db_name = normalize_product_name(p.get("name", ""))
        if category and p.get("category") != category:
            continue

        if target in db_name or db_name in target:
            return p

    return None

def find_ai_candidate_data(product_name, ai_image_db):
    target = normalize_product_name(product_name)

    if not target:
        return None, 0

    # まず完全一致
    for item in ai_image_db:
        db_name = normalize_product_name(item.get("name", ""))

        if target == db_name:
            image_file = item.get("image", "")
            price = item.get("price", 0)

            image_path = f"/static/images/products/{image_file}" if image_file else None
            return image_path, price

    # 次に部分一致
    for item in ai_image_db:
        db_name = normalize_product_name(item.get("name", ""))

        if target in db_name or db_name in target:
            image_file = item.get("image", "")
            price = item.get("price", 0)

            image_path = f"/static/images/products/{image_file}" if image_file else None
            return image_path, price

    return None, 0

def normalize_text(value):
    if value is None:
        return ""
    return str(value).strip().lower()

def ensure_result_structure(data):
    if not isinstance(data, dict):
        data = {}

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

    return data

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
    retinol_level = int(product.get("retinol_level", 0) or 0)
    price_ref = safe_price(product.get("price_ref", 0))
    availability = product.get("availability_japan", [])
    product_functions = product.get("main_functions", [])
    product_focuses = product.get("ingredient_focus", [])
    product_formulation = product.get("formulation", [])
    product_technology = product.get("technology", [])
    product_texture = normalize_text(product.get("texture", ""))
    product_contra = product.get("contraindications", [])
    ingredient_strength_map = product.get("ingredient_strength", {})

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
    spf = int(uv_info.get("spf", 0) or 0)
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
            "aha", "bha", "pha", "lha",
            "glycolic_acid", "lactic_acid",
            "salicylic_acid", "mandelic_acid"
        ]

        product_actives = product.get("active_ingredients", [])

        if not any(i in product_actives for i in PEELING_INGREDIENTS):
            return -9999  # 強制除外

    user_skin = normalize_text(user_data.get("skin_type", ""))
    if not user_skin:
        user_skin = normalize_text(user_data.get("oil", ""))

    purpose = step.get("purpose", "")
    ingredient_focus = step.get("ingredient_focus", "")
    category = step.get("category", "")
    product_category = product.get("category", "")

    # カテゴリ一致は最優先
    if product_category != category:
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
    if category in ["クレンジング", "洗顔"]:
        score += apply_cleansing_score_rules(
            product=product,
            user_data=user_data,
            concern_tags=concern_tags
        )

    elif category == "日焼け止め":
        score += apply_sunscreen_score_rules(
            product=product,
            step=step,
            user_data=user_data,
            concern_tags=concern_tags
        )

 
    product_actives = product.get("active_ingredients", [])
    if ingredient_tag and ingredient_tag in product_actives:
        score += 15  # ←強めにする（10〜20調整可）

    product_skin_types = product.get("skin_types") or []
    if user_skin in product_skin_types:
        score += 5
    elif "normal" in product_skin_types:
        score += 2

    return score


def score_improvement(product, improvement_plan):
    """
    改善プランとの一致スコア
    使う項目:
    - active_ingredients
    - support_ingredients
    - ingredient_strength
    - main_functions
    - ingredient_focus
    """
    score = 0

    if not improvement_plan:
        return 0

    key_ingredients = []
    actions = []

    immediate = improvement_plan.get("immediate", {})
    short_term = improvement_plan.get("short_term", {})

    key_ingredients += immediate.get("key_ingredients", [])
    key_ingredients += short_term.get("key_ingredients", [])

    actions += immediate.get("actions", [])
    actions += short_term.get("actions", [])

    product_actives = product.get("active_ingredients", [])
    product_support = product.get("support_ingredients", [])
    product_functions = product.get("main_functions", [])
    product_focuses = product.get("ingredient_focus", [])
    ingredient_strength_map = product.get("ingredient_strength", {})

    normalized = []
    for ing in key_ingredients:
        tag = normalize_ingredient_tag(ing)
        if tag:
            normalized.append(tag)

    normalized = list(dict.fromkeys(normalized))

    # 成分一致
    for ing in normalized:
        if ing in product_actives:
            score += 22
            score += get_strength_score(ingredient_strength_map.get(ing))

        elif ing in product_support:
            score += 8

    # main_functions一致
    for f in product_functions:
        f_norm = normalize_text(f)
        for act in actions:
            act_norm = normalize_text(act)
            if f_norm and act_norm and (f_norm in act_norm or act_norm in f_norm):
                score += 10
                break

    # ingredient_focus一致
    for focus in product_focuses:
        focus_norm = normalize_text(focus)
        for act in actions:
            act_norm = normalize_text(act)
            if focus_norm and act_norm and (focus_norm in act_norm or act_norm in focus_norm):
                score += 8
                break

    return score

# =========================================================
# SCORE BLOCK END
# =========================================================

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

def build_virtual_product_from_ai_candidate(step, candidate):
    category = step.get("category", "")
    ingredient_focus = step.get("ingredient_focus", "")
    purpose = step.get("purpose", "")

    if isinstance(candidate, str):
        candidate = {
            "name": candidate,
            "price_ref": safe_price(step.get("estimated_price", 0)),
            "active_ingredients": [],
            "support_ingredients": [],
            "signature_ingredients": [],
            "concerns": purpose_to_concern_tags(purpose),
            "skin_types": [],
            "sensitive_ok": "unknown",
            "retinol_level": 0,
            "main_functions": [],
            "formulation": [],
            "technology": [],
            "texture": "",
            "contraindications": [],
            "reason": ""
        }

    active_ingredients = [
        normalize_ingredient_tag(x)
        for x in candidate.get("active_ingredients", [])
    ]
    active_ingredients = [x for x in active_ingredients if x]

    support_ingredients = [
        normalize_ingredient_tag(x)
        for x in candidate.get("support_ingredients", [])
    ]
    support_ingredients = [x for x in support_ingredients if x]

    signature_ingredients = [
        normalize_ingredient_tag(x)
        for x in candidate.get("signature_ingredients", [])
    ]
    signature_ingredients = [x for x in signature_ingredients if x in signature_ingredient_effects]

    ingredient_tag = normalize_ingredient_tag(ingredient_focus)
    if ingredient_tag and ingredient_tag not in active_ingredients:
        active_ingredients.append(ingredient_tag)

    concerns = []
    for c in candidate.get("concerns", []):
        c = normalize_text(c)
        if c in ["pores", "acne", "redness", "oil_control", "dryness", "barrier", "dullness", "whitening", "aging"]:
            concerns.append(c)
    if not concerns:
        concerns = purpose_to_concern_tags(purpose)

    skin_types = []
    for s in candidate.get("skin_types", []):
        s = normalize_text(s)
        if s in ["dry", "oily", "mixed", "sensitive"]:
            skin_types.append(s)

    sensitive_ok = normalize_text(candidate.get("sensitive_ok", "unknown"))
    if sensitive_ok not in ["yes", "no", "unknown"]:
        sensitive_ok = "unknown"

    texture = normalize_text(candidate.get("texture", ""))
    if texture not in ["light", "watery", "gel", "medium", "essence", "cream", "rich", "oil", "balm", "foam", "powder"]:
        texture = ""

    main_functions = [str(x) for x in candidate.get("main_functions", []) if str(x).strip()]
    formulation = [str(x) for x in candidate.get("formulation", []) if str(x).strip()]
    technology = [str(x) for x in candidate.get("technology", []) if str(x).strip()]
    contraindications = [str(x) for x in candidate.get("contraindications", []) if str(x).strip()]

    return {
        "name": candidate.get("name", ""),
        "category": category,
        "active_ingredients": list(dict.fromkeys(active_ingredients)),
        "support_ingredients": list(dict.fromkeys(support_ingredients)),
        "signature_ingredients": list(dict.fromkeys(signature_ingredients)),
        "concerns": list(dict.fromkeys(concerns)),
        "skin_types": list(dict.fromkeys(skin_types)),
        "sensitive_ok": sensitive_ok,
        "retinol_level": int(candidate.get("retinol_level", 0) or 0),
        "price_ref": safe_price(candidate.get("price_ref", 0)),
        "main_functions": list(dict.fromkeys(main_functions)),
        "formulation": list(dict.fromkeys(formulation)),
        "technology": list(dict.fromkeys(technology)),
        "texture": texture,
        "contraindications": list(dict.fromkeys(contraindications)),
        "image": ""
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
    name = str(product.get("name", "") or product.get("product", "")).lower()

    for kw in DISCONTINUED_KEYWORDS:
        if kw.lower() in name:
            return True

    status = str(product.get("status", "")).lower()
    if status in ["discontinued", "out_of_stock", "ended"]:
        return True

    return False

def select_best_market_candidate(step, db_products, user_data, budget_value, improvement_plan=None, exclude_names=None):
    if exclude_names is None:
        exclude_names = set()

    category = step.get("category", "")
    candidates = normalize_ai_candidates(step)

    all_candidates = []

    # DB商品を全件候補化
    for p in db_products:
        if not isinstance(p, dict):
            continue
        if p.get("category") != category:
            continue
        if p.get("name") in exclude_names:
            continue

        product = dict(p)

        base_score = score_product(product, step, user_data, budget_value)
        improve_score = score_improvement(product, improvement_plan or {})

        base_weight, improve_weight = get_dynamic_score_weights(step, user_data)
        final_score = (base_score * base_weight) + (improve_score * improve_weight)

        product["_score"] = round(final_score, 1)
        product["_base_score"] = round(base_score, 1)
        product["_improve_score"] = round(improve_score, 1)
        product["_source"] = "db"

        all_candidates.append(product)

    # AI候補を全件候補化
    for candidate in candidates:
        candidate_name = candidate.get("name", "")

        if not candidate_name:
            continue

        if is_discontinued_or_suspicious_product(candidate):
            continue

        if candidate_name in exclude_names:
            continue

        db_match = find_db_product_by_name(db_products, candidate_name, category)

        # AI候補名がDBにあるなら、DB詳細を使って昇格
        if db_match:
            product = dict(db_match)

            base_score = score_product(product, step, user_data, budget_value)
            improve_score = score_improvement(product, improvement_plan or {})

            base_weight, improve_weight = get_dynamic_score_weights(step, user_data)
            final_score = (base_score * base_weight) + (improve_score * improve_weight)

            product["_score"] = round(final_score, 1)
            product["_base_score"] = round(base_score, 1)
            product["_improve_score"] = round(improve_score, 1)
            product["_source"] = "ai+db"

            all_candidates.append(product)
            continue

        # DBにないAI候補は仮想商品として評価
        virtual = build_virtual_product_from_ai_candidate(candidate, step)
        if is_wrong_cleanser_candidate(virtual,step):
            continue

        if is_non_cosmetic(virtual):
            continue
        base_score = score_product(virtual, step, user_data, budget_value)
        improve_score = score_improvement(virtual, improvement_plan or {})

        base_weight, improve_weight = get_dynamic_score_weights(step, user_data)
        final_score = (base_score * base_weight) + (improve_score * improve_weight)

        virtual["_score"] = round(final_score, 1)
        virtual["_base_score"] = round(base_score, 1)
        virtual["_improve_score"] = round(improve_score, 1)
        virtual["_source"] = "ai_virtual"

        all_candidates.append(virtual)

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

    top_candidates = sorted_candidates[:3]
    best = dict(top_candidates[0])
    best["_top_candidates"] = [
        {
            "name": c.get("name", ""),
            "score": c.get("_score", 0),
            "base_score": c.get("_base_score", 0),
            "improve_score": c.get("_improve_score", 0),
            "source": c.get("_source", ""),
            "price_ref": c.get("price_ref", 0),
        }
        for c in top_candidates
    ]

    log_candidate_battle(step, sorted_candidates, best)

    return best


def apply_moisture_plan(data):
    import json

    if isinstance(data, str):
        data = json.loads(data)

    moisture_plan = data.get("moisture_plan", {})
    need_emulsion = moisture_plan.get("need_emulsion", False)
    need_cream = moisture_plan.get("need_cream", False)
    need_double_moisture = moisture_plan.get("need_double_moisture", False)

    for section in ["morning", "night"]:
        steps = data.get(section, {}).get("steps", [])
        filtered_steps = []

        for step in steps:
            if not isinstance(step, dict):
                continue

            category = step.get("category", "")

            if category == "乳液" and not need_emulsion:
                continue

            if category == "クリーム" and not need_cream:
                continue

            filtered_steps.append(step)

        categories = [s.get("category") for s in filtered_steps if isinstance(s, dict)]

        if need_double_moisture:
            if "乳液" not in categories:
                filtered_steps.append({
                    "category": "乳液",
                    "role": "main",
                    "purpose": "保湿強化",
                    "ingredient_focus": "セラミド",
                    "risk_note": "",
                    "priority": 4,
                    "product_candidates": []
                })

            if "クリーム" not in categories:
                filtered_steps.append({
                    "category": "クリーム",
                    "role": "main",
                    "purpose": "バリア保護",
                    "ingredient_focus": "セラミド",
                    "risk_note": "",
                    "priority": 5,
                    "product_candidates": []
                })

        data[section]["steps"] = filtered_steps

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
                            "items": {"type": "string"}
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
・各ステップごとに product_candidates を 8〜10 個出すこと
・最低でも 8 個以上出すこと
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
        model="gemini-2.5-flash",
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
    if not name:
        return ""
    text = str(name).strip().lower()
    text = text.replace("　", " ")
    text = text.replace("・", "")
    text = text.replace("-", "")
    text = text.replace("（", "")
    text = text.replace("）", "")
    text = text.replace("(", "")
    text = text.replace(")", "")
    text = text.replace("  ", " ")
    text = text.replace("the ", "")
    return text


def merge_candidate_lists(original, extra, max_items=20):
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

def normalize_ai_candidates(step):
    raw_candidates = step.get("product_candidates", [])
    normalized = []
    seen = set()

    if not isinstance(raw_candidates, list):
        return []

    for candidate in raw_candidates:
        if isinstance(candidate, dict):
            name = str(candidate.get("name", "")).strip()
            if not name:
                continue

            item = {
                "name": name,
                "price_ref": safe_price(candidate.get("price_ref", 0)),
                "active_ingredients": candidate.get("active_ingredients", []) if isinstance(candidate.get("active_ingredients", []), list) else [],
                "support_ingredients": candidate.get("support_ingredients", []) if isinstance(candidate.get("support_ingredients", []), list) else [],
                "signature_ingredients": candidate.get("signature_ingredients", []) if isinstance(candidate.get("signature_ingredients", []), list) else [],
                "concerns": candidate.get("concerns", []) if isinstance(candidate.get("concerns", []), list) else [],
                "skin_types": candidate.get("skin_types", []) if isinstance(candidate.get("skin_types", []), list) else [],
                "sensitive_ok": candidate.get("sensitive_ok", "unknown"),
                "retinol_level": int(candidate.get("retinol_level", 0) or 0),
                "main_functions": candidate.get("main_functions", []) if isinstance(candidate.get("main_functions", []), list) else [],
                "formulation": candidate.get("formulation", []) if isinstance(candidate.get("formulation", []), list) else [],
                "technology": candidate.get("technology", []) if isinstance(candidate.get("technology", []), list) else [],
                "texture": str(candidate.get("texture", "") or ""),
                "contraindications": candidate.get("contraindications", []) if isinstance(candidate.get("contraindications", []), list) else [],
                "reason": str(candidate.get("reason", "") or "")
            }

        else:
            name = str(candidate).strip()
            if not name:
                continue

            item = {
                "name": name,
                "price_ref": safe_price(step.get("estimated_price", 0)),
                "active_ingredients": [],
                "support_ingredients": [],
                "signature_ingredients": [],
                "concerns": purpose_to_concern_tags(step.get("purpose", "")),
                "skin_types": [],
                "sensitive_ok": "unknown",
                "retinol_level": 0,
                "main_functions": [],
                "formulation": [],
                "technology": [],
                "texture": "",
                "contraindications": [],
                "reason": str(step.get("selection_reason", "") or "")
            }

        norm_name = normalize_candidate_name_for_merge(item["name"])
        if not norm_name or norm_name in seen:
            continue

        seen.add(norm_name)
        normalized.append(item)

    return normalized

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
                step["product_candidates"] = merge_candidate_lists(
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
    return candidates[0]

def assign_products_to_all_steps(data, products, user_data, budget_value):
    print("MARKET VERSION assign_products_to_all_steps")

    ai_image_db = load_ai_product_images()
    improvement_plan = data.get("improvement_plan", {})

    def assign_one_step(step, used_product_names, section_name):
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
        )

        print("====== MARKET BATTLE ======")
        print("section:", section_name)
        print("category:", category)
        print("ingredient_focus:", step.get("ingredient_focus", ""))
        print("candidates:", step.get("product_candidates", []))
        print("best:", best.get("name") if best else "なし")
        print("score:", best.get("_score") if best else "なし")
        print("source:", best.get("_source") if best else "なし")
        print("===========================")

        # 1) 通常選定で勝者がいればそれを採用
        if best:
            used_product_names.add(best.get("name"))

            step["top_candidates"] = best.get("_top_candidates", [])
            source = best.get("_source", "")

            if source in ["db", "ai+db", "fallback_db"]:
                apply_db_product_to_step(step, best, user_data)
                step["product_source"] = source if source else "db"

            elif source == "ai":
                # ここでは best を直接使って AI候補を適用
                step["product"] = best.get("name", category)
                step["price"] = safe_price(best.get("price_ref", 0))
                step["estimated_price"] = safe_price(best.get("price_ref", 0))

                image_path = None
                if ai_image_db:
                    image_path, found_price = find_ai_candidate_data(best.get("name", ""), ai_image_db)
                    if found_price and step["price"] <= 0:
                        step["price"] = safe_price(found_price)
                        step["estimated_price"] = safe_price(found_price)

                step["image"] = image_path if image_path else get_product_image(category)
                step["match_score"] = best.get("_score", 0) or 0
                step["base_score"] = best.get("_base_score", 0) or 0
                step["improve_score"] = best.get("_improve_score", 0) or 0
                step["recommend_reason"] = best.get("reason") or step.get("selection_reason") or build_ai_reason(step, user_data)
                step["product_source"] = "ai"

                impact = calculate_step_impact(step, best)
                step["impact_scores"] = impact
                step["top_impacts"] = format_top_impacts(impact)

            else:
                apply_db_product_to_step(step, best, user_data)
                step["product_source"] = source if source else "db"

            return normalize_step_price_fields(step)

        # 2) 通常選定でダメなら、DBからカテゴリ一致だけで最低1個強制取得
        fallback_db = pick_best_db_fallback_product(
            step=step,
            products=products,
            user_data=user_data,
            budget_value=budget_value,
            exclude_names=used_product_names
        )

        if fallback_db:
            used_product_names.add(fallback_db.get("name"))
            step["top_candidates"] = []
            apply_db_product_to_step(step, fallback_db, user_data)
            step["product_source"] = "fallback"
            return normalize_step_price_fields(step)

        # 3) DBにも無いなら最後に汎用fallback
        step["top_candidates"] = []
        apply_category_fallback_to_step(step, user_data)
        step["product_source"] = "fallback"
        return normalize_step_price_fields(step)

    # 朝・夜
    for section in ["morning", "night"]:
        used_product_names = set()

        steps = data.get(section, {}).get("steps", [])
        if not isinstance(steps, list):
            continue

        for idx, step in enumerate(steps):
            steps[idx] = assign_one_step(step, used_product_names, section)

    # 週ケア
    used_weekly_names = set()
    weekly_steps = data.get("weekly_care", [])
    if isinstance(weekly_steps, list):
        for idx, step in enumerate(weekly_steps):
            weekly_steps[idx] = assign_one_step(step, used_weekly_names, "weekly_care")

    return data
def build_recommend_reason(product, step, user_data):
    reasons = []

    purpose = str(step.get("purpose", ""))
    ingredient_focus = str(step.get("ingredient_focus", ""))
    skin_oil = str(user_data.get("oil", ""))
    skin_sens = str(user_data.get("sens", ""))

    product_concerns = product.get("concerns", [])
    active_ingredients = product.get("active_ingredients", [])
    support_ingredients = product.get("support_ingredients", [])
    skin_types = product.get("skin_types", [])
    sensitive_ok = product.get("sensitive_ok", "unknown")

    # 目的との一致
    if "毛穴" in purpose and "pores" in product_concerns:
        reasons.append("毛穴悩みに向く設計")
    if ("くすみ" in purpose or "透明感" in purpose or "美白" in purpose) and (
        "dullness" in product_concerns or "whitening" in product_concerns
    ):
        reasons.append("透明感ケア向き")
    if ("乾燥" in purpose or "保湿" in purpose or "水分" in purpose) and (
        "dryness" in product_concerns or "barrier" in product_concerns
    ):
        reasons.append("保湿・バリアケア向き")
    if ("赤み" in purpose) and "redness" in product_concerns:
        reasons.append("赤みケア向き")
    if ("ニキビ" in purpose) and "acne" in product_concerns:
        reasons.append("ニキビ悩みに向く設計")
    if ("ハリ" in purpose or "エイジング" in purpose) and "aging" in product_concerns:
        reasons.append("ハリ・エイジングケア向き")
   
    normalized_ingredient = normalize_ingredient_tag(ingredient_focus)
    if normalized_ingredient in active_ingredients:
        reasons.append(f"{ingredient_map.get(normalized_ingredient, normalized_ingredient)}が主役成分")
    elif normalized_ingredient in support_ingredients:
        reasons.append(f"{ingredient_map.get(normalized_ingredient, normalized_ingredient)}を補助的に配合")

    for sig in product.get("signature_ingredients", []):
        if sig in signature_ingredient_labels:
            reasons.append(signature_ingredient_labels[sig])

    # 肌質との一致
    if skin_oil == "dry" and "dry" in skin_types:
        reasons.append("乾燥肌向き")
    elif skin_oil == "oily" and "oily" in skin_types:
        reasons.append("脂性肌向き")
    elif skin_oil == "mixed" and "mixed" in skin_types:
        reasons.append("混合肌向き")

    # 敏感肌との一致
    if skin_sens == "high":
        if sensitive_ok == "yes":
            reasons.append("敏感肌でも使いやすい設計")
        elif sensitive_ok == "unknown":
            reasons.append("刺激は強すぎない想定")


    for f in product.get("formulation", []):
        if f in formulation_labels:
            reasons.append(formulation_labels[f])
            break

    if not reasons:
        reasons.append("肌状態とカテゴリ条件に合いやすいバランス型")

   

    # technology
    for tech in product.get("technology", []):
        if tech in technology_labels:
                reasons.append(technology_labels[tech])
    # texture
    tex = product.get("texture")
    if tex in texture_labels:
            reasons.append(texture_labels[tex])

    warnings = []
    for c in product.get("contraindications", []):
        if c in contraindications_labels:
            warnings.append(contraindications_labels[c])

    # main_functions追加
    functions = product.get("main_functions", [])
    for f in functions:
        if f not in reasons:
            reasons.append(f)
            if len(reasons) >= 3:
                break

    result = "・".join(reasons[:3])

    if warnings:
        result += f"。注意: {warnings[0]}"

    return result

def build_ai_reason(step, user_data):
    parts = []

    category = step.get("category", "")
    purpose = step.get("purpose", "")
    ingredient_focus = step.get("ingredient_focus", "")
    risk_note = step.get("risk_note", "")
    section = step.get("_section", "")

    oil = user_data.get("oil", "")
    sens = user_data.get("sens", "")
    exp = user_data.get("exp", "")

    if purpose:
        parts.append(f"{purpose}を優先した提案")

    if ingredient_focus:
        parts.append(f"{ingredient_focus}を軸にした設計")

    if section == "morning":
        parts.append("朝は刺激を上げすぎず、扱いやすさと継続性を重視")
    elif section == "night":
        parts.append("夜は補修や集中的なケアを意識した設計")
    elif section == "weekly_care":
        parts.append("毎日ではなく週単位で補助的に取り入れる想定")

    if oil == "oily":
        parts.append("脂性肌を踏まえて重すぎない方向で調整")
    elif oil == "dry":
        parts.append("乾燥しやすさを踏まえて保湿も意識")
    elif oil == "mixed":
        parts.append("混合肌を踏まえてバランス重視で調整")

    if sens == "high":
        parts.append("敏感傾向を考慮して刺激面にも注意")
    if exp == "beginner" and ("レチノール" in ingredient_focus or "レチナール" in ingredient_focus):
        parts.append("レチノール初心者のため使い方は慎重にする前提")

    if risk_note:
        parts.append(f"注意点: {risk_note}")

    if not parts:
        parts.append(f"{category}カテゴリの中で肌状態に合わせやすい候補として提案")

    return "。".join(parts[:5]) + "。"
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

import time

def call_gemini_with_quota_guard(**kwargs):
    max_retries = 3

    for attempt in range(max_retries):
        try:
            return client.models.generate_content(**kwargs)

        except Exception as e:
            error_text = str(e)

            if "503" in error_text or "UNAVAILABLE" in error_text:
                print(f"[RETRY] Gemini 503 error, attempt {attempt+1}")

                if attempt < max_retries - 1:
                    time.sleep(2)  # 少し待って再試行
                    continue

            # それ以外 or リトライ失敗
            raise e

    

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

def build_virtual_product_from_ai_candidate(cand, step):
    """
    AI候補 → DB完全互換に変換（不足補完込み）
    """

    return {
        "name": cand.get("name", ""),
        "brand": cand.get("brand", "unknown"),
        "category": step.get("category", ""),

        "price_ref": cand.get("price", 2000),

        "active_ingredients": cand.get("active_ingredients", []),
        "support_ingredients": cand.get("support_ingredients", []),

        "main_functions": cand.get("main_functions", []),
        "concerns": cand.get("concerns", []),

        "skin_types": cand.get("skin_types", ["normal"]),
        "retinol_level": cand.get("retinol_level", "none"),

        "sensitive_ok": cand.get("sensitive_ok", "yes"),
        "availability_japan": cand.get("availability_japan", ["rakuten", "amazon"]),

        "ingredient_strength": cand.get("ingredient_strength", "medium"),

        "technology": cand.get("technology", []),
        "texture": cand.get("texture", "light"),

        "contraindications": cand.get("contraindications", []),

        "signature_ingredients": cand.get("signature_ingredients", []),

        "_is_virtual": True
    }

def pick_product(category, products):
    candidates = [p for p in products if p.get("category", "") == category]
    if not candidates:
        return None
    return max(candidates, key=lambda x: x.get("score", 0))
# 履歴読み込み
def load_results():
    if not os.path.exists(RESULTS_FILE):
        return []

    try:
        with open(RESULTS_FILE, "r", encoding="utf-8") as f:
            raw = f.read().strip()

        if not raw:
            return []

        data = json.loads(raw)

        if not isinstance(data, list):
            print("===== RESULTS LOAD WARNING =====")
            print("results.json is not a list. reset to []")
            print("================================")
            return []

        cleaned = []
        for item in data:
            if isinstance(item, dict):
                cleaned.append(item)

        return cleaned

    except (json.JSONDecodeError, OSError) as e:
        print("===== RESULTS LOAD ERROR =====")
        print(e)
        print("results.json is broken or unreadable. return []")
        print("================================")
        return []

    except Exception as e:
        print("===== RESULTS UNKNOWN LOAD ERROR =====")
        print(e)
        print("======================================")
        return []

# 履歴保存
def save_results(data):
    if not isinstance(data, list):
        raise ValueError("save_results: data must be a list")

    tmp_path = RESULTS_FILE + ".tmp"

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        os.replace(tmp_path, RESULTS_FILE)
        return True

    except Exception:
        # tmp が残っていたら消す
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise

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

    category = str(step.get("category", "") or "美容液")
    ingredient_focus = str(step.get("ingredient_focus", "") or "")
    purpose = str(step.get("purpose", "") or "肌状態に合わせた基本ケア")

    # product
    if not step.get("product"):
        candidate_name = get_first_concrete_candidate(step)

        if candidate_name:
            step["product"] = candidate_name
            step["product_source"] = "ai_fallback"
        else:
            step["product"] = ""
            step["product_source"] = "missing"
        # image
        if not step.get("image"):
            step["image"] = get_product_image(category)

    # recommend_reason
    if not step.get("recommend_reason"):
        step["recommend_reason"] = build_ai_reason(step, user_data)

    # product_source
    if not step.get("product_source"):
        step["product_source"] = "fallback"


    step = normalize_step_price_fields(step)

    # price_band
    if not step.get("price_band"):
        price = step.get("price", 0)
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

    # 数値系
    for key in ["match_score", "base_score", "improve_score", "priority"]:
        value = step.get(key, 0)
        if isinstance(value, (int, float)):
            step[key] = value
        else:
            step[key] = safe_price(value)

    # score_detail
    if not isinstance(step.get("score_detail"), dict):
        step["score_detail"] = {
            "base": step.get("base_score", 0),
            "improve": step.get("improve_score", 0),
            "final": step.get("match_score", 0)
        }
    else:
        sd = step["score_detail"]
        step["score_detail"] = {
            "base": safe_price(sd.get("base", step.get("base_score", 0))),
            "improve": safe_price(sd.get("improve", step.get("improve_score", 0))),
            "final": safe_price(sd.get("final", step.get("match_score", 0))),
        }

    # product_candidates
    if not isinstance(step.get("product_candidates"), list):
        step["product_candidates"] = []

    # top_candidates
    if not isinstance(step.get("top_candidates"), list):
        step["top_candidates"] = []

    cleaned_top = []
    for c in step.get("top_candidates", [])[:3]:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name", "")).strip()
        if not name:
            continue
        cleaned_top.append({
            "name": name,
            "score": c.get("score", 0) if isinstance(c.get("score", 0), (int, float)) else safe_price(c.get("score", 0)),
            "base_score": c.get("base_score", 0) if isinstance(c.get("base_score", 0), (int, float)) else safe_price(c.get("base_score", 0)),
            "improve_score": c.get("improve_score", 0) if isinstance(c.get("improve_score", 0), (int, float)) else safe_price(c.get("improve_score", 0)),
            "source": str(c.get("source", "") or ""),
            "price_ref": safe_price(c.get("price_ref", 0)),
        })
    step["top_candidates"] = cleaned_top

    # impact_scores / top_impacts
    if not isinstance(step.get("impact_scores"), dict):
        impact = calculate_step_impact(step, None)
        step["impact_scores"] = impact
        step["top_impacts"] = format_top_impacts(impact)
    else:
        if not isinstance(step.get("top_impacts"), list):
            step["top_impacts"] = format_top_impacts(step["impact_scores"])

    # display_role
    step["display_role"] = get_step_display_role(step)

    # 文字列系を最低限整える
    for key in ["category", "role", "purpose", "ingredient_focus", "risk_note", "product", "recommend_reason", "product_source", "frequency", "display_role"]:
        if key in step:
            step[key] = "" if step[key] is None else str(step[key])

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

    # warnings
    if not isinstance(data.get("warnings"), list):
        data["warnings"] = []
    data["warnings"] = build_rule_based_warnings(data, user_data)
    # budget
    data["input_budget"] = safe_price(data.get("input_budget", 0))
    data["total_price"] = safe_price(data.get("total_price", 0))
    data["budget_fit_total"] = safe_price(data.get("budget_fit_total", 0))

    if not isinstance(data.get("budget_fit_plan"), dict):
        data["budget_fit_plan"] = {"morning": [], "night": [], "weekly_care": []}
    else:
        for key in ["morning", "night", "weekly_care"]:
            if not isinstance(data["budget_fit_plan"].get(key), list):
                data["budget_fit_plan"][key] = []

    # 文字列系
    for key in ["record_date", "analysis_date", "skin_summary", "budget_status"]:
        if key not in data or data[key] is None:
            data[key] = ""
        else:
            data[key] = str(data[key])

    return data

# Gemini結果を保存用フォーマットに変換
def normalize_result(raw_data, image_path=""):
    return {
        "record_date": raw_data.get("record_date", datetime.today().strftime("%Y-%m-%d")),
        "analysis_date": raw_data.get("analysis_date", datetime.today().strftime("%Y-%m-%d")),
        "skin_score": raw_data.get("skin_score", 0),
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
        "morning": {
            "steps": [
                {
                    **step,
                    "display_role": get_step_display_role(step),
                    "image": step.get("image", get_product_image(step.get("category", "")))
                }
                for step in raw_data.get("morning", {}).get("steps", [])
            ]
        },
        "night": {
            "steps": [
                {
                    **step,
                    "display_role": get_step_display_role(step),
                    "image": step.get("image", get_product_image(step.get("category", "")))
                }
                for step in raw_data.get("night", {}).get("steps", [])
            ]
        },
        "weekly_care": [
            {
                **step,
                "display_role": get_step_display_role(step),
                "image": step.get("image", get_product_image(step.get("category", "")))
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
        "model": "gemini-2.5-flash",
        "version": "1.0"
    }
    


# 診断結果を履歴に追加
def append_result(raw_data, image_path=""):
    history = load_results()

    if not isinstance(history, list):
        history = []

    normalized = normalize_result(raw_data, image_path=image_path)

    record_id = None
    if isinstance(normalized, dict):
        record_id = normalized.get("id")

    if not record_id:
        record_id = generate_result_id(history)

    record = {
        "id": record_id,
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        **normalized
    }

    history.append(record)
    save_results(history)
    return record

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

    # scores の最低保証
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

    # ルーティンの最低保証
    morning = safe_dict(result.get("morning"))
    night = safe_dict(result.get("night"))

    result["morning"] = {
        "steps": safe_list(morning.get("steps"))
    }
    result["night"] = {
        "steps": safe_list(night.get("steps"))
    }
    result["weekly_care"] = safe_list(result.get("weekly_care"))

    return result
# トップページ
@app.route("/", methods=["GET", "POST"])
def home():
    return render_template("index.html")


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
    "クレンジング":1,
    "洗顔": 2,
    "化粧水": 3,
    "美容液": 4,
    "乳液": 5,
    "クリーム": 6,
    "日焼け止め": 7,
    "パック": 8,
    "ピーリング": 9
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

def limit_serum_steps(data):
    for section in ["morning", "night"]:
        steps = data.get(section, {}).get("steps", [])

        serum_steps = [s for s in steps if s.get("category") == "美容液"]

        # 2個までに制限
        if len(serum_steps) > 2:
            # スコア順で上位2つ残す
            serum_steps_sorted = sorted(
                serum_steps,
                key=lambda x: x.get("match_score", 0),
                reverse=True
            )

            keep = set(id(s) for s in serum_steps_sorted[:2])

            new_steps = []
            for s in steps:
                if s.get("category") != "美容液":
                    new_steps.append(s)
                else:
                    if id(s) in keep:
                        new_steps.append(s)

            data[section]["steps"] = new_steps

    return data

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
    category = step.get("category", "")
    role = step.get("role")
    priority = step.get("priority", 999)

    # 導入美容液は化粧水の前
    if category == "美容液" and role == "booster":
        return (1.5, priority)  # 洗顔(1)と化粧水(2)の間

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

    front_img = Image.open(io.BytesIO(front_file.read())).convert("RGB")
    left_img = Image.open(io.BytesIO(left_file.read())).convert("RGB")
    right_img = Image.open(io.BytesIO(right_file.read())).convert("RGB")

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
    return {
        "type": "object",
        "properties": {
            "record_date": {"type": "string"},
            "analysis_date": {"type": "string"},
            "skin_score": {"type": "integer"},

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
                    "tone_evenness": {"type": "integer"}
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
                    "tone_evenness"
                ]
            },

            "skin_summary": {"type": "string"},

            "morning": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {
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
                                    "items": {"type": "string"}
                                },
                                "selection_reason": {"type": "string"},
                                "estimated_price": {"type": "integer"},
                                "price_band": {"type": "string"}
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
                    }
                },
                "required": ["steps"]
            },

            "night": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {
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
                                    "items": {"type": "string"}
                                },
                                "selection_reason": {"type": "string"},
                                "estimated_price": {"type": "integer"},
                                "price_band": {"type": "string"}
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
                    }
                },
                "required": ["steps"]
            },

            "weekly_care": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string"},
                        "ingredient_focus": {"type": "string"},
                        "frequency": {"type": "string"},
                        "role": {"type": "string"},
                        "purpose": {"type": "string"},
                        "priority": {"type": "integer"},
                        "product_candidates": {
                            "type": "array",
                            "items": {"type": "string"}
                        },
                        "selection_reason": {"type": "string"},
                        "estimated_price": {"type": "integer"},
                        "price_band": {"type": "string"}
                    },
                    "required": [
                        "category",
                        "role",
                        "ingredient_focus",
                        "frequency",
                        "purpose",
                        "priority",
                        "product_candidates"
                    ]
                }
            },

            "warnings": {
                "type": "array",
                "items": {"type": "string"}
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

            "improvement_plan": {
                "type": "object",
                "properties": {
                    "score_projection": {
                        "type": "object",
                        "properties": {
                            "1week": {"type": "integer"},
                            "1month": {"type": "integer"},
                            "3month": {"type": "integer"}
                        },
                        "required": ["1week", "1month", "3month"]
                    },
                    "improvement_steps": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "immediate": {
                        "type": "object",
                        "properties": {
                            "goal": {"type": "string"},
                            "actions": {
                                "type": "array",
                                "items": {"type": "string"}
                            },
                            "key_ingredients": {
                                "type": "array",
                                "items": {"type": "string"}
                            },
                            "note": {"type": "string"}
                        },
                        "required": ["goal", "actions", "key_ingredients", "note"]
                    },
                    "short_term": {
                        "type": "object",
                        "properties": {
                            "goal": {"type": "string"},
                            "actions": {
                                "type": "array",
                                "items": {"type": "string"}
                            },
                            "key_ingredients": {
                                "type": "array",
                                "items": {"type": "string"}
                            },
                            "note": {"type": "string"}
                        },
                        "required": ["goal", "actions", "key_ingredients", "note"]
                    },
                    "long_term": {
                        "type": "object",
                        "properties": {
                            "goal": {"type": "string"},
                            "actions": {
                                "type": "array",
                                "items": {"type": "string"}
                            },
                            "key_ingredients": {
                                "type": "array",
                                "items": {"type": "string"}
                            },
                            "note": {"type": "string"}
                        },
                        "required": ["goal", "actions", "key_ingredients", "note"]
                    }
                },
                "required": [
                    "score_projection",
                    "improvement_steps",
                    "immediate",
                    "short_term",
                    "long_term"
                ]
            }
        },
        "required": [
            "record_date",
            "analysis_date",
            "skin_score",
            "scores",
            "skin_summary",
            "morning",
            "night",
            "weekly_care",
            "warnings",
            "moisture_plan",
            "improvement_plan"
        ]
    }

def build_analysis_prompt(user_data):
    return f"""
あなたは日本の市販スキンケアに詳しい美容アドバイザーです。
肌画像とユーザー情報をもとにスキンケア診断をしてください。

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

【診断方針】
・正面は全体の肌バランス確認に使う
・左右頬は毛穴、赤み、キメ、ニキビ跡の確認に使う
・左右差があれば診断に反映する

【診断ルール】
・洗顔から保湿までトータルでスキンケアを提案する
・朝と夜のルーティンを分ける
・最小限のアイテムで最大効果を優先する
・メイク落としが必要な場合は「クレンジング」を使うこと
・朝の洗浄は基本「洗顔」を使うこと
・クレンジングと洗顔は別カテゴリとして扱うこと
・クレンジングはメイク除去や 선크림 除去目的、洗顔は汗・皮脂・軽い汚れ除去目的として区別すること

【クレンジングと洗顔の使い分け】
・クレンジングはメイクや日焼け止めを落とす目的で使う
・洗顔は汗、皮脂、軽い汚れを落とす目的で使う
・夜にメイクまたは日焼け止め使用が前提ならクレンジングを優先する
・朝は基本的に洗顔を優先する
・必要な場合のみクレンジングと洗顔の両方を提案してよい

【カテゴリ固定ルール】
category は必ず次のいずれかのみを使うこと
- クレンジング
- 洗顔
- 化粧水
- 美容液
- 乳液
- クリーム
- 日焼け止め
- パック
- ピーリング

【role固定ルール】
role は必ず次のいずれかのみを使うこと
- main
- booster

【ingredient_focus固定候補】
ingredient_focus はできるだけ次の語から選ぶこと
- ビタミンC
- ナイアシンアミド
- レチノール
- レチナール
- アゼライン酸
- トラネキサム酸
- PDRN
- ペプチド
- セラミド
- ヒアルロン酸
- CICA
- ドクダミ
- AHA
- BHA
- PHA

【美容液ルール】
・美容液は最大2つまで提案できる
・異なる目的の場合のみ2つ提案する
・同じ目的の美容液は重複させない
・美容液はスキンケアの中で最も重要なアイテムとして優先的に選定する
・ただしブースター美容液は別枠とし、メイン美容液の本数に含めない
・ブースターに該当する場合は role を "booster" とする
・通常の美容液は role を "main" とする
・ブースターは浸透補助、土台強化、バリア補助を目的とする
・メイン美容液は毛穴、美白、赤み、ハリなどの主目的を担う

【安全ルール】
・刺激や成分相性を考慮する
・レチノールなど強い成分は経験レベルを考慮する

【追加ケア】
・パック、ピーリング、酵素洗顔などの週ケアは必要な場合のみ提案する

【商品ルール】
・必要なケア内容、カテゴリ、成分の方向性を優先して出す
・各ステップの product_candidates には、可能であれば 8〜10 個の具体的な商品候補を入れること
・最低でも 8 個以上の候補を入れること
・日本で市販されていて比較的入手しやすい商品を優先すること
・異なるブランドから幅広く候補を出すこと
・同じブランドや同系統の商品ばかりに偏らないこと
・メジャー商品、定番商品、ドラッグストア商品、バラエティショップ商品、韓国スキンケアを適度に混ぜること
・成分、肌質、敏感度、予算に合う候補を優先すること
・DBにない商品名でもよい
・商品候補が多いほどよいが、明らかに条件に合わない商品は入れないこと
・ product_candidates は比較対象の候補一覧として使うため、1個だけで終わらせないこと

【安定性ルール】
・同じ画像と同じ入力条件に対しては、できるだけ同じ診断結果を返すこと
・表現の言い換えを増やさず、毎回同じ基準で判定すること

【候補収集ルール】
・最終的に1つを選ぶ前段階として、まず比較対象を広く集める意識で候補を挙げること
・ product_candidates は最終回答ではなく比較用の候補集合として考えること
・ 成分が近いだけの類似商品ばかりではなく、同じ目的を満たす別ブランドの商品も含めること
・ 特定ブランドに偏らず、できるだけ市場全体を広く見ること

【予算ルール】
ユーザーの予算は月間スキンケア予算として扱う。
美容液: 40%
化粧水: 20%
保湿（乳液・クリーム）: 20%
洗顔: 10%
日焼け止め: 10%

ただし以下の場合は柔軟に調整する。
・肌悩みが強い場合は美容液の割合を増やす
・敏感肌は刺激の少ない商品を優先
・脂性肌は保湿を軽くする
・乾燥肌は保湿割合を増やす

【価格ルール】
・DBにない商品候補でも、日本で一般的な販売価格を想定して estimated_price を入れること
・estimated_price は税込みの参考価格として整数で入れること
・不明な場合でも category と商品候補から大きく外さない現実的な価格を推定すること
・同時に price_band も記載すること
・price_band は次のいずれかのみを使うこと
  - 〜1500円
  - 1501〜3000円
  - 3001〜5000円
  - 5001円以上

【スコア】
肌状態を100点満点で評価する。
以下の項目も100点満点で評価する。
- oil_balance
- redness
- pores
- hydration
- firmness
- acne
- dullness
- barrier
- texture
- tone_evenness

各項目の意味は以下。
- oil_balance: 皮脂バランス
- redness: 赤み
- pores: 毛穴
- hydration: 保湿状態
- firmness: ハリ
- acne: ニキビの出やすさ・炎症状態
- dullness: くすみ
- barrier: バリア機能
- texture: キメの整い
- tone_evenness: 色ムラの少なさ

【候補分散ルール】
・特定ブランドに偏らず、できるだけ市場を広く見ること
・同じ成分軸でも、別ブランドの代表商品を複数含めること
・ product_candidates は最終回答ではなく比較対象の候補集合として考えること

【保湿レイヤールール】
・乳液とクリームは肌状態に応じて必要数を判断すること
・軽い乾燥なら乳液またはクリームのどちらか一方でもよい
・乾燥が強い場合、またはバリア機能低下が目立つ場合は乳液＋クリームの両方を提案してよい
・脂性肌や毛穴詰まり傾向が強い場合は、重すぎる保湿を避けること
・moisture_plan に以下を必ず入れること
  - moisture_level: low / medium / high
  - need_emulsion: true / false
  - need_cream: true / false
  - need_double_moisture: true / false
  - reason: 判定理由

【提案強化ルール】
・各ステップの product_candidates には、具体的な実在商品名を5〜8個入れること
・product_candidates は空配列 [] にしないこと
・「おすすめ美容液」「美容液候補」「レチノール系候補」などの曖昧な商品名は禁止
・product_candidates には商品カテゴリ名ではなく、ブランド名を含む具体的な商品名だけを入れること
・DB商品とAI候補をあとで比較するため、この段階では最終決定ではなく候補収集まで行うこと
・候補はドラッグストア、韓国コスメ、通販定番、バラエティショップ系をなるべく混ぜること
・同じブランドばかりに偏らせないこと
・単一の成分提案ではなく組み合わせで設計すること
・朝と夜で役割を分けること
・刺激が強くなる組み合わせは warnings に必ず記載すること
・成分ベースで理由説明すること
・現在スコアから改善予測を出すこと
・改善の順番を improvement_steps に入れること
・各ステップごとに selection_reason を必ず記載すること
・selection_reason では以下を自然な日本語で簡潔に説明すること
  1. なぜそのカテゴリ・商品候補なのか
  2. どの肌悩みに対応しているか
  3. どの成分が軸なのか
  4. 朝夜での役割
  5. 注意点があれば一言入れること
・曖昧な表現は禁止
・「なんとなく良い」「人気だから」などは禁止

【最重要ルール】
必ずJSONのみで返してください。
JSON以外の文章、説明、前置き、後書き、箇条書き、markdown記法は禁止です。
必ずすべて日本語で出力すること。
必ず実在する商品名を出すこと。
英語は一切使用しないこと。
JSONの中身もすべて日本語で書くこと。
"""


def analyze_skin_with_gemini(user_data, front_img, left_img, right_img):

    # ===== DEV_MODE_START =====
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
    # ===== DEV_MODE_END =====

    schema = get_analysis_schema()
    prompt = build_analysis_prompt(user_data)

    response = call_gemini_with_retry(
        client,
        "gemini-2.5-flash",
        contents=[prompt, front_img, left_img, right_img],
        config=types.GenerateContentConfig(
            temperature=0,
            top_p=1,
            response_mime_type="application/json",
            response_schema=schema
        )
    )

    raw_text = response.text.strip()
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
        return json.loads(raw_text)
    except json.JSONDecodeError as e:
        print("===== GEMINI JSON ERROR =====")
        print(e)
        print("===== RAW TEXT =====")
        print(raw_text)
        print("====================")

        raise ValueError("AIの診断結果JSONが壊れています。もう一度診断してください。")

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
・10〜15個の候補を返す
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
        model="gemini-2.5-flash",
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

    step["product"] = product.get("name", category)
    step["price"] = safe_price(product.get("price_ref", 0))

    image_file = product.get("image", "")
    if image_file:
        step["image"] = f"/static/images/products/{image_file}"
    else:
        step["image"] = get_product_image(category)

    step["affiliate_provider"] = product.get("affiliate_provider", "")
    step["affiliate_item_id"] = product.get("affiliate_item_id", "")

    step["match_score"] = product.get("_score") or 0
    step["base_score"] = product.get("_base_score") or 0
    step["improve_score"] = product.get("_improve_score") or 0
    step["recommend_reason"] = build_recommend_reason(product, step, user_data)
    step["product_source"] = "db"

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

    step["image"] = image_path if image_path else get_product_image(category)
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
    step["image"] = get_product_image(category)

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

def step_sort_key(step):
    category = step.get("category", "")
    role = step.get("role")
    priority = step.get("priority", 999)

    base_order = CATEGORY_ORDER.get(category, 99)

    if role == "booster":
        base_order += 0.5

    return (base_order, priority)

def sort_steps(data):
    if "morning" in data and "steps" in data["morning"]:
        data["morning"]["steps"].sort(key=step_sort_key)

    if "night" in data and "steps" in data["night"]:
        data["night"]["steps"].sort(key=step_sort_key)

    if "weekly_care" in data and isinstance(data["weekly_care"], list):
        data["weekly_care"].sort(key=step_sort_key)

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
    
    if request.method == "POST":
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
                    "lab.html",
                    error_message=str(e),
                    remaining_free_count=get_remaining_free_count(client_ip),
                    DISABLE_USAGE_LIMIT=DISABLE_USAGE_LIMIT
                )

            global_used = get_global_usage_count()
            global_remaining = GLOBAL_MONTHLY_LIMIT - global_used
            # =========================
            # ③ AI分析
            # =========================
            try:
                data = analyze_skin_with_gemini(user_data, front_img, left_img, right_img)
            except Exception as e:
                print("===== LAB ERROR =====")
                print(e)
                traceback.print_exc()
                print("=====================")

                error_text = str(e)

                if "503" in error_text or "UNAVAILABLE" in error_text:
                    message = "現在AI診断が混み合っています。少し時間をおいて再度お試しください。"

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
            data = apply_moisture_plan(data)

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
            budget_value = parse_budget(user_data.get("budget", ""))
            debug_log("BUDGET VALUE", budget_value)

            data = assign_products_to_all_steps(data, products, user_data, budget_value)
            affiliate_ai_db = load_affiliate_links_ai()
            data = attach_affiliate_links_to_all_steps(data, affiliate_ai_db)
            
            debug_log("AFTER ASSIGN PRODUCTS")
            debug_step_summary("morning assigned", data.get("morning", {}).get("steps", []))
            debug_step_summary("night assigned", data.get("night", {}).get("steps", []))
            debug_step_summary("weekly assigned", data.get("weekly_care", []))

            # =========================
            # ⑧ 選定後の調整
            # =========================
            data = limit_serum_steps(data)
            data = sort_steps(data)

            # =========================
            # ⑨ 最終整形
            # =========================
            data = finalize_result_data(data, user_data)

            debug_log("AFTER FINALIZE")
            debug_step_summary("morning finalized", data.get("morning", {}).get("steps", []))
            debug_step_summary("night finalized", data.get("night", {}).get("steps", []))
            debug_step_summary("weekly finalized", data.get("weekly_care", []))

            # =========================
            # ⑩ 予算情報
            # =========================
            data = finalize_budget_info(data, budget_value)

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

            # 無料回数の消費は、診断自体がここまで完了したら進める
            try:
                increment_free_usage(client_ip)
            except Exception as e:
                print("===== FREE USAGE SAVE ERROR =====")
                print(e)
                print("=================================")

            try:
                increment_free_usage(client_ip)
                increment_global_usage()
            except Exception as e:
                print("===== USAGE SAVE ERROR =====")
                print(e)

            saved_record = None
            try:
                saved_record = append_result(data)
                if isinstance(saved_record, dict) and saved_record.get("id"):
                    data["id"] = saved_record["id"]
            except Exception as e:
                print("===== RESULT SAVE ERROR =====")
                print(e)
                traceback.print_exc()
                print("=============================")
                # 保存に失敗しても結果表示は止めない
            PRODUCT_LOG_FILE = "product_log.json"

            def log_displayed_products(data):
                logs = []

                if os.path.exists(PRODUCT_LOG_FILE):
                    try:
                        with open(PRODUCT_LOG_FILE, "r", encoding="utf-8") as f:
                            logs = json.load(f)
                            if not isinstance(logs, list):
                                logs = []
                    except Exception:
                        logs = []

                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                def extract(section_name, steps):
                    for step in steps:
                        if not isinstance(step, dict):
                            continue

                        logs.append({
                            "time": now,
                            "section": section_name,
                            "category": step.get("category", ""),
                            "product": step.get("product", "")
                        })

                extract("morning", data.get("morning", {}).get("steps", []))
                extract("night", data.get("night", {}).get("steps", []))
                extract("weekly", data.get("weekly_care", []))

                with open(PRODUCT_LOG_FILE, "w", encoding="utf-8") as f:
                    json.dump(logs, f, ensure_ascii=False, indent=2)
            # =========================
            # ⑫ 表示
            # =========================
            html = render_template("result.html", data=data)

            if is_ajax:
                return jsonify({
                    "success": True,
                    "html": html
                })

            return html

        except ValueError as e:
            print("\n===== LAB VALUE ERROR =====")
            print(str(e))
            print("===========================\n")
            return f"<h1>{str(e)}</h1>"

        except Exception as e:
            print("\n===== LAB ERROR =====")
            print("ERROR:", e)
            traceback.print_exc()
            print("=====================\n")
            return f"<pre>{traceback.format_exc()}</pre>"

    client_ip = get_client_ip()
    remaining_free_count = get_remaining_free_count(client_ip)
    return render_template("lab.html", remaining_free_count=remaining_free_count, DISABLE_USAGE_LIMIT=DISABLE_USAGE_LIMIT)

@app.route("/click")
def product_click():
    source = request.args.get("source", "unknown")
    product = request.args.get("product", "")
    category = request.args.get("category", "")
    url = request.args.get("url", "")

    log_product_click(source, product, category)

    if not url:
        return "リンクがありません", 400

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

            view_item = prepare_result_for_view(item)

            prepared.append({
                "id": view_item.get("id", ""),
                "record_date": view_item.get("record_date", ""),
                "analysis_date": view_item.get("analysis_date", ""),
                "saved_at": view_item.get("saved_at", ""),
                "skin_score": view_item.get("skin_score", 0),
                "skin_summary": view_item.get("skin_summary", ""),
                "scores": view_item.get("scores", {}),
            })

        prepared = prepared[::-1]

        labels = []
        skin_scores = []

        for item in prepared:
            labels.append(item.get("record_date") or item.get("saved_at") or "")
            skin_scores.append(safe_int(item.get("skin_score", 0)))

        return render_template(
            "history.html",
            history=prepared,
            labels=labels,
            scores=skin_scores
        )

    except Exception as e:
        print("===== HISTORY ROUTE ERROR =====")
        print(e)
        traceback.print_exc()
        print("===============================")

        # 履歴が壊れていても一覧ページ自体は出す
        return render_template(
            "history.html",
            history=[],
            labels=[],
            scores=[]
        )

@app.route("/result/<result_id>")
def result_detail(result_id):
    try:
        history_data = load_results()

        for item in history_data:
            if str(item.get("id")) == str(result_id):
                data = prepare_result_for_view(item)
                data["is_premium"] = is_premium_user()
                return render_template("result.html", data=data)

        return "結果が見つかりません", 404

    except Exception as e:
        print(e)
        return "エラーが発生しました", 500

@app.route("/history/<result_id>")
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
# ==========================================
# Flaskサーバー起動
# ==========================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)