"""Step54: レビュー件数が同じ出品は最安値(送料込み・送料別を区別しない)で選び、選ぶ出品の販売ページで
通常販売・価格・在庫を確認する。販売ページは実データ(楽天の商品ページの埋め込みJSON)の形式を使う。

実楽天・実HTTP・本番DBは使わない。
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402,F401
import product_collection_pipeline as pipeline  # noqa: E402
from test_step52_listing_selection import _item  # noqa: E402

# 実データ(creidea-coms:10000764)の形式: 1袋/3袋を選べる出品
SKU_PAGE = ('"sellType":"NORMAL","sku":[{"variantId":"r-sku00000004","selectorValues":["3袋"],"images":[],'
            '"taxIncludedPrice":4180.0,"isTelecom":false},{"variantId":"r-sku00000003","selectorValues":["1袋"],"images":[],'
            '"taxIncludedPrice":1480.0}],"variantMappedInventories":[{"sku":"r-sku00000004","inventoryId":"2146","quantity":%d},'
            '{"sku":"r-sku00000003","inventoryId":"2147","quantity":%d}]')
# 実データ(city-mono-shop:10016886)の形式: 販売単位が1つの出品(送料別)
PLAIN_PAGE = ('"taxIncludedPrice":34220.0,"shipping":{"postageIncluded":false,"singleItemShipping":0},"sellType":"%s",'
              '"sku":[],"variantId":"no0bgn8pgkm","variantMappedInventories":[{"sku":"no0bgn8pgkm","inventoryId":"fcd0","quantity":%d}]')


class PageCheckTests(unittest.TestCase):
    def test_sku_page_checks_the_unit_with_the_api_price(self):
        item = {"itemPrice": 1480}
        self.assertEqual(pipeline.listing_page_check(item, SKU_PAGE % (4, 5)), (True, "ok"))
        self.assertEqual(pipeline.listing_page_check(item, SKU_PAGE % (4, 0)), (False, "out_of_stock"))   # 1袋だけ品切れ
        self.assertEqual(pipeline.listing_page_check({"itemPrice": 1500}, SKU_PAGE % (4, 5)), (False, "price_not_on_page"))

    def test_plain_page(self):
        item = {"itemPrice": 34220}
        self.assertEqual(pipeline.listing_page_check(item, PLAIN_PAGE % ("NORMAL", 11)), (True, "ok"))
        self.assertEqual(pipeline.listing_page_check(item, PLAIN_PAGE % ("NORMAL", 0)), (False, "out_of_stock"))
        self.assertEqual(pipeline.listing_page_check(item, PLAIN_PAGE % ("RESERVE", 11)), (False, "not_normal_sale"))
        self.assertEqual(pipeline.listing_page_check({"itemPrice": 29480}, PLAIN_PAGE % ("NORMAL", 11)), (False, "price_not_on_page"))

    def test_unreadable_page_is_not_confirmed(self):
        self.assertEqual(pipeline.listing_page_check({"itemPrice": 1}, "<html>404</html>"), (False, "page_format_unknown"))
        with patch.object(pipeline, "_fetch_listing_page", return_value=None):
            self.assertEqual(pipeline.listing_page_check({"itemPrice": 1}), (False, "page_unavailable"))

    def test_page_url_from_affiliate_link(self):
        aff = "https://hb.afl.rakuten.co.jp/hgc/x/?pc=https%3A%2F%2Fitem.rakuten.co.jp%2Fshop%2Fabc%2F&m=y"
        self.assertEqual(pipeline._listing_page_url({"itemUrl": aff}), "https://item.rakuten.co.jp/shop/abc/")
        self.assertIsNone(pipeline._listing_page_url({"itemUrl": "https://example.com/item"}))


class CheapestSelectionTests(unittest.TestCase):
    def test_review_count_first_then_cheapest(self):
        pairs = [(1, _item("a:1", "x", reviews=5, price=9000)), (1, _item("b:1", "x", reviews=0, price=1000)),
                 (1, _item("c:1", "x", reviews=5, price=8000, average=1.0))]
        self.assertEqual(pipeline.select_listing_by_reviews(pairs, "B")["itemCode"], "c:1")

    def test_cheapest_regardless_of_shipping_flag(self):
        free = dict(_item("free:1", "x", reviews=0, price=40592), postageFlag=0)
        extra = dict(_item("extra:1", "x", reviews=0, price=31600), postageFlag=1)
        self.assertEqual(pipeline.select_listing_by_reviews([(1, free), (1, extra)], "B")["itemCode"], "extra:1")

    def test_cheapest_failing_page_check_falls_to_next(self):
        pairs = [(1, _item("a:1", "x", reviews=0, price=100)), (1, _item("b:1", "x", reviews=0, price=200)),
                 (1, _item("c:1", "x", reviews=0, price=300))]
        checks = []
        page = {"a:1": (False, "out_of_stock"), "b:1": (True, "ok"), "c:1": (True, "ok")}
        best = pipeline.select_listing_by_reviews(pairs, "B", lambda i: page[i["itemCode"]], checks)
        self.assertEqual(best["itemCode"], "b:1")
        self.assertEqual([c["item_code"] for c in checks], ["a:1", "b:1"])

    def test_all_pages_unconfirmed_is_hold(self):
        pairs = [(1, _item("a:1", "x", reviews=0, price=100)), (1, _item("b:1", "x", reviews=0, price=200))]
        self.assertIsNone(pipeline.select_listing_by_reviews(pairs, "B", lambda i: (False, "page_unavailable")))

    def test_unique_most_reviewed_needs_no_page_check(self):
        pairs = [(1, _item("a:1", "x", reviews=9, price=900)), (1, _item("b:1", "x", reviews=0, price=100))]
        called = []
        best = pipeline.select_listing_by_reviews(pairs, "B", lambda i: called.append(i) or (False, "x"))
        self.assertEqual((best["itemCode"], called), ("a:1", []))

    def test_resolver_holds_when_no_page_confirmed(self):
        from test_step54_device_color_variant import resolve_pm77
        items = [_item("a:1", "Panasonic 美顔器 ソニック RF リフト ゴールド調 EH-SR75-N", reviews=0, price=29480),
                 _item("b:1", "Panasonic 美顔器 ソニック RF リフト ゴールド調 EH-SR75-N", reviews=0, price=40592)]
        with patch.object(pipeline, "_fetch_listing_page", return_value=None):
            result = resolve_pm77(items)
        self.assertEqual((result["status"], result["reason"]), ("not_found", "listing_page_unconfirmed"))
        self.assertEqual(resolve_pm77(items)["item"]["itemCode"], "a:1")


if __name__ == "__main__":
    unittest.main()
