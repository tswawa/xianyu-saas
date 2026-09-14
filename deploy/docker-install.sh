#!/usr/bin/env bash
# 推荐的 Docker Compose 安装入口（仅本地 Linux Docker 引擎）。
#
# 全新安装：渲染 compose 配置 → 构建受管镜像 → 预装容器内 root 拥有的公钥
# 副本并准备全新数据目录 → 启动应用与更新器 → 一次性登记更新器 → 以实际
# 更新就绪能力（platform_update.update_capabilities，而非仅 /health）验收。
#
# 受管重跑：只校验并启动已登记的既有容器（docker start 保留原镜像与配置），
# 不重建、不降级、不重新登记、不创建或改写数据；当前检出的 compose/env 变更
# 不会被自动应用，需要人工维护。缺失或部分状态会明确报错，且不删除任何东西。
set -Eeuo pipefail

readonly DEFAULT_TIMEOUT=300
readonly APP_SERVICE="xianyu-saas"
readonly UPDATER_SERVICE="xianyu-updater"
readonly APP_UID="10001"
readonly APP_GID="10001"
readonly ACCEPTANCE_CODE='import sys; sys.path.insert(0, "/app/backend"); from platform_update import update_capabilities; state = update_capabilities(); print("READY" if state.get("apply") and state.get("rollback") else "PENDING:" + str(state.get("reason") or "unknown"))'

umask 077

fail() {
    printf 'docker-install: %s\n' "$1" >&2
    exit 1
}

usage() {
    cat <<'USAGE'
用法: bash deploy/docker-install.sh [选项]

在完整源码检出的项目根目录运行，安装应用与独立更新器并验收网页升级就绪。
仅支持本地 Linux Docker 引擎；不支持 DOCKER_HOST/DOCKER_CONTEXT 远程上下文。

选项:
  --project-name NAME   Compose 项目名（默认沿用已有容器或取目录名）
  --env-file FILE       环境文件（默认 config/saas.env，缺失时从示例创建；
                        同时用于 Compose 插值与服务 env_file：SAAS_ENV_FILE）
  --public-key FILE     外部受信任公钥（默认复制仓库内置 deploy/update-signing.pub）
  --timeout SECONDS     等待容器与更新就绪的上限秒数（默认 300）
  -h, --help            显示本帮助
USAGE
}

