"""
Stripe課金識別の再設計(email依存廃止 → customer_id/subscription_id正本化)の
自動テスト。実DBが必要なため、ローカルまたはCIで用意したPostgresを
DATABASE_URL(環境変数)で指定して実行する。

実行方法:
    createdb rumilog_test
    DATABASE_URL=postgresql://localhost/rumilog_test python3 -m pytest test_premium_billing.py -v
    (pytestが無い場合: python3 -m unittest test_premium_billing -v)

このファイルはproduction用DATABASE_URL(.env)を書き換えない。実行前に必ず
DATABASE_URLをテスト専用DBへ向けること(未設定時はデフォルトでpostgresql://
localhost/rumilog_testを使う)。
"""

import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402  (DATABASE_URL設定後にimportする必要がある)


def _truncate_premium_table():
    import psycopg2
    conn = psycopg2.connect(app.DATABASE_URL)
    cur = conn.cursor()
    cur.execute("TRUNCATE premium_subscriptions")
    conn.commit()
    cur.close()
    conn.close()


class IssuePremiumKeyTests(unittest.TestCase):
    """issue_premium_key(): 優先順位 subscription_id → customer_id → 新規発行。"""

    def setUp(self):
        _truncate_premium_table()

    def test_new_subscription_creates_key(self):
        key = app.issue_premium_key("User@Example.com", "cus_A", "sub_A")
        self.assertTrue(app.validate_premium_key(key))
        entry = app.find_premium_by_key(key)
        self.assertEqual(entry["email"], "user@example.com")  # 正規化される

    def test_same_subscription_id_extends_same_key(self):
        k1 = app.issue_premium_key("user@example.com", "cus_A", "sub_A")
        k2 = app.issue_premium_key("user@example.com", "cus_A", "sub_A")
        self.assertEqual(k1, k2)

    def test_email_change_does_not_create_duplicate_when_subscription_id_matches(self):
        """根本原因の修正確認: 同じsubscription_idならemail表記が変わっても
        新規行を作らず既存契約を更新する。"""
        k1 = app.issue_premium_key("original@example.com", "cus_A", "sub_A")
        k2 = app.issue_premium_key("totally-different@example.com", "cus_A", "sub_A")
        self.assertEqual(k1, k2, "subscription_idが同じなら同一契約として扱われるべき")
        entry = app.find_premium_by_key(k1)
        self.assertEqual(entry["email"], "totally-different@example.com")

    def test_different_subscription_creates_new_key(self):
        k1 = app.issue_premium_key("a@example.com", "cus_A", "sub_A")
        k2 = app.issue_premium_key("b@example.com", "cus_B", "sub_B")
        self.assertNotEqual(k1, k2)

    def test_revoked_entry_is_not_extended(self):
        k1 = app.issue_premium_key("a@example.com", "cus_A", "sub_A")
        app.revoke_premium_key_by_subscription("sub_A")
        self.assertFalse(app.validate_premium_key(k1))
        k2 = app.issue_premium_key("a@example.com", "cus_A", "sub_A")
        self.assertNotEqual(k1, k2, "revoked済み契約への再発行は新規キーになるべき")

    def test_unique_constraint_prevents_duplicate_active_rows(self):
        """issue_premium_key以外の経路で直接INSERTしようとしても、部分UNIQUE制約が
        同一subscription_idの複数有効行を防ぐことを確認する(競合時の最終防衛線)。"""
        import psycopg2
        app.issue_premium_key("a@example.com", "cus_A", "sub_A")
        conn = psycopg2.connect(app.DATABASE_URL)
        cur = conn.cursor()
        with self.assertRaises(psycopg2.errors.UniqueViolation):
            cur.execute("""
                INSERT INTO premium_subscriptions
                    (premium_key, stripe_customer_id, stripe_subscription_id, revoked)
                VALUES (%s, %s, %s, FALSE)
            """, ("some_other_key", "cus_A", "sub_A"))
        conn.rollback()
        cur.close()
        conn.close()

    def test_unique_constraint_prevents_duplicate_active_customer_rows(self):
        """業務仕様の確定(ユーザー指示): 1 Stripe Customerにつき同時に1つの
        有効なプレミアム契約、という前提をDB制約として固定する。
        同一customer_idで別のsubscription_idの行を作ろうとしても拒否される。"""
        import psycopg2
        app.issue_premium_key("a@example.com", "cus_ONE_ACTIVE", "sub_FIRST")
        conn = psycopg2.connect(app.DATABASE_URL)
        cur = conn.cursor()
        with self.assertRaises(psycopg2.errors.UniqueViolation):
            cur.execute("""
                INSERT INTO premium_subscriptions
                    (premium_key, stripe_customer_id, stripe_subscription_id, revoked)
                VALUES (%s, %s, %s, FALSE)
            """, ("some_other_key", "cus_ONE_ACTIVE", "sub_SECOND"))
        conn.rollback()
        cur.close()
        conn.close()

    def test_customer_id_can_be_reused_after_previous_row_revoked(self):
        """「同時に1つ」であって「生涯で1つ」ではないことの確認: 既存契約が
        revoked済みなら、同じcustomer_idで新しい有効契約を作れる
        (解約後の再契約を妨げない)。"""
        k1 = app.issue_premium_key("a@example.com", "cus_REUSE", "sub_OLD")
        app.revoke_premium_key_by_subscription("sub_OLD")
        k2 = app.issue_premium_key("a@example.com", "cus_REUSE", "sub_NEW")
        self.assertNotEqual(k1, k2)
        self.assertTrue(app.validate_premium_key(k2))


