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


# 显式启用的独立更新器；保留既有 Docker CLI / Buildx 固定版本，
# 仅从官方固定镜像复制 Compose 5.5.1。普通 runtime 不含管理工具。
FROM docker:28.3.3-cli AS updater-docker-cli
FROM docker/buildx-bin:0.26.1 AS updater-buildx
FROM docker:29.8.0-cli AS updater-compose-cli
FROM python:3.12-slim-bookworm AS updater
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    SAAS_DOCKER_UPDATE_ROOT=/updates \
    SAAS_DOCKER_UPDATER_STATE_DIR=/var/lib/xianyu-updater \
    SAAS_UPDATE_PUBLIC_KEY_FILE=/app/update-signing.pub
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install cryptography==46.0.1 \
    && install -d -m 0755 /app /opt/updater /updates \
    && install -d -m 0700 /var/lib/xianyu-updater
COPY --from=updater-docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=updater-buildx /buildx /usr/local/lib/docker/cli-plugins/docker-buildx
COPY --from=updater-compose-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose
COPY backend/docker_updater.py backend/docker_engine.py backend/docker_update_protocol.py backend/update_maintenance.py /opt/updater/
RUN test "$(docker compose version --short)" = "5.5.1" \
    && chmod -R go-w /opt/updater /usr/local/bin/docker /usr/local/lib/docker/cli-plugins
USER 0:0
WORKDIR /opt/updater
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import json,time;v=json.load(open('/updates/status/capabilities.json'));assert v['schema']==1 and v['protocol']==1 and 0<=time.time()-v['heartbeat_at']<30"
ENTRYPOINT ["/usr/local/bin/python", "/opt/updater/docker_updater.py"]

# 最后一个 target 仍是非 root 的普通应用；未使用覆盖配置时不启用更新器。
FROM python:3.12-slim-bookworm AS runtime

ENV TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    SAAS_APP_ROOT=/app \
    SAAS_DEPLOYMENT_MODE=docker \
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
COPY docker/launcher.sh /app/docker/launcher.sh
# GPL 要求随二进制分发许可与来源署名。
COPY LICENSE LICENSING.md CHANGELOG.md package.json /app/
# 不可变的受信任公钥（root 所有、只读）供内置文件更新器校验签名。
COPY deploy/update-signing.pub /app/update-signing.pub

ARG SAAS_BUILD_COMMIT=""
ARG SAAS_BUILD_DIRTY="unknown"
RUN SAAS_BUILD_COMMIT="$SAAS_BUILD_COMMIT" SAAS_BUILD_DIRTY="$SAAS_BUILD_DIRTY" \
    python backend/version.py --write-build-info

# 代码保持 root 拥有且不可写；仅 /data 与运行期目录对服务账号开放。
RUN chmod +x /app/docker/entrypoint.sh /app/docker/launcher.sh \
    && find /app -name '__pycache__' -type d -prune -exec rm -rf {} + \
    && chmod -R a+rX,go-w /app \
    && install -d -o xianyu -g xianyu -m 0700 /data /app/.local

VOLUME ["/data"]
EXPOSE 4173 8096
USER xianyu:xianyu

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD /app/backend/.venv/bin/python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8096/health',timeout=3).status==200 else 1)"

ENTRYPOINT ["/app/docker/entrypoint.sh"]
