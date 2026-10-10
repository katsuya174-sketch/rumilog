"""Step49.7: 楽天照合の候補ごとの除外理由ログ・集計のテスト(判定は変えない)。

実Gemini・実楽天・実HTTP・本番DBは使わない。
"""

import contextlib
import io
import json
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402
import product_collection_pipeline as pipeline  # noqa: E402

JAN_115, JAN_134 = "4540790319235", "4540790758621"


def _item(code, title, caption=""):
    return {"itemCode": code, "itemName": title, "itemCaption": caption, "itemPrice": 30000,
            "itemUrl": f"https://item.rakuten.co.jp/{code.replace(':', '/')}/", "shopName": "公式ショップ",
            "reviewCount": 3, "reviewAverage": 4.0, "mediumImageUrls": [{"imageUrl": "https://img.example/x.jpg"}]}


def run(items, brand, name, jan, category="美容機器"):
    def fake_get(kind, endpoint, params=None, **kwargs):
        res = MagicMock(status_code=200, text="")
        res.json.return_value = {"Items": [dict(i) for i in items]}
        return res
    app._rakuten_candidates_cache.clear()
    out = io.StringIO()
    with patch.object(app, "_rakuten_api_get", side_effect=fake_get), \
         patch.object(app, "wait_for_rakuten_rate_limit", return_value=None), contextlib.redirect_stdout(out):
        result = pipeline.resolve_item_code_for_product(brand, name, category, jan_code=jan)
    lines = [ln for ln in out.getvalue().splitlines() if ln.startswith("[RAKUTEN MATCH TRACE] ")]
    return result, json.loads(lines[-1][len("[RAKUTEN MATCH TRACE] "):]), lines[-1]


OFFICIAL_115 = _item("yaman-official:1", "美顔器 ウェアラブル ハンズフリー 【ヤーマン公式】《メディリフト プラス MediLift PLUS》"
                     "EPM-18BB 専用美容液と併用でさらに効果的", caption=f"JAN {JAN_115}")


class Case115Tests(unittest.TestCase):

    def test_skincare_word_rejection_is_identifiable(self):
        result, log, line = run([OFFICIAL_115], "ヤーマン（YA-MAN）", "メディリフト プラス EPM-18BB", JAN_115)
        self.assertEqual(result["status"], "not_found")  # 判定は従来どおり
        entry = log["items"][0]
        self.assertEqual((entry["stage"], entry["rule"], entry["matched_words"]),
                         ("score_reject", "device_skincare_word", ["美容液"]))
        self.assertEqual(entry["title"], OFFICIAL_115["itemName"])  # タイトル全文(上限内)
        summary = result["match_summary"]
        self.assertEqual(summary["score_reject_rules"], {"device_skincare_word": 1})
        self.assertEqual((summary["search_count"], summary["brand_match_count"]), (4, 1))  # 同一候補は1回
        self.assertEqual(summary["final_reason"], "not_found")
        for secret in ("https://", "accessKey", "applicationId", "公式ショップ", JAN_115):
            self.assertNotIn(secret, line)


