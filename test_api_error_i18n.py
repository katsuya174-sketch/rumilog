"""
APIエラーメッセージ(_api_error()経由、および内部でImageQualityError等を送出する
load_uploaded_images/validate_questionnaire_values等)のgettext多言語化(ja/en)を
検証する。

観点:
- ?lang=en / Accept-Language: en を指定すると英語のメッセージが返ること
- 指定がない場合は既存どおり日本語のままであること(select_locale()の
  「明示指定 → セッション → Accept-Language → 日本語フォールバック」を踏襲)
- 上記いずれの場合もHTTP status・error.codeは変化しないこと(レスポンス構造も不変)
- IMAGE_QUALITY_ERROR/INPUT_MISSING の分類が表示言語に関わらず正しいこと
  (以前はメッセージ文字列が「【」で始まるかどうかで判定していたため、英語化すると
  誤分類する回帰リスクがあった。ImageQualityError型による判定へ切り替えた結果を確認する)

DB接続を伴わないエンドポイント/関数だけを対象にしており、実行にDATABASE_URLの
実DBは必要としない(必須項目欠落・入力値不正など、DBアクセス前に短絡する経路のみ)。
"""

import io
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402

REVIEWER_KEY = "K" * 43  # secrets.token_urlsafe(32)相当の長さのダミー値(実キーは使わない)


def _client():
    return app.app.test_client()


def _dark_photo_bytes():
    """brightness<35(check_image_qualityの「暗すぎる」閾値)を確実に満たす黒画像。"""
    from PIL import Image

    img = Image.new("L", (60, 60), color=0)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    buf.seek(0)
    return buf


def _dark_photos_multipart():
    return {
        "front_photo": (_dark_photo_bytes(), "front.jpg"),
        "left_photo": (_dark_photo_bytes(), "left.jpg"),
        "right_photo": (_dark_photo_bytes(), "right.jpg"),
    }


class AuthVerifyMessageLocalizationTests(unittest.TestCase):
    """POST /api/v1/auth/verify (token未送信) のja/en切り替え。"""

    def test_missing_token_japanese_by_default(self):
        resp = _client().post("/api/v1/auth/verify", data={})
        body = resp.get_json()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(body["error"]["code"], "INPUT_MISSING")
        self.assertEqual(body["error"]["message"], "トークンが必要です")

    def test_missing_token_english_via_lang_query(self):
        resp = _client().post("/api/v1/auth/verify?lang=en", data={})
        body = resp.get_json()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(body["error"]["code"], "INPUT_MISSING")
        self.assertEqual(body["error"]["message"], "Token is required")

    def test_missing_token_english_via_accept_language_header(self):
        resp = _client().post(
            "/api/v1/auth/verify", data={}, headers={"Accept-Language": "en"}
        )
        body = resp.get_json()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(body["error"]["code"], "INPUT_MISSING")
        self.assertEqual(body["error"]["message"], "Token is required")

    def test_unsupported_accept_language_falls_back_to_japanese(self):
        resp = _client().post(
            "/api/v1/auth/verify", data={}, headers={"Accept-Language": "fr"}
        )
        body = resp.get_json()
        self.assertEqual(body["error"]["message"], "トークンが必要です")


class DeleteAccountMessageLocalizationTests(unittest.TestCase):
    """DELETE /api/v1/account (未ログイン) のja/en切り替え。DB接続前に401で短絡する。"""

    def test_not_logged_in_japanese_by_default(self):
        resp = _client().delete("/api/v1/account")
        body = resp.get_json()
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(body["error"]["code"], "NOT_LOGGED_IN")
        self.assertEqual(body["error"]["message"], "ログインが必要です")

    def test_not_logged_in_english(self):
        resp = _client().delete("/api/v1/account?lang=en")
        body = resp.get_json()
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(body["error"]["code"], "NOT_LOGGED_IN")
        self.assertEqual(body["error"]["message"], "Login is required")


