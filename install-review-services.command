#!/bin/zsh
set -e

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"
REVIEW_PORT="${KUAIMAI_REVIEW_PORT:-8787}"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
ASCII_PROJECT_DIR="$HOME/.kuaimai-review-launchd"
CENTER_LABEL="com.kuaimai.review-center"
TUNNEL_LABEL="com.kuaimai.review-tunnel"
CENTER_PLIST="$LAUNCH_AGENTS_DIR/${CENTER_LABEL}.plist"
TUNNEL_PLIST="$LAUNCH_AGENTS_DIR/${TUNNEL_LABEL}.plist"
GUI_DOMAIN="gui/$(id -u)"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "尚未创建 Python 环境。请先双击 run-review-center.command 完成首次初始化。"
  exit 1
fi
if ! .venv/bin/python -m local_review.cli has-users >/dev/null 2>&1; then
  echo "审核账号尚未初始化。请先双击 run-review-center.command 设置管理员密码。"
  exit 1
fi

mkdir -p "$LAUNCH_AGENTS_DIR" "$SCRIPT_DIR/.local-state/review-center"

CANONICAL_SCRIPT_DIR="$(cd "$SCRIPT_DIR" && pwd -P)"
if [[ "$CANONICAL_SCRIPT_DIR" == "$HOME/Desktop/"* ]]; then
  launchctl bootout "$GUI_DOMAIN/$CENTER_LABEL" >/dev/null 2>&1 || true
  launchctl bootout "$GUI_DOMAIN/$TUNNEL_LABEL" >/dev/null 2>&1 || true
  rm -f "$CENTER_PLIST" "$TUNNEL_PLIST"
  echo "macOS 会限制后台 LaunchAgent 访问 Desktop 内的项目目录。请先将整个项目移到 ~/Projects 等非受保护目录，再运行本安装脚本。" >&2
  exit 1
fi
if [[ -L "$ASCII_PROJECT_DIR" ]]; then
  rm "$ASCII_PROJECT_DIR"
elif [[ -e "$ASCII_PROJECT_DIR" && ! -d "$ASCII_PROJECT_DIR" ]]; then
  echo "$ASCII_PROJECT_DIR 已存在且不是目录，请先检查后再安装。" >&2
  exit 1
fi
mkdir -p "$ASCII_PROJECT_DIR"
for entrypoint in run-review-center.command run-review-tunnel.command; do
  ln -f "$CANONICAL_SCRIPT_DIR/$entrypoint" "$ASCII_PROJECT_DIR/$entrypoint"
done

write_plist() {
  local output="$1" label="$2" entrypoint="$3" stdout_path="$4" stderr_path="$5"
  python3 - "$output" "$label" "$ASCII_PROJECT_DIR" "$CANONICAL_SCRIPT_DIR" "$entrypoint" "$REVIEW_PORT" "$stdout_path" "$stderr_path" <<'PY'
import plistlib
import sys
from pathlib import Path

output, label, launcher_dir, project_dir, entrypoint, port, stdout_path, stderr_path = sys.argv[1:]
plist = {
    "Label": label,
    "ProgramArguments": [str(Path(launcher_dir) / entrypoint)],
    "WorkingDirectory": launcher_dir,
    "EnvironmentVariables": {
        "KUAIMAI_REVIEW_LAUNCHD": "1",
        "KUAIMAI_REVIEW_OPEN_BROWSER": "0",
        "KUAIMAI_REVIEW_PORT": port,
        "KUAIMAI_REVIEW_PROJECT_DIR": project_dir,
        "LANG": "en_US.UTF-8",
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    },
    "RunAtLoad": True,
    "KeepAlive": {"SuccessfulExit": False},
    "ThrottleInterval": 5,
    "StandardOutPath": str(Path(project_dir) / stdout_path),
    "StandardErrorPath": str(Path(project_dir) / stderr_path),
}
with open(output, "wb") as handle:
    plistlib.dump(plist, handle, sort_keys=False)
PY
}

write_plist "$CENTER_PLIST" "$CENTER_LABEL" "run-review-center.command" \
  ".local-state/review-center/launchd-center.log" ".local-state/review-center/launchd-center.error.log"
write_plist "$TUNNEL_PLIST" "$TUNNEL_LABEL" "run-review-tunnel.command" \
  ".local-state/review-center/launchd-tunnel.log" ".local-state/review-center/launchd-tunnel.error.log"

launchctl bootout "$GUI_DOMAIN/$CENTER_LABEL" >/dev/null 2>&1 || true
launchctl bootout "$GUI_DOMAIN/$TUNNEL_LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "$GUI_DOMAIN" "$CENTER_PLIST"

echo "正在等待本地审核中心启动……"
for _ in {1..30}; do
  if curl --fail --silent --max-time 1 "http://127.0.0.1:${REVIEW_PORT}/health" >/dev/null; then
    break
  fi
  sleep 1
done
if ! curl --fail --silent --max-time 2 "http://127.0.0.1:${REVIEW_PORT}/health" >/dev/null; then
  echo "审核中心未能启动，请查看 .local-state/review-center/launchd-center.error.log。" >&2
  exit 1
fi

launchctl bootstrap "$GUI_DOMAIN" "$TUNNEL_PLIST"
open "http://127.0.0.1:${REVIEW_PORT}/" >/dev/null 2>&1 &
echo "已安装并启动 macOS 持久化服务："
echo "  本地审核中心：${CENTER_LABEL}"
echo "  Cloudflare Tunnel：${TUNNEL_LABEL}"
echo "服务会在登录时自动启动，异常退出后自动重启；日志在 .local-state/review-center/。"
