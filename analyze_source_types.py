"""Phase 3品質監査: citationのtitleを元に、公式メーカー/ブランド・正規販売店・
その他の比率を集計する(全38商品成功分が対象)。"""

import os
import re
import sys

import psycopg2

import app  # noqa: F401

BATCH_ID = sys.argv[1] if len(sys.argv) > 1 else "pilot-1791040996"

# 既知の正規販売店・プラットフォームドメイン(楽天/Amazon等、既存コードベースの
# availability_japan語彙とも整合する一般的なECモール・バラエティストア)。
RETAILER_DOMAINS = {
    "rakuten.co.jp", "amazon.co.jp", "yahoo.co.jp", "qoo10.jp", "aeonretail.com",
    "hands.net", "tsuruha.co.jp", "matsukiyococokara-online.com", "loft.co.jp",
    "0101.co.jp", "askul.co.jp", "plazastyle.com", "cosmekitchen-webstore.jp",
    "oliveyoung.com", "eprice.co.jp", "premier-factory.co.jp", "sakuraya-ecshop.com",
    "biccamera.com", "buyma.com", "buymejapan.com", "daimaru-matsuzakaya.jp",
    "greenbeans.com", "hmv.co.jp", "iherb.com", "itoyokado.co.jp", "mistore.jp",
    "monochoice.jp", "netdeoroshi.com", "odakyu-dept.co.jp", "seims.co.jp",
    "stores.jp", "sundrug-online.com", "tomods-ap.com", "zozo.jp",
}
# 手動で確認した正規メーカー/ブランド公式ドメイン(ブランド名が英語表記でなく
# カタカナ/日本語のため、単純な文字列一致では自動検出できなかったもの)。
OFFICIAL_DOMAINS = {
    "fancl.jp", "fancl.co.jp", "orbis.co.jp", "shuuemura.jp", "laroche-posay.jp",
    "kao-kirei.com", "kose.co.jp", "rohto.com", "attenir.co.jp", "ci-labo.com",
    "v-labo-doctork.jp", "doctork.jp", "kanebo-cosmetics.jp", "aurelie.tokyo",
}
# 情報・レビュー系(メーカー・販売店いずれでもない)
OTHER_INFO_DOMAINS = {
    "cosme.com", "cosme.net", "ameblo.jp", "note.com", "lemon8-app.com", "poink.eu",
    "youtube.com", "prtimes.jp", "fashionsnap.com", "biyougeka.com", "hpplus.jp",
    "whatsinmyjar.com", "make-up-solution.com", "dga.jp", "sirok.jp", "o-l-y.com",
    "coreelle.jp", "onecosme.jp", "arasalife.com", "atpress.ne.jp", "be-story.jp",
    "cosmebi.jp", "cosmetis.com", "create-sd.co.jp", "fc2.com", "hancosme.jp",
    "hatenablog.com", "hugmug.jp", "humpty-dumpty.jp", "inkeedecoder.com",
    "ishampoo.jp", "kakaku.com", "li1l.tokyo", "lipscosme.com",
    "purebeautypicks-lab.com", "sappi-blog.jp", "skincare-note.com",
    "sparkleskinkorea.com", "sunsmarche.jp", "tantaka.co.jp", "thetruescents.com",
    "tvert.jp", "wwdjapan.com",
}
# 製品と無関係と見られる検索ノイズ(鉄道・航空会社等、citationとしては
# 使われたが実際の成分claimの根拠には使われていないと見られるもの)。
IRRELEVANT_NOISE_DOMAINS = {"jal.co.jp", "jreast.co.jp", "muji.com", "purecera.com", "maihada.jp"}


def classify(title, brand):
    if not title:
        return "other"
    t = title.lower()
    if t in OFFICIAL_DOMAINS:
        return "official"
    if t in RETAILER_DOMAINS:
        return "retailer"
    if t in OTHER_INFO_DOMAINS:
        return "other"
    if t in IRRELEVANT_NOISE_DOMAINS:
        return "irrelevant_noise"
    # ブランド名のローマ字/カナ表記の一部がドメインに含まれるか(簡易判定)。
    brand_token = re.sub(r"[^a-z0-9]", "", brand.lower())
    if brand_token and brand_token in re.sub(r"[^a-z0-9]", "", t):
        return "official"
    return "unclassified"


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("""
        SELECT brand, stage1_citations FROM product_collection_staging
        WHERE batch_id = %s AND stage1_status = 'ok'
    """, (BATCH_ID,))
    rows = cur.fetchall()
    conn.close()

    counts = {"official": 0, "retailer": 0, "other": 0, "unclassified": 0, "irrelevant_noise": 0}
    unclassified_titles = set()
    for brand, citations in rows:
        for c in (citations or []):
            category = classify(c.get("title", ""), brand)
            counts[category] += 1
            if category == "unclassified":
                unclassified_titles.add(c.get("title", ""))

    total = sum(counts.values())
    print(f"source_type集計(citation総数={total}):")
    for k, v in counts.items():
        print(f"  {k}: {v} ({v/total*100:.1f}%)")
    print(f"\n未分類ドメイン一覧(要目視確認): {sorted(unclassified_titles)}")


if __name__ == "__main__":
    main()
