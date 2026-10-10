"""Step49.5: 美容機器の楽天ブランド事前フィルタ(括弧内外の明示的表記)と、楽天照合の
拒否条件(ブランド誤一致・別モデル・JAN不一致・中古・レンタル・セット)のテスト。

実Gemini・実楽天・実HTTP・本番DBは使わない。
"""

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402

JAN = "4540790319235"
BRAND, NAME = "ヤーマン（YA-MAN）", "メディリフト プラス EPM-18BB"


def _item(code, title, caption=""):
    return {"itemCode": code, "itemName": title, "itemCaption": caption, "itemPrice": 30000,
            "itemUrl": f"https://item.rakuten.co.jp/{code.replace(':', '/')}/", "shopName": "shop",
            "reviewCount": 3, "reviewAverage": 4.0, "mediumImageUrls": [{"imageUrl": "https://img.example/x.jpg"}]}


def resolve(items, brand=BRAND, name=NAME, jan=JAN):
    def fake_get(kind, endpoint, params=None, **kwargs):
        res = MagicMock(status_code=200, text="")
        res.json.return_value = {"Items": [dict(i) for i in items]}
        return res
    app._rakuten_candidates_cache.clear()
    with patch.object(app, "_rakuten_api_get", side_effect=fake_get), \
         patch.object(app, "wait_for_rakuten_rate_limit", return_value=None):
        return pipeline.resolve_item_code_for_product(brand, name, "美容機器", jan_code=jan)


GOOD = _item("good:1", "ヤーマン メディリフト プラス EPM-18BB EMS 美顔器 正規品", caption=f"JAN {JAN}")


class BrandFormsTests(unittest.TestCase):

    def test_explicit_forms(self):
        self.assertEqual(app.explicit_brand_forms("ヤーマン（YA-MAN）"), ["ヤーマン(YA-MAN)", "ヤーマン", "YA-MAN"])
        self.assertEqual(app.explicit_brand_forms("YA-MAN TOKYO JAPAN（ヤーマン）"),
                         ["YA-MAN TOKYO JAPAN(ヤーマン)", "YA-MAN TOKYO JAPAN", "ヤーマン"])
        self.assertEqual(app.explicit_brand_forms("MYTREX"), ["MYTREX"])
        self.assertEqual(app.explicit_brand_forms("X（A）"), ["X(A)"])  # 2文字未満の表記は使わない

    def test_annotated_brand_matches_listing_by_either_form(self):
        self.assertEqual(resolve([GOOD])["status"], "confirmed")
        english = _item("good:2", "YA-MAN メディリフト プラス EPM-18BB 美顔器", caption=f"JAN {JAN}")
        self.assertEqual(resolve([english])["status"], "confirmed")


class RejectionTests(unittest.TestCase):

    def test_other_brand_is_rejected(self):
        other = _item("other:1", "互換ブランド メディリフト プラス EPM-18BB 対応 美顔器", caption=f"JAN {JAN}")
        self.assertEqual(resolve([other])["status"], "not_found")

    def test_fuzzy_brand_spelling_is_not_accepted(self):
        fuzzy = _item("fz:1", "YAMAN メディリフト プラス EPM-18BB 美顔器", caption=f"JAN {JAN}")
        self.assertEqual(resolve([fuzzy])["status"], "not_found")

    def test_other_model_is_rejected(self):
        other_model = _item("m:1", "ヤーマン メディリフト EPM-500 EMS 美顔器", caption=f"JAN {JAN}")
        self.assertEqual(resolve([other_model])["status"], "not_found")

    def test_jan_mismatch_is_rejected(self):
        wrong_jan = _item("j:1", "ヤーマン メディリフト プラス EPM-18BB EMS 美顔器", caption="JAN 4540790999999")
        self.assertEqual(resolve([wrong_jan])["reason"], "jan_mismatch")

    def test_used_and_rental_are_rejected(self):
        for title in ("【中古】ヤーマン メディリフト プラス EPM-18BB 美顔器", "【レンタル】ヤーマン メディリフト プラス EPM-18BB 美顔器"):
            with self.subTest(title=title):
                # 既存のスコア判定(中古等のハード除外)または販売形態判定のどちらかで拒否される。
                result = resolve([_item("u:1", title, caption=f"JAN {JAN}")])
                self.assertEqual(result["status"], "not_found")
                self.assertIn(result.get("reason"), (None, "only_non_new_sale_listings"))

    def test_set_is_rejected(self):
        bundle = _item("s:1", "ヤーマン メディリフト プラス EPM-18BB 美顔器 2個セット", caption=f"JAN {JAN}")
        result = resolve([bundle])
        self.assertEqual(result["status"], "not_found")
        self.assertIn(result.get("reason"), (None, "only_set_items"))

    def test_correct_listing_is_chosen_among_rejects(self):
        mixed = [_item("other:1", "互換ブランド メディリフト プラス EPM-18BB 美顔器", caption=f"JAN {JAN}"),
                 _item("u:1", "【中古】ヤーマン メディリフト プラス EPM-18BB 美顔器", caption=f"JAN {JAN}"), GOOD]
        result = resolve(mixed)
        self.assertEqual((result["status"], result["item"]["itemCode"]), ("confirmed", "good:1"))


if __name__ == "__main__":
    unittest.main()
