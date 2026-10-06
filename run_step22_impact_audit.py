"""Step22-3: score_rakuten_item()の長音記号修正前後の影響監査(read-only)。
修正前(直前commit eb3b9ccのapp.py)をサブプロセスでimportし、修正後(現行app.py)
と同じ入力セットでscore_rakuten_item()を比較する。
"""
import json
import subprocess

import app


def load_accepted_62():
    with open("/tmp/step20_accepted_62.json") as f:
        rows = json.load(f)
    cases = []
    for product_id, brand, name, rakuten_title, shop_name in rows:
        cases.append({
            "id": f"accepted_{product_id}",
            "name": name,
            "brand": brand,
            "title": rakuten_title,
            "shop": shop_name,
            "category": "",  # カテゴリはscore_rakuten_item内で未使用の分岐もあるため後段で補完
        })
    return cases


# product_idごとの実カテゴリ(product_masterのcategory列、Step18〜21のログから既知)
CATEGORY_BY_PRODUCT_ID = {}


def fetch_categories():
    import os
    import psycopg2
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("SELECT product_id, category FROM product_master")
    for pid, cat in cur.fetchall():
        CATEGORY_BY_PRODUCT_ID[pid] = cat
    conn.close()


KNOWN_SAMPLES = [
    # 59 ドクターサニー: 真候補 + bundle variants
    {"id": "59_true", "name": "ドクターサニー AHAクリアソープ", "brand": "ドクターサニー",
     "title": "フルーツ酸(AHA)5％配合AHAクリアソープ(ピーリング石鹸)", "shop": "株式会社ドクターサニー",
     "category": "洗顔", "price": 1980},
    {"id": "59_bundle_3", "name": "ドクターサニー AHAクリアソープ", "brand": "ドクターサニー",
     "title": "AHAクリアソープ(ピーリング石鹸)3個セット【送料無料】【あす楽対応】", "shop": "株式会社ドクターサニー",
     "category": "洗顔", "price": 4950},
    {"id": "59_bundle_2", "name": "ドクターサニー AHAクリアソープ", "brand": "ドクターサニー",
     "title": "AHAクリアソープ(ピーリング石鹸)2個セット【送料無料】【あす楽対応】", "shop": "株式会社ドクターサニー",
     "category": "洗顔", "price": 3520},
    {"id": "59_bundle_7", "name": "ドクターサニー AHAクリアソープ", "brand": "ドクターサニー",
     "title": "AHAクリアソープ(ピーリング石鹸)7個セット【送料無料】【あす楽対応】", "shop": "株式会社ドクターサニー",
     "category": "洗顔", "price": 9900},
    {"id": "59_hajimete_set", "name": "ドクターサニー AHAクリアソープ", "brand": "ドクターサニー",
     "title": "ピーリング石鹸はじめてセットフルーツ酸5％配合AHAクリアソープ + 泡立てネット + 縦置きソープケース【送料無料】【あす楽対応】",
     "shop": "株式会社ドクターサニー", "category": "洗顔", "price": 2632},

    # 28 WHITH WHITE: 真候補 + 誤候補
    {"id": "28_true", "name": "WHITH WHITE 薬用美白乳液", "brand": "WHITH WHITE",
     "title": "【4日 20時〜】30%OFFクーポン有！美白 薬用 乳液 フィス ホワイト「 しみ くすみ を ケア 予防 」「プラセンタ + コラーゲン 配合 」で肌のキメを整える 「美容液 や 化粧水 と セット使い でさらに 肌に透明感を与える 」150mlWHITH WHITE",
     "shop": "イルミルド公式ショップ", "category": "乳液", "price": 2500},
    {"id": "28_wrong_serum", "name": "WHITH WHITE 薬用美白乳液", "brand": "WHITH WHITE",
     "title": "【4日 20時〜】30%OFFクーポン有！美白 薬用 美容液フィスホワイト「 しみ くすみ をケア 予防 」「 プラセンタ コラーゲン ヒアルロン酸配合」肌のキメを整える「化粧水や乳液とセット使い でさらに 肌に透明感を与える 」 50ml",
     "shop": "イルミルド公式ショップ", "category": "乳液", "price": 2500},
    {"id": "28_wrong_hadalabo", "name": "WHITH WHITE 薬用美白乳液", "brand": "WHITH WHITE",
     "title": "肌ラボ 白潤 薬用美白乳液(140ml)【ハダラボ】[トラネキサム酸 シミ そばかす 無着色 無香料]",
     "shop": "ロート製薬公式", "category": "乳液", "price": 1200},

    # 48 JUMISO: 全て誤候補(別ブランド)
    {"id": "48_wrong_skinfood", "name": "JUMISO ナイアシンアミド20セラム", "brand": "JUMISO",
     "title": "SKINFOOD レモングラスナイアシンアミド20セラム(韓国コスメ)／スキンフード（SKINFOOD）",
     "shop": "丸井(マルイ)楽天市場店", "category": "美容液", "price": 3080},
    {"id": "48_wrong_dermafactory", "name": "JUMISO ナイアシンアミド20セラム", "brand": "JUMISO",
     "title": "2コ【ダーマファクトリー】ナイアシンアミド20%セラム 各30ml 美容液 韓国コスメDermafactory 【海外通販】",
     "shop": "BALLA", "category": "美容液", "price": 1635},

    # 63 ツボクサレディ: 全て誤候補(別ブランド)
    {"id": "63_wrong_komoace", "name": "ツボクサレディ マイルドクレンジングクリーム", "brand": "ツボクサレディ",
     "title": "【クレンジングクリーム】コモエース マイルドクレンジングクリーム｜130g｜ベスコス受賞 無香料 保湿 乾燥肌 敏感肌",
     "shop": "コモエースストア 楽天市場店", "category": "クレンジング", "price": 3960},

    # 49 クレアラシル: 既に本反映済みの候補(regressionチェック用)
    {"id": "49_resolved", "name": "クレアラシル 薬用洗顔クリーム マイルドタイプ", "brand": "クレアラシル",
     "title": "医薬部外品 クレアラシル ニキビ対策 薬用 洗顔クリーム マイルドタイプ 120g 東京南倉庫",
     "shop": "トレンドオフィス 楽天市場店", "category": "洗顔", "price": 1020},
]


