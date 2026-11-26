#!/bin/sh
# 在 NAS 上重建映像並換掉 bot 容器(程式碼更新後用)。ngrok 容器不動。
# 用法:cd /volume1/docker/line-receipt-bot && sudo sh deploy/redeploy.sh
set -e
BASE=/volume1/docker/line-receipt-bot
cd "$BASE"

docker build -f deploy/Dockerfile -t line-receipt-bot .
docker rm -f line-receipt-bot 2>/dev/null || true
docker network inspect line-receipt-net >/dev/null 2>&1 || docker network create line-receipt-net

docker run -d --name line-receipt-bot --restart unless-stopped \
  --network line-receipt-net \
  --env-file "$BASE/deploy/bot.env" \
  -e USERS_FILE=/data/users.json -e TZ=Asia/Taipei \
  -v "$BASE/deploy/data:/data" \
  --health-cmd "python -c \"import urllib.request;urllib.request.urlopen('http://localhost:8000/',timeout=5)\"" \
  --health-interval 30s \
  line-receipt-bot

sleep 8
docker ps --filter name=line-receipt
docker logs line-receipt-bot --tail 8
