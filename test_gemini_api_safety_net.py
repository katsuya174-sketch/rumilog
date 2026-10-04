"""
conftest.pyの自動テスト安全装置(実Gemini APIへ到達できないようにする
autouseフィクスチャ)自体のテスト。2026-10のインシデント再発防止策。
"""

import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/rumilog_test")

import app  # noqa: E402


class GeminiApiSafetyNetTests(unittest.TestCase):
    def test_unmocked_generate_content_call_is_blocked(self):
        """
        conftest.pyのautouseフィクスチャにより、何もモックしなければ
        app.client.models.generate_content の呼び出しは必ず例外になる
        (実APIへ到達しない)ことを確認する。
        """
        with self.assertRaises(RuntimeError) as ctx:
            app.client.models.generate_content(model="gemini-3.1-flash-lite", contents="test")
        self.assertIn("実Gemini APIへの到達がブロックされました", str(ctx.exception))

    def test_explicit_local_mock_overrides_the_safety_net(self):
        """
        テストが明示的にpatch.objectで差し替えた場合は、その場では実際に
        (モックされた)呼び出しが成功する(安全装置が過剰に汎用モックまで
        ブロックしないことを確認する)。
        """
        from unittest.mock import patch, MagicMock
        fake_response = MagicMock()
        fake_response.text = "ok"
        with patch.object(app.client.models, "generate_content", return_value=fake_response):
            result = app.client.models.generate_content(model="gemini-3.1-flash-lite", contents="test")
        self.assertEqual(result.text, "ok")

    def test_safety_net_restored_after_local_mock_exits(self):
        """ローカルのモックがwithブロックを抜けた後は、再び安全装置(例外)に
        戻ることを確認する(モックの後片付けが正しく行われていること)。"""
        from unittest.mock import patch, MagicMock
        fake_response = MagicMock()
        fake_response.text = "ok"
        with patch.object(app.client.models, "generate_content", return_value=fake_response):
            app.client.models.generate_content(model="gemini-3.1-flash-lite", contents="test")

        with self.assertRaises(RuntimeError):
            app.client.models.generate_content(model="gemini-3.1-flash-lite", contents="test")


if __name__ == "__main__":
    unittest.main()
