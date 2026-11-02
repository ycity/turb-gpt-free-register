#!/bin/sh
# 容器入口：先拉起虚拟显示，再启动 WebUI。
#
# Xvfb 必须和 Flask 一起留在前台。写成 `Xvfb & exec python` 的话，shell 被
# exec 替换后 Xvfb 失去父进程直接退出，只在 /tmp 留下一个连不上的 socket，
# 之后有头 Chrome 启动就会报 "Missing X server or $DISPLAY"。
set -e

DISPLAY_NUM="${DISPLAY_NUM:-99}"
SCREEN="${XVFB_SCREEN:-1280x1024x24}"
SOCK="/tmp/.X11-unix/X${DISPLAY_NUM}"

# 清掉上次异常退出留下的残骸。只删 socket 不够：Xvfb 还会检查 /tmp/.X<N>-lock，
# lock 还在就直接报 "Server is already active" 退出，于是重启后没有 X 可用。
rm -f "$SOCK" "/tmp/.X${DISPLAY_NUM}-lock"

Xvfb ":${DISPLAY_NUM}" -screen 0 "$SCREEN" -nolisten tcp &
XVFB_PID=$!

cleanup() {
    kill "$XVFB_PID" 2>/dev/null || true
    kill "$WEB_PID" 2>/dev/null || true
    kill "$VNC_PID" 2>/dev/null || true
    kill "$WSK_PID" 2>/dev/null || true
}
trap cleanup TERM INT

# 等 X 真正就绪再放行，否则先到的浏览器请求会失败
i=0
while [ ! -S "$SOCK" ] && [ "$i" -lt 50 ]; do
    sleep 0.2
    i=$((i + 1))
done
if [ -S "$SOCK" ]; then
    echo "[entrypoint] Xvfb ready on :${DISPLAY_NUM} (pid ${XVFB_PID})"
else
    echo "[entrypoint] WARNING: Xvfb socket not ready, headed browser will fail" >&2
fi

# 远程观看/操作支付页面。人机验证检测鼠标轨迹，截图回传那套跟不上，
# 所以把真实画面通过 VNC 推出去，让人直接操作这个浏览器。
if [ "${ENABLE_VNC:-1}" = "1" ] && command -v x11vnc >/dev/null 2>&1; then
    x11vnc -display ":${DISPLAY_NUM}" -rfbport 5900 -localhost -nopw \
           -forever -shared -noxdamage -quiet >/tmp/x11vnc.log 2>&1 &
    VNC_PID=$!
    # 等 x11vnc 真正监听再起 websockify：固定 sleep 不够，
    # x11vnc 要先连上 X，这期间 websockify 连 5900 会直接失败退出。
    i=0
    while [ "$i" -lt 40 ]; do
        if python3 -c "
import socket, sys
s = socket.socket(); s.settimeout(0.5)
try:
    s.connect(('127.0.0.1', 5900))
except Exception:
    sys.exit(1)
finally:
    s.close()
" 2>/dev/null; then
            break
        fi
        sleep 0.25
        i=$((i + 1))
    done
    if [ "$i" -lt 40 ]; then
        echo "[entrypoint] x11vnc ready on :5900"
        if [ -d /usr/share/novnc ]; then
            # 必须绑 0.0.0.0：只绑容器内回环的话，docker 端口映射转不进来
            python3 -m websockify --web /usr/share/novnc 0.0.0.0:6080 localhost:5900 \
                >/tmp/websockify.log 2>&1 &
            WSK_PID=$!
            echo "[entrypoint] noVNC ready on :6080"
        fi
    else
        echo "[entrypoint] WARNING: x11vnc did not come up, remote view unavailable" >&2
    fi
fi

python web.py --host 0.0.0.0 --port 5000 &
WEB_PID=$!
wait "$WEB_PID"
