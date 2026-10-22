#!/usr/bin/env bash
# 在云服务器上检查能否访问上游接口（部署前先跑一次）
# 用法：bash scripts/check_upstream.sh sk-你的上游apiKey
set -u
KEY="${1:?用法: bash scripts/check_upstream.sh <上游apiKey>}"
URL="${UPSTREAM_URL:-https://grayassistant.umetrip.com/gateway/api/web/sse/v1/chat/completions}"

echo "本机出口 IP：$(curl -s -m 5 https://ifconfig.me || echo 未知)"
echo "检查上游：$URL"
code=$(curl -sS -o /tmp/upstream_check.txt -w '%{http_code}' -m 60 -X POST "$URL" \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $KEY" \
  -d '{"model":"qianrang","messages":[{"role":"user","content":"hi"}],"stream":true}')
echo "HTTP 状态码：$code"
head -c 600 /tmp/upstream_check.txt; echo
if [ "$code" = "200" ] && grep -q 'chat.completion.chunk' /tmp/upstream_check.txt; then
  echo "✅ 上游可访问"
else
  echo "❌ 上游不可访问或返回异常：检查网络、IP 白名单（把上面的出口 IP 发给接口方加白）或 apiKey"
  exit 1
fi
