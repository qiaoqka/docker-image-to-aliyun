#!/bin/bash
set -euo pipefail

# ============================================================
#  串行把 images.yaml 清单中的镜像同步到阿里云容器镜像服务。
#
#  用法：
#      scripts/sync-images.sh [images.yaml 路径]
#
#  环境变量（必需）：
#      ALIYUN_REGISTRY           阿里云 registry 地址
#      ALIYUN_NAME_SPACE         阿里云命名空间
#      ALIYUN_REGISTRY_USER      登录用户名
#      ALIYUN_REGISTRY_PASSWORD  登录密码（仅通过 stdin 传递）
#  环境变量（可选）：
#      PYTHON                    运行清单模块的 Python，默认 python3
#
#  主要流程：
#      1. 调用 scripts/image_manifest.py 解析清单，得到每行
#         (source, effective_platform, mirror, local) 四个 TAB 字段
#      2. 解析失败或行格式错误立刻退出，不执行 docker login / pull
#      3. docker login 一次，密码走 --password-stdin
#      4. 逐条比对 config.digest：相同则跳过，不同或未知则 pull/tag/push
#      5. 单条失败累计并继续其余镜像
#      6. 结束 logout，输出状态汇总；存在失败时最终退出非零
# ============================================================

# ============================================================
#  配置与默认值
# ============================================================
DEFAULT_PYTHON="python3"
MANIFEST_MODULE_NAME="image_manifest.py"
DEFAULT_MANIFEST_NAME="images.yaml"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MANIFEST_MODULE="$SCRIPT_DIR/$MANIFEST_MODULE_NAME"

# 单条镜像同步耗时（秒），由 sync_image 设置
SYNC_CHECK_SECS=0
SYNC_PULL_SECS=0
SYNC_TAG_SECS=0
SYNC_PUSH_SECS=0
# 是否已成功登录，决定退出时是否需要 logout
LOGGED_IN=0

# ============================================================
#  日志与工具函数
# ============================================================

# 打印错误并以状态 1 退出。$*=错误信息
die() {
    printf '[ERROR] %s\n' "$*" >&2
    exit 1
}

# 打印提示日志到 stderr。$*=信息
log_info() {
    printf '[INFO] %s\n' "$*" >&2
}

# 打印警告日志到 stderr。$*=信息
log_warn() {
    printf '[WARN] %s\n' "$*" >&2
}

# 当前 epoch 秒。stdout: 整数秒
now_epoch() {
    date +%s
}

# 把秒数格式化为易读时长。$1=秒数，stdout: 如 "1m32s" 或 "7s"
format_duration() {
    local seconds="$1"
    if ((seconds >= 60)); then
        printf '%dm%ds' "$((seconds / 60))" "$((seconds % 60))"
    else
        printf '%ds' "$seconds"
    fi
}

# 校验必需环境变量。$1...=变量名
require_env() {
    local name
    for name in "$@"; do
        if [[ -z "${!name:-}" ]]; then
            die "缺少必需环境变量 $name"
        fi
    done
}

# @desc 退出时清理：已登录则执行 docker logout。
#       原有退出状态非零时保留原状态；原有状态为 0 但 logout 失败时改为非零。
cleanup() {
    local status=$?
    if [[ "$LOGGED_IN" == "1" ]]; then
        log_info '===== 阶段：docker logout ====='
        if ! docker logout "$ALIYUN_REGISTRY" >&2; then
            log_warn 'docker logout 失败'
            if (( status == 0 )); then
                status=1
            fi
        fi
    fi
    exit "$status"
}
trap cleanup EXIT

usage() {
    printf '用法：%s [images.yaml 路径]\n' "$0"
    printf '默认清单：%s\n' "$REPO_ROOT/$DEFAULT_MANIFEST_NAME"
    exit 0
}

# ============================================================
#  digest 检查
# ============================================================

