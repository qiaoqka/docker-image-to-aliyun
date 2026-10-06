"""GitHub/控制端共用的镜像上传诊断实现：原始 HTTP 对照与 Docker 交叉验证。

职责边界：

- 唯一 owner：对专用测试仓 ``{namespace}/image-sync-perf`` 执行一次受控上传样本，测量各阶段墙钟、
  真实发送字节与 ``docker push`` 墙钟，并输出去敏结果。CI 与本机使用同一实现。
- 不做业务镜像同步、不改 ``images.yaml``/``servers.yaml``、不启动业务容器、不做 registry 全局 GC。
- 不产出 D05 的临时 ``DOCKER_CONFIG``/credential helper；密码只走内存请求头与 ``--password-stdin``。

核心工作流程：

1. 校验 registry/auth_origin（HTTPS、精确 host+port），并构造只访问本次仓库的客户端。
2. 预检 ``GET /v2/{namespace}/{repo}/tags/list`` 只是**可读观测**：200 表示仓库已存在；404 且响应体是
   正经的 ``NAME_UNKNOWN`` 时只记一次 unknown/empty 观测（数据面空仓不等同于逻辑仓未建）并继续；
   非 ``NAME_UNKNOWN`` 的 404、401/403、5xx 及其它状态一律 fail-closed。**能否开始上传由 POST 决定**：
   202 且带可信 Location 才继续，403/404 明确失败且不发送任何载荷字节。绝不代建逻辑仓、不预置
   seed、也不改用业务仓库。
3. 流式生成 64 MiB 系统随机**内层**到临时文件；再以 gzip（mtime=0）包装为线体并计算线体 SHA-256。
   ACR 个人版会嗅探 blob 类型，裸随机大 PATCH 返回 ``BLOB_TYPE_INVALID``；线体必须是 gzip。
4. 新鲜度 ``HEAD``：只有 404 才继续；200 判样本 invalid（载荷已存在，不是新鲜样本）；
   401/403/500 判 failed。该阶段之前不发送任何上传请求与 body 字节。
5. ``POST`` 开始上传会话（要求 202），随后用返回的 opaque ``Location`` 做**一次持续 PATCH**
   （Content-Length 为线体精确长度，按块发送并统计真实写入字节）。PATCH 响应先登记经过校验的最新
   Location 供清理使用，再判定：必须 202、必须发满完整线体、必须有语法合法且起点为 0、
   终点等于已发送字节数的 ``Range``；任一条不满足都判失败且绝不提交。
6. ``PUT`` 使用最新 ``Location``，仅在其已有 query 后追加 ``digest`` 参数（已有 query 时用 ``&``），
   要求 201 且 ``Docker-Content-Digest`` 等于本地 digest。发起前先登记“本次 blob 提交结果未知”；
   响应丢失时保留未知残留，绝不写成“未提交”，也绝不重放。
7. ``HEAD`` 验证 200、精确 Content-Length 与 digest 一致。**原始 HTTP 臂到此结束**：它只提交 blob
   并验证落地，不发布任何 manifest（随机二进制不是合法的 Docker config，伪造对象类型不可接受）。
   孤立 blob 以 ``blob:<digest>`` 残留在报告中如实列出。
8. 参考测量：本地 gzip 该载荷的耗时与输出大小。只在 HTTP 臂完整走通后执行，鉴权/新鲜度早退样本跳过；
   gzip 的真实 I/O 失败记录 ``gzip_reference`` failed 阶段并使样本失败（不改变已完成的 HTTP 证据）。
9. 可选 Docker 臂（``http_only=False``）：用同份载荷生成单文件 rootfs tar、``docker import`` 得到
   单层镜像后真实 ``docker push``（独立 tag、独立截止时间、有界流式输出），再读回 manifest 并校验
   config digest 绑定本次 import 的镜像身份、layer descriptor 与 push 输出证据一致；没有真实传输
   证据或命中 layer 去重都判 invalid。
10. ``finally`` 中有界清理：取消本次未完成上传、按“从本次响应原文重新计算的”精确 digest 尝试删除
   本次 Docker manifest、删除本地镜像与文件、沿用既有 ``docker logout``；无法删除的 raw/docker
   blob 精确列入 residuals，不宣称空间已释放、不删 blob/GC/业务对象。清理受拒会使退出码非零，
    但不覆盖主测量原因。

安全约束（不可放宽）：

- 只与 HTTPS 目标通信，始终校验证书与主机名，不提供 insecure 模式，也不跟随任何重定向。
- Bearer challenge 的 realm 只允许 registry 同 origin，或显式配置且 host+port 精确匹配的
  ``auth_origin``；不按域名后缀通配，也不因服务端响应自我扩大信任。realm 自带 query 里的
  ``scope``/``service`` 一律拒绝，避免绕过本仓 pull/push 收窄。Basic 凭据只发往 registry 同
  origin 或该受信认证 origin。
- upload ``Location`` 必须与 registry 严格同 origin；跨 origin、http 降级、302 一律拒绝，
  且绝不向其它 origin 转发 Authorization 或发送载荷。
- 密码/token 只在内存中用于请求头；``Location``、``Authorization``、异常正文与原始命令输出永不落盘。
  结果只包含安全枚举、状态码、耗时、字节数与非能力型标识（upload 只以 SHA-256 短指纹引用）。
- HTTP 响应定界交给标准库 ``http.client.HTTPResponse``（Content-Length / chunked / 连接关闭），
  额外在其读取器上强制绝对 deadline 与 header/body 配额，并拒绝声明长度后提前 EOF 的不完整响应。

用法示例：

    ALIYUN_REGISTRY=registry.cn-hangzhou.aliyuncs.com ALIYUN_NAME_SPACE=<ns> \
    ALIYUN_REGISTRY_USER=<user> ALIYUN_REGISTRY_PASSWORD=<password> \
    python3 diagnostics/push-perf.py --http-only --environment control \
        --run-id "$GITHUB_RUN_ID" --output perf-summary.json --summary perf-summary.md

退出码：样本 valid 且清理完整、且所有要求的产物写出成功时为 0；其余为非零。
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import http.client
import json
import os
import platform
import re
import secrets
import select
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, cast

# --------------------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------------------

# 内层为 64 MiB 不可压缩随机；PATCH/PUT 发送其 gzip 包装（ACR 拒绝裸随机大 body）。
PAYLOAD_SIZE_BYTES = 64 * 1024 * 1024
GZIP_WIRE_COMPRESS_LEVEL = 1

# 专用测试仓名（方案固定），完整仓库路径为 "{namespace}/{REPOSITORY_NAME}"。
REPOSITORY_NAME = "image-sync-perf"

# 发送分块：块越小，越早发现服务端提前拒收，发送字节下界也越精确。
SEND_BLOCK_BYTES = 64 * 1024
PAYLOAD_WRITE_BLOCK_BYTES = 1 << 20
GZIP_COMPRESS_LEVEL = 6
FILE_READ_BLOCK_BYTES = 1 << 20

# 响应读取配额：header 与 body 各自上限，避免对端用无限数据拖垮样本。
MAX_RESPONSE_HEADER_BYTES = 64 * 1024
MAX_RESPONSE_BODY_BYTES = 1 << 20
# 单次底层 recv 的读取块：每次 recv 前都会按剩余预算重设 socket 超时。
SOCKET_READ_BLOCK_BYTES = 64 * 1024

DEFAULT_HTTPS_PORT = 443
DEFAULT_CONNECT_TIMEOUT_SECONDS = 30.0
DEFAULT_UPLOAD_TIMEOUT_SECONDS = 2100.0
# 清理必须有界：整体预算与单次请求预算分开，避免 90min job 被清理拖死。
CLEANUP_TIMEOUT_SECONDS = 20.0
CLEANUP_REQUEST_TIMEOUT_SECONDS = 5.0
DOCKER_PROBE_TIMEOUT_SECONDS = 30.0
# Docker 命令输出按块流式消费，只保留有界尾部（供 import/version 这类小输出解析）。
DOCKER_READ_BLOCK_BYTES = 64 * 1024
DOCKER_OUTPUT_TAIL_BYTES = 4096
DOCKER_MAX_DIGEST_LINES = 16

MANIFEST_MEDIA_TYPE = "application/vnd.docker.distribution.manifest.v2+json"
MANIFEST_ACCEPT = ", ".join(
    [
        MANIFEST_MEDIA_TYPE,
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    ]
)

_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_TAG_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
_REPOSITORY_PATTERN = re.compile(r"^[a-z0-9]+(?:(?:[._-])[a-z0-9]+)*$")
_HOST_PORT_PATTERN = re.compile(r"^(?P<host>[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)(?::(?P<port>[0-9]{1,5}))?$")
_SCOPE_PATTERN = re.compile(r"^repository:(?P<name>[a-z0-9]+(?:(?:[._/-])[a-z0-9]+)*):(?P<actions>[a-z,]+)$")
_RANGE_PATTERN = re.compile(r"^(?P<start>[0-9]+)-(?P<end>[0-9]+)$")
_SAFE_UPLOAD_ACTIONS = frozenset({"pull", "push"})
_VERSION_PATTERN = re.compile(r"^[0-9A-Za-z._+-]{1,32}$")
_GIT_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
# realm 的 query 里出现这些认证保留参数时整段拒绝，避免绕过本仓 scope 收窄。
_FORBIDDEN_REALM_QUERY_KEYS = frozenset({"scope", "service"})

# Docker push 输出标记：只用于判定去重与补充证据，原始输出不落盘。
_DOCKER_DEDUP_MARKERS = ("Layer already exists", "Mounted from")
_DOCKER_PUSH_MARKER = "Pushed"
_DOCKER_DIGEST_LINE_PATTERN = re.compile(rb"digest:\s*(sha256:[0-9a-f]{64})\s+size:\s*([0-9]+)")

# 允许出现在结果 reason 里的安全枚举：绝不包含 URL、token、异常正文。
REASON_OK = "ok"
REASON_FRESH_BLOB_EXISTS = "fresh_blob_exists"
REASON_INVALID_CONFIG = "invalid_config"
REASON_REPOSITORY_UNKNOWN = "repository_unknown"
REASON_REPOSITORY_UNREACHABLE = "repository_unreachable"
REASON_ACCESS_DENIED = "access_denied"
REASON_AUTH_CHALLENGE_MISSING = "auth_challenge_missing"
REASON_UNTRUSTED_AUTH_REALM = "untrusted_auth_realm"
REASON_TOKEN_MISSING = "token_missing"
REASON_TOKEN_REJECTED = "token_rejected"
REASON_CONNECTION_FAILED = "connection_failed"
REASON_TLS_FAILED = "tls_failed"
REASON_PROTOCOL_ERROR = "protocol_error"
REASON_DEADLINE_EXCEEDED = "deadline_exceeded"
REASON_FRESHNESS_HEAD_UNEXPECTED = "freshness_head_unexpected_status"
REASON_UPLOAD_SESSION_REJECTED = "upload_session_rejected"
REASON_UNTRUSTED_LOCATION = "untrusted_location"
REASON_UNEXPECTED_REDIRECT = "unexpected_redirect"
REASON_UPLOAD_PATCH_REJECTED = "upload_patch_rejected"
REASON_UPLOAD_SEND_FAILED = "upload_send_failed"
REASON_UPLOAD_INCOMPLETE_SEND = "upload_incomplete_send"
REASON_UPLOAD_RANGE_MISSING = "upload_range_missing"
REASON_UPLOAD_RANGE_MISMATCH = "upload_range_mismatch"
REASON_COMMIT_REJECTED = "commit_rejected"
REASON_COMMIT_DIGEST_MISMATCH = "commit_digest_mismatch"
REASON_COMMIT_RESULT_UNKNOWN = "commit_result_unknown"
REASON_VERIFY_MISMATCH = "verify_mismatch"
REASON_PAYLOAD_FAILED = "payload_generation_failed"
REASON_GZIP_FAILED = "gzip_failed"
REASON_DOCKER_UNAVAILABLE = "docker_unavailable"
REASON_DOCKER_PREPARE_FAILED = "docker_prepare_failed"
REASON_DOCKER_LOGIN_FAILED = "docker_login_failed"
REASON_DOCKER_PUSH_FAILED = "docker_push_failed"
REASON_DOCKER_DEADLINE_EXCEEDED = "docker_deadline_exceeded"
REASON_DOCKER_NO_UPLOAD_EVIDENCE = "docker_no_upload_evidence"
REASON_DOCKER_LAYER_DEDUPLICATED = "docker_layer_deduplicated"
REASON_DOCKER_IDENTITY_UNKNOWN = "docker_identity_unknown"
REASON_CLEANUP_INCOMPLETE = "cleanup_incomplete"
REASON_UNEXPECTED_ERROR = "unexpected_error"

STATUS_VALID = "valid"
STATUS_INVALID = "invalid"
STATUS_FAILED = "failed"

STAGE_OK = "ok"
STAGE_FAILED = "failed"
STAGE_SKIPPED = "skipped"
STAGE_DEADLINE = "deadline_exceeded"
# 观测型阶段状态：拿到合法的空仓/未知信号时不阻断样本，只留下可查的观测。
STAGE_OBSERVED = "observed"

CLEANUP_OK = "ok"
CLEANUP_PARTIAL = "partial"
CLEANUP_SKIPPED = "skipped"

LOGOUT_OK = "ok"
LOGOUT_FAILED = "failed"
LOGOUT_SKIPPED = "skipped"

# 阶段名：消费方不断言具体字符串，但保持稳定便于查询。
STAGE_REPOSITORY_PRECHECK = "repository_precheck"
STAGE_PAYLOAD = "payload_generation"
STAGE_FRESHNESS = "freshness_head"
STAGE_UPLOAD_POST = "upload_post"
STAGE_UPLOAD_PATCH = "upload_patch"
STAGE_UPLOAD_PUT = "upload_put"
STAGE_VERIFY = "verify_head"
STAGE_GZIP = "gzip_reference"
STAGE_DOCKER_PREPARE = "docker_prepare"
STAGE_DOCKER_LOGIN = "docker_login"
STAGE_DOCKER_PUSH = "docker_push"
STAGE_DOCKER_LOGOUT = "docker_logout"


# --------------------------------------------------------------------------------------
# 配置与凭据
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """一次样本的运行配置。

    Args:
        registry: registry 目标，接受 ``host[:port]`` 或 ``https://origin``。
        namespace: 阿里云命名空间，专用测试仓就建在其下。
        environment: 运行环境标识（控制端用 ``control``，CI 用 runner 标识）。
        run_id: 本次运行标识，用于生成唯一 tag；为空时用环境标识代替。
        http_only: 只跑原始 HTTP 臂；False 时追加 Docker 交叉验证臂。
        connect_timeout: 单次连接与 TLS 握手的上限（秒）。
        upload_timeout: 原始 HTTP 臂的总截止时间（秒）；整个臂共享同一绝对截止时间。
        auth_origin: 唯一额外受信的认证 origin（如 ACR 官方 realm）；None 表示只信任 registry 同 origin。

    两个臂各自持有独立预算：原始 HTTP 臂和 Docker 臂分别从各自起点计算 ``upload_timeout`` 秒的
    绝对截止时间，互不共享，也不从 HTTP 起点推算。
    """

    registry: str
    namespace: str
    environment: str = "control"
    run_id: str = ""
    http_only: bool = True
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    upload_timeout: float = DEFAULT_UPLOAD_TIMEOUT_SECONDS
    auth_origin: str | None = None


@dataclass(frozen=True)
class Credentials:
    """registry 凭据；密码不参与 repr，避免日志与异常泄漏。"""

    username: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class Origin:
    """已校验的 HTTPS origin（scheme 恒为 https）。"""

    host: str
    port: int

    @property
    def netloc(self) -> str:
        """返回 host[:port] 形式（默认端口 443 省略）。"""
        if self.port == DEFAULT_HTTPS_PORT:
            return self.host
        return f"{self.host}:{self.port}"

    @property
    def url(self) -> str:
        """返回 ``https://host[:port]``。"""
        return f"https://{self.netloc}"

    def host_header(self) -> str:
        """返回请求 Host 头取值。"""
        return self.netloc

    def matches(self, other: Origin) -> bool:
        """判断两个 origin 的 host 与 port 是否完全一致。"""
        return self.host == other.host and self.port == other.port


