"""Step21-4: 修正後のis_same_verified_rakuten_product()(app.py本体)を、
既存62件のaccepted + 既知のreject候補サンプルに対して再シミュレーション(read-only)。
"""
import json

import app

with open("/tmp/step20_accepted_62.json") as f:
    ACCEPTED_62 = json.load(f)


def main():
    print("[1] 既存62件(accepted)への回帰確認")
    regressions = []
    for product_id, brand, name, rakuten_title, shop_name in ACCEPTED_62:
        ok = app.is_same_verified_rakuten_product(name, rakuten_title, brand, shop_name)
        if not ok:
            regressions.append((product_id, brand, name, rakuten_title))
    print(f"  accepted->reject件数: {len(regressions)} (期待: 0)")
    for r in regressions:
        print(f"    {r}")

    print("\n[2] 既知reject候補サンプルでの意図しないaccept確認(59以外)")
    other_known_rejects = [
        (48, "JUMISO", "JUMISO ナイアシンアミド20セラム", "SKINFOOD レモングラスナイアシンアミド20セラム(韓国コスメ)／スキンフード（SKINFOOD）", "丸井(マルイ)楽天市場店"),
        (48, "JUMISO", "JUMISO ナイアシンアミド20セラム", "2コ【ダーマファクトリー】ナイアシンアミド20%セラム 各30ml 美容液 韓国コスメDermafactory 【海外通販】", "BALLA"),
        (63, "ツボクサレディ", "ツボクサレディ マイルドクレンジングクリーム", "【クレンジングクリーム】コモエース マイルドクレンジングクリーム｜130g｜ベスコス受賞", "コモエースストア 楽天市場店"),
        (28, "WHITH WHITE", "WHITH WHITE 薬用美白乳液", "肌ラボ 白潤 薬用美白乳液(140ml)【ハダラボ】[トラネキサム酸 シミ そばかす 無着色 無香料]", "ロート製薬公式"),
        (28, "WHITH WHITE", "WHITH WHITE 薬用美白乳液", "【4日 20時〜】30%OFFクーポン有！美白 薬用 美容液フィスホワイト「 しみ くすみ をケア 予防 」「 プラセンタ コラーゲン ヒアルロン酸配合」肌のキメを整える「化粧水や乳液とセット使い でさらに 肌に透明感を与える 」 50ml", "イルミルド公式ショップ"),
    ]
    unintended = []
    for product_id, brand, name, title, shop in other_known_rejects:
        ok = app.is_same_verified_rakuten_product(name, title, brand, shop)
        print(f"  product_id={product_id} accept={ok} title={title[:50]}")
        if ok:
            unintended.append((product_id, title))
    print(f"\n  意図しないaccept件数: {len(unintended)} (期待: 0)")

    print("\n[3] product_id=59 真候補の最終確認")
    ok59 = app.is_same_verified_rakuten_product(
        "ドクターサニー AHAクリアソープ",
        "フルーツ酸(AHA)5％配合AHAクリアソープ(ピーリング石鹸)",
        "ドクターサニー",
        "株式会社ドクターサニー",
    )
    print(f"  59 true candidate accept={ok59} (期待: True)")


if __name__ == "__main__":
    main()
