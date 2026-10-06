"""Step28-4: 洗顔カテゴリ「バー」判定修正の全体影響監査(read-only)。
現在のCLEANSER_KEYWORDS(新)と、4語追加前(旧)のリストを同一プロセス内で
切り替えてis_wrong_cleanser_candidate()の結果を比較する。
"""
import app

OLD_CLEANSER_KEYWORDS = [
    "洗顔", "フォーム", "フォーマー", "クレンザー", "ウォッシュ", "ジェルウォッシュ",
    "泡", "ホイップ", "石鹸", "せっけん", "サボン", "ソープ", "soap", "cleanser",
    "cleansing foam", "face wash", "facial wash",
]
NEW_CLEANSER_KEYWORDS = list(app.CLEANSER_KEYWORDS)


def all_facewash_candidates():
    products = app.load_products()
    pm_candidates = app.query_product_master_candidates("洗顔", limit=100)
    verified = app.load_verified_products_cache()

    names = []
    for src, items in (("db_products", products), ("product_master", pm_candidates), ("verified_cache", verified)):
        for p in items:
            if not isinstance(p, dict):
                continue
            cat = app.normalize_candidate_category(p.get("category", ""), fallback=p.get("category", ""))
            if cat != "洗顔":
                continue
            name = p.get("name") or p.get("product") or ""
            names.append((src, f"{p.get('brand', '')}/{name}".strip("/"), name))
    return names


def main():
    step = {"category": "洗顔"}
    candidates = all_facewash_candidates()
    print(f"洗顔カテゴリ候補総数(db_products+product_master+verified_cache): {len(candidates)}\n")

    new_accepts = []
    new_rejects = []
    unchanged_count = 0

    for src, full_name, name in candidates:
        app.CLEANSER_KEYWORDS = OLD_CLEANSER_KEYWORDS
        old_wrong = app.is_wrong_cleanser_candidate({"name": name}, step)
        app.CLEANSER_KEYWORDS = NEW_CLEANSER_KEYWORDS
        new_wrong = app.is_wrong_cleanser_candidate({"name": name}, step)

        if old_wrong == new_wrong:
            unchanged_count += 1
            continue

        if old_wrong and not new_wrong:
            new_accepts.append((src, full_name))
        elif not old_wrong and new_wrong:
            new_rejects.append((src, full_name))

    print(f"新規accept数(旧:hard-exclude -> 新:通過): {len(new_accepts)}")
    for src, name in new_accepts:
        print(f"  [{src}] {name}")

    print(f"\n新規reject数(旧:通過 -> 新:hard-exclude): {len(new_rejects)}")
    for src, name in new_rejects:
        print(f"  [{src}] {name}")

    print(f"\n不変: {unchanged_count}件")

    # 意図しないaccept確認: 新規acceptの中に「サンソリット」以外で明らかに
    # 洗顔と無関係な商品が含まれていないか目視確認用に全件出す(上記new_acceptsで既出)
    unexpected = [n for src, n in new_accepts if "サンソリット" not in n]
    print(f"\n意図しないaccept(サンソリット以外の新規accept): {len(unexpected)}")
    for n in unexpected:
        print(f"  {n}")


if __name__ == "__main__":
    main()
