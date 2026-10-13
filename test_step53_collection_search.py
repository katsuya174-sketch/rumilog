"""Step53: Product Master収集時の楽天検索(取得範囲の拡大・複数検索語の併用)のテスト。
実楽天APIには接続しない(_rakuten_api_getを偽の応答に差し替える)。診断時の検索が変わらないことも確認する。
"""

import contextlib
import io
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402


def _items(prefix, n, title="ファンケル ビタミンD 30日分 30粒"):
    return [{"itemCode": f"{prefix}:{i}", "itemName": title, "itemPrice": 400, "itemUrl": f"https://item.rakuten.co.jp/{prefix}/{i}/",
             "affiliateUrl": "", "shopName": "店", "reviewCount": 0, "reviewAverage": 0,
             "mediumImageUrls": [{"imageUrl": "https://img.example/x.jpg"}]} for i in range(n)]


class FakeRakuten:
    """検索語ごとの総ヒット件数・結果を返す偽の楽天検索。呼び出しを記録する。"""

    def __init__(self, by_keyword):
        self.by_keyword, self.calls = by_keyword, []

    def __call__(self, kind, endpoint, params=None, **kwargs):
        self.calls.append(dict(params))
        all_items = self.by_keyword.get(params["keyword"], [])
        hits, page = params.get("hits", 30), params.get("page", 1)
        res = MagicMock(status_code=200, text="")
        res.json.return_value = {"count": len(all_items), "Items": all_items[(page - 1) * hits: page * hits]}
        return res


def run(fake, product_name, brand, category="サプリメント", jan="", collection=True):
    app._rakuten_candidates_cache.clear()
    tokens = [app._PM_COLLECTION_MODE.set(collection), app._PM_COLLECTION_JAN.set(jan)]
    try:
        with patch.object(app, "_rakuten_api_get", side_effect=fake), \
                patch.object(app, "wait_for_rakuten_rate_limit", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()):
            return app.fetch_rakuten_candidates(product_name=product_name, category=category, brand=brand)
    finally:
        app._PM_COLLECTION_JAN.reset(tokens[1])
        app._PM_COLLECTION_MODE.reset(tokens[0])
        app._rakuten_candidates_cache.clear()


class KeywordTests(unittest.TestCase):

    def test_brand_plus_name_model_and_jan(self):
        self.assertEqual(app.collection_search_keywords("ビタミンD", "ファンケル", "サプリメント", "4908049491925"),
                         ["ファンケル ビタミンD", "4908049491925"])
        # 美容機器はJANでは検索しない(実測で採用候補が0件だったため)
        self.assertEqual(app.collection_search_keywords("リフトケア美顔器 バイタリフト RF EH-SR85", "Panasonic（パナソニック）", "美容機器",
                                                        "4549980667286"),
                         ["Panasonic リフトケア美顔器 バイタリフト RF EH SR85", "Panasonic EH SR85"])
        # 商品名にブランドが含まれていれば重ねない
        self.assertEqual(app.collection_search_keywords("ネイチャーメイド スーパーフィッシュオイル", "ネイチャーメイド（大塚製薬）", "サプリメント"),
                         ["ネイチャーメイド スーパーフィッシュオイル"])


class PagingTests(unittest.TestCase):

    def pages(self, total):
        fake = FakeRakuten({"ファンケル ビタミンD": _items("s", total)})
        result = run(fake, "ビタミンD", "ファンケル")
        return [c["page"] for c in fake.calls], [c["hits"] for c in fake.calls], len(result)

    def test_only_needed_pages_up_to_three(self):
        self.assertEqual(self.pages(20), ([1], [30], 20))            # 少なければ1ページだけ
        self.assertEqual(self.pages(75), ([1, 2, 3], [30] * 3, 75))
        self.assertEqual(self.pages(200), ([1, 2, 3], [30] * 3, 90))  # 最大3ページ
        self.assertEqual(self.pages(0)[0][:1], [1])

    def test_missing_count_uses_received_items(self):
        fake = FakeRakuten({"ファンケル ビタミンD": _items("s", 5)})
        original = fake.__call__

        def no_count(kind, endpoint, params=None, **kw):
            res = original(kind, endpoint, params=params)
            data = res.json.return_value
            data.pop("count")
            return res
        result = run(no_count, "ビタミンD", "ファンケル")
        self.assertEqual(len(result), 5)