class Case134Tests(unittest.TestCase):

    def _listing(self, code, caption):
        return _item(code, "【国内正規品】YA-MAN ヤーマン RF美顔器 フォトプラス 美顔器", caption=caption)

    def test_jan_states_are_distinguished(self):
        items = [self._listing("a:1", "JAN 4540790999994"),   # 別の正当なJAN
                 self._listing("b:1", "メーカー保証付き"),          # JAN記載なし
                 self._listing("c:1", None)]                      # 商品説明なし
        items[2]["itemCaption"] = None
        result, log, _ = run(items, "YA-MAN TOKYO JAPAN（ヤーマン）", "RF美顔器 フォトプラス", JAN_134)
        self.assertEqual(result["reason"], "jan_mismatch")
        self.assertEqual(result["match_summary"]["jan_status_counts"],
                         {"match": 0, "explicit_mismatch": 1, "absent": 1, "undeterminable": 1})
        self.assertEqual({e["item_code"]: e["resolver_stage"] for e in log["items"]},
                         {"a:1": "jan_not_matched", "b:1": "jan_not_matched", "c:1": "jan_not_matched"})

    def test_jan_match_is_recorded_and_decision_unchanged(self):
        result, log, _ = run([self._listing("ok:1", f"JAN {JAN_134}")], "YA-MAN TOKYO JAPAN（ヤーマン）",
                             "RF美顔器 フォトプラス", JAN_134)
        self.assertEqual((result["status"], result["item"]["itemCode"]), ("confirmed", "ok:1"))
        self.assertEqual(result["match_summary"]["jan_status_counts"]["match"], 1)
        self.assertEqual(log["items"][0]["resolver_stage"], "eligible")

    def test_115_and_134_causes_are_distinguishable_from_logs_alone(self):
        _, log115, _ = run([OFFICIAL_115], "ヤーマン（YA-MAN）", "メディリフト プラス EPM-18BB", JAN_115)
        _, log134, _ = run([self._listing("a:1", "JAN 4540790999994")], "YA-MAN TOKYO JAPAN（ヤーマン）",
                           "RF美顔器 フォトプラス", JAN_134)
        self.assertEqual(log115["summary"]["final_reason"], "not_found")
        self.assertEqual(log115["items"][0]["stage"], "score_reject")
        self.assertEqual(log134["summary"]["final_reason"], "jan_mismatch")
        self.assertEqual(log134["items"][0]["jan_status"], "explicit_mismatch")


class OtherStageTests(unittest.TestCase):

    def test_brand_model_and_sale_stages_are_recorded(self):
        items = [_item("x:1", "他社 メディリフト プラス EPM-18BB 美顔器"),
                 _item("x:2", "ヤーマン メディリフト プラス EPM-500 EMS 美顔器", caption=f"JAN {JAN_115}"),
                 _item("x:3", "【レンタル】ヤーマン メディリフト プラス EPM-18BB 美顔器", caption=f"JAN {JAN_115}"),
                 _item("x:4", "【中古】ヤーマン メディリフト プラス EPM-18BB 美顔器")]
        result, log, _ = run(items, "ヤーマン（YA-MAN）", "メディリフト プラス EPM-18BB", JAN_115)
        by_code = {e["item_code"]: e for e in log["items"]}
        self.assertEqual(by_code["x:1"]["stage"], "brand_prefilter")
        self.assertEqual(by_code["x:2"]["resolver_stage"], "device_model_mismatch")
        self.assertEqual(by_code["x:3"]["resolver_stage"], "non_new_sale")
        self.assertEqual((by_code["x:4"]["stage"], by_code["x:4"]["rule"]), ("score_reject", "hard_reject_word"))

    def test_log_volume_is_capped(self):
        long_title = "ヤーマン メディリフト プラス EPM-18BB 美顔器 " + "あ" * 400
        items = [_item(f"m:{i}", long_title + str(i)) for i in range(60)]
        result, log, _ = run(items, "ヤーマン（YA-MAN）", "メディリフト プラス EPM-18BB", JAN_115)
        self.assertEqual(len(log["items"]), app.RAKUTEN_TRACE_MAX_ITEMS)
        self.assertGreater(result["match_summary"]["omitted_item_count"], 0)
        self.assertTrue(all(len(e["title"]) <= app.RAKUTEN_TRACE_TITLE_MAX_CHARS for e in log["items"]))

    def test_no_trace_outside_product_master_resolver(self):
        self.assertIsNone(app._rakuten_trace())
        explain = {}
        self.assertEqual(app.score_rakuten_item({"itemName": "【中古】美顔器"}, "美顔器", category="美容機器",
                                                explain=explain), -9999)
        self.assertEqual(explain, {"rule": "hard_reject_word", "matched_words": ["中古"]})
        self.assertEqual(app.score_rakuten_item({"itemName": "【中古】美顔器"}, "美顔器", category="美容機器"), -9999)


if __name__ == "__main__":
    unittest.main()