def main():
    fetch_categories()
    cases = load_accepted_62()
    for c in cases:
        pid = int(c["id"].split("_")[1])
        c["category"] = CATEGORY_BY_PRODUCT_ID.get(pid, "")
        c["price"] = 1000
    cases.extend(KNOWN_SAMPLES)

    # 修正後(現行app.py)でスコア計算
    new_scores = {}
    for c in cases:
        score = app.score_rakuten_item(
            {
                "itemName": c["title"], "itemPrice": c.get("price", 1000),
                "shopName": c.get("shop", ""), "mediumImageUrls": [{"imageUrl": "x"}],
                "reviewCount": c.get("reviewCount", 0),
            },
            product_name=c["name"], brand=c.get("brand", ""), category=c.get("category", "")
        )
        new_scores[c["id"]] = score

    # 修正前(直前commitのapp.py)でスコア計算(サブプロセス)
    proc = subprocess.run(
        ["/Users/katsuya/rumilog/.venv/bin/python3", "/tmp/step22_old_app/score_old.py"],
        input=json.dumps(cases), capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        print("[OLD SCORE SUBPROCESS ERROR]")
        print(proc.stderr[-3000:])
        return
    old_results = json.loads(proc.stdout.strip().splitlines()[-1])
    old_scores = {r["id"]: r["score"] for r in old_results}

    print("=" * 70)
    print(f"総ケース数: {len(cases)} (既存accepted62件 + 既知サンプル{len(KNOWN_SAMPLES)}件)")
    print("=" * 70)

    changed = []
    accept_became_reject = []
    reject_became_accept = []
    THRESHOLD = 20

    for c in cases:
        cid = c["id"]
        old_s = old_scores.get(cid)
        new_s = new_scores.get(cid)
        if old_s != new_s:
            changed.append((cid, old_s, new_s))
        old_pass = (old_s is not None and old_s >= THRESHOLD)
        new_pass = (new_s is not None and new_s >= THRESHOLD)
        if old_pass and not new_pass:
            accept_became_reject.append((cid, old_s, new_s))
        if not old_pass and new_pass:
            reject_became_accept.append((cid, old_s, new_s))

    print(f"\nscoreが変化した候補数: {len(changed)}")
    for cid, o, n in changed:
        print(f"  {cid}: {o} -> {n}")

    print(f"\naccepted(score>=20)->reject化した候補: {len(accept_became_reject)} (期待: 0)")
    for cid, o, n in accept_became_reject:
        print(f"  {cid}: {o} -> {n}")

    print(f"\nreject->新規accept化した候補: {len(reject_became_accept)}")
    for cid, o, n in reject_became_accept:
        print(f"  {cid}: {o} -> {n}")

    unexpected_new_accepts = [c for c in reject_became_accept if not c[0].startswith("59_true")]
    print(f"\n59以外で新規acceptされた候補: {len(unexpected_new_accepts)} (期待: 0)")
    for cid, o, n in unexpected_new_accepts:
        print(f"  {cid}: {o} -> {n}")


if __name__ == "__main__":
    main()
