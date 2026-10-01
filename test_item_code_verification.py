"""
楽天APIのitemCode検索を実環境で検証するための最小限の仕組み(Phase 2
本実装前の事前確認)のテスト。

設計(ユーザーとの合意事項):
- 保存済みitem_codeを加工せずそのまま使う
- keywordは併用しない
- elementsは価格・URL・画像・availability・itemCode等、更新に必要な項目だけ
- 認証済み管理用途に限定
- 成功/失敗のHTTP statusと楽天エラー内容を確認可能にする
- APIキー等の秘密情報はログ・レスポンスへ一切出さない
- 既存診断フローにはまだ接続しない

実際の楽天APIは呼ばない(requests.getをモックする)。実APIでの検証手順は
ユーザー側に別途提示する。
"""

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402


class FetchRakutenItemByItemCodeTests(unittest.TestCase):
    def setUp(self):
        # 実際のレート制限待機(time.sleep)はテストの価値に寄与しないため、
        # ここでは無効化する(レートリミッター自体は別途検証されている前提)。
        patcher = patch("app.wait_for_rakuten_rate_limit")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_empty_item_code_returns_error_without_calling_api(self):
        with patch("app.requests.get") as mock_get:
            result = app.fetch_rakuten_item_by_item_code("")
        mock_get.assert_not_called()
        self.assertEqual(result["http_status"], 0)
        self.assertFalse(result["ok"])
        self.assertIn("item_code is required", result["rakuten_error"])

    def test_200_with_item_returns_ok_true_and_item(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "Items": [{"itemName": "テスト商品", "itemPrice": 1980, "itemCode": "shop:123"}]
        }
        with patch("app.requests.get", return_value=mock_response):
            result = app.fetch_rakuten_item_by_item_code("shop:123")

        self.assertEqual(result["http_status"], 200)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["rakuten_error"])
        self.assertEqual(result["item"]["itemName"], "テスト商品")
        self.assertEqual(result["item"]["itemPrice"], 1980)

    def test_200_with_empty_items_returns_ok_false(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"Items": []}
        with patch("app.requests.get", return_value=mock_response):
            result = app.fetch_rakuten_item_by_item_code("shop:999")

        self.assertEqual(result["http_status"], 200)
        self.assertFalse(result["ok"])
        self.assertIsNotNone(result["rakuten_error"])
        self.assertIsNone(result["item"])

    def test_400_response_returns_status_and_error_body(self):
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "wrong parameter"
        with patch("app.requests.get", return_value=mock_response):
            result = app.fetch_rakuten_item_by_item_code("shop:bad")

        self.assertEqual(result["http_status"], 400)
        self.assertFalse(result["ok"])
        self.assertIn("wrong parameter", result["rakuten_error"])
        self.assertIsNone(result["item"])

    def test_request_exception_is_handled_gracefully(self):
        with patch("app.requests.get", side_effect=ConnectionError("network down")):
            result = app.fetch_rakuten_item_by_item_code("shop:123")

        self.assertEqual(result["http_status"], 0)
        self.assertFalse(result["ok"])
        self.assertIn("network down", result["rakuten_error"])

    def test_sends_item_code_only_not_keyword(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"Items": []}
        with patch("app.requests.get", return_value=mock_response) as mock_get:
            app.fetch_rakuten_item_by_item_code("shop:123")

        sent_params = mock_get.call_args.kwargs["params"]
        self.assertEqual(sent_params["itemCode"], "shop:123")
        self.assertNotIn("keyword", sent_params)

    def test_item_code_is_used_verbatim_without_modification(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"Items": []}
        raw_item_code = "shop-abc:item_999-X"
        with patch("app.requests.get", return_value=mock_response) as mock_get:
            app.fetch_rakuten_item_by_item_code(raw_item_code)

        sent_params = mock_get.call_args.kwargs["params"]
        self.assertEqual(sent_params["itemCode"], raw_item_code)

    def test_requests_narrowed_elements_for_minimal_payload(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"Items": []}
        with patch("app.requests.get", return_value=mock_response) as mock_get:
            app.fetch_rakuten_item_by_item_code("shop:123")

        sent_params = mock_get.call_args.kwargs["params"]
        elements = sent_params.get("elements", "")
        for field in ("itemPrice", "itemUrl", "itemCode", "availability"):
            self.assertIn(field, elements)

    def test_result_never_contains_api_credentials(self):
        with patch.object(app, "RAKUTEN_APP_ID", "secret-app-id"), \
             patch.object(app, "RAKUTEN_ACCESS_KEY", "secret-access-key"):
            mock_response = MagicMock()
            mock_response.status_code = 400
            mock_response.text = "some error body"
            with patch("app.requests.get", return_value=mock_response):
                result = app.fetch_rakuten_item_by_item_code("shop:123")

        result_text = str(result)
        self.assertNotIn("secret-app-id", result_text)
        self.assertNotIn("secret-access-key", result_text)


class AdminVerifyItemCodeSearchRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def test_requires_admin_key(self):
        with patch.dict(os.environ, {"ADMIN_KEY": "correct-key"}):
            resp = self.client.get("/admin/verify-item-code-search?item_code=shop:123")
        self.assertEqual(resp.status_code, 403)

    def test_rejects_wrong_admin_key(self):
        with patch.dict(os.environ, {"ADMIN_KEY": "correct-key"}):
            resp = self.client.get("/admin/verify-item-code-search?key=wrong&item_code=shop:123")
        self.assertEqual(resp.status_code, 403)

    def test_requires_item_code_param(self):
        with patch.dict(os.environ, {"ADMIN_KEY": "correct-key"}):
            resp = self.client.get("/admin/verify-item-code-search?key=correct-key")
        self.assertEqual(resp.status_code, 400)

    def test_authorized_request_calls_fetch_and_returns_json(self):
        fake_result = {"http_status": 200, "ok": True, "rakuten_error": None, "item": {"itemName": "x"}}
        with patch.dict(os.environ, {"ADMIN_KEY": "correct-key"}), \
             patch("app.fetch_rakuten_item_by_item_code", return_value=fake_result) as mock_fetch:
            resp = self.client.get("/admin/verify-item-code-search?key=correct-key&item_code=shop:123")

        self.assertEqual(resp.status_code, 200)
        mock_fetch.assert_called_once_with("shop:123")
        self.assertEqual(resp.get_json(), fake_result)

    def test_response_never_contains_admin_key_or_api_credentials(self):
        with patch.dict(os.environ, {"ADMIN_KEY": "correct-key"}), \
             patch.object(app, "RAKUTEN_APP_ID", "secret-app-id"), \
             patch.object(app, "RAKUTEN_ACCESS_KEY", "secret-access-key"):
            mock_response = MagicMock()
            mock_response.status_code = 400
            mock_response.text = "error body"
            with patch("app.requests.get", return_value=mock_response):
                resp = self.client.get(
                    "/admin/verify-item-code-search?key=correct-key&item_code=shop:123"
                )

        body = resp.get_data(as_text=True)
        self.assertNotIn("secret-app-id", body)
        self.assertNotIn("secret-access-key", body)
        self.assertNotIn("correct-key", body)


if __name__ == "__main__":
    unittest.main()
