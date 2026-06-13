import os

# Worker timeout: Gemini分析90s×2回リトライを考慮して180sに設定
# 環境変数 TIMEOUT が設定されている場合はそちらを優先するが、最低180sを保証する
timeout = max(180, int(os.environ.get("TIMEOUT", "180")))

workers = int(os.environ.get("WEB_CONCURRENCY", "1"))  # Renderフリープラン512MB対応
bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
