"""
自動テストから実Gemini APIへ到達できないようにする安全装置。

2026-10のインシデント(テストのモック対象ミスで、実Gemini APIへの呼び出しが
複数回発生し、Phase 3の収集費用とは別に未記録のAPI使用が生じた事故)の
再発防止策。app.client.models.generate_content自体をテスト実行中は常に
例外を投げるように差し替え、個別のテストがpatch.object等で明示的に
差し替えた場合のみ実際の(モックされた)呼び出しを許可する。

autouse=Trueのため、全テストに自動的に適用される。個別テストが
unittest.mock.patch(.object)でapp.client.models.generate_content
(またはそれを呼ぶより上位の関数、例: pipeline.call_gemini_for_collection/
app.call_gemini_with_retry)を明示的にモックした場合、そのモックが
この安全装置より優先される(patchのスコープがこのフィクスチャの内側で
開始・終了するため、正しく積み重なる)。
"""

import pytest
from unittest.mock import patch


@pytest.fixture(autouse=True)
def block_real_gemini_api_calls():
    import app

    with patch.object(
        app.client.models,
        "generate_content",
        side_effect=RuntimeError(
            "実Gemini APIへの到達がブロックされました。テストでは "
            "pipeline.call_gemini_for_collection (Phase 3) または "
            "app.call_gemini_with_retry (診断) を明示的にモックしてください。"
            "(2026-10のインシデント再発防止策、conftest.py参照)"
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def block_real_rakuten_api_calls():
    """Step45.9a: 自動テストから実楽天APIへ到達しないようにする安全装置。
    (Step45.9a実装中、reflect前の楽天確認を追加した際にモック漏れのテストが
    実楽天検索APIへ到達したことの再発防止策。)app.pyはrequests.get()で楽天へ
    アクセスするため、requestsの全リクエストが通るSession.requestで楽天の
    ドメイン宛てだけを例外にする。個別テストがapp.fetch_rakuten_candidates等や
    requests.getをpatchした場合はそちらが優先される。"""
    import requests

    original = requests.sessions.Session.request

    def guarded(self, method, url, *args, **kwargs):
        if "rakuten" in str(url).lower():
            raise RuntimeError(
                "実楽天APIへの到達がブロックされました。テストでは app.fetch_rakuten_candidates / "
                "pipeline.resolve_item_code_for_product 等を明示的にモックしてください(conftest.py参照)。"
            )
        return original(self, method, url, *args, **kwargs)

    with patch.object(requests.sessions.Session, "request", guarded):
        yield


@pytest.fixture(autouse=True)
def block_real_citation_page_fetch():
    """Step45.5: citation先ページ取得(citation_verification)が自動テストから
    実DNS・実HTTPへ到達しないようにする安全装置。個別テストがfetch_html/
    _open_url/_getaddrinfoをpatchした場合はそちらが優先される。"""
    import citation_verification

    blocked = RuntimeError(
        "実HTTP/DNSへの到達がブロックされました。テストでは citation_verification."
        "fetch_html / _open_url / _getaddrinfo を明示的にモックしてください(conftest.py参照)。"
    )
    with patch.object(citation_verification, "_getaddrinfo", side_effect=blocked), \
            patch.object(citation_verification, "_open_url", side_effect=blocked):
        yield