class _SampleError(Exception):
    """样本按预期失败时抛出的错误；只暴露安全 reason 枚举。"""

    def __init__(self, reason: str, *, status: str = STATUS_FAILED) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class _DeadlineExceeded(_SampleError):
    """阶段或整臂的绝对截止时间已到。"""

    def __init__(self, reason: str = REASON_DEADLINE_EXCEEDED) -> None:
        super().__init__(reason)


class _TransportError(_SampleError):
    """连接、TLS、HTTP 定界或响应读取层面的失败。"""


# --------------------------------------------------------------------------------------
# origin、目标与标识校验
# --------------------------------------------------------------------------------------


def _parse_registry(value: str) -> Origin:
    """把 registry/auth_origin 配置解析成受信 Origin。

    只接受 ``host[:port]`` 或 ``https://host[:port]``；不接受 userinfo、path、query、fragment、
    控制字符与非 https scheme。

    Raises:
        _SampleError: 配置为空、scheme 非 https、含非法成分或端口越界时。
    """
    text = value.strip()
    if not text:
        raise _SampleError(REASON_INVALID_CONFIG)
    if any(character.isspace() or ord(character) < 32 for character in text):
        raise _SampleError(REASON_INVALID_CONFIG)
    if "://" in text:
        scheme, _, remainder = text.partition("://")
        if scheme.lower() != "https":
            raise _SampleError(REASON_INVALID_CONFIG)
        remainder = remainder[:-1] if remainder.endswith("/") else remainder
        text = remainder
    for forbidden in ("/", "?", "#", "@"):
        if forbidden in text:
            raise _SampleError(REASON_INVALID_CONFIG)
    matched = _HOST_PORT_PATTERN.match(text)
    if matched is None:
        raise _SampleError(REASON_INVALID_CONFIG)
    host = matched.group("host").lower()
    if ".." in host or host.startswith(".") or host.endswith("."):
        raise _SampleError(REASON_INVALID_CONFIG)
    raw_port = matched.group("port")
    port = DEFAULT_HTTPS_PORT if raw_port is None else int(raw_port)
    if not 1 <= port <= 65535:
        raise _SampleError(REASON_INVALID_CONFIG)
    return Origin(host=host, port=port)


def _parse_optional_origin(value: str | None) -> Origin | None:
    """解析可选的额外认证 origin；沿用 registry 的严格校验。"""
    if value is None or not value.strip():
        return None
    return _parse_registry(value)


def _parse_realm(value: str) -> tuple[Origin, str]:
    """把 Bearer challenge 的 realm 解析成 Origin 与目标路径。

    允许路径；拒绝明文 scheme、userinfo、fragment、控制字符与非法端口；realm 自带 query 里出现
    ``scope``/``service`` 时整段拒绝——否则受信 origin 上的 realm 就能绕过本仓权限收窄。

    Raises:
        _SampleError: realm 不合法或携带认证保留参数时。
    """
    text = value.strip()
    if any(character.isspace() or ord(character) < 32 for character in text):
        raise _SampleError(REASON_UNTRUSTED_AUTH_REALM)
    parts = urllib.parse.urlsplit(text)
    if parts.scheme.lower() != "https" or not parts.netloc or parts.fragment:
        raise _SampleError(REASON_UNTRUSTED_AUTH_REALM)
    if "@" in parts.netloc:
        raise _SampleError(REASON_UNTRUSTED_AUTH_REALM)
    if parts.query:
        try:
            query_keys = {name.lower() for name, _ in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)}
        except ValueError as error:
            raise _SampleError(REASON_UNTRUSTED_AUTH_REALM) from error
        if query_keys & _FORBIDDEN_REALM_QUERY_KEYS:
            raise _SampleError(REASON_UNTRUSTED_AUTH_REALM)
    host = (parts.hostname or "").lower()
    if not host:
        raise _SampleError(REASON_UNTRUSTED_AUTH_REALM)
    try:
        port = parts.port
    except ValueError as error:
        raise _SampleError(REASON_UNTRUSTED_AUTH_REALM) from error
    resolved_port = DEFAULT_HTTPS_PORT if port is None else port
    if not 1 <= resolved_port <= 65535:
        raise _SampleError(REASON_UNTRUSTED_AUTH_REALM)
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"
    return Origin(host=host, port=resolved_port), target


def _resolve_location(base_origin: Origin, base_target: str, location: str) -> tuple[Origin, str]:
    """把服务端下发的 Location 解析为目标 origin 与目标路径（query 原样保留）。

    Args:
        base_origin: 返回该 Location 的请求 origin。
        base_target: 返回该 Location 的请求目标（path?query）。
        location: 响应头 Location 的原始取值。

    Returns:
        ``(origin, target)``，target 保留原编码与参数顺序。

    Raises:
        _SampleError: 含控制字符、非 https、带 fragment/userinfo、端口非法或无法解析时。
    """
    text = location.strip()
    if not text or any(character.isspace() or ord(character) < 32 for character in text):
        raise _SampleError(REASON_UNTRUSTED_LOCATION)
    absolute = urllib.parse.urljoin(f"{base_origin.url}{base_target}", text)
    parts = urllib.parse.urlsplit(absolute)
    if parts.scheme.lower() != "https" or parts.fragment or "@" in parts.netloc:
        raise _SampleError(REASON_UNTRUSTED_LOCATION)
    host = (parts.hostname or "").lower()
    if not host:
        raise _SampleError(REASON_UNTRUSTED_LOCATION)
    try:
        port = parts.port
    except ValueError as error:
        raise _SampleError(REASON_UNTRUSTED_LOCATION) from error
    resolved_port = DEFAULT_HTTPS_PORT if port is None else port
    if not 1 <= resolved_port <= 65535:
        raise _SampleError(REASON_UNTRUSTED_LOCATION)
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"
    return Origin(host=host, port=resolved_port), target


def _append_digest_parameter(target: str, digest: str) -> str:
    """在 opaque upload 目标上追加 digest 参数，保留已有 query 的原始形式。

    Args:
        target: 最新 Location 的 path?query。
        digest: 本次载荷 digest。

    Returns:
        追加 digest 后的目标；已有 query 时用 ``&`` 连接。

    Raises:
        _SampleError: 已有 digest 参数且与本次 digest 冲突时（绝不覆盖服务端状态）。
    """
    path, separator, query = target.partition("?")
    if separator and query:
        try:
            existing = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))
        except ValueError as error:
            raise _SampleError(REASON_UNTRUSTED_LOCATION) from error
        if "digest" in existing:
            if existing["digest"] == digest:
                return target
            raise _SampleError(REASON_UNTRUSTED_LOCATION)
        return f"{path}?{query}&digest={urllib.parse.quote(digest, safe='')}"
    return f"{path}?digest={urllib.parse.quote(digest, safe='')}"


def _parse_range(value: str) -> tuple[int, int] | None:
    """解析 PATCH 响应 Range 的起点与结束端点（含端点），兼容可选 ``bytes=`` 前缀。

    Returns:
        ``(start, end)``；语法不合法时返回 None（缺失或畸形 Range 都不能作为成功证据）。
    """
    text = value.strip()
    if text.lower().startswith("bytes="):
        text = text[len("bytes=") :]
    matched = _RANGE_PATTERN.match(text)
    if matched is None:
        return None
    return int(matched.group("start")), int(matched.group("end"))


def _is_plain_digest(expected: str, actual: str | None) -> bool:
    """判断响应 digest 是否与期望 digest 一致（大小写与空白容错）。"""
    if actual is None:
        return False
    return actual.strip().lower() == expected.lower()


def _is_name_unknown_body(body: bytes) -> bool:
    """判断 404 响应体是否是正经的 ``NAME_UNKNOWN`` 错误码。

    只有 Registry API 明确回 ``errors[].code == "NAME_UNKNOWN"`` 才算数据面的空仓/未知观测；
    body 不可解析、结构不符或换成别的错误码，都不能当作空仓放行。
    """
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return False
    return any(isinstance(entry, dict) and entry.get("code") == "NAME_UNKNOWN" for entry in errors)


def _canonical_scope_actions(actions: list[str]) -> list[str]:
    """把允许的动作集合规范化为 push 在前的顺序（服务端按集合解释，顺序无语义）。"""
    ordered: list[str] = []
    for action in ("push", "pull"):
        if action in actions and action not in ordered:
            ordered.append(action)
    for action in actions:
        if action not in ordered:
            ordered.append(action)
    return ordered


def _tag_from_identity(run_id: str, environment: str, suffix: str = "") -> str:
    """生成符合 registry tag 规则且唯一的 tag；``suffix`` 用于区分不同臂。"""
    base = re.sub(r"[^A-Za-z0-9._-]", "-", run_id or environment).strip(".-")
    if not base or not (base[0].isalnum() or base[0] == "_"):
        base = f"run-{base}" if base else "run"
    tail = f"{suffix}-{secrets.token_hex(4)}" if suffix else secrets.token_hex(4)
    tag = f"{base[:72]}-{tail}"
    if not _TAG_PATTERN.match(tag):
        tag = f"run-{secrets.token_hex(6)}"
    return tag


def _safe_upload_reference(location: str) -> str:
    """把 upload Location 映射成不可逆、无能力的短引用。

    签名 query 属于能力凭证，这里只输出 SHA-256 短指纹，绝不输出原文。
    """
    return hashlib.sha256(location.encode("utf-8", "surrogatepass")).hexdigest()[:12]


# --------------------------------------------------------------------------------------
# 有界响应读取器（配合标准库 HTTPResponse 做 HTTP 定界）
# --------------------------------------------------------------------------------------


