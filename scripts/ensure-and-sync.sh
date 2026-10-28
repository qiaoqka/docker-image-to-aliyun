#!/bin/bash
# 一步到位把指定镜像同步到阿里云：补清单条目 → 提交并 push → 触发 scoped Action → 可选等待。
#
# 为什么需要它：Action 只同步 images.yaml 里的条目，且 push 触发会全量复核 41 条 digest
# （每条 3-4 秒）。这个脚本先用 --only 把范围收窄到目标镜像，再用 [skip ci] 避免 push
# 本身触发一次全量运行。
#
# 用法：
#     scripts/ensure-and-sync.sh [--wait] [--timeout 秒] [--repo owner/name] <镜像>...
#     scripts/ensure-and-sync.sh --local-only python:3.11.9-slim-bookworm  # 只改清单，不提交不触发
#     scripts/ensure-and-sync.sh --dry-run python:3.11.9-slim-bookworm  # 只打印计划
#
# 前置：
#     - 在 docker-image-to-aliyun 仓内执行（需要 images.yaml、tests/ 与 git push 权限）
#     - 已安装并登录 gh，且有该仓 Actions 写权限
#     - PYTHON 环境能 import yaml（默认 python3）
#
# 退出码：0 表示清单已同步、Action 成功（或未要求等待）；非 0 表示补条目、push、触发或运行失败。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MANIFEST="$REPO_ROOT/images.yaml"
TEST_FILE="$REPO_ROOT/tests/test_image_manifest.py"
WORKFLOW="docker.yml"
PYTHON_BIN="${PYTHON:-python3}"
DEFAULT_TIMEOUT=900
WAIT_TIMEOUT="$DEFAULT_TIMEOUT"
WAIT=0
LOCAL_ONLY=0
DRY_RUN=0
REPO_SLUG=""
IMAGES=()

die() {
    printf '[ERROR] %s\n' "$*" >&2
    exit 1
}

log_info() {
    printf '[INFO] %s\n' "$*" >&2
}

usage() {
    printf '用法：%s [--wait] [--timeout 秒] [--repo owner/name] [--local-only] [--dry-run] <镜像>...\n' "$0"
    printf '示例：%s --wait python:3.11.9-slim-bookworm\n' "$0"
    exit 0
}

while (( $# > 0 )); do
    case "$1" in
        --wait) WAIT=1; shift ;;
        --local-only) LOCAL_ONLY=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --timeout)
            shift
            [[ "${1:-}" =~ ^[0-9]+$ ]] || die '--timeout 需要整数秒'
            WAIT_TIMEOUT="$1"
            shift
            ;;
        --repo)
            shift
            [[ -n "${1:-}" ]] || die '--repo 需要 owner/name'
            REPO_SLUG="$1"
            shift
            ;;
        --help|-h) usage ;;
        -*) die "未知参数：$1" ;;
        *) IMAGES+=("$1"); shift ;;
    esac
done

