"""
Google Play審査専用キー(REVIEWER_ACCESS_KEY)方式のテスト。
- _reviewer_access_fingerprint() / _is_reviewer_access_session() 単体
- POST /api/v1/reviewer/verify のキー検証
- is_premium_user()のreviewer分岐(実際にPremium限定APIがis_premium=trueを
  返すところまで確認する)
- REVIEWER_ACCESS_KEYローテーション時の既存セッション失効
- 既存の通常magic linkログイン・通常premium_key判定・DEV_PREMIUM_MODE/
  is_creator()/PREMIUM_PREVIEW_KEYへの非干渉

実DBが必要なため、DATABASE_URL(環境変数)でテスト専用DBを指定して実行する。

実行方法:
    DATABASE_URL=postgresql://localhost/rumilog_test python3 -m pytest test_reviewer_access.py -v
    (pytestが無い場合: python3 -m unittest test_reviewer_access -v)

このファイルはproduction用DATABASE_URL(.env)を書き換えない。実行前に必ず
DATABASE_URLをテスト専用DBへ向けること(未設定時はデフォルトでpostgresql://
localhost/rumilog_testを使う)。
"""

import hashlib
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402  (DATABASE_URL設定後にimportする必要がある)

REVIEWER_KEY = "K" * 43  # secrets.token_urlsafe(32)相当の長さのダミー値
OTHER_KEY = "not-the-reviewer-key"


def _clear_reviewer_env():
    os.environ.pop("REVIEWER_ACCESS_KEY", None)


def _truncate_premium_table():
    import psycopg2
    conn = psycopg2.connect(app.DATABASE_URL)
    cur = conn.cursor()
    cur.execute("TRUNCATE premium_subscriptions")
    conn.commit()
    cur.close()
    conn.close()


class ReviewerAccessFingerprintUnitTests(unittest.TestCase):
    """_reviewer_access_fingerprint() / _is_reviewer_access_session()単体の検証。"""

    def test_fingerprint_is_sha256_hex_of_key(self):
        expected = hashlib.sha256(REVIEWER_KEY.encode()).hexdigest()
        self.assertEqual(app._reviewer_access_fingerprint(REVIEWER_KEY), expected)

    def test_session_with_matching_fingerprint_is_true(self):
        with patch.dict(os.environ, {}, clear=False), app.app.test_request_context("/"):
            os.environ["REVIEWER_ACCESS_KEY"] = REVIEWER_KEY
            app.flask_session["reviewer_access_fingerprint"] = app._reviewer_access_fingerprint(REVIEWER_KEY)
            self.assertTrue(app._is_reviewer_access_session())

    def test_session_with_stale_fingerprint_after_rotation_is_false(self):
        """REVIEWER_ACCESS_KEYをローテーションした後、古いキーの指紋を持つ
        セッションは無効になること(即時失効の確認)。"""
        with patch.dict(os.environ, {}, clear=False), app.app.test_request_context("/"):
            os.environ["REVIEWER_ACCESS_KEY"] = REVIEWER_KEY
            app.flask_session["reviewer_access_fingerprint"] = app._reviewer_access_fingerprint(REVIEWER_KEY)
            self.assertTrue(app._is_reviewer_access_session())

            # キーをローテーション(変更)
            os.environ["REVIEWER_ACCESS_KEY"] = "a-brand-new-rotated-key"
            self.assertFalse(app._is_reviewer_access_session())

    def test_env_var_deleted_after_rotation_invalidates_session(self):
        with patch.dict(os.environ, {}, clear=False), app.app.test_request_context("/"):
            os.environ["REVIEWER_ACCESS_KEY"] = REVIEWER_KEY
            app.flask_session["reviewer_access_fingerprint"] = app._reviewer_access_fingerprint(REVIEWER_KEY)
            self.assertTrue(app._is_reviewer_access_session())

            os.environ.pop("REVIEWER_ACCESS_KEY", None)
            self.assertFalse(app._is_reviewer_access_session())

    def test_no_fingerprint_in_session_is_false(self):
        with patch.dict(os.environ, {}, clear=False), app.app.test_request_context("/"):
            os.environ["REVIEWER_ACCESS_KEY"] = REVIEWER_KEY
            self.assertFalse(app._is_reviewer_access_session())

    def test_reviewer_access_key_env_missing_disables_branch_even_with_fingerprint(self):
        with patch.dict(os.environ, {}, clear=False), app.app.test_request_context("/"):
            _clear_reviewer_env()
            app.flask_session["reviewer_access_fingerprint"] = app._reviewer_access_fingerprint(REVIEWER_KEY)
            self.assertFalse(app._is_reviewer_access_session())