abspath() {
    local value="$1"
    if [[ -z "$value" ]]; then
        printf '%s' ""
        return 0
    fi
    if [[ "$value" != /* ]]; then
        value="$(pwd -P)/$value"
    fi
    printf '%s' "$value"
}

project=""
env_file=""
public_key=""
timeout="$DEFAULT_TIMEOUT"

while (( $# > 0 )); do
    case "$1" in
        --project-name)
            (( $# >= 2 )) || fail "--project-name 缺少参数"
            project="$2"; shift 2 ;;
        --env-file)
            (( $# >= 2 )) || fail "--env-file 缺少参数"
            env_file="$2"; shift 2 ;;
        --public-key)
            (( $# >= 2 )) || fail "--public-key 缺少参数"
            public_key="$2"; shift 2 ;;
        --timeout)
            (( $# >= 2 )) || fail "--timeout 缺少参数"
            timeout="$2"; shift 2
            [[ "$timeout" =~ ^[0-9]+$ ]] && (( timeout >= 1 )) || fail "--timeout 需要正整数秒数"
            ;;
        -h|--help)
            usage; exit 0 ;;
        *)
            fail "未知参数: $1（使用 --help 查看用法）" ;;
    esac
done

if [[ -n "${DOCKER_HOST:-}" || ( -n "${DOCKER_CONTEXT:-}" && "${DOCKER_CONTEXT}" != "default" ) ]]; then
    fail "仅支持本地 Linux Docker 引擎；请取消 DOCKER_HOST/DOCKER_CONTEXT 远程上下文后重试"
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
root="$(abspath "$root")"

command -v docker >/dev/null 2>&1 || fail "未找到 docker 命令"
context="$(docker context show)" || fail "无法读取 Docker 上下文"
endpoint="$(docker context inspect "$context" --format '{{.Endpoints.docker.Host}}')" || fail "无法读取 Docker 引擎地址"
[[ "$endpoint" == unix:///* ]] || fail "仅支持本地 Linux Docker socket，不支持当前远程上下文"
docker compose version >/dev/null 2>&1 || fail "需要 Docker Compose v2（docker compose）"
engine_os="$(docker info --format '{{.OSType}}' 2>/dev/null || true)"
[[ "$engine_os" == "linux" ]] || fail "Docker 引擎不可用或不是本地 Linux 容器模式"

for required in docker-compose.yml docker-compose.updates.yml Dockerfile config/saas.env.docker.example; do
    [[ -f "$root/$required" && ! -L "$root/$required" ]] || fail "缺少 $required；请在完整源码检出的项目根目录运行"
done

if [[ -z "$env_file" ]]; then
    env_file="$root/config/saas.env"
else
    env_file="$(abspath "$env_file")"
fi
[[ ! -L "$env_file" ]] || fail "环境文件不能是符号链接: $env_file"
if [[ -e "$env_file" && ! -f "$env_file" ]]; then
    fail "环境文件不是普通文件: $env_file"
fi
env_file_missing=false
if [[ ! -e "$env_file" ]]; then
    [[ "$env_file" == "$root/config/saas.env" ]] || fail "环境文件不存在: $env_file"
    env_file_missing=true
fi
if [[ "$env_file_missing" == false ]]; then
    [[ -f "$env_file" && -r "$env_file" ]] || fail "环境文件无效: $env_file"
fi

staged_key="$root/.local/docker-install/update-signing.pub"
if [[ -n "$public_key" ]]; then
    key_mode="explicit"
    key_source="$(abspath "$public_key")"
    key_path="$key_source"
else
    key_mode="staged"
    key_source="$root/deploy/update-signing.pub"
    key_path="$staged_key"
    [[ ! -L "$root/.local" ]] || fail ".local 不能是符号链接: $root/.local"
    [[ ! -L "$root/.local/docker-install" ]] || fail ".local/docker-install 不能是符号链接: $root/.local/docker-install"
    if [[ -e "$root/.local" && ! -d "$root/.local" ]]; then
        fail ".local 不是目录: $root/.local"
    fi
    if [[ -e "$root/.local/docker-install" && ! -d "$root/.local/docker-install" ]]; then
        fail ".local/docker-install 不是目录: $root/.local/docker-install"
    fi
fi
[[ -f "$key_source" && ! -L "$key_source" && -r "$key_source" ]] || fail "公钥文件无效: $key_source"
key_size="$(wc -c < "$key_source")"
(( key_size > 0 && key_size <= 4096 )) || fail "公钥文件大小无效: $key_source"

need_stage=false
if [[ "$key_mode" == "staged" ]]; then
    [[ ! -L "$key_path" ]] || fail "受信任公钥路径不能是符号链接: $key_path"
    if [[ -e "$key_path" ]]; then
        [[ -f "$key_path" && ! -L "$key_path" ]] || fail "受信任公钥路径不是普通文件: $key_path"
        cmp -s -- "$key_source" "$key_path" || fail "本地受信任公钥与仓库内置公钥不一致: $key_path；请人工核对后再运行"
    else
        need_stage=true
    fi
fi

existing_projects=()
if ! workdir_rows="$(docker ps --all --filter "label=com.docker.compose.project.working_dir=$root" --format '{{.Label "com.docker.compose.project"}}')"; then
    fail "无法读取 Docker 容器状态；请确认本地 Linux Docker 引擎可用"
fi
while IFS= read -r name; do
    if [[ -n "$name" ]]; then
        existing_projects+=("$name")
    fi
done < <(printf '%s\n' "$workdir_rows" | sort -u)
if (( ${#existing_projects[@]} > 1 )); then
    fail "当前目录检测到多个 Compose 项目（${existing_projects[*]}），请用 --project-name 指定"
fi
if [[ -z "$project" ]]; then
    if (( ${#existing_projects[@]} == 1 )); then
        project="${existing_projects[0]}"
    else
        project="$(basename -- "$root" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9_-]+/-/g; s/^-+//; s/-+$//' | cut -c1-63)"
    fi
elif (( ${#existing_projects[@]} == 1 )) && [[ "${existing_projects[0]}" != "$project" ]]; then
    fail "--project-name $project 与当前目录已有的 Compose 项目 ${existing_projects[0]} 不一致"
fi
[[ "$project" =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] || fail "项目名不合法: $project（请用 --project-name 指定）"

compose=(
    docker compose
    --project-directory "$root"
    -p "$project"
    --env-file "$env_file"
    -f "$root/docker-compose.yml"
    -f "$root/docker-compose.updates.yml"
)
export COMPOSE_PROJECT_NAME="$project"
export SAAS_ENV_FILE="$env_file"

app_container=""
updater_container=""
has_app=false
has_updater=false
if ! project_rows="$(docker ps --all --filter "label=com.docker.compose.project=$project" \
        --format '{{.ID}}{{"\u001f"}}{{.Label "com.docker.compose.service"}}{{"\u001f"}}{{.Label "io.xianyu.updates.role"}}{{"\u001f"}}{{.Label "com.docker.compose.project.working_dir"}}{{"\u001f"}}{{.Label "com.docker.compose.oneoff"}}')"; then
    fail "无法读取 Docker 容器状态；请确认本地 Linux Docker 引擎可用"
fi
while IFS=$'\x1f' read -r container_id container_service container_role container_workdir container_oneoff; do
    [[ -n "$container_id" ]] || continue
    # The updater's own short-lived probe tasks and compose one-offs are not
    # part of the managed deployment identity.
    if [[ "$container_role" == "task" ]]; then
        continue
    fi
    if [[ "$container_oneoff" == "True" || "$container_oneoff" == "true" ]]; then
        continue
    fi
    # Web updates recreate only the app through the updater's private Compose
    # directory. The updater itself retains the original host directory label.
    if [[ -n "$container_workdir" && "$container_workdir" != "$root" ]] &&
       ! [[ "$container_service" == "$APP_SERVICE" && "$container_role" == app && "$container_workdir" == /var/lib/xianyu-updater/compose ]]; then
        fail "项目名 $project 已被 $container_workdir 下的容器使用；请显式指定其他 --project-name"
    fi
    case "$container_service" in
        "$APP_SERVICE")
            [[ "$container_role" == "app" ]] || fail "检测到未接入独立更新器的应用容器；本入口只支持全新的推荐安装或本脚本的受管重跑，不会替换、迁移或调整既有容器"
            [[ "$has_app" == false ]] || fail "检测到多个应用容器；拒绝在重复状态上操作，请维护者人工处理"
            app_container="$container_id"; has_app=true ;;
        "$UPDATER_SERVICE")
            [[ "$container_role" == "updater" ]] || fail "检测到不属于本更新器的既有容器（service=$container_service）"
            [[ "$has_updater" == false ]] || fail "检测到多个更新器容器；拒绝在重复状态上操作，请维护者人工处理"
            updater_container="$container_id"; has_updater=true ;;
        *)
            fail "项目名 $project 已被服务 $container_service 占用；请显式指定其他 --project-name" ;;
    esac
done <<< "$project_rows"

managed=false
if [[ "$has_app" == true || "$has_updater" == true ]]; then
    managed=true
fi
if [[ "$managed" == false ]] && docker image inspect "${project}-${APP_SERVICE}:managed" >/dev/null 2>&1; then
    managed=true
fi
if [[ "$managed" == false ]] && docker volume inspect "${project}_updater-private" >/dev/null 2>&1; then
    managed=true
fi

if [[ "$has_app" == true ]]; then
    if ! mounted_key="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/app/update-signing.pub"}}{{.Source}}{{end}}{{end}}' "$app_container")"; then
        fail "无法读取应用容器的挂载信息；请确认本地 Linux Docker 引擎可用"
    fi
    if [[ -n "$mounted_key" ]]; then
        if [[ "$key_mode" == "explicit" && "$mounted_key" != "$key_source" ]]; then
            fail "现有应用容器挂载的公钥为 $mounted_key，与本次 $key_source 不一致；请用 --public-key 指定原路径"
        fi
        [[ -f "$mounted_key" && ! -L "$mounted_key" ]] || fail "受管安装挂载的公钥文件缺失: $mounted_key；拒绝静默重建"
        key_path="$mounted_key"
        need_stage=false
    fi
fi
export SAAS_UPDATE_PUBLIC_KEY_HOST_FILE="$key_path"

data_dir="$root/data"
[[ ! -L "$data_dir" ]] || fail "数据目录不能是符号链接: $data_dir"
if [[ -e "$data_dir" && ! -d "$data_dir" ]]; then
    fail "数据路径不是目录: $data_dir"
fi

require_existing_data() {
    [[ -d "$data_dir" ]] || fail "受管安装的数据目录缺失: $data_dir；拒绝创建空数据库替换，请人工恢复备份"
    local entries
    if ! entries="$(ls -A "$data_dir" 2>/dev/null)"; then
        fail "无法读取受管数据目录: $data_dir"
    fi
    [[ -n "$entries" ]] || fail "受管安装的数据目录为空: $data_dir；拒绝创建空数据库替换，请人工恢复备份"
    [[ -f "$data_dir/saas.db" && ! -L "$data_dir/saas.db" && -s "$data_dir/saas.db" ]] || fail "受管安装的数据库缺失或无效；拒绝创建空数据库替换，请人工恢复备份"
}

if [[ "$managed" == true ]]; then
    printf 'docker-install: 检测到已登记的受管安装：只校验并启动既有容器；当前 compose/env 变更不会被自动应用\n'
    [[ "$env_file_missing" == false ]] || fail "受管安装缺少环境文件 $env_file；拒绝用示例覆盖既有部署"
    [[ "$has_app" == true ]] || fail "受管安装缺少应用容器；拒绝在既有状态上重建或替换，请维护者人工处理"
    [[ "$has_updater" == true ]] || fail "受管安装缺少更新器容器；拒绝在既有状态上重建或替换，请维护者人工处理"
    for image_name in "${project}-${APP_SERVICE}:managed" "${project}-${UPDATER_SERVICE}:local"; do
        docker image inspect "$image_name" >/dev/null 2>&1 || fail "受管安装缺少镜像 $image_name；拒绝自动重建，请维护者人工处理"
    done
    [[ "$need_stage" == false ]] || fail "受管安装缺少本地公钥副本 $key_path；拒绝静默重新生成，请人工核对"
    require_existing_data
    "${compose[@]}" config >/dev/null || fail "compose 配置渲染失败（请检查 $env_file；受管重跑只用于解析既有项目，不会应用变更）"
else
    if [[ "$env_file_missing" == true ]]; then
        cp -- "$root/config/saas.env.docker.example" "$env_file"
        chmod 0600 "$env_file" 2>/dev/null || true
        printf 'docker-install: 已从示例创建 %s，请按需修改公开地址等配置\n' "$env_file"
    fi
    export SAAS_BUILD_COMMIT=""
    export SAAS_BUILD_DIRTY="unknown"
    if command -v git >/dev/null 2>&1 && git -C "$root" rev-parse --git-dir >/dev/null 2>&1; then
        commit="$(git -C "$root" rev-parse --verify HEAD 2>/dev/null || true)"
        if [[ "$commit" =~ ^[0-9a-f]{7,40}$ ]]; then
            export SAAS_BUILD_COMMIT="$commit"
        fi
        if status_output="$(git -C "$root" status --porcelain 2>/dev/null)"; then
            if [[ -z "$status_output" ]]; then
                export SAAS_BUILD_DIRTY="false"
            else
                export SAAS_BUILD_DIRTY="true"
            fi
        else
            export SAAS_BUILD_DIRTY="true"
        fi
    fi
    printf 'docker-install: 构建来源 commit=%s dirty=%s\n' "${SAAS_BUILD_COMMIT:-none}" "$SAAS_BUILD_DIRTY"

    "${compose[@]}" config >/dev/null || fail "compose 配置渲染失败（请检查 $env_file 与公钥路径）"
    printf 'docker-install: 构建应用与更新器镜像...\n'
    "${compose[@]}" build || fail "镜像构建失败"

    fresh_data=false
    if [[ ! -e "$data_dir" ]]; then
        mkdir -p "$data_dir"
        chmod 0700 "$data_dir" 2>/dev/null || true
        fresh_data=true
    elif entries="$(ls -A "$data_dir" 2>/dev/null)"; then
        if [[ -z "$entries" ]]; then
            fresh_data=true
        fi
    else
        fail "无法读取数据目录: $data_dir"
    fi

    need_root_prep=false
    if [[ "$need_stage" == true || "$fresh_data" == true ]]; then
        need_root_prep=true
    fi
    if [[ "$need_root_prep" == true ]]; then
        staging_image="${project}-${APP_SERVICE}:managed"
        if ! docker image inspect "$staging_image" >/dev/null 2>&1; then
            staging_image="${project}-${UPDATER_SERVICE}:local"
        fi
        docker image inspect "$staging_image" >/dev/null 2>&1 \
            || fail "缺少已构建镜像，无法准备 root 拥有的公钥副本与全新数据目录"
    fi

    if [[ "$need_stage" == true ]]; then
        printf 'docker-install: 预装受信任公钥（容器内 root 拥有）...\n'
        mkdir -p -- "$(dirname -- "$key_path")"
        docker run --rm --user 0:0 --entrypoint /bin/sh \
            --mount "type=bind,source=$(dirname -- "$key_source"),target=/seed,readonly" \
            --mount "type=bind,source=$(dirname -- "$key_path"),target=/trust" \
            "$staging_image" -c 'set -eu; cp -- "$1" "/trust/$2"; chown 0:0 "/trust/$2"; chmod 0644 "/trust/$2"' \
            sh "/seed/$(basename -- "$key_source")" "$(basename -- "$key_path")" \
            || fail "受信任公钥副本创建失败"
        [[ -f "$key_path" && ! -L "$key_path" ]] || fail "受信任公钥副本创建失败: $key_path"
    fi

    if [[ "$fresh_data" == true ]]; then
        printf 'docker-install: 准备全新数据目录 %s...\n' "$data_dir"
        docker run --rm --user 0:0 --entrypoint /bin/sh \
            --mount "type=bind,source=$data_dir,target=/data" \
            "$staging_image" -c 'set -eu; chown "$1:$2" /data; chmod 0700 /data' \
            sh "$APP_UID" "$APP_GID" \
            || fail "无法为全新数据目录设置容器用户属主: $data_dir"
    fi

    printf 'docker-install: 启动容器...\n'
    "${compose[@]}" up --detach --no-build || fail "容器启动失败；请查看 docker compose logs"
fi

find_container() {
    local service="$1" output
    if ! output="$(docker ps --all --filter "label=com.docker.compose.project=$project" \
            --filter "label=com.docker.compose.service=$service" --format '{{.ID}}')"; then
        fail "无法读取 Docker 容器状态；请确认本地 Linux Docker 引擎可用"
    fi
    printf '%s' "${output%%$'\n'*}"
}

if [[ "$managed" == true ]]; then
    for pair in "$app_container:$APP_SERVICE" "$updater_container:$UPDATER_SERVICE"; do
        container_id="${pair%%:*}"
        container_service="${pair##*:}"
        if [[ "$(docker inspect --format '{{.State.Running}}' "$container_id" 2>/dev/null || true)" != "true" ]]; then
            printf 'docker-install: 启动既有 %s 容器 %s\n' "$container_service" "$container_id"
            docker start "$container_id" >/dev/null || fail "无法启动既有 $container_service 容器；请人工排查"
        fi
    done
else
    app_container="$(find_container "$APP_SERVICE")"
    updater_container="$(find_container "$UPDATER_SERVICE")"
    [[ -n "$app_container" && -n "$updater_container" ]] || fail "容器启动后未找到应用或更新器容器"
fi

wait_running_id() {
    local container_id="$1" deadline=$((SECONDS + timeout))
    while (( SECONDS < deadline )); do
        if [[ "$(docker inspect --format '{{.State.Running}}' "$container_id" 2>/dev/null || true)" == "true" ]]; then
            return 0
        fi
        sleep 2
    done
    return 1
}

wait_healthy_id() {
    local container_id="$1" deadline=$((SECONDS + timeout)) status
    while (( SECONDS < deadline )); do
        status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)"
        case "$status" in
            healthy|running) return 0 ;;
        esac
        sleep 2
    done
    return 1
}

wait_running_id "$updater_container" || fail "更新器容器未在 ${timeout}s 内运行；请查看 ${UPDATER_SERVICE} 日志"
wait_running_id "$app_container" || fail "应用容器未在 ${timeout}s 内运行；请查看 ${APP_SERVICE} 日志"
wait_healthy_id "$app_container" || fail "应用容器未在 ${timeout}s 内健康；请查看 ${APP_SERVICE} 日志"

printf 'docker-install: 检查更新器登记状态...\n'
registration="$("${compose[@]}" exec -T "$UPDATER_SERVICE" python -c 'from pathlib import Path; print("registered" if (Path("/var/lib/xianyu-updater") / "docker-deployment.json").is_file() else "unregistered")' 2>/dev/null || true)"
if [[ "$registration" == "unregistered" ]]; then
    [[ "$managed" == false ]] || fail "受管安装缺少更新器登记；拒绝在既有状态下重新初始化，请维护者人工核对私有状态"
    printf 'docker-install: 执行一次性更新器登记...\n'
    retry_deadline=$((SECONDS + timeout))
    while :; do
        if initialize_output="$("${compose[@]}" config --format json | "${compose[@]}" exec -T "$UPDATER_SERVICE" python docker_updater.py initialize)"; then
            break
        fi
        code="$(printf '%s' "$initialize_output" | sed -n 's/.*"error_code":"\([a-z0-9_]*\)".*/\1/p' | head -n 1)"
        case "$code" in
            update_executor_busy)
                (( SECONDS < retry_deadline )) || fail "更新器持续繁忙，登记重试超时（$code）"
                sleep 2
                continue ;;
            update_compose_initialization_conflict)
                fail "更新器私有状态与本次登记冲突（$code）；不会删除任何私有文件，请维护者人工核对" ;;
            update_compose_config_mismatch|update_compose_registration_invalid)
                fail "登记发现配置漂移（$code）；请核对项目名、工作目录、env-file 与配置文件顺序" ;;
            update_target_not_unique|update_unique_updater_required)
                fail "登记要求唯一且运行中的应用/更新器容器（$code）；请检查重复容器" ;;
            *)
                fail "更新器登记失败（${code:-unknown}）" ;;
        esac
    done
    [[ "$initialize_output" == *'"ok":true'* ]] || fail "更新器登记返回了意外结果"