# @desc 查询镜像的 config.digest。
#       单平台镜像直接读顶层 config.digest；多平台 manifest list 先按 platform
#       选出子 manifest，再读其 config.digest。查询失败视为未知（输出空串）。
# $1   镜像引用
# $2   平台，如 linux/amd64（可空）
# stdout: digest 字符串；无法确定时输出空串
# exit: 始终 0（未知不当作相等）
get_image_digest() {
    local image="$1"
    local platform="${2:-}"
    local manifest=""

    manifest="$(docker manifest inspect "$image")" || return 0
    if [[ -z "$manifest" ]]; then
        printf ''
        return 0
    fi

    local config_digest=""
    config_digest="$(printf '%s' "$manifest" | jq -r '.config.digest // empty')" || return 0
    if [[ -n "$config_digest" ]]; then
        printf '%s' "$config_digest"
        return 0
    fi

    # platform 的 OS、架构、variant 都参与匹配，避免选中 Windows 描述符。
    local os arch variant
    IFS=/ read -r os arch variant <<< "${platform:-linux/amd64}"
    local platform_digest=""
    platform_digest="$(printf '%s' "$manifest" | jq -r --arg os "$os" --arg arch "$arch" --arg variant "$variant" '
        [.manifests[] | select(.platform.os == $os and .platform.architecture == $arch)
         | select(if $arch == "arm64" and ($variant == "" or $variant == "v8")
                  then (.platform.variant // "v8") == "v8"
                  else (.platform.variant // "") == $variant end)]
        | first | .digest // empty')" || return 0
    if [[ -z "$platform_digest" ]]; then
        printf ''
        return 0
    fi

    local base="${image%:*}"
    [[ "$base" == "$image" ]] && base="$image"
    local platform_manifest=""
    platform_manifest="$(docker manifest inspect "${base}@${platform_digest}")" || return 0
    if [[ -n "$platform_manifest" ]]; then
        printf '%s' "$platform_manifest" | jq -r '.config.digest // empty' || true
    fi
    return 0
}

# @desc 判断是否需要同步。源与目标 digest 都已知且相等时视为已是最新。
# $1   源镜像
# $2   目标（阿里云）镜像
# $3   平台
# stderr: digest 查询结果日志
# exit: 0 需要更新（含任一 digest 未知），1 已是最新可跳过
image_needs_update() {
    local source_image="$1"
    local target_image="$2"
    local platform="${3:-}"

    local source_digest=""
    local target_digest=""
    source_digest="$(get_image_digest "$source_image" "$platform")"
    target_digest="$(get_image_digest "$target_image")"

    printf '  源 digest:   %s\n' "${source_digest:-<未知>}" >&2
    printf '  阿里云 digest: %s\n' "${target_digest:-<未知>}" >&2

    # 任一 digest 未知都不能判定为相等
    if [[ -n "$source_digest" && -n "$target_digest" && "$source_digest" == "$target_digest" ]]; then
        return 1
    fi
    return 0
}

# ============================================================
#  单条镜像同步
# ============================================================

# @desc 同步单条镜像：必要时 pull / tag / push。
#       计时结果写入全局 SYNC_*_SECS。
# $1   source   上游镜像（已带 tag）
# $2   platform 平台，如 linux/amd64
# $3   target   阿里云镜像（含 registry/namespace）
# stderr: 各阶段日志
# exit: 0 已推送，1 已跳过，2 拉取失败，3 打标签失败，4 推送失败
sync_image() {
    local source="$1"
    local platform="$2"
    local target="$3"
    local t0

    SYNC_CHECK_SECS=0
    SYNC_PULL_SECS=0
    SYNC_TAG_SECS=0
    SYNC_PUSH_SECS=0

    t0="$(now_epoch)"
    if ! image_needs_update "$source" "$target" "$platform"; then
        SYNC_CHECK_SECS=$(( $(now_epoch) - t0 ))
        return 1
    fi
    SYNC_CHECK_SECS=$(( $(now_epoch) - t0 ))

    printf '[PULL] docker pull --platform %s %s\n' "$platform" "$source" >&2
    t0="$(now_epoch)"
    if ! docker pull --platform "$platform" "$source" >&2; then
        SYNC_PULL_SECS=$(( $(now_epoch) - t0 ))
        return 2
    fi
    SYNC_PULL_SECS=$(( $(now_epoch) - t0 ))

    printf '[TAG] %s -> %s\n' "$source" "$target" >&2
    t0="$(now_epoch)"
    if ! docker tag "$source" "$target" >&2; then
        SYNC_TAG_SECS=$(( $(now_epoch) - t0 ))
        return 3
    fi
    SYNC_TAG_SECS=$(( $(now_epoch) - t0 ))

    printf '[PUSH] %s\n' "$target" >&2
    t0="$(now_epoch)"
    if ! docker push "$target" >&2; then
        SYNC_PUSH_SECS=$(( $(now_epoch) - t0 ))
        return 4
    fi
    SYNC_PUSH_SECS=$(( $(now_epoch) - t0 ))

    docker rmi "$source" "$target" >/dev/null 2>&1 || true
    return 0
}

# ============================================================
#  主流程
# ============================================================

# @desc 解析清单模块输出，返回合法 rows（已校验为 4 个 TAB 字段）。
#       解析失败或格式错误时 die，不会执行任何网络操作。
# $1   清单模块路径
# $2   清单文件路径
# stdout: 每行一条原始 row（调用方读取）
load_rows() {
    local module="$1"
    local manifest="$2"

    if ! "$PYTHON_BIN" "$module" "$manifest"; then
        die "清单解析失败：$manifest"
    fi
}

main() {
    local manifest_path="${1:-$REPO_ROOT/$DEFAULT_MANIFEST_NAME}"

    [[ -f "$MANIFEST_MODULE" ]] || die "清单模块不存在：$MANIFEST_MODULE"
    [[ -f "$manifest_path" ]] || die "清单文件不存在：$manifest_path"
    command -v docker >/dev/null 2>&1 || die "找不到 docker 命令"
    command -v jq >/dev/null 2>&1 || die "找不到 jq 命令（digest 检查需要）"

    log_info '===== 阶段：解析清单 ====='
    local rows_raw
    rows_raw="$(load_rows "$MANIFEST_MODULE" "$manifest_path")"

    # 先把所有 row 读入数组并校验，避免 docker 命令消费循环 stdin
    local rows=()
    local line=""
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        local field_source="" field_platform="" field_mirror="" field_local=""
        IFS=$'\t' read -r field_source field_platform field_mirror field_local <<< "$line"
        if [[ -z "$field_source" || -z "$field_platform" || -z "$field_mirror" || -z "$field_local" ]]; then
            die "清单行格式错误（需要 4 个 TAB 字段）：$line"
        fi
        rows+=("$line")
    done <<< "$rows_raw"

    if [[ "${#rows[@]}" -eq 0 ]]; then
        die "清单为空：$manifest_path"
    fi
    log_info "清单解析完成，共 ${#rows[@]} 条"

    require_env ALIYUN_REGISTRY ALIYUN_NAME_SPACE ALIYUN_REGISTRY_USER ALIYUN_REGISTRY_PASSWORD

    log_info '===== 阶段：docker login ====='
    printf '%s' "$ALIYUN_REGISTRY_PASSWORD" | docker login -u "$ALIYUN_REGISTRY_USER" --password-stdin "$ALIYUN_REGISTRY"
    LOGGED_IN=1

    log_info '===== 阶段：同步镜像 ====='
    local total=0
    local pushed=0
    local skipped=0
    local failed=0
    local failures=()
    local row="" source="" platform="" mirror="" local_tag="" target="" rc=0
    local total_rows="${#rows[@]}"

    for row in "${rows[@]}"; do
        IFS=$'\t' read -r source platform mirror local_tag <<< "$row"
        total=$((total + 1))
        target="$ALIYUN_REGISTRY/$ALIYUN_NAME_SPACE/$mirror"

        log_info "----- [$total/$total_rows] $source -> $target ====="
        printf '  平台: %s\n' "$platform" >&2
        # local 字段由 30 机拉取脚本使用，Action 不额外打本地 tag
        printf '  本地额外 tag: %s\n' "$local_tag" >&2

        rc=0
        sync_image "$source" "$platform" "$target" || rc=$?

        local timings
        timings="check=$(format_duration "$SYNC_CHECK_SECS") pull=$(format_duration "$SYNC_PULL_SECS") tag=$(format_duration "$SYNC_TAG_SECS") push=$(format_duration "$SYNC_PUSH_SECS")"
        case "$rc" in
            0)
                pushed=$((pushed + 1))
                printf '[OK] %s 同步成功（%s）\n' "$target" "$timings" >&2
                ;;
            1)
                skipped=$((skipped + 1))
                printf '[SKIP] %s 已是最新，跳过（%s）\n' "$target" "$timings" >&2
                ;;
            2)
                failed=$((failed + 1))
                failures+=("$source: pull 失败（${timings}）")
                printf '[FAIL] %s 拉取失败（%s）\n' "$source" "$timings" >&2
                ;;
            3)
                failed=$((failed + 1))
                failures+=("$source: tag 失败（${timings}）")
                printf '[FAIL] %s 打标签失败（%s）\n' "$target" "$timings" >&2
                ;;
            4)
                failed=$((failed + 1))
                failures+=("$source: push 失败（${timings}）")
                printf '[FAIL] %s 推送失败（%s）\n' "$target" "$timings" >&2
                ;;
            *)
                failed=$((failed + 1))
                failures+=("$source: 未知错误 rc=$rc")
                printf '[FAIL] %s 未知错误 rc=%s\n' "$source" "$rc" >&2
                ;;
        esac
    done

    log_info "===== 汇总：共 ${total} 条，成功 ${pushed}，跳过 ${skipped}，失败 ${failed} ====="
    if ((failed > 0)); then
        log_warn '以下条目同步失败：'
        local item
        for item in "${failures[@]}"; do
            printf '  - %s\n' "$item" >&2
        done
        return 1
    fi
    return 0
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    usage
fi

PYTHON_BIN="${PYTHON:-$DEFAULT_PYTHON}"
main "$@"