class ReviewerVerifyEndpointTests(unittest.TestCase):
    """POST /api/v1/reviewer/verify のHTTPエンドポイントテスト。"""

    def setUp(self):
        _truncate_premium_table()

    # 1. 正しいkey → 検証成功
    def test_correct_key_succeeds(self):
        with patch.dict(os.environ, {"REVIEWER_ACCESS_KEY": REVIEWER_KEY}):
            client = app.app.test_client()
            resp = client.post("/api/v1/reviewer/verify", data={"key": REVIEWER_KEY})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.get_json()["success"])

    # 2. 不正key → 400
    def test_wrong_key_fails(self):
        with patch.dict(os.environ, {"REVIEWER_ACCESS_KEY": REVIEWER_KEY}):
            client = app.app.test_client()
            resp = client.post("/api/v1/reviewer/verify", data={"key": OTHER_KEY})
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.get_json()["error"]["code"], "INVALID_TOKEN")

    # 3. REVIEWER_ACCESS_KEY未設定 → 常に失敗
    def test_env_var_missing_always_fails(self):
        with patch.dict(os.environ, {}, clear=False):
            _clear_reviewer_env()
            client = app.app.test_client()
            resp = client.post("/api/v1/reviewer/verify", data={"key": REVIEWER_KEY})
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.get_json()["error"]["code"], "INVALID_TOKEN")

    # 4. key未送信 → 400 INPUT_MISSING
    def test_missing_key_field_returns_input_missing(self):
        with patch.dict(os.environ, {"REVIEWER_ACCESS_KEY": REVIEWER_KEY}):
            client = app.app.test_client()
            resp = client.post("/api/v1/reviewer/verify", data={})
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.get_json()["error"]["code"], "INPUT_MISSING")

    # 5. 検証成功後の同一セッションでis_premium_user()がTrue
    #    (単にverify APIが200になるだけでなく、実際にPremium限定API
    #    /api/v1/historyがis_premium=trueを返すところまで確認する)
    def test_verified_session_reports_premium_true_on_real_premium_gated_endpoint(self):
        with patch.dict(os.environ, {"REVIEWER_ACCESS_KEY": REVIEWER_KEY}):
            client = app.app.test_client()
            verify_resp = client.post("/api/v1/reviewer/verify", data={"key": REVIEWER_KEY})
            self.assertEqual(verify_resp.status_code, 200)

            session_resp = client.get("/api/v1/auth/session")
            self.assertTrue(session_resp.get_json()["is_premium"])

            history_resp = client.get("/api/v1/history")
            self.assertEqual(history_resp.status_code, 200)
            self.assertTrue(history_resp.get_json()["is_premium"])

    # 6. フィンガープリントを持たない別セッションではPremiumにならない
    def test_unverified_session_is_not_premium(self):
        with patch.dict(os.environ, {"REVIEWER_ACCESS_KEY": REVIEWER_KEY}):
            client = app.app.test_client()
            session_resp = client.get("/api/v1/auth/session")
            self.assertFalse(session_resp.get_json()["is_premium"])

    # 7. REVIEWER_ACCESS_KEYローテーション後、既存セッションもPremiumでなくなる
    def test_rotating_key_invalidates_existing_verified_session_on_real_endpoint(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ["REVIEWER_ACCESS_KEY"] = REVIEWER_KEY
            client = app.app.test_client()
            verify_resp = client.post("/api/v1/reviewer/verify", data={"key": REVIEWER_KEY})
            self.assertEqual(verify_resp.status_code, 200)

            before = client.get("/api/v1/auth/session").get_json()
            self.assertTrue(before["is_premium"])

            # ローテーション(変更)
            os.environ["REVIEWER_ACCESS_KEY"] = "rotated-" + REVIEWER_KEY
            after = client.get("/api/v1/auth/session").get_json()
            self.assertFalse(after["is_premium"])

    # 8. 通常のpremium_key判定(Google Play購入)が変わらないこと
    def test_normal_google_play_premium_key_judgement_unaffected(self):
        key = app.issue_premium_key_for_google_play("tok_reviewer_key_regression", "2099-01-01T00:00:00")
        with patch.dict(os.environ, {}, clear=False):
            _clear_reviewer_env()
            with app.app.test_request_context(f"/?premium_key={key}"):
                self.assertTrue(app.is_premium_user())

    # 9. 通常のmagic linkログインが変わらないこと
    def test_normal_magic_link_login_unaffected(self):
        with patch.dict(os.environ, {"REVIEWER_ACCESS_KEY": REVIEWER_KEY}):
            token = app.create_magic_token("normal-user-reviewer-regression@example.com")
            client = app.app.test_client()
            resp = client.post("/api/v1/auth/verify", data={"token": token})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.get_json()["email"], "normal-user-reviewer-regression@example.com")

            session_resp = client.get("/api/v1/auth/session")
            body = session_resp.get_json()
            self.assertEqual(body["email"], "normal-user-reviewer-regression@example.com")
            self.assertFalse(body["is_premium"])
        import psycopg2
        conn = psycopg2.connect(app.DATABASE_URL)
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE email = %s", ("normal-user-reviewer-regression@example.com",))
        conn.commit()
        cur.close()
        conn.close()

    # 10. DEV_PREMIUM_MODE/is_creator()/PREMIUM_PREVIEW_KEYの判定が変わらないこと
    def test_dev_premium_mode_and_preview_key_unaffected(self):
        with patch.dict(os.environ, {}, clear=False):
            _clear_reviewer_env()
            os.environ["DEV_PREMIUM_MODE"] = "true"
            try:
                app.DEV_PREMIUM_MODE = True
                with app.app.test_request_context("/"):
                    self.assertTrue(app.is_premium_user())
            finally:
                app.DEV_PREMIUM_MODE = False

        with patch.dict(os.environ, {}, clear=False):
            _clear_reviewer_env()
            os.environ["PREMIUM_PREVIEW_KEY"] = "legacy-preview-key"
            with app.app.test_request_context("/?premium_key=legacy-preview-key"):
                self.assertTrue(app.is_premium_user())
            with app.app.test_request_context("/?premium_key=wrong-value"):
                self.assertFalse(app.is_premium_user())


if __name__ == "__main__":
    unittest.main()
