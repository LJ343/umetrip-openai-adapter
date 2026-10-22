#!/usr/bin/env bash
# 一键安装 / 更新：创建虚拟环境、安装依赖、注册 systemd 服务并启动
# 用法：把项目解压到 /opt/openai-proxy 后执行  sudo bash /opt/openai-proxy/scripts/install.sh
# 重复执行即为更新（不会覆盖已有的 .env）
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE=openai-proxy
RUN_USER=openai-proxy
PIP_INDEX_URL="${PIP_INDEX_URL:-https://mirrors.cloud.tencent.com/pypi/simple}"

[ "$(id -u)" = 0 ] || { echo "请用 sudo 运行：sudo bash $0"; exit 1; }
cd "$APP_DIR"

echo "==> 1/5 安装 Python"
if command -v apt-get >/dev/null; then
  apt-get update -y -q && apt-get install -y -q python3 python3-venv python3-pip curl
elif command -v dnf >/dev/null; then
  dnf install -y -q python3 python3-pip curl
elif command -v yum >/dev/null; then
  yum install -y -q python3 python3-pip curl
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || { echo "需要 Python 3.9 及以上，当前为 $(python3 -V)。建议使用 Ubuntu 22.04 镜像"; exit 1; }

echo "==> 2/5 创建虚拟环境并安装依赖（PyPI 镜像：$PIP_INDEX_URL）"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q -i "$PIP_INDEX_URL" --upgrade pip
.venv/bin/pip install -q -i "$PIP_INDEX_URL" -r requirements.txt

echo "==> 3/5 准备运行用户和配置"
id "$RUN_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$RUN_USER"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "    已生成 .env（默认配置），稍后可按需修改"
fi
chown root:"$RUN_USER" .env && chmod 640 .env
chown -R root:root app requirements.txt .venv
PORT=$(grep -E '^PORT=' .env | tail -1 | cut -d= -f2); PORT=${PORT:-8000}

echo "==> 4/5 注册 systemd 服务"
cat > /etc/systemd/system/${SERVICE}.service <<EOF
[Unit]
Description=OpenAI compatible proxy
After=network-online.target
Wants=network-online.target

[Service]
User=${RUN_USER}
Group=${RUN_USER}
WorkingDirectory=${APP_DIR}
Environment=HOST=0.0.0.0 PORT=8000 WORKERS=2
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/.venv/bin/uvicorn app.main:app --host \${HOST} --port \${PORT} --workers \${WORKERS} --no-access-log --timeout-keep-alive 75
Restart=always
RestartSec=3
LimitNOFILE=65535
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true
LogsDirectory=openai-proxy
LogsDirectoryMode=0700

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"

# 系统防火墙开着的话放行端口（腾讯云控制台的防火墙/安全组仍需另外放行）
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow "${PORT}/tcp" >/dev/null && echo "    ufw 已放行 ${PORT}/tcp"
elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port="${PORT}/tcp" >/dev/null && firewall-cmd --reload >/dev/null && echo "    firewalld 已放行 ${PORT}/tcp"
fi

echo "==> 5/5 检查服务"
for _ in $(seq 1 15); do
  curl -sf "http://127.0.0.1:${PORT}/healthz" >/dev/null && break
  sleep 1
done
if curl -sf "http://127.0.0.1:${PORT}/healthz" >/dev/null; then
  IP=$(curl -s -m 5 https://ifconfig.me || echo "服务器公网IP")
  echo
  echo "✅ 服务已启动"
  echo "   BaseURL：http://${IP}:${PORT}/v1/chat/completions"
  echo "   查看日志：journalctl -u ${SERVICE} -f"
else
  echo "❌ 服务没有正常启动，查看日志：journalctl -u ${SERVICE} -n 100 --no-pager"
  exit 1
fi