class IssuePremiumKeyInvariantTests(unittest.TestCase):
    """issue_premium_key()はcustomer_id/subscription_idの両方を必須とする
    不変条件を持つ(ユーザー指示: customer-only発行経路が将来誤って追加
    されないよう、入力契約として保証する)。"""

    def setUp(self):
        _truncate_premium_table()

    def test_missing_subscription_id_raises(self):
        with self.assertRaises(ValueError):
            app.issue_premium_key("a@example.com", "cus_A", "")

    def test_missing_customer_id_raises(self):
        with self.assertRaises(ValueError):
            app.issue_premium_key("a@example.com", "", "sub_A")

    def test_missing_both_raises(self):
        with self.assertRaises(ValueError):
            app.issue_premium_key("a@example.com", "", "")

    def test_none_values_raise(self):
        with self.assertRaises(ValueError):
            app.issue_premium_key("a@example.com", None, None)

    def test_checkout_session_completed_without_subscription_does_not_call_issue(self):
        """Webhookハンドラ側の不変条件維持: customer/subscriptionのどちらかが
        欠けたcheckout.session.completedはissue_premium_key()を呼ばずスキップする
        (呼べばValueErrorで500になり、Stripeが無意味な再送を繰り返すため)。"""
        client = app.app.test_client()
        with patch.object(app.stripe.Webhook, "construct_event") as m, \
             patch("app.issue_premium_key") as mock_issue:
            m.return_value = {"type": "checkout.session.completed", "data": {"object": {
                "customer_email": "buyer@example.com",
                "customer_details": {"email": "buyer@example.com"},
                "customer": "",  # 欠落
                "subscription": "sub_X",
            }}}
            resp = client.post("/stripe-webhook", data=b"{}", headers={"Stripe-Signature": "x"})
        self.assertEqual(resp.status_code, 200)
        mock_issue.assert_not_called()


class WebhookHandlerTests(unittest.TestCase):
    def setUp(self):
        _truncate_premium_table()
        self.client = app.app.test_client()

    def _fake_event(self, event_type, obj):
        return {"type": event_type, "data": {"object": obj}}

    def test_checkout_session_completed_issues_key(self):
        with patch.object(app.stripe.Webhook, "construct_event") as m:
            m.return_value = self._fake_event("checkout.session.completed", {
                "customer_email": "buyer@example.com",
                "customer_details": {"email": "buyer@example.com"},
                "customer": "cus_X",
                "subscription": "sub_X",
            })
            resp = self.client.post("/stripe-webhook", data=b"{}",
                                     headers={"Stripe-Signature": "x"})
        self.assertEqual(resp.status_code, 200)
        entry = app.find_premium_by_subscription_id("sub_X")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["email"], "buyer@example.com")

    def test_invoice_payment_succeeded_does_not_corrupt_email_when_missing(self):
        """sg()ヘルパーの修正確認: customer_emailがNoneでも文字列"None"に
        化けず、既存のemailを壊さない。"""
        app.issue_premium_key("buyer@example.com", "cus_X", "sub_X")
        with patch.object(app.stripe.Webhook, "construct_event") as m:
            m.return_value = self._fake_event("invoice.payment_succeeded", {
                "customer_email": None,
                "customer": "cus_X",
                "subscription": "sub_X",
            })
            resp = self.client.post("/stripe-webhook", data=b"{}",
                                     headers={"Stripe-Signature": "x"})
        self.assertEqual(resp.status_code, 200)
        entry = app.find_premium_by_subscription_id("sub_X")
        self.assertEqual(entry["email"], "buyer@example.com")

    def test_customer_updated_syncs_email_only(self):
        """customer.updatedは表示用emailの同期のみ。契約の紐付けには使わない。"""
        app.issue_premium_key("old@example.com", "cus_X", "sub_X")
        with patch.object(app.stripe.Webhook, "construct_event") as m:
            m.return_value = self._fake_event("customer.updated", {
                "id": "cus_X", "email": "NewEmail@Example.com",
            })
            resp = self.client.post("/stripe-webhook", data=b"{}",
                                     headers={"Stripe-Signature": "x"})
        self.assertEqual(resp.status_code, 200)
        entry = app.find_premium_by_subscription_id("sub_X")
        self.assertEqual(entry["email"], "newemail@example.com")
        self.assertEqual(entry["stripe_customer_id"], "cus_X")
        self.assertEqual(entry["stripe_subscription_id"], "sub_X")

    def test_subscription_deleted_revokes(self):
        app.issue_premium_key("a@example.com", "cus_X", "sub_X")
        with patch.object(app.stripe.Webhook, "construct_event") as m:
            m.return_value = self._fake_event("customer.subscription.deleted", {"id": "sub_X"})
            resp = self.client.post("/stripe-webhook", data=b"{}",
                                     headers={"Stripe-Signature": "x"})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(app.find_premium_by_subscription_id("sub_X"))


