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
