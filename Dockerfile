# 整站运行镜像：控制面 API、任务消费者、静态工作台与 Worker 运行时。
#
# 控制面会在容器内派生 Worker 子进程，并校验解释器与入口的真实路径，
# 因此 backend 与 worker 必须位于同一镜像，且保持 <root>/.venv/bin/python 布局。
FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# 两个虚拟环境彼此独立，避免控制面与 Worker 的依赖互相污染。
COPY backend/requirements.txt /tmp/backend-requirements.txt
RUN python -m venv /opt/backend-venv \
    && /opt/backend-venv/bin/pip install -r /tmp/backend-requirements.txt

COPY worker/requirements.txt /tmp/worker-requirements.txt
RUN python -m venv /opt/worker-venv \
    && /opt/worker-venv/bin/pip install -r /tmp/worker-requirements.txt


FROM python:3.12-slim-bookworm AS runtime

ENV TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    SAAS_APP_ROOT=/app \
    SAAS_BOT_ROOT=/app/worker \
    SAAS_DB=/data/saas.db \
    SAAS_TENANTS_DIR=/data/tenants

# nodejs 用于静态工作台服务；util-linux 提供 setsid，procps 供进程身份校验。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates tzdata nodejs util-linux procps \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && groupadd -g 10001 xianyu \
    && useradd -u 10001 -g xianyu -M -d /app -s /usr/sbin/nologin xianyu

WORKDIR /app

COPY --from=builder /opt/backend-venv /app/backend/.venv
COPY --from=builder /opt/worker-venv /app/worker/.venv

COPY backend/ /app/backend/
COPY worker/ /app/worker/
COPY frontend/ /app/frontend/
COPY scripts/dev-server.mjs /app/scripts/dev-server.mjs
COPY docker/entrypoint.sh /app/docker/entrypoint.sh
# GPL 要求随二进制分发许可与来源署名。
COPY LICENSE LICENSING.md /app/

# 代码保持 root 拥有且不可写；仅 /data 与运行期目录对服务账号开放。
RUN chmod +x /app/docker/entrypoint.sh \
    && find /app -name '__pycache__' -type d -prune -exec rm -rf {} + \
    && chmod -R go-w /app \
    && install -d -o xianyu -g xianyu -m 0700 /data /app/.local

VOLUME ["/data"]
EXPOSE 4173 8096
USER xianyu:xianyu

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD /app/backend/.venv/bin/python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8096/health',timeout=3).status==200 else 1)"

ENTRYPOINT ["/app/docker/entrypoint.sh"]