class MultiKeywordTests(unittest.TestCase):

    def test_does_not_stop_at_first_keyword_and_dedupes(self):
        jan = "4908049491925"
        fake = FakeRakuten({"ファンケル ビタミンD": _items("a", 3), jan: _items("a", 2) + _items("b", 2)})
        result = run(fake, "ビタミンD", "ファンケル", jan=jan)
        self.assertEqual([c["keyword"] for c in fake.calls], ["ファンケル ビタミンD", jan])
        self.assertEqual(sorted(i["itemCode"] for _, i in result), ["a:0", "a:1", "a:2", "b:0", "b:1"])

    def test_old_keywords_only_when_collection_keywords_have_no_hits(self):
        fake = FakeRakuten({"ファンケル ビタミンD サプリメント": _items("c", 2)})
        result = run(fake, "ビタミンD", "ファンケル")
        keywords = [c["keyword"] for c in fake.calls]
        self.assertEqual(keywords[0], "ファンケル ビタミンD")
        self.assertIn("ファンケル ビタミンD サプリメント", keywords)
        self.assertEqual(len(result), 2)

    def test_filters_are_unchanged(self):
        items = _items("ok", 1) + _items("set", 1, "ファンケル ビタミンD 30日分×2個セット") + _items("used", 1, "【中古】ファンケル ビタミンD")
        result = run(FakeRakuten({"ファンケル ビタミンD": items}), "ビタミンD", "ファンケル")
        self.assertEqual([i["itemCode"] for _, i in result], ["ok:0"])


class RobustnessTests(unittest.TestCase):

    def test_unexpected_error_does_not_stop_collection(self):
        def boom(*a, **k):
            raise RuntimeError("unexpected")
        self.assertEqual(run(boom, "ビタミンD", "ファンケル"), [])

    def test_persistent_429_stops_with_cooldown(self):
        calls = []

        def too_many(kind, endpoint, params=None, **kw):
            calls.append(params["keyword"])
            return MagicMock(status_code=429, text="Try again in 1 seconds")
        with patch.object(app.time, "sleep", return_value=None):
            self.assertEqual(run(too_many, "ビタミンD", "ファンケル", jan="4908049491925"), [])
        self.assertEqual(len(calls), 2)            # 1回だけ再試行し、残りの検索語は試さない
        app.RAKUTEN_COOLDOWN_UNTIL = 0


class DiagnosisUnchangedTests(unittest.TestCase):

    def test_diagnosis_search_keeps_ten_hits_and_first_keyword_cutoff(self):
        fake = FakeRakuten({"ファンケル ビタミンD サプリメント": _items("d", 25)})
        result = run(fake, "ビタミンD", "ファンケル", collection=False)
        self.assertEqual([(c["keyword"], c["hits"], c.get("page")) for c in fake.calls],
                         [("ファンケル ビタミンD サプリメント", 10, None)])
        self.assertEqual(len(result), 10)


class IrregularSaleTests(unittest.TestCase):

    def test_irregular_sale_wording(self):
        for title in ("【アウトレット】DHC ビタミンBミックス 20日分【パッケージ擦れあり】", "ワケあり！DHCヒアルロン酸 20日分【期限2027年1月】",
                      "「新品未開封」パナソニック バイタリフト RF EH-SR85", "DHC 亜鉛 外箱潰れ", "賞味期限間近 DHC 亜鉛"):
            with self.subTest(title=title):
                self.assertTrue(pipeline.pm_irregular_sale(title))
        for title in ("DHC ビタミンBミックス 60日分120粒 賞味期限2029.01", "【新品】Panasonic バイタリフト RF EH-SR85"):
            with self.subTest(title=title):
                self.assertFalse(pipeline.pm_irregular_sale(title))