elif [[ "$registration" != "registered" ]]; then
    fail "无法确认更新器登记状态（容器未就绪）"
fi

printf 'docker-install: 等待实际更新就绪能力...\n'
deadline=$((SECONDS + timeout))
reason="unknown"
ready=false
while (( SECONDS < deadline )); do
    result="$("${compose[@]}" exec -T "$APP_SERVICE" env PYTHONPATH=/app/backend /app/backend/.venv/bin/python -c "$ACCEPTANCE_CODE" 2>/dev/null || true)"
    if [[ "$result" == "READY" ]]; then
        ready=true
        break
    fi
    if [[ "$result" == PENDING:* ]]; then
        reason="${result#PENDING:}"
    fi
    sleep 3
done
if [[ "$ready" != true ]]; then
    case "$reason" in
        update_updater_not_initialized|update_compose_registration_invalid)
            fail "网页升级未就绪（$reason）：更新器登记缺失或无效，请维护者检查私有状态" ;;
        update_public_key_invalid|update_public_key_mismatch|update_public_key_missing)
            fail "网页升级未就绪（$reason）：受信任公钥未按容器内 root 拥有且不可改写的要求挂载" ;;
        update_service_stale|update_installation_unavailable|update_service_unavailable)
            fail "网页升级未就绪（$reason）：更新器或共享 IPC 目录不可用" ;;
        update_maintenance_protocol_unavailable|update_health_invalid)
            fail "网页升级未就绪（$reason）：应用未提供独立更新器所需的本机协议" ;;
        *)
            fail "等待网页升级就绪超时（最后原因: $reason）；请查看 ${UPDATER_SERVICE} 日志" ;;
    esac
fi

printf '\n'
printf 'docker-install: 安装完成，网页升级已就绪。\n'
printf '  工作台: http://127.0.0.1:4173/xianyu-saas/（公开地址以 %s 为准）\n' "$env_file"
printf '  数据目录: %s（升级切换镜像，不恢复或覆盖业务数据）\n' "$data_dir"
printf '  受管重跑只校验/启动既有容器；本地配置变更需人工维护，不会自动应用。\n'
