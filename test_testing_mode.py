"""
Google Playクローズドテスト参加者向け Testing Mode(TESTING_MODE)のテスト。

- TESTING_MODE / TESTING_MODE_VERSION_CODES の判定(is_testing_mode_user())単体
- POST /api/v1/testing-mode/nonce, /api/v1/testing-mode/verify のPlay Integrity検証
  (実際のGoogle API呼び出しは_get_play_integrity_service()をモックして再現する。
  Play Integrityの実クレデンシャルが無くても実行できる)
- Testing Mode専用の月次カウンター(can_use_testing_mode_diagnosis/
  increment_testing_mode_usage、既存gemini_usageテーブルを流用)
- TESTING_MODE OFFによる既存セッションの即時失効
- 正規Premium・reviewer-access・premium_subscriptionsへの非干渉

実DBが必要なため、DATABASE_URL(環境変数)でテスト専用DBを指定して実行する。

実行方法:
    DATABASE_URL=postgresql://localhost/rumilog_test python3 -m pytest test_testing_mode.py -v
"""

import hashlib
import os
import unittest
from unittest.mock import patch, MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402  (DATABASE_URL設定後にimportする必要がある)

TEST_VERSION_CODE = 999901  # クローズドテスト対象ビルドを模したダミーversionCode
OTHER_VERSION_CODE = 999902  # 対象外ビルド(例: 一般Production版)を模したversionCode


def _clear_testing_mode_env():
    os.environ.pop("TESTING_MODE", None)
    os.environ.pop("TESTING_MODE_VERSION_CODES", None)


def _truncate_premium_table():
    import psycopg2
    conn = psycopg2.connect(app.DATABASE_URL)
    cur = conn.cursor()
    cur.execute("TRUNCATE premium_subscriptions")
    conn.commit()
    cur.close()
    conn.close()


def _delete_testing_mode_usage_rows():
    import psycopg2
    conn = psycopg2.connect(app.DATABASE_URL)
    cur = conn.cursor()
    cur.execute("DELETE FROM gemini_usage WHERE usage_key LIKE 'testing_mode:%%'")
    conn.commit()
    cur.close()
    conn.close()


def _count_premium_subscriptions_rows():
    import psycopg2
    conn = psycopg2.connect(app.DATABASE_URL)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM premium_subscriptions")
    count = cur.fetchone()[0]
    cur.close()
    conn.close()
    return count


def _valid_verdict(version_code=TEST_VERSION_CODE, request_hash="", package_name=None,
                    app_verdict="PLAY_RECOGNIZED", license_verdict="LICENSED"):
    return {
        "tokenPayloadExternal": {
            "requestDetails": {"requestHash": request_hash},
            "appIntegrity": {
                "appRecognitionVerdict": app_verdict,
                "packageName": package_name or app.TESTING_MODE_PACKAGE_NAME,
                "versionCode": version_code,
            },
            "accountDetails": {"appLicensingVerdict": license_verdict},
        }
    }


def _mock_play_integrity_service(verdict_response):
    """_get_play_integrity_service()の戻り値を模す。
    service.v1().decodeIntegrityToken(packageName=..., body=...).execute() を
    Google API呼び出しの代わりに使う(実クレデンシャル不要)。"""
    service = MagicMock()
    service.v1.return_value.decodeIntegrityToken.return_value.execute.return_value = verdict_response
    return service