class KeywordStatsTests(unittest.TestCase):

    def test_stats_per_keyword_without_jan_value(self):
        trace = {"searches": [{"keyword": "ファンケル ビタミンD", "page": 1, "item_count": 30, "total_count": 51},
                              {"keyword": "ファンケル ビタミンD", "page": 2, "item_count": 21, "total_count": 51},
                              {"keyword": "4908049491925", "page": 1, "item_count": 3, "total_count": 3}],
                 "item_keyword": {"a:1": "ファンケル ビタミンD", "b:1": "ファンケル ビタミンD", "c:1": "4908049491925"}}
        result = {"status": "confirmed", "tiebreak_pool": [{"item_code": "a:1"}, {"item_code": "c:1"}]}
        stats = pipeline._keyword_stats(trace, result)
        self.assertEqual(stats["ファンケル ビタミンD"], {"calls": 2, "fetched": 51, "new_items": 2, "accepted": 1, "total_count": 51})
        self.assertEqual(stats["JAN"], {"calls": 1, "fetched": 3, "new_items": 1, "accepted": 1, "total_count": 3})
        self.assertNotIn("4908049491925", str(stats))


def score(title, product, brand, category="サプリメント", collection=True):
    token = app._PM_COLLECTION_MODE.set(collection)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return app.score_rakuten_item({"itemName": title}, product, brand=brand, category=category)
    finally:
        app._PM_COLLECTION_MODE.reset(token)


class TrialSizeTests(unittest.TestCase):
    """お試しサイズは収集時に通常品と同じ商品の容量違いとして扱う。試供品・セット品は除外。"""

    TRIAL = ("【公式キユーピーウエルネス】ヒアルロン酸 サプリ ヒアロモイスチャー240 お試し 320mg×28粒 7日分 "
             "飲むヒアルロン酸 高純度 サプリメント")   # 実データ(事例集 #144)

    def test_trial_size_is_not_rejected_in_collection_only(self):
        self.assertGreater(score(self.TRIAL, "ヒアロモイスチャー240", "キユーピーウエルネス"), -9000)
        self.assertLessEqual(score(self.TRIAL, "ヒアロモイスチャー240", "キユーピーウエルネス", collection=False), -9000)  # 診断時は従来どおり
        self.assertFalse(app.pm_is_set_item(self.TRIAL))
        self.assertFalse(pipeline.pm_irregular_sale(self.TRIAL))

    def test_trial_sets_samples_are_still_excluded(self):
        for title in ("DHC ヒアルロン酸 お試しセット", "ヒアロモイスチャー240 トライアルキット", "ヒアロモイスチャー240 サンプル 3粒"):
            with self.subTest(title=title):
                self.assertLessEqual(score(title, "ヒアロモイスチャー240", "キユーピーウエルネス"), -9000)
        self.assertTrue(pipeline.pm_irregular_sale("ヒアロモイスチャー240 試供品 7日分"))
        self.assertTrue(pipeline.pm_irregular_sale("ヒアロモイスチャー240 非売品"))