class _BoundedReader:
    """``HTTPResponse`` 使用的有界字节源：直接驱动 socket 上的原始读取。

    标准库 ``makefile`` 缓冲读取器会在单次 ``readline``/``read`` 调用内部做多次底层读取，只受调用前
    设置一次的超时约束；对端缓慢滴流时，一连串“各自未超时”的底层读取会把总耗时推到远超绝对预算。
    这里改为每次底层 ``recv`` 之前都按剩余预算重设 socket 超时，使任何循环都被同一条绝对 deadline
    收敛；HTTP 定界（Content-Length / chunked / 连接关闭）仍完全由 ``HTTPResponse`` 负责。
    """

    def __init__(self, sock: ssl.SSLSocket, deadline: float, initial: bytes = b"") -> None:
        self._sock = sock
        self._deadline = deadline
        self._pending = bytearray(initial)
        self._header_bytes = 0
        self._body_bytes = 0

    def _recv(self) -> bytes:
        """一次底层读取：每次调用前重算剩余预算并重设 socket 超时。"""
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise _DeadlineExceeded()
        self._sock.settimeout(remaining)
        try:
            return self._sock.recv(SOCKET_READ_BLOCK_BYTES)
        except (TimeoutError, socket.timeout) as error:
            raise _DeadlineExceeded() from error
        except OSError as error:
            raise _TransportError(REASON_CONNECTION_FAILED) from error

    def _recv_into_pending(self) -> bool:
        """把一次底层读取并入内部缓冲；EOF 时返回 False。"""
        chunk = self._recv()
        if not chunk:
            return False
        self._pending += chunk
        return True

    def _count_header(self, size: int) -> None:
        """累计 header 字节并强制上限。"""
        self._header_bytes += size
        if self._header_bytes > MAX_RESPONSE_HEADER_BYTES:
            raise _TransportError(REASON_PROTOCOL_ERROR)

    def _count_body(self, size: int) -> None:
        """累计 body 字节并强制上限。"""
        self._body_bytes += size
        if self._body_bytes > MAX_RESPONSE_BODY_BYTES:
            raise _TransportError(REASON_PROTOCOL_ERROR)

    def _take_pending(self, size: int) -> bytes:
        """取出内部缓冲中的字节；``size < 0`` 表示全部取出。"""
        if not self._pending:
            return b""
        if size < 0 or size >= len(self._pending):
            data = bytes(self._pending)
            self._pending.clear()
            return data
        data = bytes(self._pending[:size])
        del self._pending[:size]
        return data

    def readline(self, limit: int = -1) -> bytes:
        """读取一行（状态行/chunk 长度/trailer）；循环中每次底层读取都重算预算。"""
        data = bytearray()
        while True:
            newline = self._pending.find(b"\n")
            if newline != -1:
                take = newline + 1 if limit <= 0 else min(newline + 1, max(limit - len(data), 0))
                data += self._pending[:take]
                del self._pending[:take]
                self._count_header(len(data))
                return bytes(data)
            if limit > 0 and len(self._pending) >= limit - len(data):
                take = limit - len(data)
                data += self._pending[:take]
                del self._pending[:take]
                self._count_header(len(data))
                return bytes(data)
            if len(self._pending) + len(data) > MAX_RESPONSE_HEADER_BYTES:
                raise _TransportError(REASON_PROTOCOL_ERROR)
            if not self._recv_into_pending():
                take = len(self._pending)
                if limit > 0:
                    take = min(take, max(limit - len(data), 0))
                data += self._pending[:take]
                del self._pending[:take]
                self._count_header(len(data))
                return bytes(data)

    def read1(self, size: int = -1) -> bytes:
        """至多一次底层读取的 body 读取，计入 body 配额。"""
        data = self._take_pending(size)
        if not data:
            data = self._recv()
        self._count_body(len(data))
        return data

    def read(self, size: int = -1) -> bytes:
        """读取 body；循环中每次底层读取都重算预算。"""
        if size is None or size < 0:
            chunks = bytearray()
            while len(chunks) < MAX_RESPONSE_BODY_BYTES:
                data = self.read1(MAX_RESPONSE_BODY_BYTES - len(chunks))
                if not data:
                    break
                chunks += data
            return bytes(chunks)
        data = bytearray()
        while len(data) < size:
            chunk = self.read1(size - len(data))
            if not chunk:
                break
            data += chunk
        return bytes(data)

    def readinto(self, buffer: Any) -> int:
        """读取到调用方缓冲区（至多一次底层读取）。"""
        data = self.read1(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def peek(self, size: int = -1) -> bytes:
        """返回缓冲中的数据（必要时先读一次），不消费。"""
        if not self._pending and not self._recv_into_pending():
            return b""
        if size < 0 or size >= len(self._pending):
            return bytes(self._pending)
        return bytes(self._pending[:size])

    def fileno(self) -> int:
        """返回底层 socket 的文件描述符。"""
        return self._sock.fileno()

    def flush(self) -> None:
        """只读流，无需 flush。"""
        return None

    def close(self) -> None:
        """不关闭 socket：连接生命周期由调用方统一管理。"""
        return None


# --------------------------------------------------------------------------------------
# 最小 HTTPS 客户端
# --------------------------------------------------------------------------------------


@dataclass
class _Response:
    """一次 HTTP 响应（body 按上限截断读取）。"""

    status: int
    headers: dict[str, str]
    body: bytes
    body_bytes: int


@dataclass
class _Exchange:
    """一次请求的完整结果，包含 body 发送进度与安全失败原因。"""

    response: _Response | None
    sent_body_bytes: int
    send_uncertain: bool
    error_reason: str | None

    @property
    def status(self) -> int | None:
        """响应状态码；未拿到响应时为 None。"""
        return None if self.response is None else self.response.status


@dataclass
class _Ledger:
    """样本级字节与请求计数（只统计真实交给 socket 的应用 body 数据）。"""

    request_body_bytes: int = 0
    response_body_bytes: int = 0
    requests: int = 0
    body_bytes_uncertain: bool = False


class _Client:
    """单样本 HTTPS 客户端：严格 origin 校验、有限认证协商、绝对截止时间。"""

    def __init__(
        self,
        registry: Origin,
        auth_origin: Origin | None,
        credentials: Credentials,
        tls_context: ssl.SSLContext,
        connect_timeout: float,
        ledger: _Ledger,
        full_repository: str,
    ) -> None:
        self._registry = registry
        self._auth_origin = auth_origin
        self._credentials = credentials
        self._tls_context = tls_context
        self._connect_timeout = connect_timeout
        self._ledger = ledger
        self._full_repository = full_repository
        self._authorization: str | None = None

    @property
    def registry(self) -> Origin:
        """registry origin。"""
        return self._registry

    def basic_header(self) -> str:
        """返回本次凭据的 Basic 头取值（只用于受信 origin 的请求头）。"""
        raw = f"{self._credentials.username}:{self._credentials.password}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    # ------------------------------------------------------------------ 传输层

    def _budget(self, deadline: float, *, connect: bool) -> float:
        """计算本次阻塞调用的时间预算。"""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _DeadlineExceeded()
        return min(remaining, self._connect_timeout) if connect else remaining

    def _resolve(self, origin: Origin, deadline: float) -> list[tuple[int, tuple[Any, ...]]]:
        """在预算内解析主机名。

        标准库 ``getaddrinfo`` 不受 socket 超时约束，因此在受限线程里等待，避免 DNS 挂起
        突破整臂截止时间。
        """
        budget = self._budget(deadline, connect=True)
        results: list[tuple[int, tuple[Any, ...]]] = []
        failures: list[BaseException] = []

        def worker() -> None:
            try:
                for item in socket.getaddrinfo(origin.host, origin.port, type=socket.SOCK_STREAM):
                    results.append((item[0], item[4]))
            except OSError as error:  # pragma: no cover - 取决于本机解析器行为
                failures.append(error)

        thread = threading.Thread(target=worker, name="push-perf-resolve", daemon=True)
        thread.start()
        thread.join(budget)
        if thread.is_alive():
            raise _DeadlineExceeded(REASON_CONNECTION_FAILED)
        if failures or not results:
            raise _TransportError(REASON_CONNECTION_FAILED)
        return results

    def _open(self, origin: Origin, deadline: float) -> ssl.SSLSocket:
        """在预算内完成 DNS、TCP 连接与 TLS 握手（始终校验证书与主机名）。"""
        last_error: OSError | None = None
        for family, address in self._resolve(origin, deadline):
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.settimeout(self._budget(deadline, connect=True))
            try:
                sock.connect(address)
            except OSError as error:
                last_error = error
                sock.close()
                continue
            try:
                sock.settimeout(self._budget(deadline, connect=True))
                return self._tls_context.wrap_socket(sock, server_hostname=origin.host)
            except ssl.SSLError as error:
                sock.close()
                raise _TransportError(REASON_TLS_FAILED) from error
            except OSError as error:
                sock.close()
                raise _TransportError(REASON_CONNECTION_FAILED) from error
        raise _TransportError(REASON_CONNECTION_FAILED) from last_error

    def _send_all(self, tls: ssl.SSLSocket, data: memoryview, deadline: float) -> int:
        """在预算内送出给定字节，返回真实写入的应用字节数。"""
        sent = 0
        while sent < len(data):
            tls.settimeout(self._budget(deadline, connect=False))
            sent += tls.send(data[sent:])
        return sent

    def _peek_application_data(self, tls: ssl.SSLSocket) -> tuple[str, bytes]:
        """非阻塞探测是否已有应用层数据。

        TLS 1.3 的 session ticket 等记账数据也会让 socket 变为可读，但它们不是 HTTP 响应；
        这里用零超时 recv 区分，只有真正拿到应用数据才继续读响应。

        Returns:
            ``("data", chunk)``、``("wanted", b"")``（只有记账数据）、``("closed", b"")``
            或 ``("error", b"")``。
        """
        previous = tls.gettimeout()
        tls.setblocking(False)
        try:
            chunk = tls.recv(MAX_RESPONSE_HEADER_BYTES)
        except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
            return "wanted", b""
        except (ssl.SSLError, OSError):
            return "error", b""
        finally:
            tls.settimeout(previous)
        if not chunk:
            return "closed", b""
        return "data", chunk

    def _send_body(self, method: str, tls: ssl.SSLSocket, source: IO[bytes], size: int, deadline: float) -> _Exchange:
        """按块发送请求 body 并读取响应。

        每块之后探测可读性：服务端可能在读完 headers 或部分 body 后就回 4xx/202 并关闭，必须在
        连接被重置前把状态码读出来。发送字节只统计成功交给 TLS socket 的应用数据；发送中断时
        该计数是下界（最后一块可能部分送出）。

        Returns:
            本次请求的结果；``sent_body_bytes`` 始终是已成功交给 TLS socket 的 body 字节数，
            因此“发完但读响应超时”也会保留阶段证据。
        """
        sent = 0
        while sent < size:
            block = source.read(min(SEND_BLOCK_BYTES, size - sent))
            if not block:
                return _Exchange(None, sent, True, REASON_UPLOAD_SEND_FAILED)
            try:
                written = self._send_all(tls, memoryview(block), deadline)
            except _DeadlineExceeded:
                return _Exchange(None, sent, True, REASON_DEADLINE_EXCEEDED)
            except (TimeoutError, socket.timeout):
                return _Exchange(None, sent, True, REASON_DEADLINE_EXCEEDED)
            except (ssl.SSLError, OSError):
                return _Exchange(None, sent, True, REASON_UPLOAD_SEND_FAILED)
            sent += written
            self._ledger.request_body_bytes += written
            readable, _, _ = select.select([tls], [], [], 0)
            if not readable:
                continue
            kind, initial = self._peek_application_data(tls)
            if kind == "wanted":
                continue
            if kind != "data":
                return _Exchange(None, sent, True, REASON_UPLOAD_SEND_FAILED)
            try:
                response = self._read_response(tls, method, deadline, initial)
            except _SampleError as error:
                reason = error.reason if isinstance(error, _DeadlineExceeded) else REASON_UPLOAD_SEND_FAILED
                return _Exchange(None, sent, True, reason)
            return _Exchange(response, sent, False, None)
        try:
            response = self._read_response(tls, method, deadline)
        except _SampleError as error:
            # body 已完整发送，但响应读取失败：保留已发送字节与真实原因供阶段记录使用。
            return _Exchange(None, sent, False, error.reason)
        return _Exchange(response, sent, False, None)

    def _read_response(
        self, tls: ssl.SSLSocket, method: str, deadline: float, initial: bytes = b""
    ) -> _Response:
        """用标准库 ``HTTPResponse`` 读取响应头与（有界）响应体。

        HTTP 定界（Content-Length / chunked / 连接关闭）交给标准库，本方法只负责：
        注入带绝对 deadline 与配额的读取器、按块读取、拒绝声明长度后提前 EOF 的不完整响应。

        Args:
            initial: 早响应探测时已读到的应用数据，作为读取器前缀。

        Raises:
            _TransportError: 定界/配额/连接层面的失败（含不完整响应）。
            _DeadlineExceeded: 超过绝对截止时间。
        """
        response = http.client.HTTPResponse(tls, method=method)
        original = response.fp
        response.fp = cast(Any, _BoundedReader(tls, deadline, initial))
        if original is not None:
            # 丢弃 makefile 缓冲读取器：它的 readline/read 会在一次调用内做多次底层读取，
            # 无法按每次 recv 重算剩余预算（HTTP-10）。关闭它只释放缓冲对象，不关闭 socket。
            try:
                original.close()
            except OSError:  # pragma: no cover - 关闭失败不影响测量结果
                pass
        try:
            response.begin()
        except http.client.HTTPException as error:
            raise _TransportError(REASON_PROTOCOL_ERROR) from error
        except (TimeoutError, socket.timeout) as error:
            raise _DeadlineExceeded() from error
        except OSError as error:
            raise _TransportError(REASON_CONNECTION_FAILED) from error

        status = response.status
        headers = {name.lower(): value for name, value in response.getheaders()}
        body = bytearray()
        declared = int(headers["content-length"]) if headers.get("content-length", "").isdigit() else None
        readable_body = method != "HEAD" and status >= 200 and status not in {204, 304}
        if readable_body and (declared is None or declared > 0):
            while len(body) < MAX_RESPONSE_BODY_BYTES:
                try:
                    chunk = response.read1(MAX_RESPONSE_BODY_BYTES - len(body))
                except http.client.IncompleteRead as error:
                    raise _TransportError(REASON_PROTOCOL_ERROR) from error
                except (TimeoutError, socket.timeout) as error:
                    raise _DeadlineExceeded() from error
                except _SampleError:
                    raise
                except OSError as error:
                    raise _TransportError(REASON_CONNECTION_FAILED) from error
                if not chunk:
                    break
                body += chunk
        if readable_body and declared is not None and len(body) < min(declared, MAX_RESPONSE_BODY_BYTES):
            # 声明了长度却提前 EOF：不完整响应必须拒绝，不能当成功处理。
            raise _TransportError(REASON_PROTOCOL_ERROR)
        self._ledger.response_body_bytes += len(body)
        return _Response(status=status, headers=headers, body=bytes(body), body_bytes=len(body))

    def _exchange(
        self,
        method: str,
        origin: Origin,
        target: str,
        headers: dict[str, str],
        deadline: float,
        body: IO[bytes] | None = None,
        body_size: int = 0,
    ) -> _Exchange:
        """执行一次请求；绝不跟随重定向，也绝不自动重放带 body 的请求。"""
        tls = self._open(origin, deadline)
        try:
            request_headers = {
                "Host": origin.host_header(),
                "User-Agent": "push-perf/1",
                "Accept": "*/*",
                **headers,
            }
            if body is not None:
                request_headers["Content-Length"] = str(body_size)
            head = [f"{method} {target} HTTP/1.1"]
            head.extend(f"{name}: {value}" for name, value in request_headers.items())
            raw = ("\r\n".join(head) + "\r\n\r\n").encode("latin-1")
            self._send_all(tls, memoryview(raw), deadline)
            self._ledger.requests += 1
            if body is not None:
                return self._send_body(method, tls, body, body_size, deadline)
            return _Exchange(self._read_response(tls, method, deadline), 0, False, None)
        finally:
            try:
                tls.close()
            except OSError:  # pragma: no cover - 关闭失败不影响测量结果
                pass

    # ------------------------------------------------------------------ 认证

    def _parse_challenge(self, header: str | None) -> tuple[str, dict[str, str]] | None:
        """解析 WWW-Authenticate；引号内的逗号不切分，支持反斜杠转义。"""
        if not header:
            return None
        text = header.strip()
        space = text.find(" ")
        if space == -1:
            return text.lower(), {}
        scheme = text[:space].lower()
        params: dict[str, str] = {}
        index = space + 1
        length = len(text)
        while index < length:
            while index < length and text[index] in ", \t":
                index += 1
            equals = text.find("=", index)
            if equals == -1:
                break
            key = text[index:equals].strip().lower()
            index = equals + 1
            while index < length and text[index] in " \t":
                index += 1
            if index < length and text[index] == '"':
                index += 1
                buffer: list[str] = []
                while index < length:
                    character = text[index]
                    if character == "\\" and index + 1 < length:
                        buffer.append(text[index + 1])
                        index += 2
                        continue
                    if character == '"':
                        index += 1
                        break
                    buffer.append(character)
                    index += 1
                params[key] = "".join(buffer)
            else:
                end = index
                while end < length and text[end] != ",":
                    end += 1
                params[key] = text[index:end].strip()
                index = end
        return scheme, params

    def _restricted_scope(self, raw_scope: str, full_repository: str) -> str | None:
        """只保留本次仓库的 pull/push scope；其它仓库或更高权限一律丢弃。

        动作集合按 push 在前规范化：token 服务端把动作当集合解释，顺序无语义。
        """
        kept: list[str] = []
        for entry in raw_scope.split():
            matched = _SCOPE_PATTERN.match(entry)
            if matched is None or matched.group("name") != full_repository:
                continue
            actions = [action for action in matched.group("actions").split(",") if action]
            if not actions or any(action not in _SAFE_UPLOAD_ACTIONS for action in actions):
                continue
            kept.append(f"repository:{matched.group('name')}:{','.join(_canonical_scope_actions(actions))}")
        return " ".join(kept) if kept else None

    def _fetch_token(self, realm: Origin, realm_target: str, params: dict[str, str], deadline: float) -> str:
        """向受信认证 origin 申请 Bearer token；只带 Basic 凭据，绝不转发已有 Authorization。"""
        query = [(name, value) for name, value in params.items() if value]
        query.append(("client_id", "push-perf"))
        separator = "&" if "?" in realm_target else "?"
        token_target = f"{realm_target}{separator}{urllib.parse.urlencode(query)}"
        exchange = self._exchange(
            "GET",
            realm,
            token_target,
            {"Authorization": self.basic_header(), "Accept": "application/json"},
            deadline,
        )
        if exchange.response is None:
            raise _SampleError(exchange.error_reason or REASON_TOKEN_REJECTED)
        if exchange.response.status != 200:
            # 3xx 一律不跟随；非 200 视为 token 端点拒绝。
            raise _SampleError(REASON_TOKEN_REJECTED)
        try:
            payload = json.loads(exchange.response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _SampleError(REASON_TOKEN_MISSING) from error
        if not isinstance(payload, dict):
            raise _SampleError(REASON_TOKEN_MISSING)
        token = payload.get("token") or payload.get("access_token")
        if not isinstance(token, str) or not token.strip():
            raise _SampleError(REASON_TOKEN_MISSING)
        return token

    def _negotiate(self, challenge: tuple[str, dict[str, str]], request_origin: Origin, deadline: float) -> None:
        """按 challenge 完成一次认证协商；凭据只发往受信 origin。"""
        scheme, params = challenge
        if scheme == "basic":
            # Basic 凭据只回到发起挑战的 registry 请求，不额外发起任何连接。
            if not request_origin.matches(self._registry):
                raise _SampleError(REASON_ACCESS_DENIED)
            self._authorization = self.basic_header()
            return
        if scheme != "bearer":
            raise _SampleError(REASON_AUTH_CHALLENGE_MISSING)
        realm, realm_target = _parse_realm(params.get("realm", ""))
        trusted = realm.matches(self._registry) or (
            self._auth_origin is not None and realm.matches(self._auth_origin)
        )
        if not trusted:
            raise _SampleError(REASON_UNTRUSTED_AUTH_REALM)
        token_params: dict[str, str] = {}
        if params.get("service"):
            token_params["service"] = params["service"]
        if params.get("scope"):
            restricted = self._restricted_scope(params["scope"], self._full_repository)
            if restricted is None:
                raise _SampleError(REASON_UNTRUSTED_AUTH_REALM)
            token_params["scope"] = restricted
        self._authorization = f"Bearer {self._fetch_token(realm, realm_target, token_params, deadline)}"

    # ------------------------------------------------------------------ 调用入口

    def call(
        self,
        method: str,
        origin: Origin,
        target: str,
        deadline: float,
        *,
        headers: dict[str, str] | None = None,
    ) -> _Exchange:
        """发送无 body 请求，并在 401 时做一次受控认证协商与重试。"""
        request_headers = dict(headers or {})
        if self._authorization is not None:
            request_headers["Authorization"] = self._authorization
        exchange = self._exchange(method, origin, target, request_headers, deadline)
        if exchange.status == 401:
            challenge = self._parse_challenge(
                None if exchange.response is None else exchange.response.headers.get("www-authenticate")
            )
            if challenge is None:
                raise _SampleError(REASON_AUTH_CHALLENGE_MISSING)
            self._negotiate(challenge, origin, deadline)
            request_headers["Authorization"] = self._authorization or ""
            exchange = self._exchange(method, origin, target, request_headers, deadline)
        return exchange

    def call_with_body(
        self,
        method: str,
        origin: Origin,
        target: str,
        deadline: float,
        *,
        source: IO[bytes],
        size: int,
        headers: dict[str, str] | None = None,
    ) -> _Exchange:
        """发送带 body 的请求；此类请求绝不重放，也不做认证重试。"""
        request_headers = dict(headers or {})
        if self._authorization is not None:
            request_headers["Authorization"] = self._authorization
        return self._exchange(method, origin, target, request_headers, deadline, body=source, body_size=size)


# --------------------------------------------------------------------------------------
# Docker 命令的有界执行
# --------------------------------------------------------------------------------------


@dataclass
class _CommandResult:
    """有界命令执行结果：只保留必要状态与有界尾部，不保存完整原始输出。"""

    returncode: int | None
    timed_out: bool
    output_bytes: int
    markers: dict[str, bool]
    digest_lines: list[tuple[str, int]]
    output_tail: bytes


class _StdinPump:
    """在命令的同一预算内、非阻塞地把 stdin 交给子进程。

    越界风险在于：stdin 写入若发生在 deadline 与 kill/wait 作用域之外，子进程不读取输入时
    write 会无限等待，对端提前关闭又会以 BrokenPipeError 绕过进程回收。这里改为非阻塞写入，
    每次只写出内核当前能接受的部分，阻塞与对端关闭都立即返回，回收交给外层 finally。

    ``os.set_blocking`` 不可用的平台上退化为一次阻塞写入（仅用于短密码场景）。
    """

    def __init__(self, stream: IO[bytes], payload: bytes) -> None:
        self._stream = stream
        self._pending = memoryview(payload)
        self._nonblocking = True
        try:
            os.set_blocking(stream.fileno(), False)
        except (OSError, ValueError):
            self._nonblocking = False

    @property
    def done(self) -> bool:
        """剩余输入已写完或对端已关闭时为 True。"""
        return not self._pending

    def pump(self) -> None:
        """尽力写出当前可接受的部分；阻塞或对端关闭都立即返回，绝不长时间等待。"""
        if not self._pending:
            return
        if not self._nonblocking:  # pragma: no cover - 仅无 os.set_blocking 的平台
            try:
                self._stream.write(bytes(self._pending))
            except OSError:
                pass
            self._pending = memoryview(b"")
            return
        while self._pending:
            try:
                written = os.write(self._stream.fileno(), self._pending)
            except (BlockingIOError, InterruptedError):
                return
            except (BrokenPipeError, OSError):
                # 对端已关闭 stdin：放弃剩余输入，进程回收仍由外层 finally 负责。
                self._pending = memoryview(b"")
                return
            if written <= 0:
                self._pending = memoryview(b"")
                return
            self._pending = self._pending[written:]

    def close(self) -> None:
        """关闭 stdin（子进程据此看到 EOF）；关闭失败不影响进程回收。"""
        try:
            self._stream.close()
        except OSError:
            pass


def _keep_unterminated(region: bytes) -> bytes:
    """返回尚未以换行结束的尾部（跨块保留），并限制其长度上限。"""
    end = region.rfind(b"\n")
    remainder = region[end + 1 :] if end != -1 else region
    if len(remainder) > DOCKER_OUTPUT_TAIL_BYTES:
        return remainder[-DOCKER_OUTPUT_TAIL_BYTES:]
    return remainder


def _scan_docker_output(
    region: bytes,
    *,
    allow_unterminated: bool,
    markers: tuple[str, ...],
    marker_hits: dict[str, bool],
    digest_hits: list[tuple[str, int]],
    seen_digests: set[str],
) -> None:
    """在「上一轮未终结残留 + 本次完整新块」上解析标记与 digest 行。

    digest 行的 size 只有在该行以换行结束后才提交，避免把 ``size: 6`` 当成完整数字；
    ``allow_unterminated`` 只在输出已经结束（不会再有新字节）时为 True。
    """
    for marker in markers:
        if marker.encode("ascii") in region:
            marker_hits[marker] = True
    parse_region = region
    if not allow_unterminated:
        end = region.rfind(b"\n")
        if end == -1:
            return
        parse_region = region[: end + 1]
    for match in _DOCKER_DIGEST_LINE_PATTERN.finditer(parse_region):
        digest = match.group(1).decode("ascii")
        if digest in seen_digests or len(digest_hits) >= DOCKER_MAX_DIGEST_LINES:
            continue
        seen_digests.add(digest)
        digest_hits.append((digest, int(match.group(2))))


def _run_bounded(
    arguments: list[str],
    timeout: float,
    *,
    stdin_bytes: bytes | None = None,
    capture_tail: bool = False,
    markers: tuple[str, ...] = (_DOCKER_PUSH_MARKER, *_DOCKER_DEDUP_MARKERS),
) -> _CommandResult:
    """在绝对 deadline 内流式消费命令输出，超时终止并回收。

    deadline 与 kill/wait 作用域在**任何 stdin I/O 之前**建立；stdin 在同一预算内非阻塞写入，
    子进程不读取或提前关闭 stdin 都不会绕过超时与回收。输出按块读取，每轮先在「上一轮未终结
    残留 + 本次完整新块」上解析标记与 digest 行（不丢弃未解析字节），再裁剪出下一轮残留。
    仅在 ``capture_tail`` 时保留有界尾部；原始输出永不进入结果。
    """
    result_markers: dict[str, bool] = {}
    digest_lines: list[tuple[str, int]] = []
    seen_digests: set[str] = set()
    process = subprocess.Popen(
        arguments,
        stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    handle = process.stdout
    pump = _StdinPump(process.stdin, stdin_bytes) if process.stdin is not None and stdin_bytes else None
    deadline = time.monotonic() + timeout
    window = b""
    tail = bytearray()
    output_bytes = 0
    timed_out = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if pump is not None:
                pump.pump()
                if pump.done:
                    pump.close()
                    pump = None
            readable, _, _ = select.select([handle], [], [], min(remaining, 0.5))
            if readable:
                chunk = os.read(handle.fileno(), DOCKER_READ_BLOCK_BYTES)
                if not chunk:
                    break
                output_bytes += len(chunk)
                _scan_docker_output(
                    window + chunk,
                    allow_unterminated=False,
                    markers=markers,
                    marker_hits=result_markers,
                    digest_hits=digest_lines,
                    seen_digests=seen_digests,
                )
                window = _keep_unterminated(window + chunk)
                if capture_tail:
                    tail += chunk
                    if len(tail) > DOCKER_OUTPUT_TAIL_BYTES:
                        del tail[: len(tail) - DOCKER_OUTPUT_TAIL_BYTES]
                continue
            if process.poll() is not None:
                break
        if window and not timed_out:
            # 输出已结束：未终结的残留也解析一次，避免最后一行没有换行时漏判。
            _scan_docker_output(
                window,
                allow_unterminated=True,
                markers=markers,
                marker_hits=result_markers,
                digest_hits=digest_lines,
                seen_digests=seen_digests,
            )
    finally:
        if pump is not None:
            pump.close()
        if timed_out or process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:  # pragma: no cover - 已 kill 后仍不退出
            process.kill()
            process.wait(timeout=10.0)
        handle.close()
    return _CommandResult(
        returncode=process.returncode,
        timed_out=timed_out,
        output_bytes=output_bytes,
        markers=result_markers,
        digest_lines=digest_lines,
        output_tail=bytes(tail),
    )


# --------------------------------------------------------------------------------------
# 样本执行
# --------------------------------------------------------------------------------------


@dataclass
class _StageRecord:
    """单个阶段的去敏结果。"""

    name: str
    status: str
    http_status: int | None
    seconds: float
    body_bytes: int

    def as_dict(self) -> dict[str, object]:
        """转换为结果里的 stages 元素。"""
        return {
            "name": self.name,
            "status": self.status,
            "http_status": self.http_status,
            "seconds": round(self.seconds, 6),
            "body_bytes": self.body_bytes,
        }


class _SampleRun:
    """一次上传样本的完整执行：测量、证据收集与有界清理。"""

    def __init__(self, config: Config, credentials: Credentials, tls_context: ssl.SSLContext | None) -> None:
        self._config = config
        self._credentials = credentials
        self._tls_context = tls_context if tls_context is not None else ssl.create_default_context()
        self._stages: list[_StageRecord] = []
        self._ledger = _Ledger()
        self._status = STATUS_FAILED
        self._reason = REASON_OK
        self._errors: list[str] = []
        self._arm_started = time.monotonic()
        self._tempdir: Path | None = None
        self._payload_path: Path | None = None
        self._inner_path: Path | None = None
        self._inner_size = 0
        self._payload_digest = ""
        self._payload_size = 0
        self._repository = REPOSITORY_NAME
        self._full_repository = ""
        self._tag = ""
        self._docker_tag = ""
        self._origin: Origin | None = None
        self._auth_origin: Origin | None = None
        self._client: _Client | None = None
        self._upload_target: str | None = None
        self._upload_reference = ""
        self._upload_completed = False
        # blob 提交状态：未尝试 / 已确认提交 / 提交结果未知（响应丢失）。
        self._blob_commit_confirmed = False
        self._blob_commit_unknown = False
        self._docker_manifest_digest: str | None = None
        self._docker_layer_digest: str | None = None
        self._docker_config_digest: str | None = None
        self._docker_push_unknown = False
        self._cleanup_status = CLEANUP_SKIPPED
        self._residuals: list[str] = []
        self._cleanup_errors: list[str] = []
        self._gzip: dict[str, object] = {"status": STAGE_SKIPPED}
        self._docker: dict[str, object] = {"status": STAGE_SKIPPED}
        self._versions: dict[str, object] = {}
        self._registry_api_version = ""
        self._local_removed = False
        self._docker_login_attempted = False
        self._docker_logout = LOGOUT_SKIPPED
        self._docker_image_created = False

    # ------------------------------------------------------------------ 小工具

    def _record(self, name: str, status: str, http_status: int | None, seconds: float, body_bytes: int = 0) -> None:
        """登记一个阶段结果。"""
        self._stages.append(
            _StageRecord(
                name=name,
                status=status,
                http_status=http_status,
                seconds=seconds,
                body_bytes=body_bytes,
            )
        )

    def _arm_deadline(self) -> float:
        """原始 HTTP 臂的绝对截止时间（整臂共享，不随阶段重置）。"""
        return self._arm_started + self._config.upload_timeout

    def _request_deadline(self) -> float:
        """清理期每条请求的独立截止时间（与已过期的臂 deadline 解耦）。"""
        return time.monotonic() + CLEANUP_REQUEST_TIMEOUT_SECONDS

    def _docker_deadline(self) -> float:
        """Docker 臂的绝对截止时间：从本臂起点独立计算 upload_timeout 秒。"""
        return time.monotonic() + self._config.upload_timeout

    def _transport_stage_status(self, reason: str) -> str:
        """把传输层安全失败原因映射成阶段状态。"""
        return STAGE_DEADLINE if reason == REASON_DEADLINE_EXCEEDED else STAGE_FAILED

    def _safe_call(
        self,
        method: str,
        target: str,
        deadline: float,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[_Exchange | None, str | None]:
        """执行无 body 请求，把传输层安全失败转成 reason。

        阶段记录必须先于异常抛出：否则连接/截止失败会越过阶段登记，使故障证据不可用。
        """
        assert self._client is not None and self._origin is not None
        try:
            return self._client.call(method, self._origin, target, deadline, headers=headers), None
        except _SampleError as error:
            return None, error.reason

    # ------------------------------------------------------------------ 载荷

    def _generate_payload(self) -> None:
        """生成 64 MiB 内层随机，再 gzip 包装为线体；digest 以线体为准。"""
        started = time.monotonic()
        assert self._tempdir is not None
        inner = self._tempdir / "payload.bin"
        written = 0
        try:
            with inner.open("wb") as handle:
                while written < PAYLOAD_SIZE_BYTES:
                    block = os.urandom(min(PAYLOAD_WRITE_BLOCK_BYTES, PAYLOAD_SIZE_BYTES - written))
                    handle.write(block)
                    written += len(block)
            wire = self._tempdir / "payload.bin.gz"
            with inner.open("rb") as source, wire.open("wb") as handle, gzip.GzipFile(
                fileobj=handle,
                mode="wb",
                compresslevel=GZIP_WIRE_COMPRESS_LEVEL,
                mtime=0,
            ) as compressor:
                while True:
                    block = source.read(FILE_READ_BLOCK_BYTES)
                    if not block:
                        break
                    compressor.write(block)
        except OSError as error:
            raise _SampleError(REASON_PAYLOAD_FAILED) from error
        self._inner_path = inner
        self._inner_size = written
        self._payload_path = wire
        digest, size = _file_digest(wire)
        self._payload_digest = digest
        self._payload_size = size
        self._record(STAGE_PAYLOAD, STAGE_OK, None, time.monotonic() - started)

    # ------------------------------------------------------------------ 预检

    def _precheck_repository(self) -> None:
        """预检只做可读观测：空仓/未知信号继续，能否上传由 POST 决定。

        200 视为仓库已存在；404 且响应体是正经 ``NAME_UNKNOWN`` 时只记录一次 unknown/empty 观测后
        继续（数据面空仓不等同于逻辑仓未建，专用仓已由控制台创建）；非 ``NAME_UNKNOWN`` 的 404、
        401/403、5xx 及其它状态一律 fail-closed，绝不代建逻辑仓、不预置 seed、也不改用业务仓库。
        连接/截止失败同样要留下阶段记录，避免故障样本缺失阶段证据。
        """
        started = time.monotonic()
        exchange, transport_reason = self._safe_call(
            "GET",
            f"/v2/{self._full_repository}/tags/list",
            self._arm_deadline(),
            headers={"Accept": "application/json"},
        )
        seconds = time.monotonic() - started
        if exchange is None:
            reason = transport_reason or REASON_REPOSITORY_UNREACHABLE
            self._record(STAGE_REPOSITORY_PRECHECK, self._transport_stage_status(reason), None, seconds)
            raise _SampleError(reason)
        if exchange.response is None:
            self._record(STAGE_REPOSITORY_PRECHECK, STAGE_FAILED, None, seconds)
            raise _SampleError(exchange.error_reason or REASON_REPOSITORY_UNREACHABLE)
        status = exchange.response.status
        api_version = exchange.response.headers.get("docker-distribution-api-version")
        if api_version:
            self._registry_api_version = api_version
        if status == 200:
            self._record(STAGE_REPOSITORY_PRECHECK, STAGE_OK, status, seconds)
            return
        if status == 404 and _is_name_unknown_body(exchange.response.body):
            # 合法的空仓/未知观测：保留 404 观测并继续；上传许可与 Location 可信性由 POST 权威确认。
            self._record(STAGE_REPOSITORY_PRECHECK, STAGE_OBSERVED, status, seconds)
            return
        self._record(STAGE_REPOSITORY_PRECHECK, STAGE_FAILED, status, seconds)
        if status == 404:
            raise _SampleError(REASON_REPOSITORY_UNKNOWN)
        if status in {401, 403}:
            raise _SampleError(REASON_ACCESS_DENIED)
        raise _SampleError(REASON_REPOSITORY_UNREACHABLE)

    def _check_freshness(self) -> None:
        """新鲜度 HEAD：只有 404 才继续上传，其它状态都不开始上传。"""
        started = time.monotonic()
        exchange, transport_reason = self._safe_call(
            "HEAD",
            f"/v2/{self._full_repository}/blobs/{self._payload_digest}",
            self._arm_deadline(),
        )
        seconds = time.monotonic() - started
        if exchange is None:
            reason = transport_reason or REASON_CONNECTION_FAILED
            self._record(STAGE_FRESHNESS, self._transport_stage_status(reason), None, seconds)
            raise _SampleError(reason)
        if exchange.response is None:
            self._record(STAGE_FRESHNESS, STAGE_FAILED, None, seconds)
            raise _SampleError(exchange.error_reason or REASON_CONNECTION_FAILED)
        status = exchange.response.status
        if status == 404:
            self._record(STAGE_FRESHNESS, STAGE_OK, status, seconds)
            return
        self._record(STAGE_FRESHNESS, STAGE_FAILED, status, seconds)
        if status == 200:
            raise _SampleError(REASON_FRESH_BLOB_EXISTS, status=STATUS_INVALID)
        if status in {401, 403}:
            raise _SampleError(REASON_ACCESS_DENIED)
        raise _SampleError(REASON_FRESHNESS_HEAD_UNEXPECTED)

    # ------------------------------------------------------------------ 上传

    def _open_upload(self) -> None:
        """POST 开始上传会话并登记最新 Location（必须与 registry 严格同 origin）。"""
        base_target = f"/v2/{self._full_repository}/blobs/uploads/"
        started = time.monotonic()
        exchange, transport_reason = self._safe_call(
            "POST",
            base_target,
            self._arm_deadline(),
            headers={"Content-Length": "0", "Content-Type": "application/octet-stream"},
        )
        seconds = time.monotonic() - started
        if exchange is None:
            reason = transport_reason or REASON_CONNECTION_FAILED
            self._record(STAGE_UPLOAD_POST, self._transport_stage_status(reason), None, seconds)
            raise _SampleError(reason)
        if exchange.response is None:
            self._record(STAGE_UPLOAD_POST, STAGE_FAILED, None, seconds)
            raise _SampleError(exchange.error_reason or REASON_CONNECTION_FAILED)
        status = exchange.response.status
        self._record(STAGE_UPLOAD_POST, STAGE_OK if status == 202 else STAGE_FAILED, status, seconds)
        if status in {301, 302, 303, 307, 308}:
            raise _SampleError(REASON_UNEXPECTED_REDIRECT)
        if status != 202:
            if status in {401, 403}:
                raise _SampleError(REASON_ACCESS_DENIED)
            raise _SampleError(REASON_UPLOAD_SESSION_REJECTED)
        location = exchange.response.headers.get("location")
        if not location:
            raise _SampleError(REASON_UPLOAD_SESSION_REJECTED)
        self._register_upload_location(base_target, location)

    def _register_upload_location(self, base_target: str, location: str) -> None:
        """校验并登记上传 Location；跨 origin 只记录去敏引用后失败。"""
        assert self._origin is not None
        origin, target = _resolve_location(self._origin, base_target, location)
        self._upload_reference = _safe_upload_reference(location)
        if not origin.matches(self._origin):
            # 载荷只允许发往 registry 同 origin；受信 auth_origin 也不接收上传。
            raise _SampleError(REASON_UNTRUSTED_LOCATION)
        self._upload_target = target

    def _send_payload_patch(self) -> None:
        """一次持续 PATCH 发送全部载荷，并按完整发送证据决定是否允许提交。"""
        assert self._client is not None and self._origin is not None
        assert self._payload_path is not None and self._upload_target is not None
        current_target = self._upload_target
        started = time.monotonic()
        transport_reason: str | None = None
        try:
            with self._payload_path.open("rb") as source:
                exchange = self._client.call_with_body(
                    "PATCH",
                    self._origin,
                    current_target,
                    self._arm_deadline(),
                    source=source,
                    size=self._payload_size,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Range": f"bytes 0-{self._payload_size - 1}",
                    },
                )
        except _SampleError as error:
            # 连接/握手层面的失败也要留下阶段记录，不能越过 upload_patch。
            exchange = None
            transport_reason = error.reason
        seconds = time.monotonic() - started
        if exchange is None:
            reason = transport_reason or REASON_UPLOAD_SEND_FAILED
            self._record(STAGE_UPLOAD_PATCH, self._transport_stage_status(reason), None, seconds)
            raise _SampleError(reason)
        self._ledger.body_bytes_uncertain = self._ledger.body_bytes_uncertain or exchange.send_uncertain
        sent = exchange.sent_body_bytes

        # 先登记服务端下发的最新可信 Location 供清理使用；登记失败不影响主错误判定。
        registration_error: _SampleError | None = None
        if exchange.response is not None:
            location = exchange.response.headers.get("location")
            if location:
                try:
                    self._register_upload_location(current_target, location)
                except _SampleError as error:
                    registration_error = error

        if exchange.response is None:
            stage_status = STAGE_DEADLINE if exchange.error_reason == REASON_DEADLINE_EXCEEDED else STAGE_FAILED
            self._record(STAGE_UPLOAD_PATCH, stage_status, None, seconds, sent)
            raise _SampleError(exchange.error_reason or REASON_UPLOAD_SEND_FAILED)
        status = exchange.response.status
        if registration_error is not None:
            self._record(STAGE_UPLOAD_PATCH, STAGE_FAILED, status, seconds, sent)
            raise registration_error
        if status != 202:
            self._record(STAGE_UPLOAD_PATCH, STAGE_FAILED, status, seconds, sent)
            raise _SampleError(REASON_UPLOAD_PATCH_REJECTED)
        if sent != self._payload_size:
            # 提前 202（例如只接受前缀）不构成完整发送证据。
            self._record(STAGE_UPLOAD_PATCH, STAGE_FAILED, status, seconds, sent)
            raise _SampleError(REASON_UPLOAD_INCOMPLETE_SEND)
        range_header = exchange.response.headers.get("range")
        if range_header is None:
            self._record(STAGE_UPLOAD_PATCH, STAGE_FAILED, status, seconds, sent)
            raise _SampleError(REASON_UPLOAD_RANGE_MISSING)
        parsed = _parse_range(range_header)
        if parsed is None or parsed[0] != 0 or parsed[1] != sent - 1:
            self._record(STAGE_UPLOAD_PATCH, STAGE_FAILED, status, seconds, sent)
            raise _SampleError(REASON_UPLOAD_RANGE_MISMATCH)
        self._record(STAGE_UPLOAD_PATCH, STAGE_OK, status, seconds, sent)

    def _commit_payload(self) -> None:
        """PUT 提交载荷：使用最新 Location 并仅追加 digest 参数。"""
        assert self._upload_target is not None
        target = _append_digest_parameter(self._upload_target, self._payload_digest)
        started = time.monotonic()
        # 发起写请求前先登记“提交结果未知”：响应丢失时才能如实报告残留。
        self._blob_commit_unknown = True
        exchange, transport_reason = self._safe_call(
            "PUT",
            target,
            self._arm_deadline(),
            headers={"Content-Length": "0", "Content-Type": "application/octet-stream"},
        )
        seconds = time.monotonic() - started
        if exchange is None:
            reason = (
                REASON_DEADLINE_EXCEEDED
                if transport_reason == REASON_DEADLINE_EXCEEDED
                else REASON_COMMIT_RESULT_UNKNOWN
            )
            self._record(STAGE_UPLOAD_PUT, self._transport_stage_status(reason), None, seconds)
            raise _SampleError(reason)
        if exchange.response is None:
            self._record(STAGE_UPLOAD_PUT, STAGE_FAILED, None, seconds)
            raise _SampleError(REASON_COMMIT_RESULT_UNKNOWN)
        status = exchange.response.status
        if status != 201:
            self._record(STAGE_UPLOAD_PUT, STAGE_FAILED, status, seconds)
            self._blob_commit_unknown = False
            raise _SampleError(REASON_COMMIT_REJECTED)
        digest_header = exchange.response.headers.get("docker-content-digest")
        if digest_header is not None and not _is_plain_digest(self._payload_digest, digest_header):
            self._record(STAGE_UPLOAD_PUT, STAGE_FAILED, status, seconds)
            self._blob_commit_unknown = False
            self._blob_commit_confirmed = True
            raise _SampleError(REASON_COMMIT_DIGEST_MISMATCH)
        self._record(STAGE_UPLOAD_PUT, STAGE_OK, status, seconds)
        self._blob_commit_unknown = False
        self._blob_commit_confirmed = True
        self._upload_completed = True
        self._upload_target = None

    def _verify_blob(self) -> None:
        """提交后 HEAD 校验长度与 digest，形成证据闭环。"""
        started = time.monotonic()
        exchange, transport_reason = self._safe_call(
            "HEAD",
            f"/v2/{self._full_repository}/blobs/{self._payload_digest}",
            self._arm_deadline(),
        )
        seconds = time.monotonic() - started
        if exchange is None:
            reason = transport_reason or REASON_CONNECTION_FAILED
            self._record(STAGE_VERIFY, self._transport_stage_status(reason), None, seconds)
            raise _SampleError(reason)
        if exchange.response is None:
            self._record(STAGE_VERIFY, STAGE_FAILED, None, seconds)
            raise _SampleError(exchange.error_reason or REASON_CONNECTION_FAILED)
        status = exchange.response.status
        headers = exchange.response.headers
        declared = headers.get("content-length", "")
        length_ok = declared.isdigit() and int(declared) == self._payload_size
        digest_ok = _is_plain_digest(self._payload_digest, headers.get("docker-content-digest"))
        if status == 200 and length_ok and digest_ok:
            self._record(STAGE_VERIFY, STAGE_OK, status, seconds)
            return
        self._record(STAGE_VERIFY, STAGE_FAILED, status, seconds)
        raise _SampleError(REASON_VERIFY_MISMATCH)

    # ------------------------------------------------------------------ gzip 参考

    def _measure_gzip_reference(self) -> None:
        """本地 gzip 参考测量；真实 I/O 失败记录 failed 阶段并让样本失败。"""
        assert self._inner_path is not None
        started = time.monotonic()
        sink = _CountingSink()
        try:
            with self._inner_path.open("rb") as source, gzip.GzipFile(
                fileobj=sink, mode="wb", compresslevel=GZIP_COMPRESS_LEVEL, mtime=0
            ) as compressor:
                while True:
                    block = source.read(FILE_READ_BLOCK_BYTES)
                    if not block:
                        break
                    compressor.write(block)
        except OSError as error:
            seconds = time.monotonic() - started
            self._gzip = {"status": STAGE_FAILED, "seconds": round(seconds, 6), "reason": REASON_GZIP_FAILED}
            self._record(STAGE_GZIP, STAGE_FAILED, None, seconds)
            raise _SampleError(REASON_GZIP_FAILED) from error
        seconds = time.monotonic() - started
        self._gzip = {
            "status": STAGE_OK,
            "seconds": round(seconds, 6),
            "size_bytes": sink.total,
            "input_size_bytes": self._payload_size,
            "level": GZIP_COMPRESS_LEVEL,
            "ratio": round(sink.total / self._payload_size, 4) if self._payload_size else None,
        }
        self._record(STAGE_GZIP, STAGE_OK, None, seconds)

    # ------------------------------------------------------------------ Docker 臂

    def _docker_versions(self) -> tuple[str, str]:
        """取 docker 客户端与 daemon 版本；不可用时写 unknown，不造值。"""
        try:
            result = _run_bounded(
                ["docker", "version", "--format", "{{.Client.Version}}|{{.Server.Version}}"],
                DOCKER_PROBE_TIMEOUT_SECONDS,
                capture_tail=True,
                markers=(),
            )
        except OSError:
            # docker CLI 不存在属于可预期的环境缺口，按 unavailable 处理而不是未知异常。
            return "unknown", "unknown"
        if result.returncode != 0:
            return "unknown", "unknown"
        lines = result.output_tail.decode("utf-8", "replace").strip().splitlines()
        if not lines:
            return "unknown", "unknown"
        client, _, server = lines[-1].partition("|")
        return (
            client if _VERSION_PATTERN.match(client) else "unknown",
            server if _VERSION_PATTERN.match(server) else "unknown",
        )

    def _build_rootfs_tar(self) -> tuple[str, int]:
        """用同份载荷生成只含一个文件的 rootfs tar，返回 digest 与大小。"""
        assert self._inner_path is not None and self._tempdir is not None
        tar_path = self._tempdir / "payload-rootfs.tar"
        with tarfile.open(tar_path, "w", format=tarfile.GNU_FORMAT) as archive:
            info = tarfile.TarInfo(name="payload.bin")
            info.size = self._inner_size
            info.mode = 0o644
            info.mtime = 0
            with self._inner_path.open("rb") as source:
                archive.addfile(info, source)
        return _file_digest(tar_path)

    def _imported_image_id(self, image_reference: str, import_tail: bytes) -> str:
        """取本次 ``docker import`` 的镜像身份（config digest），无法确定时返回空串。"""
        for line in reversed(import_tail.decode("utf-8", "replace").strip().splitlines()):
            candidate = line.strip()
            if _DIGEST_PATTERN.match(candidate):
                return candidate
        try:
            inspected = _run_bounded(
                ["docker", "image", "inspect", "--format", "{{.Id}}", image_reference],
                DOCKER_PROBE_TIMEOUT_SECONDS,
                capture_tail=True,
                markers=(),
            )
        except OSError:
            return ""
        if inspected.returncode != 0:
            return ""
        for line in reversed(inspected.output_tail.decode("utf-8", "replace").strip().splitlines()):
            candidate = line.strip()
            if _DIGEST_PATTERN.match(candidate):
                return candidate
        return ""

    def _run_docker_arm(self) -> None:
        """Docker 交叉验证臂：单层 import + 真实 push + 传输证据与身份绑定。"""
        assert self._client is not None and self._origin is not None
        deadline = self._docker_deadline()
        client_version, server_version = self._docker_versions()
        self._versions["docker_client"] = client_version
        self._versions["docker_server"] = server_version
        if server_version == "unknown":
            self._docker = {"status": "unavailable", "wall_seconds": None}
            self._record(STAGE_DOCKER_PREPARE, STAGE_FAILED, None, 0.0)
            raise _SampleError(REASON_DOCKER_UNAVAILABLE)

        started = time.monotonic()
        try:
            tar_digest, tar_size = self._build_rootfs_tar()
        except OSError as error:
            self._record(STAGE_DOCKER_PREPARE, STAGE_FAILED, None, time.monotonic() - started)
            raise _SampleError(REASON_DOCKER_PREPARE_FAILED) from error
        image_reference = f"{self._origin.netloc}/{self._full_repository}:{self._docker_tag}"
        try:
            prepared = _run_bounded(
                ["docker", "import", str(self._tar_path()), image_reference],
                max(deadline - time.monotonic(), 0.0),
                capture_tail=True,
                markers=(),
            )
        except OSError as error:
            self._record(STAGE_DOCKER_PREPARE, STAGE_FAILED, None, time.monotonic() - started)
            raise _SampleError(REASON_DOCKER_UNAVAILABLE) from error
        prepare_seconds = time.monotonic() - started
        if prepared.timed_out:
            self._record(STAGE_DOCKER_PREPARE, STAGE_DEADLINE, None, prepare_seconds)
            raise _SampleError(REASON_DOCKER_DEADLINE_EXCEEDED)
        if prepared.returncode != 0:
            self._record(STAGE_DOCKER_PREPARE, STAGE_FAILED, None, prepare_seconds)
            raise _SampleError(REASON_DOCKER_PREPARE_FAILED)
        self._docker_image_created = True
        image_id = self._imported_image_id(image_reference, prepared.output_tail)
        self._record(STAGE_DOCKER_PREPARE, STAGE_OK, None, prepare_seconds)

        login_started = time.monotonic()
        # 登录尝试在发起命令时即登记：即使写入 stdin 或等待过程中失败，也不能漏记已启动的尝试。
        self._docker_login_attempted = True
        try:
            logged_in = _run_bounded(
                ["docker", "login", self._origin.netloc, "-u", self._credentials.username, "--password-stdin"],
                min(DOCKER_PROBE_TIMEOUT_SECONDS, max(deadline - time.monotonic(), 0.0)),
                stdin_bytes=self._credentials.password.encode("utf-8"),
                markers=(),
            )
        except OSError as error:
            self._record(STAGE_DOCKER_LOGIN, STAGE_FAILED, None, time.monotonic() - login_started)
            raise _SampleError(REASON_DOCKER_LOGIN_FAILED) from error
        login_seconds = time.monotonic() - login_started
        if logged_in.timed_out or logged_in.returncode != 0:
            self._record(STAGE_DOCKER_LOGIN, STAGE_FAILED, None, login_seconds)
            raise _SampleError(REASON_DOCKER_LOGIN_FAILED)
        self._record(STAGE_DOCKER_LOGIN, STAGE_OK, None, login_seconds)

        push_started = time.monotonic()
        # 启动 push 之前就登记「远端结果未知」：即使退出 0，只要 manifest 读回失败或身份无法绑定，
        # 本次 tag 在远端可能已完整创建，必须保留精确残留。
        self._docker_push_unknown = True
        try:
            pushed = _run_bounded(
                ["docker", "push", image_reference],
                max(deadline - time.monotonic(), 0.0),
            )
        except OSError as error:
            self._record(STAGE_DOCKER_PUSH, STAGE_FAILED, None, time.monotonic() - push_started)
            raise _SampleError(REASON_DOCKER_UNAVAILABLE) from error
        push_seconds = time.monotonic() - push_started
        deduplicated = any(marker in pushed.markers for marker in _DOCKER_DEDUP_MARKERS)
        self._docker = {
            "status": "unknown",
            "wall_seconds": round(push_seconds, 6),
            "prepare_seconds": round(prepare_seconds, 6),
            "login_seconds": round(login_seconds, 6),
            "exit_code": pushed.returncode,
            "timed_out": pushed.timed_out,
            "output_bytes": pushed.output_bytes,
            "payload_tar_digest": tar_digest,
            "payload_tar_size_bytes": tar_size,
            "image_id": image_id or "unknown",
            "deduplicated": deduplicated,
            "pushed_marker": bool(pushed.markers.get(_DOCKER_PUSH_MARKER)),
            "identity_bound": False,
            "manifest_digest": None,
            "manifest_size_bytes": None,
            "layer_descriptors": [],
            "evidence": [],
        }
        if pushed.timed_out:
            self._record(STAGE_DOCKER_PUSH, STAGE_DEADLINE, None, push_seconds)
            raise _SampleError(REASON_DOCKER_DEADLINE_EXCEEDED)
        if pushed.returncode != 0:
            self._record(STAGE_DOCKER_PUSH, STAGE_FAILED, None, push_seconds)
            self._docker["status"] = "failed"
            raise _SampleError(REASON_DOCKER_PUSH_FAILED)
        self._record(STAGE_DOCKER_PUSH, STAGE_OK, None, push_seconds)

        manifest_digest, manifest_size, descriptors, config_digest = self._read_pushed_manifest()
        self._docker["manifest_digest"] = manifest_digest
        self._docker["manifest_size_bytes"] = manifest_size
        self._docker["layer_descriptors"] = descriptors
        self._docker["config_digest"] = config_digest
        # 只有先绑定本次 import 的镜像身份，读回的 manifest 才能被当作本次可清理对象；
        # 身份未知或不匹配时绝不登记删除目标，只保留本次 tag 的未知残留。
        identity_bound = (
            bool(image_id)
            and config_digest is not None
            and _is_plain_digest(image_id, config_digest)
            and len(descriptors) == 1
            and isinstance(descriptors[0].get("size_bytes"), int)
        )
        self._docker["identity_bound"] = identity_bound
        if identity_bound and manifest_digest is not None:
            layer_digest = descriptors[0].get("digest")
            self._docker_layer_digest = layer_digest if isinstance(layer_digest, str) else None
            self._docker_config_digest = config_digest
            self._docker_manifest_digest = manifest_digest
            self._docker_push_unknown = False
        if deduplicated:
            self._docker["status"] = "deduplicated"
            raise _SampleError(REASON_DOCKER_LAYER_DEDUPLICATED, status=STATUS_INVALID)
        if not identity_bound:
            self._docker["status"] = "identity_unknown" if not image_id else "identity_mismatch"
            raise _SampleError(
                REASON_DOCKER_IDENTITY_UNKNOWN if not image_id else REASON_DOCKER_NO_UPLOAD_EVIDENCE,
                status=STATUS_INVALID,
            )
        if not pushed.markers.get(_DOCKER_PUSH_MARKER):
            # 逐层 Pushed 正证据是真实传输的必要条件；只有 manifest 摘要不足以证明发生过上传。
            self._docker["status"] = "no_pushed_evidence"
            raise _SampleError(REASON_DOCKER_NO_UPLOAD_EVIDENCE, status=STATUS_INVALID)
        evidence = self._match_push_evidence(pushed.digest_lines, manifest_digest, manifest_size)
        self._docker["evidence"] = evidence
        if not evidence:
            self._docker["status"] = "no_upload_evidence"
            raise _SampleError(REASON_DOCKER_NO_UPLOAD_EVIDENCE, status=STATUS_INVALID)
        self._docker["status"] = "pushed"

    def _match_push_evidence(
        self,
        digest_lines: list[tuple[str, int]],
        manifest_digest: str | None,
        manifest_size: int | None,
    ) -> list[dict[str, object]]:
        """把 push 输出的 tag 摘要行与本次读回的 manifest 对齐。

        tag 摘要行的 size 是 manifest 原文的字节长度，而不是 layer 的压缩大小；digest 与长度都
        必须一致才构成证据，layer digest 冒充 manifest digest 一律不接受。
        """
        evidence: list[dict[str, object]] = []
        if manifest_digest is None:
            return evidence
        for digest, size in digest_lines:
            if digest != manifest_digest:
                continue
            if manifest_size is not None and size != manifest_size:
                continue
            evidence.append({"kind": "manifest", "digest": digest, "size_bytes": size})
        return evidence

    def _tar_path(self) -> Path:
        """返回本次 rootfs tar 路径。"""
        assert self._tempdir is not None
        return self._tempdir / "payload-rootfs.tar"

    def _read_pushed_manifest(self) -> tuple[str | None, int | None, list[dict[str, object]], str | None]:
        """读回本次 Docker tag 的 manifest：只信本次响应原文重算出的 digest 与长度。

        Returns:
            ``(manifest_digest, manifest_size_bytes, layer_descriptors, config_digest)``；
            读取失败时 digest 与长度均为 None。
        """
        assert self._client is not None and self._origin is not None
        document, body = self._fetch_manifest_document(
            f"/v2/{self._full_repository}/manifests/{self._docker_tag}"
        )
        if document is None or body is None:
            return None, None, [], None
        # manifest digest 与长度必须由本次取回的原文重新计算，不能盲信响应头。
        manifest_digest = f"sha256:{hashlib.sha256(body).hexdigest()}"
        manifest_size = len(body)
        descriptors = _layer_descriptors(document)
        config_digest = _descriptor_digest(document.get("config"))
        if not descriptors:
            children = document.get("manifests")
            if isinstance(children, list):
                for child in children:
                    if not isinstance(child, dict) or not isinstance(child.get("digest"), str):
                        continue
                    child_document, _child_body = self._fetch_manifest_document(
                        f"/v2/{self._full_repository}/manifests/{child['digest']}"
                    )
                    if child_document is None:
                        continue
                    child_layers = _layer_descriptors(child_document)
                    if child_layers:
                        return manifest_digest, manifest_size, child_layers, _descriptor_digest(
                            child_document.get("config")
                        )
        return manifest_digest, manifest_size, descriptors, config_digest

    def _fetch_manifest_document(self, target: str) -> tuple[dict[str, Any] | None, bytes | None]:
        """读取一个 manifest 文档原文；失败时返回 None。"""
        assert self._client is not None and self._origin is not None
        try:
            exchange = self._client.call(
                "GET",
                self._origin,
                target,
                self._request_deadline(),
                headers={"Accept": MANIFEST_ACCEPT},
            )
        except _SampleError:
            return None, None
        if exchange.response is None or exchange.response.status != 200:
            return None, None
        raw = exchange.response.body
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, None
        if not isinstance(document, dict):
            return None, None
        return cast(dict[str, Any], document), raw

    # ------------------------------------------------------------------ 清理

    def _mark_partial(self, error: str, residual: str) -> None:
        """记录一次清理受拒：状态转为 partial，并保留精确残留。"""
        self._cleanup_status = CLEANUP_PARTIAL
        self._cleanup_errors.append(error)
        self._residuals.append(residual)

    def _cancel_upload(self) -> None:
        """取消本次仍未完成的上传会话；失败时精确报告残留。"""
        if self._upload_target is None or self._upload_completed:
            return
        assert self._client is not None and self._origin is not None
        reference = self._upload_reference or "unknown"
        try:
            exchange = self._client.call("DELETE", self._origin, self._upload_target, self._request_deadline())
        except _SampleError as error:
            self._mark_partial(f"cancel:{error.reason}", f"upload:{reference}")
            return
        if exchange.response is None:
            self._mark_partial(
                f"cancel:{exchange.error_reason or REASON_CONNECTION_FAILED}",
                f"upload:{reference}",
            )
            return
        if exchange.response.status in {202, 204}:
            self._upload_target = None
            return
        self._mark_partial(f"cancel:http_{exchange.response.status}", f"upload:{reference}")

    def _delete_docker_manifest(self) -> None:
        """尝试按精确 digest 删除本次 Docker manifest。

        删除拒绝（含 Bearer 要求本仓 delete，而 token 只收 pull/push）只记残留，
        不扩大权限，也不把已成功的测量改成 cleanup_incomplete。
        """
        if self._docker_manifest_digest is None:
            return
        assert self._client is not None and self._origin is not None
        residual = f"docker_manifest:{self._docker_manifest_digest}"
        try:
            exchange = self._client.call(
                "DELETE",
                self._origin,
                f"/v2/{self._full_repository}/manifests/{self._docker_manifest_digest}",
                self._request_deadline(),
            )
        except _SampleError as error:
            self._cleanup_errors.append(f"docker_manifest:{error.reason}")
            self._residuals.append(residual)
            return
        if exchange.response is None:
            self._cleanup_errors.append(
                f"docker_manifest:{exchange.error_reason or REASON_CONNECTION_FAILED}"
            )
            self._residuals.append(residual)
            return
        if exchange.response.status in {200, 202}:
            return
        self._cleanup_errors.append(f"docker_manifest:http_{exchange.response.status}")
        self._residuals.append(residual)

    def _remove_docker_artifacts(self) -> None:
        """删除本地测试镜像并沿用既有 logout 处理（如实记录 logout 结果）。"""
        image_reference = f"{(self._origin.netloc if self._origin else '')}/{self._full_repository}:{self._docker_tag}"
        if self._docker_image_created:
            try:
                removed = _run_bounded(
                    ["docker", "image", "rm", image_reference], DOCKER_PROBE_TIMEOUT_SECONDS, markers=()
                )
            except OSError:
                self._mark_partial("image_remove:unavailable", "docker_image:local")
            else:
                if removed.returncode != 0:
                    self._mark_partial("image_remove:failed", "docker_image:local")
        if self._docker_login_attempted and self._origin is not None:
            started = time.monotonic()
            try:
                logged_out = _run_bounded(
                    ["docker", "logout", self._origin.netloc], DOCKER_PROBE_TIMEOUT_SECONDS, markers=()
                )
            except OSError:
                self._docker_logout = LOGOUT_FAILED
                self._record(STAGE_DOCKER_LOGOUT, STAGE_FAILED, None, time.monotonic() - started)
                self._mark_partial("logout:failed", "docker_login:local")
                return
            ok = logged_out.returncode == 0 and not logged_out.timed_out
            self._docker_logout = LOGOUT_OK if ok else LOGOUT_FAILED
            self._record(STAGE_DOCKER_LOGOUT, STAGE_OK if ok else STAGE_FAILED, None, time.monotonic() - started)
            if not ok:
                self._mark_partial("logout:failed", "docker_login:local")

    def _remove_local_files(self) -> None:
        """删除本地载荷与临时目录。"""
        if self._tempdir is None:
            return
        try:
            _remove_tree(self._tempdir)
        except OSError:
            self._mark_partial("local_files:failed", "local_files")
            return
        self._local_removed = True

    def _report_orphan_objects(self) -> None:
        """如实报告本次无法删除的服务端对象：registry 无 blob DELETE，需后台 GC。"""
        if self._blob_commit_unknown:
            self._residuals.append(f"blob:{self._payload_digest}:commit_unknown")
        elif self._blob_commit_confirmed:
            self._residuals.append(f"blob:{self._payload_digest}")
        if self._docker_layer_digest is not None:
            self._residuals.append(f"docker_layer:{self._docker_layer_digest}")
        if self._docker_config_digest is not None:
            self._residuals.append(f"docker_config:{self._docker_config_digest}")
        if self._docker_push_unknown:
            self._residuals.append(f"docker_tag:{self._docker_tag}:push_unknown")

    def cleanup(self) -> None:
        """执行有界清理：只作用于本次对象，不删 blob/GC/业务对象。"""
        if self._cleanup_status == CLEANUP_SKIPPED:
            self._cleanup_status = CLEANUP_OK
        for step in (self._cancel_upload, self._delete_docker_manifest, self._remove_docker_artifacts):
            try:
                step()
            except Exception as error:  # noqa: BLE001 - 清理错误绝不能覆盖主测量结果
                self._mark_partial(f"{step.__name__}:{type(error).__name__}", "cleanup_error")
        try:
            self._report_orphan_objects()
        except Exception as error:  # noqa: BLE001
            self._mark_partial(f"residuals:{type(error).__name__}", "cleanup_error")
        try:
            self._remove_local_files()
        except Exception as error:  # noqa: BLE001
            self._mark_partial(f"local:{type(error).__name__}", "local_files")

    # ------------------------------------------------------------------ 执行

    def execute(self) -> dict[str, object]:
        """执行样本并始终返回去敏结果。"""
        self._versions = _base_versions()
        try:
            self._tempdir = Path(tempfile.mkdtemp(prefix="push-perf-"))
            self._origin = _parse_registry(self._config.registry)
            self._auth_origin = _parse_optional_origin(self._config.auth_origin)
            self._full_repository = self._build_repository()
            self._tag = _tag_from_identity(self._config.run_id, self._config.environment)
            self._docker_tag = _tag_from_identity(self._config.run_id, self._config.environment, suffix="docker")
            self._client = _Client(
                self._origin,
                self._auth_origin,
                self._credentials,
                self._tls_context,
                self._config.connect_timeout,
                self._ledger,
                self._full_repository,
            )
            self._generate_payload()
            self._precheck_repository()
            self._check_freshness()
            self._open_upload()
            self._send_payload_patch()
            self._commit_payload()
            self._verify_blob()
        except _SampleError as error:
            self._status = error.status
            self._reason = error.reason
        except Exception as error:  # noqa: BLE001 - 未知异常只暴露类型名，绝不暴露正文
            self._status = STATUS_FAILED
            self._reason = REASON_UNEXPECTED_ERROR
            self._errors.append(type(error).__name__)
        else:
            self._status = STATUS_VALID
            self._reason = REASON_OK
            if not self._config.http_only:
                try:
                    self._run_docker_arm()
                except _SampleError as error:
                    self._status = error.status
                    self._reason = error.reason
                except Exception as error:  # noqa: BLE001
                    self._status = STATUS_FAILED
                    self._reason = REASON_UNEXPECTED_ERROR
                    self._errors.append(type(error).__name__)
            if self._status == STATUS_VALID:
                try:
                    self._measure_gzip_reference()
                except _SampleError as error:
                    self._status = STATUS_FAILED
                    self._reason = error.reason
                except Exception as error:  # noqa: BLE001
                    self._status = STATUS_FAILED
                    self._reason = REASON_UNEXPECTED_ERROR
                    self._errors.append(type(error).__name__)
        finally:
            try:
                self.cleanup()
            except Exception as error:  # noqa: BLE001
                self._cleanup_status = CLEANUP_PARTIAL
                self._errors.append(type(error).__name__)
            if self._cleanup_status == CLEANUP_PARTIAL and self._status == STATUS_VALID:
                self._status = STATUS_FAILED
                self._reason = REASON_CLEANUP_INCOMPLETE
        return self.result()

    def _build_repository(self) -> str:
        """校验命名空间并返回完整仓库路径。"""
        namespace = self._config.namespace.strip()
        if not _REPOSITORY_PATTERN.match(namespace):
            raise _SampleError(REASON_INVALID_CONFIG)
        return f"{namespace}/{REPOSITORY_NAME}"

    def result(self) -> dict[str, object]:
        """组装去敏结果（公共键固定，额外键只在安全时添加）。"""
        upload_residuals = [item for item in self._residuals if item.startswith("upload:")]
        docker_manifest_residuals = [item for item in self._residuals if item.startswith("docker_manifest:")]
        cleanup_section: dict[str, object] = {
            "status": self._cleanup_status,
            "upload_cancelled": not upload_residuals,
            "docker_manifest_deleted": (
                self._docker_manifest_digest is not None and not docker_manifest_residuals
            ),
            "docker_manifest_delete_attempted": self._docker_manifest_digest is not None,
            "local_removed": self._local_removed,
            "login_attempted": self._docker_login_attempted,
            "logout": self._docker_logout,
            "residuals": list(self._residuals),
            "errors": list(self._cleanup_errors),
            "storage_reclaimed": False,
        }
        return {
            "status": self._status,
            "reason": self._reason,
            "environment": self._config.environment,
            "run_id": self._config.run_id,
            "sample_id": self._tag,
            "repository": self._repository,
            "namespace": self._config.namespace,
            "full_repository": self._full_repository,
            "tag": self._tag,
            "docker_tag": self._docker_tag,
            "payload": {"digest": self._payload_digest, "size_bytes": self._payload_size},
            "stages": [stage.as_dict() for stage in self._stages],
            "http": {
                "body_bytes": self._ledger.request_body_bytes,
                "response_body_bytes": self._ledger.response_body_bytes,
                "requests": self._ledger.requests,
                "body_bytes_is_lower_bound": self._ledger.body_bytes_uncertain,
                "registry_api_version": self._registry_api_version or "unknown",
            },
            "docker": dict(self._docker),
            "gzip": dict(self._gzip),
            "versions": dict(self._versions),
            "cleanup": cleanup_section,
            "errors": list(self._errors),
        }


class _CountingSink:
    """只统计字节数的写目标，避免把 64 MiB 压缩结果留在内存。"""

    def __init__(self) -> None:
        self.total = 0
        self.name = "push-perf-gzip-sink"

    def write(self, data: bytes) -> int:
        """累计写入长度并返回。"""
        self.total += len(data)
        return len(data)

    def flush(self) -> None:
        """无缓冲，忽略。"""
        return None


def _file_digest(path: Path) -> tuple[str, int]:
    """流式计算文件 SHA-256 与大小。"""
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(FILE_READ_BLOCK_BYTES)
            if not block:
                break
            hasher.update(block)
            size += len(block)
    return f"sha256:{hasher.hexdigest()}", size


def _descriptor_digest(descriptor: object) -> str | None:
    """从描述符里取出 digest（仅接受合法 SHA-256 形式）。"""
    if not isinstance(descriptor, dict):
        return None
    digest = descriptor.get("digest")
    if isinstance(digest, str) and _DIGEST_PATTERN.match(digest.strip().lower()):
        return digest.strip().lower()
    return None


def _layer_descriptors(document: dict[str, Any]) -> list[dict[str, object]]:
    """从 manifest 文档提取 layer descriptor（digest 与 size）。"""
    descriptors: list[dict[str, object]] = []
    layers = document.get("layers")
    if not isinstance(layers, list):
        return descriptors
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        digest = layer.get("digest")
        if not isinstance(digest, str) or not _DIGEST_PATTERN.match(digest.strip().lower()):
            continue
        size = layer.get("size")
        descriptors.append(
            {
                "digest": digest.strip().lower(),
                "size_bytes": size if isinstance(size, int) else None,
            }
        )
    return descriptors


def _base_versions() -> dict[str, object]:
    """采集安全的版本与 revision 信息；未知一律写 unknown，不造值。"""
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "revision": os.environ.get("GITHUB_SHA") or _git_revision() or "unknown",
        "github_run_id": os.environ.get("GITHUB_RUN_ID") or "unknown",
        "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT") or "unknown",
        "docker_client": "unknown",
        "docker_server": "unknown",
        "upload_concurrency": "unknown",
    }


def _git_revision() -> str:
    """尽力取当前仓库 revision；不可知时返回空串（不造值）。"""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            timeout=5,
            check=False,
            cwd=str(Path(__file__).resolve().parent),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    text = completed.stdout.decode("utf-8", "replace").strip()
    return text if _GIT_REVISION_PATTERN.match(text) else ""


def _remove_tree(path: Path) -> None:
    """递归删除本地临时目录。"""
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_dir():
            child.rmdir()
        else:
            child.unlink()
    path.rmdir()


def _mapping(value: object) -> dict[str, Any]:
    """把结果子结构安全地当作字符串键字典使用。"""
    if isinstance(value, dict):
        return cast("dict[str, Any]", value)
    return {}


# --------------------------------------------------------------------------------------
# 公共接口
# --------------------------------------------------------------------------------------


def run_sample(
    config: Config,
    credentials: Credentials,
    *,
    tls_context: ssl.SSLContext | None = None,
) -> dict[str, object]:
    """执行一次上传样本并返回去敏结果 dict。

    Args:
        config: 样本配置（registry/namespace/超时等）。
        credentials: registry 凭据；密码只用于内存请求头与 stdin 登录。
        tls_context: 可注入的 SSLContext（测试用受信 CA）；None 时使用系统信任库。

    Returns:
        结果 dict；``status`` 为 valid/invalid/failed，``reason`` 为安全枚举。
        协议与配置失败都体现在 status 中，不向上抛异常。
    """
    return _SampleRun(config, credentials, tls_context).execute()


def _render_summary(result: dict[str, object]) -> str:
    """把结果渲染成不含敏感信息的 Markdown summary。"""
    payload = _mapping(result.get("payload"))
    http_section = _mapping(result.get("http"))
    cleanup = _mapping(result.get("cleanup"))
    versions = _mapping(result.get("versions"))
    lines = [
        "# 镜像上传诊断结果",
        "",
        f"- 状态: {result.get('status')}（reason={result.get('reason')}）",
        f"- 环境: {result.get('environment')} / run_id={result.get('run_id')}",
        f"- revision: {versions.get('revision')}",
        f"- 仓库: {result.get('namespace')}/{result.get('repository')} "
        f"tag={result.get('tag')} docker_tag={result.get('docker_tag')}",
        f"- 载荷: {payload.get('digest')} ({payload.get('size_bytes')} bytes)",
        f"- 版本: python={versions.get('python')} docker={versions.get('docker_server')}",
        "",
        "| 阶段 | 状态 | HTTP | 秒 | body bytes |",
        "| --- | --- | --- | --- | --- |",
    ]
    stages = result.get("stages")
    if isinstance(stages, list):
        for stage in stages:
            stage_map = _mapping(stage)
            lines.append(
                f"| {stage_map.get('name')} | {stage_map.get('status')} | {stage_map.get('http_status')} "
                f"| {stage_map.get('seconds')} | {stage_map.get('body_bytes')} |"
            )
    lines.extend(
        [
            "",
            f"- HTTP 请求体总字节: {http_section.get('body_bytes')}"
            f"（是否下界={http_section.get('body_bytes_is_lower_bound')}）",
            f"- registry API 版本: {http_section.get('registry_api_version')}",
            f"- Docker 臂: {json.dumps(result.get('docker'), ensure_ascii=False, sort_keys=True)}",
            f"- gzip 参考: {json.dumps(result.get('gzip'), ensure_ascii=False, sort_keys=True)}",
            "",
            "## 清理",
            "",
            f"- 状态: {cleanup.get('status')}；本地文件已删除={cleanup.get('local_removed')}；"
            f"logout={cleanup.get('logout')}",
            f"- 残留: {json.dumps(cleanup.get('residuals'), ensure_ascii=False)}",
            "- 说明: registry 无 blob DELETE/GC 接口，本次 raw/docker blob 会保留到后台 GC；"
            "残留不代表服务器空间已释放，也不单独改变该次测量结论。",
            "",
            "## 结论边界",
            "",
            "- 原始 HTTP 臂只提交并验证随机 blob，不发布 manifest；合法镜像 manifest 只由 Docker 臂产生。",
            "- 本结果只描述本次样本的客户端观测与实际 descriptor 证据，"
            "不能区分 GitHub 出口、运营商路径与 Registry 接收侧的贡献。",
            "- 单样本为探索性证据；需要稳定差异结论时须按单独许可重复多次并报告中位数与极值。",
            "- Docker 臂的 descriptor size 是对象压缩大小，不含 TLS/重试等线上开销。",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_json(path: Path, result: dict[str, object]) -> None:
    """把结果写出去敏 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_summary(path: Path, result: dict[str, object]) -> None:
    """把结果写出去敏 Markdown summary。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_summary(result), encoding="utf-8")


def _print_summary(result: dict[str, object]) -> None:
    """把关键字段打印到 stdout（只含去敏字段）。"""
    payload = _mapping(result.get("payload"))
    http_section = _mapping(result.get("http"))
    cleanup = _mapping(result.get("cleanup"))
    print(
        f"status={result.get('status')} reason={result.get('reason')} "
        f"environment={result.get('environment')} run_id={result.get('run_id')} tag={result.get('tag')}"
    )
    print(
        f"repository={result.get('namespace')}/{result.get('repository')} "
        f"payload={payload.get('digest')} size_bytes={payload.get('size_bytes')}"
    )
    stages = result.get("stages")
    if isinstance(stages, list):
        for stage in stages:
            stage_map = _mapping(stage)
            print(
                f"stage {stage_map.get('name')} status={stage_map.get('status')} "
                f"http={stage_map.get('http_status')} seconds={stage_map.get('seconds')} "
                f"body_bytes={stage_map.get('body_bytes')}"
            )
    print(
        f"http_body_bytes={http_section.get('body_bytes')} "
        f"lower_bound={http_section.get('body_bytes_is_lower_bound')}"
    )
    print(
        f"cleanup_status={cleanup.get('status')} logout={cleanup.get('logout')} "
        f"residuals={json.dumps(cleanup.get('residuals'), ensure_ascii=False)}"
    )


def _failed_result(reason: str, environment: str, run_id: str) -> dict[str, object]:
    """构造配置失败时的最小去敏结果。"""
    return {
        "status": STATUS_FAILED,
        "reason": reason,
        "environment": environment,
        "run_id": run_id,
        "sample_id": "",
        "repository": REPOSITORY_NAME,
        "namespace": "",
        "full_repository": "",
        "tag": "",
        "docker_tag": "",
        "payload": {"digest": "", "size_bytes": 0},
        "stages": [],
        "http": {
            "body_bytes": 0,
            "response_body_bytes": 0,
            "requests": 0,
            "body_bytes_is_lower_bound": False,
            "registry_api_version": "unknown",
        },
        "docker": {"status": STAGE_SKIPPED},
        "gzip": {"status": STAGE_SKIPPED},
        "versions": _base_versions(),
        "cleanup": {
            "status": CLEANUP_SKIPPED,
            "upload_cancelled": True,
            "docker_manifest_deleted": False,
            "docker_manifest_delete_attempted": False,
            "local_removed": False,
            "login_attempted": False,
            "logout": LOGOUT_SKIPPED,
            "residuals": [],
            "errors": [],
            "storage_reclaimed": False,
        },
        "errors": [],
    }


def _emit(output_path: Path, summary_path: Path | None, result: dict[str, object]) -> bool:
    """写出 JSON 与可选 summary；返回是否全部写出成功。

    单个产物写出失败只提示对应错误，不影响其它产物，也不改写样本测量结果。
    """
    written = True
    try:
        _write_json(output_path, result)
    except OSError as error:
        written = False
        print(f"错误: 无法写出结果 JSON: {type(error).__name__}", file=sys.stderr)
    if summary_path is not None:
        try:
            _write_summary(summary_path, result)
        except OSError as error:
            written = False
            print(f"错误: 无法写出 summary: {type(error).__name__}", file=sys.stderr)
    return written


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：读取环境凭据，执行样本并写出 JSON/summary。

    Args:
        argv: 命令行参数，默认取 ``sys.argv[1:]``。

    Returns:
        进程退出码：样本 valid、清理完整且全部要求产物写出成功为 0；配置缺失为 2；其它为 1。
    """
    parser = argparse.ArgumentParser(
        prog="push-perf.py",
        description="对专用测试仓执行一次受控上传样本，输出原始 HTTP/Docker 对照的去敏结果。",
    )
    parser.add_argument("--environment", default="control", help="运行环境标识，默认 control")
    parser.add_argument("--run-id", default="", help="本次运行标识，用于生成唯一 tag")
    parser.add_argument("--output", default="perf-summary.json", help="JSON 结果输出路径")
    parser.add_argument("--summary", default=None, help="可选 Markdown summary 输出路径")
    parser.add_argument("--http-only", action="store_true", help="只跑原始 HTTP 臂，跳过 Docker 交叉验证")
    parser.add_argument(
        "--auth-origin",
        default=None,
        help="唯一额外受信的认证 origin（如 ACR 官方 realm）；默认只信任 registry 同 origin",
    )
    args = parser.parse_args(argv)

    output_path = Path(args.output)
    summary_path = Path(args.summary) if args.summary else None
    registry = os.environ.get("ALIYUN_REGISTRY", "")
    namespace = os.environ.get("ALIYUN_NAME_SPACE", "")
    username = os.environ.get("ALIYUN_REGISTRY_USER", "")
    password = os.environ.get("ALIYUN_REGISTRY_PASSWORD", "")

    if not registry or not namespace or not username or not password:
        print(
            "错误: 缺少必需环境变量"
            "（ALIYUN_REGISTRY/ALIYUN_NAME_SPACE/ALIYUN_REGISTRY_USER/ALIYUN_REGISTRY_PASSWORD）",
            file=sys.stderr,
        )
        written = _emit(output_path, summary_path, _failed_result(REASON_INVALID_CONFIG, args.environment, args.run_id))
        return 2 if written else 3

    config = Config(
        registry=registry,
        namespace=namespace,
        environment=args.environment,
        run_id=args.run_id,
        http_only=args.http_only,
        auth_origin=args.auth_origin,
    )
    result = run_sample(config, Credentials(username=username, password=password))
    written = _emit(output_path, summary_path, result)
    _print_summary(result)
    if result.get("status") != STATUS_VALID:
        return 1
    if _mapping(result.get("cleanup")).get("status") != CLEANUP_OK:
        return 1
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
