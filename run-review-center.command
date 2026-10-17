#!/bin/zsh
set -e

SCRIPT_DIR="${KUAIMAI_REVIEW_PROJECT_DIR:-${0:h}}"
cd "$SCRIPT_DIR"
REVIEW_PORT="${KUAIMAI_REVIEW_PORT:-8787}"
LAUNCHD_MODE="${KUAIMAI_REVIEW_LAUNCHD:-0}"
OPEN_BROWSER="${KUAIMAI_REVIEW_OPEN_BROWSER:-1}"

# Starting the command twice used to launch a second uvicorn process. The
# second process then exited with EADDRINUSE even though the first service was
# still usable, which looked like a spontaneous service interruption. Reuse a
# healthy local instance and fail early for an unrelated port owner.
SERVICE_URL="http://127.0.0.1:${REVIEW_PORT}"
if existing_health="$(curl -sS --max-time 1 "${SERVICE_URL}/health" 2>/dev/null)"; then
  if [[ "$existing_health" == *"kuaimai-local-review"* ]]; then
    echo "商品工作台已在运行，复用现有服务：${SERVICE_URL}"
    if [[ "$OPEN_BROWSER" == "1" ]]; then
      open "${SERVICE_URL}/" >/dev/null 2>&1 &
    fi
    exit 0
  fi
  echo "端口 ${REVIEW_PORT} 已被其他服务占用，未启动商品工作台。"
  exit 1
fi

if [[ ! -x ".venv/bin/python" ]]; then
  echo "首次运行：正在创建 Python 环境……"
  python3 -m venv .venv
fi

echo "正在检查商品工作台与审核中心依赖……"
if [[ "$LAUNCHD_MODE" != "1" ]]; then
  .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
fi

KEYCHAIN_SERVICE="kuaimai-review-device-token"
KEYCHAIN_ACCOUNT="$(id -un)"
DEVICE_TOKEN="$(security find-generic-password -a "$KEYCHAIN_ACCOUNT" -s "$KEYCHAIN_SERVICE" -w 2>/dev/null || true)"
if [[ -z "$DEVICE_TOKEN" ]]; then
  DEVICE_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(48))')"
  security add-generic-password -U -a "$KEYCHAIN_ACCOUNT" -s "$KEYCHAIN_SERVICE" -w "$DEVICE_TOKEN" >/dev/null
  echo "已在 macOS 钥匙串创建设备令牌（不会写入项目文件或日志）。"
fi

export KUAIMAI_REVIEW_DEVICE_TOKEN="$DEVICE_TOKEN"
export KUAIMAI_LEARNING_DEVICE_TOKEN="$DEVICE_TOKEN"
unset DEVICE_TOKEN

if ! .venv/bin/python -m local_review.cli has-users; then
  if [[ "$LAUNCHD_MODE" == "1" ]]; then
    echo "审核账号尚未初始化；请先手动运行 run-review-center.command 完成首次初始化。" >&2
    exit 1
  fi
  echo "首次启动需要创建审核网页登录账号。"
  .venv/bin/python -m local_review.cli init-admin --username admin
fi

.venv/bin/python -m local_review.cli backup
echo "商品工作台与审核中心：${SERVICE_URL}"
if [[ "$LAUNCHD_MODE" != "1" ]]; then
  echo "手动模式：保持本窗口运行；关闭窗口后运营网页会暂时离线。"
fi
if [[ "$OPEN_BROWSER" == "1" ]]; then
  (sleep 2; open "${SERVICE_URL}/") &
fi
exec .venv/bin/python -m uvicorn local_review.app:app \
  --host 127.0.0.1 --port "$REVIEW_PORT" --proxy-headers --forwarded-allow-ips 127.0.0.1
