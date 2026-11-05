FROM node:20-slim AS node-stage

FROM python:3.12-slim

COPY --from=node-stage /usr/local/bin/node /usr/local/bin/node
COPY --from=node-stage /usr/local/lib/node_modules /usr/local/lib/node_modules

WORKDIR /app

COPY requirements.txt .

# cloakbrowser 钉死 0.6.4：0.7.0 起免费 binary 也要先领 license key，
# 容器重建时 pip 把 >=0.4.10 解析成 0.7.0，注册直接报 LicenseError。
# 0.6.4 直接跑免费 binary(v146)，无需任何 key。拿到 key 后可解除钉死。
RUN pip install --no-cache-dir \
    curl_cffi>=0.15.0 \
    "wreq>=0.13.0,<0.14" \
    pyotp>=2.9.0 \
    flask>=3.0.0 \
    requests>=2.31.0 \
    pycryptodome>=3.23.0 \
    websocket-client>=1.8.0 \
    cryptography>=41 \
    python-dotenv>=1.0.0 \
    PySocks>=1.7.1 \
    "cloakbrowser[geoip]==0.6.4" \
    "playwright>=1.49.0" \
    "playwright-stealth>=2.0.3" \
    "selenium>=4.20.0" && \
    python -m playwright install-deps chromium && \
    pip install --no-cache-dir websockify && \
    apt-get update && \
    apt-get install -y --no-install-recommends xvfb x11vnc novnc wget gnupg ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# 真 Google Chrome。Playwright 自带的是打了补丁的 Chromium，Stripe 风控和发卡行
# 3DS 认得出来，表现为 "unable to authenticate your payment method"。
# 装成 channel=chrome 后 VCC_BROWSER_CHANNEL=chrome 即可切过去。
RUN mkdir -p /etc/apt/keyrings && \
    wget -qO /etc/apt/keyrings/google-chrome.asc \
        https://dl.google.com/linux/linux_signing_key.pub && \
    echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.asc] \
http://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends google-chrome-stable && \
    rm -rf /var/lib/apt/lists/*

# 卸掉 Linux 独有的中文字体。浏览器对外伪装成 Windows 10，而字体枚举
# （按测宽，不是 fonts.check——那个对任何字体都返回 true）能真实探到
# WenQuanYi Zen Hei，一台 Windows 机器不该有它。放在 Chrome 之后执行，
# 否则会被 google-chrome-stable 的依赖重新拉回来。
RUN apt-get remove -y fonts-wqy-zenhei 2>/dev/null; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*; \
    fc-cache -fv >/dev/null 2>&1 || true

# 浏览器装进已挂载的 cache 目录，docker restart 后不丢。
# /root/.cache 没有挂载，装那里重启即失效。
ENV PLAYWRIGHT_BROWSERS_PATH=/app/cache/ms-playwright
# 同理，CloakBrowser 的 license.key 和内核也要落在挂载的 cache 里，
# 否则容器重建后 cloakbrowser login 的结果就没了。
ENV CLOAKBROWSER_CACHE_DIR=/app/cache/cloakbrowser

COPY . .

RUN mkdir -p /app/data /app/logs /app/run /app/cache

EXPOSE 5000

ENV PYTHONUNBUFFERED=1
ENV NODE_EXECUTABLE=/usr/local/bin/node

CMD ["python", "web.py", "--host", "0.0.0.0", "--port", "5000"]