(( ${#IMAGES[@]} > 0 )) || die '至少需要一个镜像名'

[[ -f "$MANIFEST" ]] || die "清单不存在：$MANIFEST"

log_info "目标镜像：${IMAGES[*]}"

# @desc 解析目标仓库；默认取 origin，避免把仓库名硬编码进脚本。只有触发运行时才需要。
resolve_repo() {
    [[ -n "$REPO_SLUG" ]] && return 0
    local remote_url
    remote_url="$(git -C "$REPO_ROOT" remote get-url origin)" || die '无法读取 origin 远程地址'
    REPO_SLUG="${remote_url#*github.com[:/]}"
    REPO_SLUG="${REPO_SLUG%.git}"
    [[ "$REPO_SLUG" == */* ]] || die "无法从 origin 推断仓库：$REPO_SLUG"
}

# @desc 把缺失条目追加到清单，并同步测试里写死的条目数常量。
# stdout: 新增的 source；exit: 补条目或校验失败为非零
add_missing_entries() {
    local check_args=()
    (( DRY_RUN )) && check_args=(--check)

    local added=""
    added="$("$PYTHON_BIN" "$SCRIPT_DIR/ensure_manifest_entries.py" "$MANIFEST" \
        ${check_args[@]+"${check_args[@]}"} "${IMAGES[@]}")" || die '补清单条目失败'
    if [[ -z "$added" ]]; then
        log_info '清单已包含全部目标镜像，无需改动'
        return 0
    fi

    local count joined
    count="$(printf '%s\n' "$added" | wc -l | tr -d ' ')"
    joined="$(printf '%s\n' "$added" | paste -sd ' ' -)"
    if (( DRY_RUN )); then
        log_info "dry-run：清单缺少 $count 条：$joined"
        return 0
    fi
    log_info "新增 $count 条：$joined"

    # 新增条目都不带 platform，展开后正好多 count 行；常量不同步会让测试变成红灯。
    if [[ -f "$TEST_FILE" ]]; then
        local before after
        before="$(sed -n 's/^REAL_MANIFEST_ENTRY_COUNT = \([0-9][0-9]*\)$/\1/p' "$TEST_FILE")"
        [[ -n "$before" ]] || die "无法读取 $TEST_FILE 的条目数常量"
        after=$(( before + count ))
        sed -i.bak "s/^REAL_MANIFEST_ENTRY_COUNT = $before\$/REAL_MANIFEST_ENTRY_COUNT = $after/" "$TEST_FILE"
        rm -f "${TEST_FILE}.bak"
        log_info "条目数常量：$before -> $after"
    fi

    "$PYTHON_BIN" "$SCRIPT_DIR/image_manifest.py" "$MANIFEST" --only "${IMAGES[@]}" >/dev/null \
        || die '补条目后清单校验失败'
}

commit_and_push() {
    if (( LOCAL_ONLY )); then
        log_info '--local-only：跳过提交、推送与触发'
        return 0
    fi
    if (( DRY_RUN )); then
        # 只报告计划：dry-run 不读 git 状态，允许在没有 git 的副本里跑。
        if [[ -n "$(git -C "$REPO_ROOT" status --porcelain -- "$MANIFEST" "$TEST_FILE" 2>/dev/null || true)" ]]; then
            log_info "dry-run：将提交 ${MANIFEST#"$REPO_ROOT"/} 与 ${TEST_FILE#"$REPO_ROOT"/}"
        fi
        return 0
    fi
    local changed
    changed="$(git -C "$REPO_ROOT" status --porcelain -- "$MANIFEST" "$TEST_FILE")"
    if [[ -z "$changed" ]]; then
        return 0
    fi
    local message
    message="chore: 补充镜像 ${IMAGES[*]}（[skip ci]）"
    git -C "$REPO_ROOT" commit -q -m "$message" -- "$MANIFEST" "$TEST_FILE"
    # [skip ci] 让这次 push 不触发全量运行，下一步的 --only 运行才是唯一一次同步。
    git -C "$REPO_ROOT" push -q origin HEAD:main || die '推送到 origin/main 失败'
    log_info "已推送：$message"
}

# @desc 触发 scoped 运行并可选等待结束。
# exit: gh 缺失、触发失败、超时或运行失败为非零
dispatch_and_wait() {
    if (( DRY_RUN )); then
        log_info "dry-run：gh workflow run $WORKFLOW --repo ${REPO_SLUG:-<origin>} --ref main -f images=${IMAGES[*]}"
        (( WAIT )) && log_info 'dry-run：跳过等待'
        return 0
    fi
    command -v gh >/dev/null 2>&1 || die '需要 gh 命令（触发 Actions）'
    resolve_repo
    log_info "仓库：$REPO_SLUG"

    local before=""
    before="$(gh run list --repo "$REPO_SLUG" --workflow "$WORKFLOW" --event workflow_dispatch \
        --limit 1 --json databaseId --jq '.[0].databaseId // empty')" || die '查询历史运行失败'

    gh workflow run "$WORKFLOW" --repo "$REPO_SLUG" --ref main \
        -f "images=${IMAGES[*]}" >/dev/null || die '触发 workflow 失败'
    log_info "已触发 ${WORKFLOW}（images=${IMAGES[*]}）"

    (( WAIT )) || return 0

    # 新触发的运行不会立刻出现在列表里，先等 id 变化，再轮询状态到截止时间。
    local run_id="" deadline=$(( SECONDS + WAIT_TIMEOUT ))
    while (( SECONDS < deadline )); do
        run_id="$(gh run list --repo "$REPO_SLUG" --workflow "$WORKFLOW" --event workflow_dispatch \
            --limit 1 --json databaseId --jq '.[0].databaseId // empty' 2>/dev/null || true)"
        if [[ -n "$run_id" && "$run_id" != "$before" ]]; then
            break
        fi
        sleep 5
    done
    [[ -n "$run_id" && "$run_id" != "$before" ]] || die "等待运行出现超时（${WAIT_TIMEOUT}s）"

    local status conclusion url
    while (( SECONDS < deadline )); do
        read -r status conclusion url <<< "$(gh run view "$run_id" --repo "$REPO_SLUG" \
            --json status,conclusion,url --jq '[.status, .conclusion, .url] | @tsv')"
        if [[ "$status" == "completed" ]]; then
            log_info "运行结束：$conclusion $url"
            [[ "$conclusion" == "success" ]] || return 1
            return 0
        fi
        sleep 10
    done
    die "等待运行超时（${WAIT_TIMEOUT}s）：$run_id"
}

add_missing_entries
commit_and_push
if (( LOCAL_ONLY )); then
    log_info "完成（本地）：$MANIFEST"
    exit 0
fi
dispatch_and_wait
log_info '完成'