class PortalByEmailRequestTests(unittest.TestCase):
    """/portal-by-email (POST): メール所有確認リンクの送信のみを行う。
    Stripeへは一切問い合わせない(確認後のconfirmルートでのみ問い合わせる)。"""

    def setUp(self):
        _truncate_premium_table()
        self.client = app.app.test_client()
        app.stripe.api_key = "sk_test_dummy"

    def test_valid_email_shows_sent_state_without_querying_stripe(self):
        with patch.object(app.stripe.Customer, "list") as mock_list:
            resp = self.client.post("/portal-by-email", data={"email": "someone@example.com"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("確認メールを送信しました", resp.get_data(as_text=True))
        mock_list.assert_not_called()  # このハンドラはStripeへ問い合わせない

    def test_creates_a_magic_token_for_the_email(self):
        with patch("app.send_portal_confirmation_email") as mock_send:
            self.client.post("/portal-by-email", data={"email": "someone@example.com"})
        self.assertEqual(mock_send.call_count, 1)
        self.assertEqual(mock_send.call_args.args[0], "someone@example.com")
        token = mock_send.call_args.args[1]
        # トークンは既存のverify_magic_token()で検証できる(専用実装を増やしていない)
        self.assertEqual(app.verify_magic_token(token), "someone@example.com")

    def test_same_response_regardless_of_whether_email_has_a_subscription(self):
        """メールの在否を列挙できないよう、常に同じ「送信しました」文言を返す
        (既存の/api/v1/auth/request-linkと同じプライバシー配慮パターン)。"""
        with patch("app.send_portal_confirmation_email"):
            resp1 = self.client.post("/portal-by-email", data={"email": "exists@example.com"})
            resp2 = self.client.post("/portal-by-email", data={"email": "never-registered@example.com"})
        text1 = resp1.get_data(as_text=True)
        text2 = resp2.get_data(as_text=True)
        self.assertIn("確認メールを送信しました", text1)
        self.assertIn("確認メールを送信しました", text2)

    def test_invalid_email_shows_error(self):
        resp = self.client.post("/portal-by-email", data={"email": "not-an-email"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("メールアドレスを入力してください", resp.get_data(as_text=True))


class PortalByEmailConfirmTests(unittest.TestCase):
    """/portal-by-email/confirm/<token>: 確認済みemailでのみStripeへ問い合わせ、
    今回の不具合(Stripeには有効な契約があるのに見つからない)の直接の修正箇所。"""

    def setUp(self):
        _truncate_premium_table()
        self.client = app.app.test_client()
        app.stripe.api_key = "sk_test_dummy"

    def test_local_db_empty_but_stripe_has_active_subscription_reaches_portal(self):
        """根本原因の再現条件そのもの: ローカルDBに何も無くてもStripeに実在する
        アクティブな契約があれば、メール確認後にポータルへ到達できる。
        premium_keyは自己修復で発行しない(発行は必ずStripe Webhook経由)。"""
        token = app.create_magic_token("recovered@example.com")
        mock_customer = MagicMock(id="cus_LIVE")
        mock_subscription = MagicMock(id="sub_LIVE")
        with patch.object(app.stripe.Customer, "list") as mock_list, \
             patch.object(app.stripe.Subscription, "list") as mock_sub_list, \
             patch.object(app.stripe.billing_portal.Session, "create") as mock_portal:
            mock_list.return_value = MagicMock(data=[mock_customer])
            mock_sub_list.return_value = MagicMock(data=[mock_subscription])
            mock_portal.return_value = MagicMock(url="https://billing.stripe.com/session/xyz")

            resp = self.client.get(f"/portal-by-email/confirm/{token}")

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers.get("Location"), "https://billing.stripe.com/session/xyz")
        self.assertEqual(mock_portal.call_args.kwargs.get("customer"), "cus_LIVE")
        self.assertIsNone(app.find_premium_by_customer_id("cus_LIVE"))

    def test_customer_without_active_subscription_still_reaches_portal(self):
        token = app.create_magic_token("cancelled@example.com")
        mock_customer = MagicMock(id="cus_NO_SUB")
        with patch.object(app.stripe.Customer, "list") as mock_list, \
             patch.object(app.stripe.Subscription, "list") as mock_sub_list, \
             patch.object(app.stripe.billing_portal.Session, "create") as mock_portal:
            mock_list.return_value = MagicMock(data=[mock_customer])
            mock_sub_list.return_value = MagicMock(data=[])
            mock_portal.return_value = MagicMock(url="https://billing.stripe.com/session/no-sub")

            resp = self.client.get(f"/portal-by-email/confirm/{token}")

        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(app.find_premium_by_customer_id("cus_NO_SUB"))

    def test_invalid_token_does_not_query_stripe(self):
        """権限昇格防止の核心: 無効/未確認のトークンではStripeへ一切問い合わせない
        (=攻撃者が他人のメールを入力しただけでは、confirmトークンを持たない限り
        Stripeの契約情報に到達できない)。"""
        with patch.object(app.stripe.Customer, "list") as mock_list:
            resp = self.client.get("/portal-by-email/confirm/invalid-or-expired-token")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("無効または期限切れ", resp.get_data(as_text=True))
        mock_list.assert_not_called()

    def test_token_is_single_use(self):
        """使い捨てトークンであることの確認: 一度confirmで使ったトークンは
        再利用できない(同じリンクを2回踏んでも2回目は無効になる)。"""
        token = app.create_magic_token("once@example.com")
        with patch.object(app.stripe.Customer, "list") as mock_list, \
             patch.object(app.stripe.billing_portal.Session, "create") as mock_portal:
            mock_list.return_value = MagicMock(data=[])
            resp1 = self.client.get(f"/portal-by-email/confirm/{token}")
            resp2 = self.client.get(f"/portal-by-email/confirm/{token}")
        self.assertIn("見つかりませんでした", resp1.get_data(as_text=True))
        self.assertIn("無効または期限切れ", resp2.get_data(as_text=True))

    def test_no_stripe_customer_shows_not_found_error(self):
        token = app.create_magic_token("nobody@example.com")
        with patch.object(app.stripe.Customer, "list") as mock_list:
            mock_list.return_value = MagicMock(data=[])
            resp = self.client.get(f"/portal-by-email/confirm/{token}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("見つかりませんでした", resp.get_data(as_text=True))

    def test_case_insensitive_local_lookup(self):
        app.issue_premium_key("MixedCase@Example.com", "cus_LOCAL", "sub_LOCAL")
        token = app.create_magic_token("mixedcase@example.com")
        with patch.object(app.stripe.Customer, "list") as mock_list, \
             patch.object(app.stripe.billing_portal.Session, "create") as mock_portal:
            mock_list.return_value = MagicMock(data=[])  # Stripe側は空でもローカルで拾える
            mock_portal.return_value = MagicMock(url="https://billing.stripe.com/session/local")
            resp = self.client.get(f"/portal-by-email/confirm/{token}")
        self.assertEqual(resp.status_code, 302)

    def test_attacker_cannot_reach_victim_portal_without_owning_token(self):
        """統合的な権限昇格防止テスト: 攻撃者が被害者のメールで/portal-by-email
        にPOSTしても、攻撃者はconfirmトークンを受け取れない(被害者の受信箱に
        送られるため)ので、Stripeの契約情報にもポータルにも到達できない。"""
        with patch("app.send_portal_confirmation_email") as mock_send, \
             patch.object(app.stripe.Customer, "list") as mock_list:
            resp = self.client.post("/portal-by-email", data={"email": "victim@example.com"})
        self.assertEqual(resp.status_code, 200)
        mock_list.assert_not_called()  # POST自体はStripeに触れない
        self.assertEqual(mock_send.call_count, 1)  # トークンは被害者のメールにのみ送られる
        # 攻撃者はレスポンスからトークンを得られない
        self.assertNotIn(mock_send.call_args.args[1], resp.get_data(as_text=True))


class MigrationTests(unittest.TestCase):
    def setUp(self):
        _truncate_premium_table()
        self.tmp_path = "/tmp/test_premium_keys_migration.json"
        with open(self.tmp_path, "w", encoding="utf-8") as f:
            json.dump({
                "OLD_KEY_1": {
                    "email": "Legacy@Example.com",
                    "stripe_customer_id": "cus_LEGACY",
                    "stripe_subscription_id": "sub_LEGACY",
                    "valid_until": "2099-01-01T00:00:00",
                    "revoked": False,
                    "created_at": "2026-01-01T00:00:00",
                    "monthly_usage": {"2026-08": 2},
                },
            }, f)
        self._orig_file = app.PREMIUM_KEYS_FILE
        app.PREMIUM_KEYS_FILE = self.tmp_path

    def tearDown(self):
        app.PREMIUM_KEYS_FILE = self._orig_file
        if os.path.exists(self.tmp_path):
            os.remove(self.tmp_path)

    def test_migration_imports_legacy_entries(self):
        result = app._migrate_premium_keys_from_json()
        self.assertEqual(result["migrated"], 1)
        entry = app.find_premium_by_key("OLD_KEY_1")
        self.assertEqual(entry["email"], "legacy@example.com")
        self.assertEqual(entry["monthly_usage"], {"2026-08": 2})

    def test_migration_is_idempotent(self):
        app._migrate_premium_keys_from_json()
        result2 = app._migrate_premium_keys_from_json()
        self.assertEqual(result2["migrated"], 1)  # エラーにならず再度成功する

    def test_migration_empty_file_is_noop(self):
        app.PREMIUM_KEYS_FILE = "/tmp/does_not_exist_premium_keys.json"
        result = app._migrate_premium_keys_from_json()
        self.assertEqual(result["migrated"], 0)

    def test_migration_merges_conflicting_state_with_existing_db_row(self):
        """同一premium_keyが既にDBに存在し、JSON側と状態(revoked/manual/
        monthly_usage)が食い違う場合のマージ方針を確認する。
        revoked/manualはOR(安全側)、monthly_usageは月ごとの大きい方を採用する。"""
        # DB側に先に行を作る(revoked=True、2026-08の利用量=5)
        app.issue_premium_key("db-side@example.com", "cus_CONFLICT", "sub_CONFLICT")
        db_key = app.find_premium_by_subscription_id("sub_CONFLICT")
        # find_premium_by_subscription_idはrevoked=FALSEしか返さないため、まず
        # 直接キーを取得してからrevoke状態と利用量を作る。
        import psycopg2
        conn = psycopg2.connect(app.DATABASE_URL)
        cur = conn.cursor()
        cur.execute(
            "SELECT premium_key FROM premium_subscriptions WHERE stripe_subscription_id = %s",
            ("sub_CONFLICT",),
        )
        premium_key = cur.fetchone()[0]
        cur.execute(
            "UPDATE premium_subscriptions SET revoked = TRUE, "
            "monthly_usage = %s WHERE premium_key = %s",
            (json.dumps({"2026-08": 5}), premium_key),
        )
        conn.commit()
        cur.close()
        conn.close()

        # JSON側は revoked=False(古い状態), 2026-08の利用量=2(DBより少ない)
        with open(self.tmp_path, "w", encoding="utf-8") as f:
            json.dump({
                premium_key: {
                    "email": "json-side@example.com",
                    "stripe_customer_id": "cus_CONFLICT",
                    "stripe_subscription_id": "sub_CONFLICT",
                    "valid_until": "2099-01-01T00:00:00",
                    "revoked": False,
                    "manual": True,
                    "created_at": "2026-01-01T00:00:00",
                    "monthly_usage": {"2026-08": 2, "2026-09": 1},
                },
            }, f)

        result = app._migrate_premium_keys_from_json()
        self.assertEqual(result["migrated"], 1)
        self.assertEqual(result["row_errors"], [])

        entry = app.find_premium_by_key(premium_key)
        self.assertTrue(entry["revoked"], "DB側のrevoked=TrueがJSON側のFalseに巻き戻ってはいけない")
        self.assertTrue(entry["manual"])
        self.assertEqual(entry["monthly_usage"]["2026-08"], 5, "DB側の大きい利用量が保持されるべき")
        self.assertEqual(entry["monthly_usage"]["2026-09"], 1, "JSON側にしか無い月はそのまま取り込まれるべき")


class AppleBillingTests(unittest.TestCase):
    """App Store(StoreKit)経由の発行・延長・失効。Stripe経路と同じ耐障害性を持つことを確認する。"""

    def setUp(self):
        _truncate_premium_table()

    def test_new_transaction_creates_key(self):
        key = app.issue_premium_key_for_apple("txn_1", "2099-01-01T00:00:00", email="apple@example.com")
        entry = app.find_premium_by_key(key)
        self.assertEqual(entry["apple_original_transaction_id"], "txn_1")
        self.assertEqual(entry["email"], "apple@example.com")

    def test_same_transaction_extends_same_key(self):
        k1 = app.issue_premium_key_for_apple("txn_1", "2099-01-01T00:00:00")
        k2 = app.issue_premium_key_for_apple("txn_1", "2099-06-01T00:00:00")
        self.assertEqual(k1, k2)
        entry = app.find_premium_by_key(k1)
        self.assertEqual(entry["valid_until"], datetime.fromisoformat("2099-06-01T00:00:00"))

    def test_revoke_by_apple_transaction_revokes_matching_row_only(self):
        """revoke_premium_key_by_apple_transactionは対象のoriginal_transaction_id
        の行だけを失効させ、無関係な行には影響しない。部分UNIQUE制約導入前の
        既存データ等で同一txn_idの行が複数残っているケースにも対応できるよう、
        「1件だけ」ではなく「該当する全行」をUPDATE対象にする実装であることを、
        既にrevoked=TRUEの行(部分UNIQUE制約の対象外なので複数存在しうる)を
        含めて確認する。"""
        import psycopg2
        conn = psycopg2.connect(app.DATABASE_URL)
        cur = conn.cursor()
        # revoked=TRUEの行は部分UNIQUE制約の対象外のため、同一txn_idで複数
        # 存在しうる(制約導入前の失効済み履歴等)。それらも含めて全て
        # revoked=TRUEのままであること、かつ無関係なtxn_idの行には
        # 影響しないことを確認する。
        cur.execute("""
            INSERT INTO premium_subscriptions (premium_key, apple_original_transaction_id, revoked)
            VALUES
                ('legacy_dup_1', 'txn_dup', FALSE),
                ('legacy_dup_2', 'txn_dup', TRUE),
                ('unrelated_key', 'txn_dup_other', FALSE)
        """)
        conn.commit()
        cur.close()
        conn.close()

        app.revoke_premium_key_by_apple_transaction("txn_dup")
        self.assertFalse(app.validate_premium_key("legacy_dup_1"))
        self.assertFalse(app.validate_premium_key("legacy_dup_2"))
        self.assertTrue(app.validate_premium_key("unrelated_key"), "無関係な行は失効させない")

    def test_concurrent_issue_for_same_transaction_does_not_duplicate(self):
        """同一original_transaction_idへの並行発行(Server Notifications再送等)で
        重複行ができないことを確認する(advisory lock + 部分UNIQUE制約)。"""
        import threading
        results = []

        def worker():
            results.append(app.issue_premium_key_for_apple("txn_concurrent", "2099-01-01T00:00:00"))

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(set(results)), 1, f"全スレッドが同じキーを返すべき: {results}")
        import psycopg2
        conn = psycopg2.connect(app.DATABASE_URL)
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM premium_subscriptions WHERE apple_original_transaction_id = %s",
            ("txn_concurrent",),
        )
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        self.assertEqual(count, 1, "並行発行しても行は1件だけであるべき")


class GooglePlayBillingTests(unittest.TestCase):
    """Google Play(Play Billing)経由の発行・延長。Apple/Stripe経路と同じ耐障害性を持つことを確認する。"""

    def setUp(self):
        _truncate_premium_table()

    def test_new_purchase_token_creates_key(self):
        key = app.issue_premium_key_for_google_play(
            "token_1", "2099-01-01T00:00:00", email="android@example.com"
        )
        entry = app.find_premium_by_key(key)
        self.assertEqual(entry["google_play_purchase_token"], "token_1")
        self.assertEqual(entry["email"], "android@example.com")

    def test_same_token_extends_same_key(self):
        """重複検証: 同じpurchase_tokenで2回発行しても同一キーを返し、行は増えない。"""
        k1 = app.issue_premium_key_for_google_play("token_1", "2099-01-01T00:00:00")
        k2 = app.issue_premium_key_for_google_play("token_1", "2099-06-01T00:00:00")
        self.assertEqual(k1, k2)
        entry = app.find_premium_by_key(k1)
        self.assertEqual(entry["valid_until"], datetime.fromisoformat("2099-06-01T00:00:00"))

    def test_revoked_entry_is_not_extended(self):
        k1 = app.issue_premium_key_for_google_play("token_1", "2099-01-01T00:00:00")
        app._update_premium_row(k1, revoked=True)
        k2 = app.issue_premium_key_for_google_play("token_1", "2099-01-01T00:00:00")
        self.assertNotEqual(k1, k2, "revoked済み契約への再発行は新規キーになるべき")

    def test_linked_purchase_token_extends_previous_row_instead_of_duplicating(self):
        """Google Playは解約後の再登録で新しいpurchase_tokenを発行する。
        linkedPurchaseTokenで前の契約に繋がっていれば、新規行ではなく既存契約を延長する。"""
        k1 = app.issue_premium_key_for_google_play("token_old", "2099-01-01T00:00:00")
        k2 = app.issue_premium_key_for_google_play(
            "token_new", "2099-06-01T00:00:00", linked_purchase_token="token_old"
        )
        self.assertEqual(k1, k2, "再購読(linkedPurchaseToken)は既存契約を延長するべき")
        entry = app.find_premium_by_key(k1)
        self.assertEqual(entry["google_play_purchase_token"], "token_new")
        self.assertIsNone(app.find_premium_by_google_play_token("token_old"))

    def test_unrelated_linked_token_creates_new_key(self):
        """linked_purchase_tokenに該当する既存行が無ければ、新規発行になる
        (無関係なtokenを渡しても既存契約を勝手に横取りしない)。"""
        key = app.issue_premium_key_for_google_play(
            "token_fresh", "2099-01-01T00:00:00", linked_purchase_token="token_never_seen"
        )
        self.assertTrue(app.validate_premium_key(key))

    def test_concurrent_issue_for_same_token_does_not_duplicate(self):
        """同一purchase_tokenへの並行発行(クライアントの二重送信・検証の再試行等)で
        重複行ができないことを確認する(advisory lock + 部分UNIQUE制約)。"""
        import threading
        results = []

        def worker():
            results.append(
                app.issue_premium_key_for_google_play("token_concurrent", "2099-01-01T00:00:00")
            )

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(set(results)), 1, f"全スレッドが同じキーを返すべき: {results}")
        import psycopg2
        conn = psycopg2.connect(app.DATABASE_URL)
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM premium_subscriptions WHERE google_play_purchase_token = %s",
            ("token_concurrent",),
        )
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        self.assertEqual(count, 1, "並行発行しても行は1件だけであるべき")


