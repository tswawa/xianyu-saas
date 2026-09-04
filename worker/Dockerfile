FROM python:3.10-alpine AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apk add --no-cache --virtual .build-deps \
    build-base \
    libffi-dev

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY requirements.txt ./
RUN pip install -r requirements.txt


FROM python:3.10-alpine AS runtime

ENV TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1 \
    XIAN_YU_DATA_DIR=/app/data

RUN apk add --no-cache ca-certificates libstdc++ tzdata \
    && addgroup -S -g 10001 xianyu-agent \
    && adduser -S -D -H -u 10001 -G xianyu-agent xianyu-agent \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo Asia/Shanghai > /etc/timezone

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY main.py XianyuAgent.py XianyuApis.py auth_state.py platform_profile.py private_auth_storage.py context_manager.py delivery_store.py products_config.json ./
COPY utils/ ./utils/
COPY scripts/list_manual_reviews.py scripts/resolve_manual_review.py scripts/manage_inbound_dead_letters.py scripts/migrate_state.py ./scripts/
COPY prompts/classify_prompt_example.txt ./prompts/classify_prompt.txt
COPY prompts/price_prompt_example.txt ./prompts/price_prompt.txt
COPY prompts/tech_prompt_example.txt ./prompts/tech_prompt.txt
COPY prompts/default_prompt_example.txt ./prompts/default_prompt.txt

# Application files stay root-owned; only persisted runtime state is writable.
RUN chmod -R go-w /app /opt/venv \
    && install -d -o xianyu-agent -g xianyu-agent -m 0700 /app/data

VOLUME ["/app/data"]
USER xianyu-agent:xianyu-agent

CMD ["python", "main.py"]