class TestingModeSessionUnitTests(unittest.TestCase):
    """is_testing_mode_user()単体の判定(TESTING_MODE/TESTING_MODE_VERSION_CODESの再評価)。"""

    def setUp(self):
        _clear_testing_mode_env()

    # 1. Testing Mode OFF (既定) では、セッションに検証済みマーカーがあってもFalse
    def test_off_returns_false_even_with_verified_session_marker(self):
        with patch.dict(os.environ, {}, clear=False):
            _clear_testing_mode_env()
            os.environ["TESTING_MODE_VERSION_CODES"] = str(TEST_VERSION_CODE)
            with app.app.test_request_context("/"):
                app.flask_session["testing_mode_version_code"] = TEST_VERSION_CODE
                self.assertFalse(app.is_testing_mode_user())

    # セッションマーカー自体が無ければTrueにならない
    # (iOS/Webは Android専用のPlay Integrity SDKを呼べないため、このマーカーが
    #  セッションに書き込まれることは構造的にない。iOS/Web = 「マーカー無し」
    #  であることを保証することが 4/5番の安全性の根拠になる)
    def test_no_session_marker_is_false_even_when_testing_mode_on(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ["TESTING_MODE"] = "true"
            os.environ["TESTING_MODE_VERSION_CODES"] = str(TEST_VERSION_CODE)
            with app.app.test_request_context("/"):
                self.assertFalse(app.is_testing_mode_user())

    # 6. 対象外Androidビルド(許可リストに無いversionCode) → 通常仕様
    def test_version_code_not_in_allowlist_is_false(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ["TESTING_MODE"] = "true"
            os.environ["TESTING_MODE_VERSION_CODES"] = str(TEST_VERSION_CODE)
            with app.app.test_request_context("/"):
                app.flask_session["testing_mode_version_code"] = OTHER_VERSION_CODE
                self.assertFalse(app.is_testing_mode_user())

    # TESTING_MODE_VERSION_CODES未設定(空)ならfail-closed
    def test_empty_allowlist_fails_closed(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ["TESTING_MODE"] = "true"
            os.environ.pop("TESTING_MODE_VERSION_CODES", None)
            with app.app.test_request_context("/"):
                app.flask_session["testing_mode_version_code"] = TEST_VERSION_CODE
                self.assertFalse(app.is_testing_mode_user())

    # 3. Testing Mode ON + 許可versionCode → True
    def test_on_with_allowed_version_code_is_true(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ["TESTING_MODE"] = "true"
            os.environ["TESTING_MODE_VERSION_CODES"] = f"{TEST_VERSION_CODE},{OTHER_VERSION_CODE + 1}"
            with app.app.test_request_context("/"):
                app.flask_session["testing_mode_version_code"] = TEST_VERSION_CODE
                self.assertTrue(app.is_testing_mode_user())

    # 8. Testing Mode OFF後、既存テスト利用者が即通常資格へ戻る(ON→OFF遷移)
    def test_turning_off_invalidates_existing_session_immediately(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ["TESTING_MODE"] = "true"
            os.environ["TESTING_MODE_VERSION_CODES"] = str(TEST_VERSION_CODE)
            with app.app.test_request_context("/"):
                app.flask_session["testing_mode_version_code"] = TEST_VERSION_CODE
                self.assertTrue(app.is_testing_mode_user())

                os.environ["TESTING_MODE"] = "false"
                self.assertFalse(app.is_testing_mode_user())

    # 許可versionCodeそのものを後から許可リストから外した場合も即時失効
    def test_removing_version_code_from_allowlist_invalidates_session(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ["TESTING_MODE"] = "true"
            os.environ["TESTING_MODE_VERSION_CODES"] = str(TEST_VERSION_CODE)
            with app.app.test_request_context("/"):
                app.flask_session["testing_mode_version_code"] = TEST_VERSION_CODE
                self.assertTrue(app.is_testing_mode_user())

                os.environ["TESTING_MODE_VERSION_CODES"] = str(OTHER_VERSION_CODE)
                self.assertFalse(app.is_testing_mode_user())

    # 1/2. Testing Mode OFF(無関係)でも既存の無料/Premium上限定数は変わらない
    def test_existing_limits_unchanged(self):
        self.assertEqual(app.FREE_MONTHLY_LIMIT, 5)
        self.assertEqual(app.PREMIUM_MONTHLY_LIMIT, 30)
        self.assertEqual(app.GLOBAL_MONTHLY_LIMIT, 1000)

    # Testing Modeの月次上限はPremiumと同じ30回
    def test_testing_mode_monthly_limit_matches_premium(self):
        self.assertEqual(app.TESTING_MODE_MONTHLY_LIMIT, app.PREMIUM_MONTHLY_LIMIT)

    # 9. 正規Premiumユーザーはis_testing_modeブランチに入らない(api_create_diagnosisの
    #    実際の分岐式 (not creator) and (not premium) and is_testing_mode_user() を再現)
    def test_real_premium_user_never_enters_testing_mode_branch(self):
        key = app.issue_premium_key_for_google_play("tok_testing_mode_regression", "2099-01-01T00:00:00")
        try:
            with patch.dict(os.environ, {}, clear=False):
                os.environ["TESTING_MODE"] = "true"
                os.environ["TESTING_MODE_VERSION_CODES"] = str(TEST_VERSION_CODE)
                with app.app.test_request_context(f"/?premium_key={key}"):
                    # 万一セッションにテストビルドのマーカーが乗っていたとしても、
                    # 正規Premiumが優先されテスト分岐には入らない。
                    app.flask_session["testing_mode_version_code"] = TEST_VERSION_CODE
                    is_premium = app.is_premium_user()
                    is_testing = (not app.is_creator()) and (not is_premium) and app.is_testing_mode_user()
                    self.assertTrue(is_premium)
                    self.assertFalse(is_testing)
        finally:
            import psycopg2
            conn = psycopg2.connect(app.DATABASE_URL)
            cur = conn.cursor()
            cur.execute("DELETE FROM premium_subscriptions WHERE premium_key = %s", (key,))
            conn.commit()
            cur.close()
            conn.close()

    # 10. reviewer-accessとTesting Modeは完全に独立している
    def test_reviewer_access_and_testing_mode_are_independent(self):
        reviewer_key = "R" * 43
        with patch.dict(os.environ, {}, clear=False):
            os.environ["REVIEWER_ACCESS_KEY"] = reviewer_key
            _clear_testing_mode_env()  # Testing ModeはOFF
            with app.app.test_request_context("/"):
                app.flask_session["reviewer_access_fingerprint"] = app._reviewer_access_fingerprint(reviewer_key)
                # reviewer-accessはis_premium_user()経由でTrueのまま(非干渉)
                self.assertTrue(app.is_premium_user())
                self.assertFalse(app.is_testing_mode_user())

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REVIEWER_ACCESS_KEY", None)
            os.environ["TESTING_MODE"] = "true"
            os.environ["TESTING_MODE_VERSION_CODES"] = str(TEST_VERSION_CODE)
            with app.app.test_request_context("/"):
                app.flask_session["testing_mode_version_code"] = TEST_VERSION_CODE
                # Testing ModeはPremium/reviewerのどちらでもない、独立した第三のOR項
                self.assertFalse(app.is_premium_user())
                self.assertTrue(app.is_testing_mode_user())
                self.assertTrue(app.has_premium_features())


class TestingModeUsageCounterTests(unittest.TestCase):
    """Testing Mode専用の月次カウンター(既存gemini_usageテーブルを流用)。"""

    def setUp(self):
        _delete_testing_mode_usage_rows()

    def tearDown(self):
        _delete_testing_mode_usage_rows()

    # 3. 1回目は許可される
    def test_first_use_is_allowed_and_counted(self):
        user_id = "testing-mode-counter-user-1"
        self.assertTrue(app.can_use_testing_mode_diagnosis(user_id))
        count = app.increment_testing_mode_usage(user_id)
        self.assertEqual(count, 1)
        self.assertEqual(app.get_testing_mode_usage_count(user_id), 1)

    # 7. Testing Mode対象でも31回目は拒否される(月30回を厳守・無制限にしない)
    def test_31st_use_is_rejected(self):
        user_id = "testing-mode-counter-user-2"
        for i in range(app.TESTING_MODE_MONTHLY_LIMIT):
            self.assertTrue(app.can_use_testing_mode_diagnosis(user_id), f"{i + 1}回目は許可されるべき")
            app.increment_testing_mode_usage(user_id)
        self.assertFalse(app.can_use_testing_mode_diagnosis(user_id), "31回目は拒否されるべき")

    # Testing Modeの利用カウントはpremium_subscriptionsに一切触れない
    def test_counter_does_not_touch_premium_subscriptions(self):
        _truncate_premium_table()
        user_id = "testing-mode-counter-user-3"
        app.increment_testing_mode_usage(user_id)
        self.assertEqual(_count_premium_subscriptions_rows(), 0)

    # 別ユーザー間でカウントが混ざらない
    def test_counts_are_isolated_per_user(self):
        app.increment_testing_mode_usage("testing-mode-user-a")
        app.increment_testing_mode_usage("testing-mode-user-a")
        app.increment_testing_mode_usage("testing-mode-user-b")
        self.assertEqual(app.get_testing_mode_usage_count("testing-mode-user-a"), 2)
        self.assertEqual(app.get_testing_mode_usage_count("testing-mode-user-b"), 1)


class TestingModeVerifyEndpointTests(unittest.TestCase):
    """POST /api/v1/testing-mode/nonce, /verify のHTTPエンドポイントテスト
    (Play Integrityの実API呼び出しは_get_play_integrity_serviceをモックする)。"""

    def setUp(self):
        _truncate_premium_table()
        _delete_testing_mode_usage_rows()

    def _issue_nonce(self, client):
        resp = client.post("/api/v1/testing-mode/nonce")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()["nonce"]

    def _verify(self, client, verdict, token="dummy-integrity-token"):
        with patch.object(app, "_get_play_integrity_service",
                           return_value=_mock_play_integrity_service(verdict)):
            return client.post("/api/v1/testing-mode/verify", json={"integrity_token": token})

    # TESTING_MODE OFF時、nonce/verifyどちらも404
    def test_endpoints_disabled_when_testing_mode_off(self):
        with patch.dict(os.environ, {}, clear=False):
            _clear_testing_mode_env()
            client = app.app.test_client()
            resp = client.post("/api/v1/testing-mode/nonce")
            self.assertEqual(resp.status_code, 404)
            resp2 = client.post("/api/v1/testing-mode/verify", json={"integrity_token": "x"})
            self.assertEqual(resp2.status_code, 404)

    # 3. 正しいintegrity token(対象versionCode) → 検証成功し、実際にPremium相当APIで反映される
    def test_valid_token_grants_testing_entitlement_on_real_endpoints(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ["TESTING_MODE"] = "true"
            os.environ["TESTING_MODE_VERSION_CODES"] = str(TEST_VERSION_CODE)
            client = app.app.test_client()
            nonce = self._issue_nonce(client)
            request_hash = hashlib.sha256(nonce.encode()).hexdigest()
            resp = self._verify(client, _valid_verdict(request_hash=request_hash))
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.get_json()["success"])

            session_resp = client.get("/api/v1/auth/session")
            self.assertTrue(session_resp.get_json()["is_premium"])

            history_resp = client.get("/api/v1/history")
            self.assertEqual(history_resp.status_code, 200)
            self.assertTrue(history_resp.get_json()["is_premium"])

            self.assertEqual(_count_premium_subscriptions_rows(), 0)

    # requestHashが一致しない(nonceの横流し・改ざん)場合は拒否
    def test_wrong_request_hash_rejected(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ["TESTING_MODE"] = "true"
            os.environ["TESTING_MODE_VERSION_CODES"] = str(TEST_VERSION_CODE)
            client = app.app.test_client()
            self._issue_nonce(client)  # nonceは発行するが、検証時に無関係なhashを送る
            resp = self._verify(client, _valid_verdict(request_hash="wrong-hash-value"))
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.get_json()["error"]["code"], "INVALID_TOKEN")
            self.assertFalse(client.get("/api/v1/auth/session").get_json()["is_premium"])

    # nonceを発行せずに(または既に使用済みで)verifyを呼ぶと拒否
    def test_verify_without_prior_nonce_rejected(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ["TESTING_MODE"] = "true"
            os.environ["TESTING_MODE_VERSION_CODES"] = str(TEST_VERSION_CODE)
            client = app.app.test_client()
            resp = self._verify(client, _valid_verdict(request_hash="anything"))
            self.assertEqual(resp.status_code, 400)

    # 6. appRecognitionVerdictがPLAY_RECOGNIZED以外(改ざん・非Play配布) → 拒否
    def test_app_not_play_recognized_rejected(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ["TESTING_MODE"] = "true"
            os.environ["TESTING_MODE_VERSION_CODES"] = str(TEST_VERSION_CODE)
            client = app.app.test_client()
            nonce = self._issue_nonce(client)
            request_hash = hashlib.sha256(nonce.encode()).hexdigest()
            verdict = _valid_verdict(request_hash=request_hash, app_verdict="UNRECOGNIZED_VERSION")
            resp = self._verify(client, verdict)
            self.assertEqual(resp.status_code, 400)

    # accountDetails.appLicensingVerdictがLICENSED以外(非Play経由の入手) → 拒否
    def test_not_licensed_rejected(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ["TESTING_MODE"] = "true"
            os.environ["TESTING_MODE_VERSION_CODES"] = str(TEST_VERSION_CODE)
            client = app.app.test_client()
            nonce = self._issue_nonce(client)
            request_hash = hashlib.sha256(nonce.encode()).hexdigest()
            verdict = _valid_verdict(request_hash=request_hash, license_verdict="UNLICENSED")
            resp = self._verify(client, verdict)
            self.assertEqual(resp.status_code, 400)

    # packageNameが一致しない → 拒否
    def test_wrong_package_name_rejected(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ["TESTING_MODE"] = "true"
            os.environ["TESTING_MODE_VERSION_CODES"] = str(TEST_VERSION_CODE)
            client = app.app.test_client()
            nonce = self._issue_nonce(client)
            request_hash = hashlib.sha256(nonce.encode()).hexdigest()
            verdict = _valid_verdict(request_hash=request_hash, package_name="jp.lumilog.app.evil")
            resp = self._verify(client, verdict)
            self.assertEqual(resp.status_code, 400)

    # 6. 対象外Androidビルド(許可リストに無いversionCode) → 拒否・通常仕様のまま
    def test_version_code_outside_allowlist_rejected(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ["TESTING_MODE"] = "true"
            os.environ["TESTING_MODE_VERSION_CODES"] = str(TEST_VERSION_CODE)
            client = app.app.test_client()
            nonce = self._issue_nonce(client)
            request_hash = hashlib.sha256(nonce.encode()).hexdigest()
            verdict = _valid_verdict(version_code=OTHER_VERSION_CODE, request_hash=request_hash)
            resp = self._verify(client, verdict)
            self.assertEqual(resp.status_code, 400)
            self.assertFalse(client.get("/api/v1/auth/session").get_json()["is_premium"])

    # 8. 検証成功後にTESTING_MODEをOFFにすると、同一セッションでも即座に通常仕様へ戻る
    def test_verified_session_reverts_when_testing_mode_turned_off(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ["TESTING_MODE"] = "true"
            os.environ["TESTING_MODE_VERSION_CODES"] = str(TEST_VERSION_CODE)
            client = app.app.test_client()
            nonce = self._issue_nonce(client)
            request_hash = hashlib.sha256(nonce.encode()).hexdigest()
            resp = self._verify(client, _valid_verdict(request_hash=request_hash))
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(client.get("/api/v1/auth/session").get_json()["is_premium"])

            os.environ["TESTING_MODE"] = "false"
            self.assertFalse(client.get("/api/v1/auth/session").get_json()["is_premium"])

    # 11. 検証フロー全体を通しても premium_subscriptions には一切書き込まれない
    def test_full_verify_flow_writes_nothing_to_premium_subscriptions(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ["TESTING_MODE"] = "true"
            os.environ["TESTING_MODE_VERSION_CODES"] = str(TEST_VERSION_CODE)
            client = app.app.test_client()
            nonce = self._issue_nonce(client)
            request_hash = hashlib.sha256(nonce.encode()).hexdigest()
            self._verify(client, _valid_verdict(request_hash=request_hash))
            self.assertEqual(_count_premium_subscriptions_rows(), 0)

    # Play Integrityサービスが利用不可(認証情報未設定等)の場合は503
    def test_service_unavailable_returns_503(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ["TESTING_MODE"] = "true"
            os.environ["TESTING_MODE_VERSION_CODES"] = str(TEST_VERSION_CODE)
            client = app.app.test_client()
            self._issue_nonce(client)
            with patch.object(app, "_get_play_integrity_service", return_value=None):
                resp = client.post("/api/v1/testing-mode/verify", json={"integrity_token": "x"})
            self.assertEqual(resp.status_code, 503)


class TestingModeWebAndIosSafetyTests(unittest.TestCase):
    """4/5番: Web(/lab)・iOSにTesting Modeが波及しないことの構造的な保証。"""

    # 5. run_diagnosis_core()のTesting Mode関連パラメータは呼び出し側が明示的に
    #    渡さない限り常にFalse/Noneで、/lab(Web)はこれらを一切渡していない。
    def test_run_diagnosis_core_defaults_testing_mode_flags_off(self):
        import inspect
        sig = inspect.signature(app.run_diagnosis_core)
        self.assertEqual(sig.parameters["is_testing_mode_flag"].default, False)
        self.assertIsNone(sig.parameters["testing_mode_user_id"].default)

    def test_lab_route_never_passes_testing_mode_flags(self):
        import inspect
        source = inspect.getsource(app.lab_test_function)
        self.assertNotIn("is_testing_mode_flag", source)
        self.assertNotIn("testing_mode_user_id", source)

    # 4. iOS/Webは Android専用のPlay Integrity SDKを呼べないため、
    #    testing_mode_version_codeがセッションに書き込まれることは構造的にない
    #    (= 未検証セッションは常に通常仕様)。
    def test_unverified_session_always_gets_normal_behaviour(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ["TESTING_MODE"] = "true"
            os.environ["TESTING_MODE_VERSION_CODES"] = str(TEST_VERSION_CODE)
            with app.app.test_request_context("/"):
                self.assertFalse(app.is_testing_mode_user())
                self.assertFalse(app.has_premium_features())


class HistoryEntitlementQuotaTests(unittest.TestCase):
    """
    GET /api/v1/history の entitlement_type/monthly_limit/remaining_count。
    既存のget_remaining_free_count/get_remaining_premium_count/
    get_remaining_testing_mode_countをそのまま呼んでいるだけであることを、
    実際のHTTPレスポンスを通して確認する(新規カウントロジックを追加して
    いないことの裏付け)。
    """

    def setUp(self):
        _truncate_premium_table()
        _delete_testing_mode_usage_rows()
        _clear_testing_mode_env()

    def _fresh_ip_headers(self, suffix):
        # free_usage.jsonは実ファイルのため、テストごとに未使用のIPを使い
        # 他テスト・実データと衝突しないようにする(RFC5737 TEST-NET-3)。
        return {"X-Forwarded-For": f"203.0.113.{suffix}"}

    # FREE: limit=5、remaining_countはget_remaining_free_count()と一致
    def test_free_entitlement_reports_limit_5(self):
        client = app.app.test_client()
        headers = self._fresh_ip_headers(11)
        resp = client.get("/api/v1/history", headers=headers)
        body = resp.get_json()
        self.assertEqual(body["entitlement_type"], "free")
        self.assertEqual(body["monthly_limit"], app.FREE_MONTHLY_LIMIT)
        self.assertEqual(body["remaining_count"], app.FREE_MONTHLY_LIMIT)

    # PREMIUM: limit=30、remaining_countはget_remaining_premium_count()と一致
    def test_premium_entitlement_reports_limit_30(self):
        key = app.issue_premium_key_for_google_play("tok_quota_premium_test", "2099-01-01T00:00:00")
        try:
            client = app.app.test_client()
            resp = client.get(f"/api/v1/history?premium_key={key}")
            body = resp.get_json()
            self.assertEqual(body["entitlement_type"], "premium")
            self.assertEqual(body["monthly_limit"], app.PREMIUM_MONTHLY_LIMIT)
            self.assertEqual(body["remaining_count"], app.get_remaining_premium_count(key))
            self.assertEqual(body["remaining_count"], app.PREMIUM_MONTHLY_LIMIT)
        finally:
            import psycopg2
            conn = psycopg2.connect(app.DATABASE_URL)
            cur = conn.cursor()
            cur.execute("DELETE FROM premium_subscriptions WHERE premium_key = %s", (key,))
            conn.commit()
            cur.close()
            conn.close()

    # TESTING MODE: limit=30、remaining_countはget_remaining_testing_mode_count()と一致
    def test_testing_mode_entitlement_reports_limit_30(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ["TESTING_MODE"] = "true"
            os.environ["TESTING_MODE_VERSION_CODES"] = str(TEST_VERSION_CODE)
            client = app.app.test_client()
            nonce_resp = client.post("/api/v1/testing-mode/nonce")
            nonce = nonce_resp.get_json()["nonce"]
            request_hash = hashlib.sha256(nonce.encode()).hexdigest()
            with patch.object(app, "_get_play_integrity_service",
                               return_value=_mock_play_integrity_service(_valid_verdict(request_hash=request_hash))):
                verify_resp = client.post("/api/v1/testing-mode/verify", json={"integrity_token": "dummy"})
            self.assertEqual(verify_resp.status_code, 200)

            resp = client.get("/api/v1/history")
            body = resp.get_json()
            self.assertEqual(body["entitlement_type"], "testing_mode")
            self.assertEqual(body["monthly_limit"], app.TESTING_MODE_MONTHLY_LIMIT)
            self.assertEqual(body["remaining_count"], app.TESTING_MODE_MONTHLY_LIMIT)

            # premium_subscriptionsへは一切書き込まれていないこと
            self.assertEqual(_count_premium_subscriptions_rows(), 0)

    # TESTING MODE OFF後、通常FREEユーザーなら次回取得時からfree表示へ戻る
    def test_testing_mode_off_reverts_to_free_entitlement(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ["TESTING_MODE"] = "true"
            os.environ["TESTING_MODE_VERSION_CODES"] = str(TEST_VERSION_CODE)
            client = app.app.test_client()
            nonce = client.post("/api/v1/testing-mode/nonce").get_json()["nonce"]
            request_hash = hashlib.sha256(nonce.encode()).hexdigest()
            with patch.object(app, "_get_play_integrity_service",
                               return_value=_mock_play_integrity_service(_valid_verdict(request_hash=request_hash))):
                client.post("/api/v1/testing-mode/verify", json={"integrity_token": "dummy"})

            before = client.get("/api/v1/history").get_json()
            self.assertEqual(before["entitlement_type"], "testing_mode")

            os.environ["TESTING_MODE"] = "false"
            after = client.get("/api/v1/history", headers=self._fresh_ip_headers(22)).get_json()
            self.assertEqual(after["entitlement_type"], "free")
            self.assertEqual(after["monthly_limit"], app.FREE_MONTHLY_LIMIT)

    # 既存のTesting Mode履歴挙動(has_premium_features()反映)を壊していないこと
    def test_history_is_premium_field_still_reflects_testing_mode(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ["TESTING_MODE"] = "true"
            os.environ["TESTING_MODE_VERSION_CODES"] = str(TEST_VERSION_CODE)
            client = app.app.test_client()
            nonce = client.post("/api/v1/testing-mode/nonce").get_json()["nonce"]
            request_hash = hashlib.sha256(nonce.encode()).hexdigest()
            with patch.object(app, "_get_play_integrity_service",
                               return_value=_mock_play_integrity_service(_valid_verdict(request_hash=request_hash))):
                client.post("/api/v1/testing-mode/verify", json={"integrity_token": "dummy"})

            body = client.get("/api/v1/history").get_json()
            self.assertTrue(body["is_premium"])


if __name__ == "__main__":
    unittest.main()