def _google_play_iso(delta_days):
    """Google Play Developer APIが返すRFC3339(UTC, 'Z'終端)形式の時刻文字列を作る。"""
    return (datetime.utcnow() + timedelta(days=delta_days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _google_play_purchase_payload(
    product_id="premium_monthly",
    state="SUBSCRIPTION_STATE_ACTIVE",
    expiry_delta_days=30,
    linked_purchase_token=None,
):
    """purchases.subscriptionsv2.get()のレスポンス形状を模したdict。"""
    payload = {
        "subscriptionState": state,
        "lineItems": [{"productId": product_id, "expiryTime": _google_play_iso(expiry_delta_days)}],
    }
    if linked_purchase_token:
        payload["linkedPurchaseToken"] = linked_purchase_token
    return payload


class _FakeGooglePlayExecutable:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
        return self._result


class _FakeAndroidPublisherService:
    """service.purchases().subscriptionsv2().get(...).execute() の呼び出し鎖を模す。"""

    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error
        self.last_call = None

    def purchases(self):
        return self

    def subscriptionsv2(self):
        return self

    def get(self, packageName=None, token=None):
        self.last_call = {"packageName": packageName, "token": token}
        return _FakeGooglePlayExecutable(self._result, self._error)


class GooglePlayVerifyPurchaseEndpointTests(unittest.TestCase):
    """POST /api/v1/premium/verify-purchase-android。Google Play Developer API自体は
    _get_android_publisher_service()をモックして呼び出さず、レスポンス形状のみ模す
    (Apple版がJWS実署名を要求するため実HTTPで検証しないのと同じ理由)。"""

    def setUp(self):
        _truncate_premium_table()
        self.client = app.app.test_client()
        self._env_patch = patch.dict(os.environ, {
            "GOOGLE_PLAY_PACKAGE_NAME": "jp.lumilog.app",
            "GOOGLE_PLAY_PREMIUM_PRODUCT_ID": "premium_monthly",
        })
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def _post(self, purchase_token="tok_1", product_id="premium_monthly"):
        return self.client.post(
            "/api/v1/premium/verify-purchase-android",
            data={"purchase_token": purchase_token, "product_id": product_id},
        )

    def test_missing_fields_returns_400(self):
        resp = self.client.post("/api/v1/premium/verify-purchase-android", data={})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"]["code"], "INPUT_MISSING")

    def test_wrong_product_id_returns_400_without_calling_google(self):
        with patch("app._get_android_publisher_service") as mock_get_service:
            resp = self._post(product_id="some_other_product")
        mock_get_service.assert_not_called()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"]["code"], "INVALID_TRANSACTION")

    def test_not_configured_returns_503(self):
        with patch("app._get_android_publisher_service", return_value=None):
            resp = self._post()
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["error"]["code"], "NOT_CONFIGURED")

    def test_valid_active_purchase_issues_premium_key(self):
        fake_service = _FakeAndroidPublisherService(result=_google_play_purchase_payload())
        with patch("app._get_android_publisher_service", return_value=fake_service):
            resp = self._post(purchase_token="tok_valid")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["success"])
        self.assertTrue(app.validate_premium_key(body["premium_key"]))
        self.assertEqual(
            fake_service.last_call, {"packageName": "jp.lumilog.app", "token": "tok_valid"}
        )

    def test_duplicate_verification_of_same_token_is_idempotent(self):
        """重複検証: 同じpurchase_tokenで2回検証しても同一premium_keyを返し、行は1件のまま。"""
        fake_service = _FakeAndroidPublisherService(result=_google_play_purchase_payload())
        with patch("app._get_android_publisher_service", return_value=fake_service):
            resp1 = self._post(purchase_token="tok_dup")
            resp2 = self._post(purchase_token="tok_dup")
        key1 = resp1.get_json()["premium_key"]
        key2 = resp2.get_json()["premium_key"]
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(key1, key2)

        import psycopg2
        conn = psycopg2.connect(app.DATABASE_URL)
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM premium_subscriptions WHERE google_play_purchase_token = %s",
            ("tok_dup",),
        )
        self.assertEqual(cur.fetchone()[0], 1)
        cur.close()
        conn.close()

    def test_unexpected_verification_error_returns_503(self):
        """token自体の正当性とは無関係な失敗(ネットワーク断等)は、400ではなく
        503(クライアントがリトライすべきエラー)として区別する。"""
        fake_service = _FakeAndroidPublisherService(error=RuntimeError("network timeout"))
        with patch("app._get_android_publisher_service", return_value=fake_service):
            resp = self._post(purchase_token="tok_network_error")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["error"]["code"], "VERIFICATION_FAILED")
        self.assertFalse(app.find_premium_by_google_play_token("tok_network_error"))

    def test_google_api_http_error_returns_400(self):
        from googleapiclient.errors import HttpError
        fake_http_resp = MagicMock()
        fake_http_resp.status = 400
        fake_http_resp.get.return_value = "application/json"
        error = HttpError(fake_http_resp, b'{"error": {"message": "Invalid Value"}}')
        fake_service = _FakeAndroidPublisherService(error=error)
        with patch("app._get_android_publisher_service", return_value=fake_service):
            resp = self._post(purchase_token="tok_bad_request")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"]["code"], "INVALID_TRANSACTION")

    def test_expired_subscription_returns_400(self):
        payload = _google_play_purchase_payload(
            state="SUBSCRIPTION_STATE_EXPIRED", expiry_delta_days=-1
        )
        fake_service = _FakeAndroidPublisherService(result=payload)
        with patch("app._get_android_publisher_service", return_value=fake_service):
            resp = self._post(purchase_token="tok_expired")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"]["code"], "INVALID_TRANSACTION")

    def test_canceled_but_not_yet_expired_is_still_valid(self):
        """キャンセル済み(自動更新オフ)でも有効期限内ならプレミアムを認める
        (Apple版のrevocationDateなし・expiresDate未到来と同じ扱い)。"""
        payload = _google_play_purchase_payload(
            state="SUBSCRIPTION_STATE_CANCELED", expiry_delta_days=10
        )
        fake_service = _FakeAndroidPublisherService(result=payload)
        with patch("app._get_android_publisher_service", return_value=fake_service):
            resp = self._post(purchase_token="tok_canceled")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["success"])

    def test_on_hold_state_returns_400(self):
        payload = _google_play_purchase_payload(state="SUBSCRIPTION_STATE_ON_HOLD")
        fake_service = _FakeAndroidPublisherService(result=payload)
        with patch("app._get_android_publisher_service", return_value=fake_service):
            resp = self._post(purchase_token="tok_on_hold")
        self.assertEqual(resp.status_code, 400)

    def test_paused_state_returns_400(self):
        payload = _google_play_purchase_payload(state="SUBSCRIPTION_STATE_PAUSED")
        fake_service = _FakeAndroidPublisherService(result=payload)
        with patch("app._get_android_publisher_service", return_value=fake_service):
            resp = self._post(purchase_token="tok_paused")
        self.assertEqual(resp.status_code, 400)

    def test_unknown_future_state_defaults_to_invalid(self):
        """ホワイトリスト方式: 将来Googleが追加する未知のsubscriptionStateも
        デフォルトで拒否する(デナイリストだと新状態を見落とし得るため)。"""
        payload = _google_play_purchase_payload(state="SUBSCRIPTION_STATE_SOMETHING_NEW")
        fake_service = _FakeAndroidPublisherService(result=payload)
        with patch("app._get_android_publisher_service", return_value=fake_service):
            resp = self._post(purchase_token="tok_unknown_state")
        self.assertEqual(resp.status_code, 400)

    def test_product_id_not_in_line_items_returns_400(self):
        payload = _google_play_purchase_payload(product_id="other_product")
        fake_service = _FakeAndroidPublisherService(result=payload)
        with patch("app._get_android_publisher_service", return_value=fake_service):
            resp = self._post(purchase_token="tok_mismatch")
        self.assertEqual(resp.status_code, 400)

    def test_db_failure_returns_500_persist_failed(self):
        fake_service = _FakeAndroidPublisherService(result=_google_play_purchase_payload())
        with patch("app._get_android_publisher_service", return_value=fake_service), \
             patch("app.psycopg2.connect", side_effect=RuntimeError("db down")):
            resp = self._post(purchase_token="tok_db_fail")
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json()["error"]["code"], "PERSIST_FAILED")

    def test_linked_purchase_token_reuses_existing_premium_key_across_verify_calls(self):
        """再購読でpurchase_tokenが変わっても、linkedPurchaseTokenで既存契約を
        延長し、新規のpremium_keyを発行しない。"""
        old_payload = _google_play_purchase_payload()
        fake_service_old = _FakeAndroidPublisherService(result=old_payload)
        with patch("app._get_android_publisher_service", return_value=fake_service_old):
            resp_old = self._post(purchase_token="tok_before_resub")
        key_old = resp_old.get_json()["premium_key"]

        new_payload = _google_play_purchase_payload(linked_purchase_token="tok_before_resub")
        fake_service_new = _FakeAndroidPublisherService(result=new_payload)
        with patch("app._get_android_publisher_service", return_value=fake_service_new):
            resp_new = self._post(purchase_token="tok_after_resub")
        key_new = resp_new.get_json()["premium_key"]

        self.assertEqual(key_old, key_new)


