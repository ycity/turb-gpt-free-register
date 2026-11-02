FROM node:20-slim AS node-stage

FROM python:3.12-slim

COPY --from=node-stage /usr/local/bin/node /usr/local/bin/node
COPY --from=node-stage /usr/local/lib/node_modules /usr/local/lib/node_modules

WORKDIR /app

COPY requirements.txt .

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
    "cloakbrowser[geoip]>=0.4.10" \
    "playwright>=1.49.0" \
    "playwright-stealth>=2.0.3" \
    "selenium>=4.20.0" && \
    python -m playwright install-deps chromium && \
    pip install --no-cache-dir websockify && \
    apt-get update && \
    apt-get install -y --no-install-recommends xvfb x11vnc novnc && \
    rm -rf /var/lib/apt/lists/*

# 浏览器装进已挂载的 cache 目录，docker restart 后不丢。
# /root/.cache 没有挂载，装那里重启即失效。
ENV PLAYWRIGHT_BROWSERS_PATH=/app/cache/ms-playwright

COPY . .

RUN mkdir -p /app/data /app/logs /app/run /app/cache

EXPOSE 5000

ENV PYTHONUNBUFFERED=1
ENV NODE_EXECUTABLE=/usr/local/bin/node

CMD ["python", "web.py", "--host", "0.0.0.0", "--port", "5000"]
