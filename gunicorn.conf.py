import os

# Render環境変数から読み取り
# Renderダッシュボードで TIMEOUT=120, WEB_CONCURRENCY=2 を設定すること
timeout = int(os.environ.get("TIMEOUT", "120"))
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"

# 診断処理（Rakuten+Gemini並列）が30s前後かかるため120sに設定
# workers=2: Renderフリープラン(512MB RAM)で安定稼働する最大数