class ConcurrentStripeIssueTests(unittest.TestCase):
    def setUp(self):
        _truncate_premium_table()

    def test_concurrent_issue_for_same_subscription_does_not_duplicate(self):
        """Webhookの再送・並行配信を模した同一subscription_idへの並行issue_premium_key
        呼び出しで、重複行ができないことを確認する。"""
        import threading
        results = []

        def worker():
            results.append(app.issue_premium_key("race@example.com", "cus_RACE", "sub_RACE"))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(set(results)), 1, f"全スレッドが同じキーを返すべき: {results}")


class WriteFailurePropagationTests(unittest.TestCase):
    """DB書き込み失敗が「成功」として握りつぶされないことを確認する。
    Webhookハンドラがこれを受けて非2xxを返し、送信元(Stripe/Apple)の
    自動リトライを受けられることが今回のrequired fixの核心。"""

    def setUp(self):
        _truncate_premium_table()
        self.client = app.app.test_client()

    def test_revoke_by_subscription_failure_propagates(self):
        with patch("app.psycopg2.connect", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                app.revoke_premium_key_by_subscription("sub_X")

    def test_revoke_by_apple_transaction_failure_propagates(self):
        with patch("app.psycopg2.connect", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                app.revoke_premium_key_by_apple_transaction("txn_X")

    def test_issue_premium_key_failure_propagates(self):
        with patch("app.psycopg2.connect", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                app.issue_premium_key("a@example.com", "cus_A", "sub_A")

    def test_webhook_returns_500_when_revoke_fails(self):
        """customer.subscription.deletedの処理でDB失効が失敗したら、
        Webhookハンドラは200ではなく500を返し、Stripeの再送を受けられること。"""
        def fake_event(event_type, obj):
            return {"type": event_type, "data": {"object": obj}}

        with patch.object(app.stripe.Webhook, "construct_event") as m, \
             patch("app.psycopg2.connect", side_effect=RuntimeError("db down")):
            m.return_value = fake_event("customer.subscription.deleted", {"id": "sub_FAIL"})
            resp = self.client.post("/stripe-webhook", data=b"{}",
                                     headers={"Stripe-Signature": "x"})
        self.assertEqual(resp.status_code, 500)


if __name__ == "__main__":
    unittest.main()
