#!/bin/bash
# AntBrowser(HK) 按需批量注册 / 接管半注册邮箱：预检 → 逐个执行 → 等 2FA 落地 → 校验 → 汇总
# 用法:
#   ./scripts/ant_auto_register.sh [数量=1] [最大连续失败=2] [其它参数...]
#   例: ./scripts/ant_auto_register.sh 3              # 注册 3 个，连败 2 次即停
#       ./scripts/ant_auto_register.sh 5 2 --gap 0    # 不等间隔，连续注册 5 个
#       ./scripts/ant_auto_register.sh --preflight    # 只做预检，不提交任务
#       ./scripts/ant_auto_register.sh --takeover --list           # 列出可接管的半注册邮箱
#       ./scripts/ant_auto_register.sh --takeover --all            # 全部接管
#       ./scripts/ant_auto_register.sh --takeover --email user@example.com    # 只接管指定邮箱
#       ./scripts/ant_auto_register.sh --help         # 全部参数
# 前提: 容器在跑；HK 节点 AntBrowser 已启动；REGISTRATION_DRIVER=ant（接管不需要这一条）
# 节奏: 两号间隔默认取 AUTO_REGISTER_INTERVAL（从上一个号 2FA 结束算起），--gap 可覆盖
# 接管: 邮箱池里标 failed 的半注册邮箱（OpenAI 侧已建号，验证码/资料页没走完），
#       用已有密码（没有则用一次性验证码）登录补完后入库，再等自动 2FA
# 退出码: 0 全部成功 / 1 有失败或提前停止 / 2 预检未通过 / 3 容器未运行
# 注意: Ctrl-C 中断本脚本不会打断已提交的注册任务——任务在 WebUI 主进程里继续跑完。
#       接管在本脚本进程里直接跑页面流程，中断会停在当时那一步。
#       每个号结束会输出一行 `RESULT {...}`（JSON）。后台跑:
#       nohup ./scripts/ant_auto_register.sh 5 > /dev/null 2>&1 &   （日志在 logs/ant-auto-register/）
set -u
ROOT=${ROOT:-/opt/turb-gpt-free-register}
CONTAINER=${CONTAINER:-turb-gpt-free-register}

if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -qx true; then
  echo "容器 $CONTAINER 未在运行" >&2
  exit 3
fi

LOGDIR="$ROOT/logs/ant-auto-register"
mkdir -p "$LOGDIR"
TS="$(date +%Y%m%d-%H%M%S)"

if [ "${1:-}" = "--preflight" ]; then
  shift
  docker exec -w /app -e PYTHONPATH=/app -e PYTHONUNBUFFERED=1 "$CONTAINER" \
    python core/ant_auto_register.py preflight "$@"
  exit $?
fi
if [ "${1:-}" = "--takeover" ]; then
  shift
  LOG="$LOGDIR/takeover-$TS.log"
  docker exec -w /app -e PYTHONPATH=/app -e PYTHONUNBUFFERED=1 "$CONTAINER" \
    python core/ant_auto_register.py takeover "$@" 2>&1 | tee "$LOG"
  RC=${PIPESTATUS[0]}
  echo "日志: $LOG"
  exit "$RC"
fi
if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  docker exec -w /app -e PYTHONPATH=/app "$CONTAINER" python core/ant_auto_register.py run --help
  docker exec -w /app -e PYTHONPATH=/app "$CONTAINER" python core/ant_auto_register.py takeover --help
  exit 0
fi

COUNT="${1:-1}"
MAXFAIL="${2:-2}"
if [ $# -gt 2 ]; then shift 2; else shift $#; fi

LOG="$LOGDIR/$TS.log"

docker exec -w /app -e PYTHONPATH=/app -e PYTHONUNBUFFERED=1 "$CONTAINER" \
  python core/ant_auto_register.py run --count "$COUNT" --max-fail "$MAXFAIL" "$@" 2>&1 | tee "$LOG"
RC=${PIPESTATUS[0]}
echo "日志: $LOG"
exit "$RC"
