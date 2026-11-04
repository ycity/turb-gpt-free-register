#!/usr/bin/env bash
# 安全重启 WebUI 容器：有后台任务在跑时拒绝重启。
#
# 为什么需要它：2FA、查活、支付、注册都跑在后台线程，docker restart 会直接
# 把它们打断。只看注册任务不够——曾经漏掉正在跑的 2FA（账号 #42），也曾因为
# 脚本只打印 busy 却继续执行而打断注册任务 #113。
#
# 用法：
#   bash scripts/safe-restart.sh           # busy 时退出码 10，不重启
#   bash scripts/safe-restart.sh --force   # 明确要求打断（会先列出在跑的任务）
#   bash scripts/safe-restart.sh --wait 600  # 最多等 600 秒直到空闲
set -u

APP_DIR=${APP_DIR:-/opt/turb-gpt-free-register}
CONTAINER=${CONTAINER:-turb-gpt-free-register}
BASE=${BASE:-http://127.0.0.1:5002}

FORCE=0
WAIT_SECONDS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --wait) WAIT_SECONDS=${2:-0}; shift 2 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

cd "$APP_DIR" || { echo "找不到 $APP_DIR" >&2; exit 2; }
CODE=$(grep -E '^WEBUI_AUTH_CODE=' .env | cut -d= -f2- | tr -d '"'"'"' \r')

busy_json() {
  curl -s --compressed --max-time 30 -H "X-Auth-Code: ${CODE}" "${BASE}/api/busy"
}

# 输出 busy 标志；接口不可用时输出 unknown，由调用方决定。
busy_flag() {
  busy_json | python3 -c "
import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    print('unknown'); raise SystemExit
print('busy' if d.get('busy') else 'idle')
" 2>/dev/null || echo unknown
}

show_detail() {
  busy_json | python3 -c "
import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    print('  (无法解析 /api/busy 响应)'); raise SystemExit
for k, v in sorted((d.get('detail') or {}).items()):
    if v not in (0, None, ''):
        print('  {}: {}'.format(k, v))
" 2>/dev/null
}

STATE=$(busy_flag)

if [ "$STATE" = busy ] && [ "$WAIT_SECONDS" -gt 0 ]; then
  echo "有后台任务在跑，最多等 ${WAIT_SECONDS}s："
  show_detail
  WAITED=0
  while [ "$WAITED" -lt "$WAIT_SECONDS" ]; do
    sleep 10
    WAITED=$((WAITED + 10))
    STATE=$(busy_flag)
    [ "$STATE" = busy ] || break
    [ $((WAITED % 60)) -eq 0 ] && echo "  已等 ${WAITED}s，仍在跑…"
  done
fi

case "$STATE" in
  busy)
    echo "拒绝重启：有后台任务正在运行" >&2
    show_detail >&2
    if [ "$FORCE" != 1 ]; then
      echo "如确实要打断，重跑并加 --force；或用 --wait <秒> 等它结束。" >&2
      exit 10
    fi
    echo "--force 已指定，继续重启（上述任务会被打断）" >&2
    ;;
  unknown)
    # 查不到状态时不能假定空闲：容器可能已经挂了，也可能只是接口超时。
    if [ "$FORCE" != 1 ]; then
      echo "无法读取 /api/busy（容器未起或接口异常）。确认无妨后用 --force 重启。" >&2
      exit 11
    fi
    echo "无法读取 /api/busy，--force 已指定，继续重启" >&2
    ;;
  idle)
    echo "当前空闲，可以安全重启"
    ;;
esac

docker restart "$CONTAINER" >/dev/null || { echo "docker restart 失败" >&2; exit 1; }
echo restarted

for i in $(seq 1 60); do
  H=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H "X-Auth-Code: ${CODE}" "${BASE}/api/summary" || true)
  if [ "$H" = "200" ]; then
    echo "ready after ${i}s"
    exit 0
  fi
  sleep 1
done
echo "重启后 60s 内未就绪，请检查容器日志" >&2
exit 1
