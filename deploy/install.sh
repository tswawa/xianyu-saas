#!/usr/bin/env bash
set -Eeuo pipefail

readonly PRODUCT="xianyu-saas"
readonly REPOSITORY="tswawa/xianyu-saas"
readonly GITHUB_ROOT="https://github.com/${REPOSITORY}"
readonly SYSTEM_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
readonly MAX_INDEX_BYTES=2097152
readonly MAX_SIGNATURE_BYTES=4096
readonly MAX_MANAGER_BYTES=268435456
readonly SEMVER_RE='^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-((0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)(\.(0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?(\+([0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*))?$'

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

valid_version() {
    [[ "$1" =~ $SEMVER_RE ]]
}

read_os_value() {
    local wanted="$1"
    local key value
    while IFS='=' read -r key value; do
        [[ "$key" == "$wanted" ]] || continue
        value="${value#\"}"
        value="${value%\"}"
        value="${value#\'}"
        value="${value%\'}"
        printf '%s' "$value"
        return 0
    done < "$OS_RELEASE_FILE"
    return 1
}

package_installed() {
    [[ "$(dpkg-query -W -f='${Status}' "$1" 2>/dev/null || true)" == "install ok installed" ]]
}

fetch() {
    local url="$1"
    local destination="$2"
    local maximum="$3"
    curl --fail --show-error --silent --location \
        --proto '=https' --proto-redir '=https' --tlsv1.2 \
        --connect-timeout 15 --max-time 300 --retry 2 \
        --max-filesize "$maximum" --output "$destination" "$url"
    [[ -f "$destination" && ! -L "$destination" ]] || fail "download_invalid"
    local actual
    actual="$(wc -c < "$destination")"
    (( actual <= maximum )) || fail "download_too_large"
}

requested_version=""
case "$#" in
    0) ;;
    2)
        [[ "$1" == "--version" ]] || fail "usage: install.sh [--version VERSION]"
        requested_version="$2"
        valid_version "$requested_version" || fail "version_invalid"
        ;;
    *) fail "usage: install.sh [--version VERSION]" ;;
esac

PATH="$SYSTEM_PATH"
OS_RELEASE_FILE="/etc/os-release"
export PATH

[[ "$(id -u)" == "0" ]] || fail "root_required"
[[ -f "$OS_RELEASE_FILE" ]] || fail "os_release_missing"
os_id="$(read_os_value ID || true)"
os_version="$(read_os_value VERSION_ID || true)"
case "${os_id}:${os_version}" in
    ubuntu:22.04|ubuntu:24.04|debian:12) ;;
    *) fail "unsupported_distribution" ;;
esac

case "$(uname -m)" in
    x86_64|amd64) architecture="x86_64" ;;
    aarch64|arm64) architecture="aarch64" ;;
    *) fail "unsupported_architecture" ;;
esac

missing_packages=()
for package in ca-certificates curl jq openssl; do
    package_installed "$package" || missing_packages+=("$package")
done
if (( ${#missing_packages[@]} > 0 )); then
    DEBIAN_FRONTEND=noninteractive apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends "${missing_packages[@]}"
fi
for package in ca-certificates curl jq openssl; do
    package_installed "$package" || fail "dependency_install_failed"
done
for command_name in curl jq openssl; do
    command -v "$command_name" >/dev/null 2>&1 || fail "dependency_missing"
done

workdir="$(mktemp -d "${TMPDIR:-/tmp}/${PRODUCT}-install.XXXXXXXX")"
cleanup() {
    rm -rf -- "$workdir"
}
trap cleanup EXIT HUP INT TERM
chmod 0700 "$workdir"

index_file="$workdir/artifacts.json"
index_signature_file="$workdir/artifacts.json.sig"
signature_binary="$workdir/artifacts.sig.bin"
public_key_file="$workdir/update-signing.pub"
manager_file="$workdir/manager"

cat > "$public_key_file" <<'PEM'
-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAkuJAi8oqZQAIUDpd07tLV/xFvfw8Qc9Y/NMFWf9l1/Y=
-----END PUBLIC KEY-----
PEM
chmod 0600 "$public_key_file"

release_root="${GITHUB_ROOT}/releases/latest/download"
fetch "${release_root}/artifacts.json" "$index_file" "$MAX_INDEX_BYTES"
fetch "${release_root}/artifacts.json.sig" "$index_signature_file" "$MAX_SIGNATURE_BYTES"

openssl base64 -d -A -in "$index_signature_file" -out "$signature_binary" 2>/dev/null \
    || fail "index_signature_encoding_invalid"
[[ "$(wc -c < "$signature_binary")" == "64" ]] || fail "index_signature_encoding_invalid"
openssl pkeyutl -verify -pubin -inkey "$public_key_file" -rawin \
    -in "$index_file" -sigfile "$signature_binary" >/dev/null 2>&1 \
    || fail "index_signature_invalid"

record="$({ jq -er --arg target "linux-${architecture}" --argjson maximum "$MAX_MANAGER_BYTES" '
    if type != "object"
       or .schema != 2
       or .manager_protocol != 1
       or (.version | type) != "string"
       or (.files | type) != "array"
       or (all(.files[]; type == "object") | not)
    then error("index_invalid")
    else
        .version as $version
        | [.files[] | select(
            .kind == "bootstrap-manager"
            and .target == $target
            and .manager_protocol == 1
        )] as $matches
        | if ($matches | length) != 1 then error("manager_record_not_unique") else $matches[0] end
        | . as $record
        | if (($record.name | type) != "string")
             or (($record.sha256 | type) != "string")
             or (($record.size | type) != "number")
             or (($record.size | floor) != $record.size)
             or $record.size < 1
             or $record.size > $maximum
          then error("manager_record_invalid")
          else [$version, $record.name, ($record.size | tostring), $record.sha256] | @tsv
          end
    end
' "$index_file"; } 2>/dev/null)" || fail "index_invalid"
IFS=$'\t' read -r release_version manager_name expected_size expected_sha256 <<< "$record"

valid_version "$release_version" || fail "index_version_invalid"
expected_name="${PRODUCT}-${release_version}-linux-${architecture}"
[[ "$manager_name" == "$expected_name" ]] || fail "manager_name_invalid"
[[ "$expected_size" =~ ^[1-9][0-9]*$ ]] || fail "manager_size_invalid"
(( expected_size <= MAX_MANAGER_BYTES )) || fail "manager_size_invalid"
[[ "$expected_sha256" =~ ^[0-9a-f]{64}$ ]] || fail "manager_sha256_invalid"

manager_url="${GITHUB_ROOT}/releases/download/v${release_version}/${manager_name}"
fetch "$manager_url" "$manager_file" "$MAX_MANAGER_BYTES"
actual_size="$(wc -c < "$manager_file")"
[[ "$actual_size" == "$expected_size" ]] || fail "manager_size_mismatch"
actual_sha256="$(openssl dgst -sha256 -r "$manager_file" 2>/dev/null)" || fail "manager_sha256_failed"
actual_sha256="${actual_sha256%% *}"
[[ "$actual_sha256" == "$expected_sha256" ]] || fail "manager_sha256_mismatch"
chmod 0755 "$manager_file"

manager_arguments=(install)
if [[ -n "$requested_version" ]]; then
    manager_arguments+=(--version "$requested_version")
fi
env -i PATH="$SYSTEM_PATH" LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    "$manager_file" "${manager_arguments[@]}"