class ReviewerVerifyMessageLocalizationTests(unittest.TestCase):
    """POST /api/v1/reviewer/verify のja/en切り替え。REVIEWER_ACCESS_KEYの実値は使わない。"""

    def test_missing_key_japanese_by_default(self):
        resp = _client().post("/api/v1/reviewer/verify", data={})
        body = resp.get_json()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(body["error"]["code"], "INPUT_MISSING")
        self.assertEqual(body["error"]["message"], "キーが必要です")

    def test_missing_key_english(self):
        resp = _client().post("/api/v1/reviewer/verify?lang=en", data={})
        body = resp.get_json()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(body["error"]["code"], "INPUT_MISSING")
        self.assertEqual(body["error"]["message"], "Key is required")

    def test_invalid_key_japanese_by_default(self):
        with patch.dict(os.environ, {"REVIEWER_ACCESS_KEY": REVIEWER_KEY}):
            resp = _client().post("/api/v1/reviewer/verify", data={"key": "wrong"})
        body = resp.get_json()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(body["error"]["code"], "INVALID_TOKEN")
        self.assertEqual(body["error"]["message"], "無効なキーです")

    def test_invalid_key_english(self):
        with patch.dict(os.environ, {"REVIEWER_ACCESS_KEY": REVIEWER_KEY}):
            resp = _client().post("/api/v1/reviewer/verify?lang=en", data={"key": "wrong"})
        body = resp.get_json()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(body["error"]["code"], "INVALID_TOKEN")
        self.assertEqual(body["error"]["message"], "Invalid key")


class VerifyPurchaseAndroidMessageLocalizationTests(unittest.TestCase):
    """POST /api/v1/premium/verify-purchase-android (必須項目欠落)のja/en切り替え。"""

    def test_missing_fields_japanese_by_default(self):
        resp = _client().post("/api/v1/premium/verify-purchase-android", data={})
        body = resp.get_json()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(body["error"]["code"], "INPUT_MISSING")
        self.assertEqual(body["error"]["message"], "purchase_tokenとproduct_idが必要です")

    def test_missing_fields_english(self):
        resp = _client().post("/api/v1/premium/verify-purchase-android?lang=en", data={})
        body = resp.get_json()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(body["error"]["code"], "INPUT_MISSING")
        self.assertEqual(
            body["error"]["message"], "purchase_token and product_id are required"
        )


class QuestionnaireValidationMessageLocalizationTests(unittest.TestCase):
    """validate_questionnaire_values()のja/en切り替え(APIレスポンスのINVALID_INPUT_VALUEに使われる)。"""

    def test_invalid_oil_status_japanese_by_default(self):
        with app.app.test_request_context("/"):
            ok, message = app.validate_questionnaire_values({"oil": "invalid_value"})
        self.assertFalse(ok)
        self.assertIn("肌質(oil_status)の値が不正です", message)

    def test_invalid_oil_status_english(self):
        with app.app.test_request_context("/", headers={"Accept-Language": "en"}):
            ok, message = app.validate_questionnaire_values({"oil": "invalid_value"})
        self.assertFalse(ok)
        self.assertIn("Invalid value for skin type (oil_status)", message)


class ImageQualityClassificationLanguageIndependenceTests(unittest.TestCase):
    """
    load_uploaded_images()がImageQualityError(ValueErrorのサブクラス)を送出し、
    その型による分類(api_create_diagnosis側のIMAGE_QUALITY_ERROR振り分け)が
    表示言語に関わらず不変であることを確認する。

    以前の実装はメッセージ文字列が「【」で始まるかどうかで判定していたため、
    メッセージを英訳すると「【」が付かなくなりINPUT_MISSINGに誤分類される
    回帰リスクがあった。この回帰が起きていないことをja/en両方で確認する。
    """

    def test_dark_photo_raises_image_quality_error_in_japanese(self):
        with app.app.test_request_context(
            "/", method="POST", data=_dark_photos_multipart(),
            content_type="multipart/form-data",
        ):
            with self.assertRaises(app.ImageQualityError) as ctx:
                app.load_uploaded_images(app.request)
            self.assertIn("画像が暗すぎます", str(ctx.exception))

    def test_dark_photo_raises_image_quality_error_in_english(self):
        with app.app.test_request_context(
            "/", method="POST", data=_dark_photos_multipart(),
            content_type="multipart/form-data", headers={"Accept-Language": "en"},
        ):
            with self.assertRaises(app.ImageQualityError) as ctx:
                app.load_uploaded_images(app.request)
            message = str(ctx.exception)
            self.assertIn("too dark", message)
            # 英語化後も「【」形式ではなくなるが、型(ImageQualityError)で
            # 正しく分類できることがこのテストの主眼(文言そのものは問わない)。

    def test_missing_photo_raises_plain_value_error_not_image_quality_error(self):
        # 未選択(入力不足)はImageQualityErrorではなく通常のValueErrorのままであること
        # (品質エラーと混同されないこと)。
        with app.app.test_request_context("/", method="POST", data={}, content_type="multipart/form-data"):
            with self.assertRaises(ValueError) as ctx:
                app.load_uploaded_images(app.request)
            self.assertNotIsInstance(ctx.exception, app.ImageQualityError)


if __name__ == "__main__":
    unittest.main()
