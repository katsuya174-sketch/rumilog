import os

# Worker timeout: 診断処理(Rakuten+Gemini並列)が最大30s前後かかるため明示的に120sを設定
# 環境変数 TIMEOUT が設定されている場合はそちらを優先するが、最低120sを保証する
timeout = max(120, int(os.environ.get("TIMEOUT", "120")))

workers = int(os.environ.get("WEB_CONCURRENCY", "1"))  # Renderフリープラン512MB対応
bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