class IdentitySegmentTests(unittest.TestCase):
    """余分な成分名の確認は、タイトルの商品を特定している部分(商品名の後の最初の内容量より前)だけで行う。"""

    def test_keyword_after_size_does_not_reject_correct_product(self):
        # 実データ(事例集 #144): 公式の含有成分はヒアルロン酸Na・葉酸のみ。「コラーゲン」は内容量の後の検索用の語。
        self.assertTrue(pipeline.pm_title_name_match("ヒアロモイスチャー240", TrialSizeTests.TRIAL, "キユーピーウエルネス",
                                                     "サプリメント", ["ヒアルロン酸Na", "葉酸"]))
        self.assertIsNone(pipeline.no_jan_title_identity_conflict("ヒアロモイスチャー240", TrialSizeTests.TRIAL, ["ヒアルロン酸Na", "葉酸"]))

    def test_other_products_are_still_rejected(self):
        cases = [("亜鉛（小林製薬の栄養補助食品）", "小林製薬の栄養補助食品(サプリメント) マカ・亜鉛 プレミアム 90粒", "小林製薬", ["亜鉛"]),
                 ("ビタミンC", "DHC ビタミンC ＋ 亜鉛 60粒", "DHC", ["ビタミンC"]),
                 ("ビタミンD 30日分", "【DHC直販】カルシウム・マグネシウム・ビタミンD増量 カルシウム／マグ 30日分", "DHC", ["ビタミンD"])]
        for name, title, brand, actives in cases:
            with self.subTest(title=title):
                self.assertFalse(pipeline.pm_title_name_match(name, title, brand, "サプリメント", actives)
                                 and not pipeline.no_jan_title_identity_conflict(name, title, actives))

    def test_segment_falls_back_to_whole_title(self):
        self.assertEqual(pipeline.title_identity_segment("DHC ヒアルロン酸 サプリ | コラーゲン", "ヒアルロン酸"), "dhc ヒアルロン酸 サプリ ")
        self.assertEqual(pipeline.title_identity_segment("全く別の商品 30日分 コラーゲン", "ヒアルロン酸"), "全く別の商品 30日分 コラーゲン")
        self.assertEqual(pipeline.title_identity_segment("DHC ヒアルロン酸 60日分 コラーゲン", "ヒアルロン酸"), "dhc ヒアルロン酸 ")


class AuditFoundCasesTests(unittest.TestCase):
    """Step53の実データ確認で見つかった誤採用(別シリーズ・まとめ売り)の再発防止。"""

    def test_professional_series_is_another_product(self):
        title = "キリン iMUSE professional プラズマ乳酸菌サプリメント 30粒 機能性表示食品 ビタミンC"
        self.assertFalse(pipeline.pm_title_name_match("iMUSE（イミューズ）プラズマ乳酸菌サプリメント", title, "iMUSE（イミューズ）",
                                                      "サプリメント", ["プラズマ乳酸菌（L. lactis strain Plasma）"]))
        self.assertIsNotNone(pipeline.series_word_conflict("ディープスキンクリア", "ミーゼ プロフェッショナル"))
        self.assertIsNone(pipeline.series_word_conflict("プロフェッショナル セラム", "PROFESSIONAL セラム 30ml"))  # 商品名にあれば同じ

    def test_times_quantity_without_unit_is_a_fixed_pack(self):
        self.assertTrue(app.pm_is_set_item("DHC ビタミンBミックス 40粒 20日分 ×2 ナイアシン ビオチン 送料無料"))
        for title in ("ネイチャーメイド ビタミンBコンプレックス 21g（350mg×60粒）", "ヒアロモイスチャー240 お試し 320mg×28粒 7日分"):
            with self.subTest(title=title):
                self.assertFalse(app.pm_is_set_item(title))


class DeterministicSelectionTests(unittest.TestCase):
    """同じ候補集合なら、並び順に関係なく必ず同じ出品を選ぶ(検索結果の変動は別問題)。"""

    def test_same_candidates_in_any_order_give_same_choice(self):
        import itertools
        def item(code, reviews, avg, shop, image=True):
            return (100, {"itemCode": code, "reviewCount": reviews, "reviewAverage": avg, "shopName": shop,
                          "mediumImageUrls": [{"imageUrl": "x"}] if image else []})
        pairs = [item("b:2", 5, 4.0, "店B"), item("a:1", 5, 4.0, "店A"), item("c:3", 5, 4.0, "店C", image=False),
                 item("d:4", 3, 5.0, "DHC楽天市場店")]
        choices = {pipeline.select_listing_by_reviews(list(p), "DHC")["itemCode"] for p in itertools.permutations(pairs)}
        self.assertEqual(choices, {"a:1"})


if __name__ == "__main__":
    unittest.main()
