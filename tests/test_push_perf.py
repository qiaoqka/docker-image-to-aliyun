"""diagnostics/push-perf.py 的真实协议与行为测试。

本文件只消费 diagnostics/push-perf.py 的公开接口（Config/Credentials/run_sample/main），
不导入内部实现，也不断言源码文本。两类接缝在测试名与断言上明确区分：

1. 真实 Registry（测试名含 ``real_registry``）：本机启动 Distribution v3.1.2，
   TLS + 临时 filesystem storage，验证固定 64 MiB 载荷单次 PATCH 落地后的 digest/长度、
   manifest 闭环和 registry 自身访问日志里的请求计数。证据来自真实服务端。
2. TLS 协议故障端点（测试名含 ``fault``）：本文件内的 HTTPS 故障服务器，按脚本重放
   鉴权 challenge、redirect、短写、错 Range/digest/长度、PUT 失败、deadline 与清理受拒。
   它不是真实 Registry，只证明客户端在协议故障下的行为，不能当作真实服务端证据。
   鉴权目标规则也在这一层验证：默认只允许 registry 同源 realm，显式 auth_origin 才允许
   一个完整的 HTTPS 跨 origin 鉴权目标；upload Location 一律不得跨 origin、降级或跟随 302。

TLS 材料由 openssl 在 pytest 临时目录动态生成（CA + SAN=localhost/IP 的服务器证书），
只信任该临时 CA，不关闭证书与主机名校验，不提交任何私钥或证书。
每个样本固定 64 MiB，不引入可变载荷大小参数；错误尽量在鉴权/新鲜度 HEAD 阶段触发，
完整 64 MiB 成功路径只有真实 Registry 与故障端点各一条。

用法：
    pytest tests/test_push_perf.py -v
    pytest tests/test_push_perf.py -k real_registry -v
"""

from __future__ import annotations

import base64
import contextlib
import errno
import functools
import hashlib
import http.client
import importlib.util
import io
import json
import os
import re
import resource
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tarfile
import threading
import time
import types
import urllib.parse
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import IO, Any, cast

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PUSH_PERF_PATH = REPO_ROOT / "diagnostics" / "push-perf.py"
REGISTRY_BIN = Path("/tmp/opencode-workspaces/docker/sync-registry/bin/registry")
OPENSSL_BIN = Path("/opt/homebrew/bin/openssl")

# 内层 64 MiB 不可压缩随机；线体为 gzip 包装，长度略大于该值。
PAYLOAD_BYTES = 64 * 1024 * 1024
# 故障端点记录请求 body 的上限；64 MiB 载荷只计数、不留在内存里。
MAX_RECORDED_BODY_BYTES = 1 << 20


def declared_content_length(request: RecordedRequest) -> int:
    """返回请求声明的 Content-Length；无法解析时回退内层大小。"""
    raw = request.headers.get("content-length", "")
    try:
        value = int(raw)
    except ValueError:
        return PAYLOAD_BYTES
    return value if value > 0 else PAYLOAD_BYTES

REGISTRY_READY_TIMEOUT = 30.0
# 故障端点在 64 MiB 成功路径上按块节流读取：真实的远端 registry 一定比客户端慢，
# 客户端必须处理发送背压（阻塞/重试可写），把背压当失败就是缺陷。
SLOW_READ_DELAY_SECONDS = 0.001

# 测试用占位凭据与哨兵值：全部为本地临时值，不来自任何真实密钥。
USERNAME = "perf-user"
PASSWORD = "perf-password0000PASSWORDSENTINEL0000"
TOKEN_SENTINEL = "tok0000TOKENSENTINEL0000tok"
SIGNED_QUERY_SENTINEL = "sig0000SIGNEDQUERYSENTINEL0000sig"
ROTATED_QUERY_SENTINEL = "sig0000ROTATEDQUERYSENTINEL0000sig"
DOWNGRADE_QUERY_SENTINEL = "sig0000DOWNGRADESENTINEL0000sig"
REDIRECT_QUERY_SENTINEL = "sig0000REDIRECTSENTINEL0000sig"

# docker 替身用的合成身份：import 报告的 imageId、push 输出证据与 manifest descriptor。
STUB_IMAGE_ID = "sha256:" + "1" * 64
DOCKER_LAYER_DIGEST = "sha256:" + "3" * 64
DOCKER_LAYER_SIZE = 4321


def docker_push_output(document: bytes, *, tag: str = "latest", pushed: bool = True) -> str:
    """构造 Docker 28 真实格式的 push 输出。

    逐层证据是 ``<layer>: Pushed``；``<tag>: digest: <manifest digest> size: N`` 里的
    digest/size 描述的是 manifest 及其原文字节数，不是 layer descriptor。
    """
    manifest_digest = "sha256:" + hashlib.sha256(document).hexdigest()
    lines: list[str] = []
    if pushed:
        lines.append(f"{DOCKER_LAYER_DIGEST[7:19]}: Pushed")
    lines.append(f"{tag}: digest: {manifest_digest} size: {len(document)}")
    return "\n".join(lines) + "\n"


def consistent_docker_manifest() -> bytes:
    """测试用合法单层 manifest：config digest 绑定替身 import 报告的 imageId。"""
    return docker_manifest_document(DOCKER_LAYER_DIGEST, DOCKER_LAYER_SIZE, config_digest=STUB_IMAGE_ID)


def consistent_push_output(*, pushed: bool = True) -> str:
    """与 consistent_docker_manifest 配套的真实格式 push 输出。"""
    return docker_push_output(consistent_docker_manifest(), pushed=pushed)


def basic_authorization() -> str:
    """返回测试凭据的 Basic Authorization 头值。"""
    raw = f"{USERNAME}:{PASSWORD}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


@functools.lru_cache(maxsize=1)
def push_perf_module() -> types.ModuleType:
    """按路径加载单文件诊断模块；模块缺失时给出明确的 RED 断言。"""
    assert PUSH_PERF_PATH.is_file(), f"待实现的诊断模块不存在: {PUSH_PERF_PATH}"
    spec = importlib.util.spec_from_file_location("push_perf_under_test", PUSH_PERF_PATH)
    assert spec is not None and spec.loader is not None, f"无法按路径加载诊断模块: {PUSH_PERF_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def module_attr(module: types.ModuleType, name: str) -> Any:
    """取动态加载模块的公开接口属性，缺失即视为接口违约。"""
    attribute = getattr(module, name, None)
    assert attribute is not None, f"diagnostics/push-perf.py 缺少公开接口 {name}"
    return attribute


def run_sample(
    *,
    registry: str,
    namespace: str,
    tls_context: ssl.SSLContext,
    environment: str = "control",
    run_id: str = "local-run",
    connect_timeout: float = 10.0,
    upload_timeout: float = 120.0,
    auth_origin: str | None = None,
    http_only: bool = True,
    password: str = PASSWORD,
) -> dict[str, Any]:
    """用公开 run_sample 接口跑一次样本，可注入受信 SSLContext、auth_origin 与缩短的超时。"""
    module = push_perf_module()
    config_factory = module_attr(module, "Config")
    credentials_factory = module_attr(module, "Credentials")
    sample_runner = module_attr(module, "run_sample")
    arguments: dict[str, Any] = {
        "registry": registry,
        "namespace": namespace,
        "environment": environment,
        "run_id": run_id,
        "http_only": http_only,
        "connect_timeout": connect_timeout,
        "upload_timeout": upload_timeout,
    }
    if auth_origin is not None:
        arguments["auth_origin"] = auth_origin
    config = config_factory(**arguments)
    credentials = credentials_factory(username=USERNAME, password=password)
    result = sample_runner(config, credentials, tls_context=tls_context)
    assert isinstance(result, dict), f"run_sample 必须返回 dict，实际为 {type(result)!r}"
    return cast(dict[str, Any], result)


def result_json(result: dict[str, Any]) -> str:
    """把结果序列化成可做脱敏断言的文本。"""
    return json.dumps(result, ensure_ascii=False, sort_keys=True, default=repr)


def assert_status(result: dict[str, Any], expected: str) -> None:
    """断言样本状态，并在失败时输出完整结果。"""
    assert result.get("status") == expected, result_json(result)


def assert_no_upload(endpoint: FaultEndpoint) -> None:
    """断言故障端点没有收到任何上传请求或上传体字节。"""
    assert endpoint.requests("POST") == [], "载荷上传前不得开始 upload 会话"
    assert endpoint.requests("PATCH") == [], "载荷上传前不得发送 PATCH"
    assert endpoint.received_body_bytes() == 0, "载荷上传前不得发送任何请求体字节"


def stages_with_body_bytes(result: dict[str, Any], body_bytes: int) -> list[dict[str, Any]]:
    """取出 body 字节数等于指定值的阶段。"""
    stages = result.get("stages")
    assert isinstance(stages, list), result_json(result)
    return [stage for stage in stages if stage.get("body_bytes") == body_bytes]


# --------------------------------------------------------------------------------------
# TLS 材料
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TlsMaterial:
    """一次性 CA 与服务器证书路径，只存在于 pytest 临时目录。"""

    ca_pem: Path
    cert_pem: Path
    key_pem: Path


def openssl_binary() -> Path:
    """返回可用的 openssl 路径，缺失时跳过依赖 TLS 的测试。"""
    if OPENSSL_BIN.is_file():
        return OPENSSL_BIN
    found = shutil.which("openssl")
    if found is None:
        pytest.skip("本机没有 openssl，无法生成测试 TLS 证书")
    return Path(found)


def run_openssl(openssl: Path, arguments: list[str]) -> None:
    """执行 openssl 命令，失败时抛出包含 stderr 的断言。"""
    completed = subprocess.run([str(openssl), *arguments], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, f"openssl 失败: {' '.join(arguments)}\n{completed.stderr}"


def generate_tls_material(directory: Path) -> TlsMaterial:
    """用 openssl 动态生成临时 CA 与 SAN=localhost/IP 的服务器证书。

    OpenSSL 4.x 生成的服务器证书必须带 authorityKeyIdentifier，否则 Python 客户端会以
    "Missing Authority Key Identifier" 拒绝；AKI 只能在签发阶段写入，因此放在 ext 文件里。
    """
    openssl = openssl_binary()
    ca_key = directory / "ca.key"
    ca_pem = directory / "ca.pem"
    server_key = directory / "server.key"
    server_csr = directory / "server.csr"
    server_pem = directory / "server.pem"
    server_ext = directory / "server.ext"
    server_ext.write_text(
        "[server_ext]\n"
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid:always\n"
        "subjectAltName=DNS:localhost,IP:127.0.0.1\n",
        encoding="utf-8",
    )
    run_openssl(
        openssl,
        [
            "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "2", "-nodes",
            "-keyout", str(ca_key), "-out", str(ca_pem), "-subj", "/CN=push-perf-local-ca",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            "-addext", "subjectKeyIdentifier=hash",
        ],
    )
    run_openssl(
        openssl,
        [
            "req", "-newkey", "rsa:2048", "-sha256", "-nodes",
            "-keyout", str(server_key), "-out", str(server_csr), "-subj", "/CN=localhost",
        ],
    )
    run_openssl(
        openssl,
        [
            "x509", "-req", "-in", str(server_csr), "-CA", str(ca_pem), "-CAkey", str(ca_key),
            "-CAcreateserial", "-out", str(server_pem), "-days", "2", "-sha256",
            "-extfile", str(server_ext), "-extensions", "server_ext",
        ],
    )
    return TlsMaterial(ca_pem=ca_pem, cert_pem=server_pem, key_pem=server_key)


def client_context(tls: TlsMaterial) -> ssl.SSLContext:
    """构造只信任本次临时 CA 的客户端上下文，不关闭证书与主机名校验。"""
    context = ssl.create_default_context(cafile=str(tls.ca_pem))
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True
    return context


@pytest.fixture(scope="session")
def tls_material(tmp_path_factory: pytest.TempPathFactory) -> TlsMaterial:
    """会话级临时 TLS 材料（CA + 服务器证书）。"""
    return generate_tls_material(tmp_path_factory.mktemp("push-perf-tls"))


@pytest.fixture
def tls_client_context(tls_material: TlsMaterial) -> ssl.SSLContext:
    """函数级客户端 SSLContext，信任会话级临时 CA。"""
    return client_context(tls_material)


# --------------------------------------------------------------------------------------
# TLS 协议故障端点（不是真实 Registry）
# --------------------------------------------------------------------------------------


@dataclass
class RecordedRequest:
    """故障端点收到的一次请求；body 只在小尺寸时保留。"""

    method: str
    target: str
    headers: dict[str, str]
    body_bytes: int = 0
    body: bytes = b""
    status: int | None = None

    def query(self) -> dict[str, list[str]]:
        """返回请求目标里解析出的 query 参数。"""
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.target).query)


@dataclass(frozen=True)
class FaultSpec:
    """一次故障响应的脚本。

    framing 决定响应定界方式：``length`` 用 Content-Length、``chunked`` 用
    Transfer-Encoding、``close`` 用连接关闭定界、``truncated`` 声明长度后提前关闭。
    """

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    read_limit: int | None = None
    read_delay_seconds: float = 0.0
    hold_seconds: float = 0.0
    hold_after_read_seconds: float = 0.0
    abort_connection: bool = False
    framing: str = "length"
    declared_content_length: int | None = None
    trickle_response: bytes = b""
    trickle_prefix_bytes: int = 0
    trickle_gap_seconds: float = 0.0
    finalizer: Callable[[RecordedRequest], FaultSpec] | None = None


Responder = Callable[[RecordedRequest], FaultSpec]


class FaultEndpointServer(ThreadingHTTPServer):
    """故障端点服务器：把响应脚本挂在实例上。"""

    daemon_threads = True
    allow_reuse_address = True
    # 不等待仍在 hold 的处理器线程，避免 deadline 用例拖慢收尾。
    block_on_close = False
    fault_endpoint: FaultEndpoint


class FaultHandler(BaseHTTPRequestHandler):
    """故障端点请求处理：记录请求、按脚本读 body 并回响应。"""

    protocol_version = "HTTP/1.1"
    server_version = "push-perf-fault-endpoint"
    sys_version = ""
    _read_delay: float = 0.0

    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def log_message(self, format: str, *args: Any) -> None:
        """静默默认访问日志；参数只为满足 http.server 约定。"""
        _ = (format, args)

    def _dispatch(self) -> None:
        """处理一次请求；客户端提前断开只关闭当前连接。"""
        server = cast(FaultEndpointServer, self.server)
        endpoint = server.fault_endpoint
        headers = {name.lower(): value for name, value in self.headers.items()}
        record = RecordedRequest(method=self.command, target=self.path, headers=headers)
        endpoint.record(record)
        try:
            spec = endpoint.respond(record)
            record.status = spec.status
            if spec.hold_seconds > 0:
                time.sleep(spec.hold_seconds)
                self.close_connection = True
                return
            body = self._read_body(spec)
            record.body_bytes = len(body)
            if len(body) <= MAX_RECORDED_BODY_BYTES:
                record.body = body
            else:
                # 大载荷不整包留存，仍保留 gzip 魔数供线体类型断言。
                record.body = body[:2]
            if spec.abort_connection:
                # 提交结果未知：收到请求体后直接断开，不回任何响应。
                self.close_connection = True
                return
            if spec.hold_after_read_seconds > 0:
                time.sleep(spec.hold_after_read_seconds)
                self.close_connection = True
                return
            if spec.trickle_response:
                self._write_trickled(spec)
                return
            if spec.finalizer is not None:
                spec = spec.finalizer(record)
                record.status = spec.status
            self._write_response(spec)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            self.close_connection = True

    def _read_body(self, spec: FaultSpec) -> bytes:
        """按 Content-Length 或 chunked 读取请求体；spec 决定读取上限与节流速率。"""
        self._read_delay = spec.read_delay_seconds
        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer_encoding:
            return self._read_chunked(spec.read_limit)
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return b""
        try:
            expected = int(raw_length)
        except ValueError:
            return b""
        if spec.read_limit is not None:
            expected = min(expected, spec.read_limit)
        return self._read_exact(expected)

    def _read_exact(self, size: int) -> bytes:
        """读取指定字节数；连接提前关闭时返回已读部分，并按需节流。"""
        remaining = size
        chunks: list[bytes] = []
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 16))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
            if self._read_delay > 0:
                time.sleep(self._read_delay)
        return b"".join(chunks)

    def _read_chunked(self, limit: int | None) -> bytes:
        """读取 chunked 请求体，limit 是最多读取的字节数。"""
        chunks: list[bytes] = []
        total = 0
        while True:
            header = self.rfile.readline(64).strip()
            if not header:
                break
            try:
                size = int(header.split(b";", 1)[0], 16)
            except ValueError:
                break
            if size == 0:
                self.rfile.readline(64)
                break
            chunk = self._read_exact(size)
            chunks.append(chunk)
            total += len(chunk)
            self.rfile.readline(64)
            if limit is not None and total >= limit:
                break
        return b"".join(chunks)

    def _write_trickled(self, spec: FaultSpec) -> None:
        """按字节节奏发送原始响应字节，用于验证真实客户端是否守住绝对截止时间。"""
        payload = spec.trickle_response
        prefix = payload[: spec.trickle_prefix_bytes]
        if prefix:
            self.wfile.write(prefix)
            self.wfile.flush()
        for byte in payload[spec.trickle_prefix_bytes :]:
            time.sleep(spec.trickle_gap_seconds)
            self.wfile.write(bytes([byte]))
            self.wfile.flush()
        self.close_connection = True

    def _write_response(self, spec: FaultSpec) -> None:
        """写出故障响应；HEAD 不发 body，framing 决定响应定界方式。"""
        self.send_response(spec.status)
        for name, value in spec.headers.items():
            self.send_header(name, value)
        framing = spec.framing
        if framing == "chunked":
            self.send_header("Transfer-Encoding", "chunked")
        elif framing == "truncated":
            declared = spec.declared_content_length
            self.send_header("Content-Length", str(declared if declared is not None else len(spec.body) + 16))
        elif framing == "length" and not any(name.lower() == "content-length" for name in spec.headers):
            self.send_header("Content-Length", str(len(spec.body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command == "HEAD":
            self.close_connection = True
            return
        if framing == "chunked":
            if spec.body:
                self.wfile.write(f"{len(spec.body):x}\r\n".encode("ascii") + spec.body + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        elif spec.body:
            self.wfile.write(spec.body)
        self.close_connection = True


class FaultEndpoint:
    """TLS 协议故障端点：不是真实 Registry，只按脚本重放协议行为。"""

    def __init__(self, tls: TlsMaterial | None, respond: Responder) -> None:
        """启动端点并绑定响应脚本；tls 为 None 时启动明文 HTTP 端点。"""
        self.respond = respond
        self._requests: list[RecordedRequest] = []
        self._lock = threading.Lock()
        self._server = FaultEndpointServer(("127.0.0.1", 0), FaultHandler)
        if tls is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile=str(tls.cert_pem), keyfile=str(tls.key_pem))
            self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self._server.fault_endpoint = self
        self._thread = threading.Thread(target=self._server.serve_forever, name="push-perf-fault-endpoint", daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        """监听端口。"""
        return int(self._server.server_address[1])

    @property
    def registry(self) -> str:
        """可直接传给 Config.registry 的 host:port。"""
        return f"localhost:{self.port}"

    def record(self, request: RecordedRequest) -> None:
        """线程安全地记录一次请求。"""
        with self._lock:
            self._requests.append(request)

    def requests(self, method: str | None = None) -> list[RecordedRequest]:
        """返回已记录请求的快照，可按 HTTP 方法过滤。"""
        with self._lock:
            snapshot = list(self._requests)
        if method is None:
            return snapshot
        return [request for request in snapshot if request.method == method]

    def received_body_bytes(self) -> int:
        """所有已记录请求收到的 body 字节总数。"""
        return sum(request.body_bytes for request in self.requests())

    def close(self) -> None:
        """停止端点并释放端口。"""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)


@pytest.fixture
def fault_endpoints(tls_material: TlsMaterial) -> Iterator[Callable[..., FaultEndpoint]]:
    """返回启动故障端点的工厂（secure=False 启动明文端点），测试结束后统一关闭。"""
    endpoints: list[FaultEndpoint] = []

    def start(respond: Responder, *, secure: bool = True) -> FaultEndpoint:
        endpoint = FaultEndpoint(tls_material if secure else None, respond)
        endpoints.append(endpoint)
        return endpoint

    yield start
    for endpoint in endpoints:
        endpoint.close()


# --------------------------------------------------------------------------------------
# docker 外部边界替身（真实可执行文件，不是真实 daemon）
# --------------------------------------------------------------------------------------

# 测试用 docker 替身：真实可执行脚本，只回放可控输出与退出码。
# 它不能作为任何“真实 daemon 已上传”的证据；只用于覆盖消费方在无证据/失败时的分支。
DOCKER_STUB_TEMPLATE = r'''#!/usr/bin/env python3
"""测试用 docker 替身：真实可执行外部边界，回放可控输出/退出码。"""
import json
import os
import sys
import time
from pathlib import Path


def env(name, default=""):
    return os.environ.get(name, default)


log = env("DOCKER_STUB_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": sys.argv[1:], "pid": os.getpid()}, ensure_ascii=False) + "\n")

command = sys.argv[1] if len(sys.argv) > 1 else ""
if command == "version":
    print(env("DOCKER_STUB_CLIENT_VERSION", "28.0.4") + "|" + env("DOCKER_STUB_SERVER_VERSION", "28.0.4"))
    raise SystemExit(int(env("DOCKER_STUB_VERSION_EXIT", "0")))
if command == "import":
    if env("DOCKER_STUB_DELETE_PAYLOAD") == "1":
        tar = Path(sys.argv[2])
        for payload in tar.parent.glob("payload.bin"):
            payload.unlink()
    print("sha256:" + "1" * 64)
    raise SystemExit(int(env("DOCKER_STUB_IMPORT_EXIT", "0")))
if command == "login":
    if env("DOCKER_STUB_LOGIN_CLOSE_STDIN") == "1":
        sys.stdin.close()
    if env("DOCKER_STUB_LOGIN_DRAIN_STDIN", "1") == "1":
        sys.stdin.read()
    hold = float(env("DOCKER_STUB_LOGIN_SLEEP_SECONDS", "0") or "0")
    if hold > 0:
        time.sleep(hold)
    raise SystemExit(int(env("DOCKER_STUB_LOGIN_EXIT", "0")))
if command == "push":
    if env("DOCKER_STUB_PUSH_STREAM") == "1":
        blob = b"x" * 65536
        while True:
            sys.stdout.buffer.write(blob)
            sys.stdout.buffer.flush()
    pieces = env("DOCKER_STUB_PUSH_PIECES")
    if pieces:
        for piece in json.loads(pieces):
            sys.stdout.write(piece)
            sys.stdout.flush()
            time.sleep(float(env("DOCKER_STUB_PUSH_PIECE_GAP", "0.05")))
    text = env("DOCKER_STUB_PUSH_STDOUT")
    if text:
        sys.stdout.write(text.replace("\n", chr(10)))
        sys.stdout.flush()
    raise SystemExit(int(env("DOCKER_STUB_PUSH_EXIT", "0")))
if command == "logout":
    raise SystemExit(int(env("DOCKER_STUB_LOGOUT_EXIT", "0")))
if command == "image":
    raise SystemExit(int(env("DOCKER_STUB_IMAGE_RM_EXIT", "0")))
raise SystemExit(3)
'''


@dataclass
class DockerStub:
    """docker 替身句柄：记录外部调用 argv 与 pid，供消费方断言调用边界与回收。"""

    path: Path
    log_path: Path

    def records(self) -> list[dict[str, Any]]:
        """返回替身记录的全部调用（argv 与 pid）。"""
        if not self.log_path.is_file():
            return []
        entries = self.log_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(entry) for entry in entries if entry.strip()]

    def calls(self) -> list[list[str]]:
        """返回替身收到的所有 docker 调用（按顺序）。"""
        return [list(record.get("argv") or []) for record in self.records()]

    def calls_with(self, subcommand: str) -> list[list[str]]:
        """返回指定子命令的调用（记录的是 argv[1:]，首项即子命令）。"""
        return [call for call in self.calls() if call and call[0] == subcommand]

    def pids_with(self, subcommand: str) -> list[int]:
        """返回指定子命令对应的子进程 PID，用于断言子进程已被回收。"""
        pids: list[int] = []
        for record in self.records():
            argv = list(record.get("argv") or [])
            if argv and argv[0] == subcommand:
                pids.append(int(record["pid"]))
        return pids


@pytest.fixture
def docker_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[DockerStub]:
    """把真实可执行的 docker 替身放在 PATH 最前，并记录每次调用。

    这是外部边界替身，不是真实 daemon；任何有效性的“上传证据”断言都不能由它满足。
    """
    stub_dir = tmp_path / "docker-stub-bin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub_path = stub_dir / "docker"
    stub_path.write_text(DOCKER_STUB_TEMPLATE, encoding="utf-8")
    stub_path.chmod(0o755)
    log_path = tmp_path / "docker-stub-calls.jsonl"
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("DOCKER_STUB_LOG", str(log_path))
    monkeypatch.setenv("DOCKER_STUB_CLIENT_VERSION", "28.0.4")
    monkeypatch.setenv("DOCKER_STUB_SERVER_VERSION", "28.0.4")
    yield DockerStub(path=stub_path, log_path=log_path)


# --------------------------------------------------------------------------------------
# 真实 Distribution v3.1.2
# --------------------------------------------------------------------------------------


@dataclass
class LocalRegistry:
    """真实 Distribution 实例（TLS + 临时 filesystem storage）。"""

    process: subprocess.Popen[bytes]
    port: int
    storage_root: Path
    log_path: Path
    log_file: IO[bytes]
    context: ssl.SSLContext

    @property
    def registry(self) -> str:
        """可直接传给 Config.registry 的 host:port。"""
        return f"localhost:{self.port}"

    def log_text(self) -> str:
        """返回 registry 自身日志文本。"""
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def request_count(self, method: str) -> int:
        """统计 registry 访问日志中该 HTTP 方法的请求数。"""
        pattern = rf"http\.request\.method={re.escape(method)}\b"
        return len(re.findall(pattern, self.log_text()))

    def log_mark(self) -> int:
        """返回当前日志的字节偏移，可只统计之后发生的请求。"""
        return self.log_path.stat().st_size

    def request_count_since(self, mark: int, method: str) -> int:
        """统计 mark 之后访问日志里该方法的请求数（预置数据不计入）。"""
        with self.log_path.open("rb") as handle:
            handle.seek(mark)
            text = handle.read().decode("utf-8", "replace")
        pattern = rf"http\.request\.method={re.escape(method)}\b"
        return len(re.findall(pattern, text))

    def stop(self) -> None:
        """停止 registry 并关闭日志文件。"""
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10.0)
        self.log_file.close()


def reserve_port() -> int:
    """占用并立即释放一个本地端口，交给 registry 使用。"""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def registry_config(port: int, storage_root: Path, tls: TlsMaterial) -> str:
    """生成 TLS + filesystem storage + 允许删除的真实 registry 配置。"""
    return (
        "version: 0.1\n"
        "log:\n"
        "  level: info\n"
        "storage:\n"
        "  filesystem:\n"
        f"    rootdirectory: {storage_root}\n"
        "  delete:\n"
        "    enabled: true\n"
        "http:\n"
        f"  addr: 127.0.0.1:{port}\n"
        f"  host: https://localhost:{port}\n"
        "  tls:\n"
        f"    certificate: {tls.cert_pem}\n"
        f"    key: {tls.key_pem}\n"
    )


def start_registry(directory: Path, tls: TlsMaterial) -> LocalRegistry:
    """启动真实 Distribution，等待 HTTPS 就绪后返回实例。"""
    storage_root = directory / "storage"
    storage_root.mkdir(parents=True, exist_ok=True)
    port = reserve_port()
    config_path = directory / "config.yml"
    config_path.write_text(registry_config(port, storage_root, tls), encoding="utf-8")
    log_path = directory / "registry.log"
    log_file = log_path.open("wb")
    process = subprocess.Popen(
        [str(REGISTRY_BIN), "serve", str(config_path)],
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    registry = LocalRegistry(
        process=process,
        port=port,
        storage_root=storage_root,
        log_path=log_path,
        log_file=log_file,
        context=client_context(tls),
    )
    deadline = time.monotonic() + REGISTRY_READY_TIMEOUT
    last_error = "registry 未响应"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            registry.stop()
            raise AssertionError(f"真实 registry 启动即退出（exit={process.returncode}）：\n{log_path.read_text(encoding='utf-8', errors='replace')}")
        try:
            status, _, _ = https_request(registry.context, port, "GET", "/v2/", timeout=2.0)
        except (OSError, http.client.HTTPException) as error:
            last_error = str(error)
        else:
            if status == 200:
                return registry
            last_error = f"GET /v2/ 返回 {status}"
        time.sleep(0.2)
    registry.stop()
    raise AssertionError(f"真实 registry 未在 {REGISTRY_READY_TIMEOUT}s 内就绪：{last_error}")


@pytest.fixture(scope="session")
def local_registry(tls_material: TlsMaterial, tmp_path_factory: pytest.TempPathFactory) -> Iterator[LocalRegistry]:
    """会话级真实 Distribution v3.1.2 实例；工具缺失时跳过集成项。"""
    if not REGISTRY_BIN.is_file():
        pytest.skip(f"缺少真实 registry 可执行文件: {REGISTRY_BIN}")
    registry = start_registry(tmp_path_factory.mktemp("push-perf-registry"), tls_material)
    yield registry
    registry.stop()


# --------------------------------------------------------------------------------------
# HTTP 与 manifest 辅助
# --------------------------------------------------------------------------------------


def https_request(
    context: ssl.SSLContext,
    port: int,
    method: str,
    target: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 60.0,
    host: str = "localhost",
) -> tuple[int, dict[str, str], bytes]:
    """用 stdlib HTTPS 客户端发一次请求，返回 (状态码, 小写头, body)。"""
    connection = http.client.HTTPSConnection(host, port, context=context, timeout=timeout)
    try:
        connection.request(method, target, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        return response.status, {name.lower(): value for name, value in response.getheaders()}, payload
    finally:
        connection.close()


def docker_manifest_document(layer_digest: str, layer_size: int, config_digest: str | None = None) -> bytes:
    """构造合法单层 schema2 manifest，供 Docker 交叉臂的 manifest 读回使用。

    config 描述符默认是真实配置 JSON 的摘要；测试可传入替身 import 报告的 imageId，
    用于验证 config digest 是否绑定本次 import 的镜像身份。
    """
    config = json.dumps({"architecture": "amd64", "os": "linux"}, separators=(",", ":")).encode()
    resolved_config_digest = config_digest or ("sha256:" + hashlib.sha256(config).hexdigest())
    document = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {
            "mediaType": "application/vnd.docker.container.image.v1+json",
            "size": len(config),
            "digest": resolved_config_digest,
        },
        "layers": [
            {
                "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
                "size": layer_size,
                "digest": layer_digest,
            }
        ],
    }
    return json.dumps(document).encode()


def registry_push_blob(registry: LocalRegistry, full_repository: str, blob: bytes) -> str:
    """用原始 HTTP 把一个 blob 推到真实 registry，返回 digest。"""
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    base = f"/v2/{full_repository}"
    status, _, _ = https_request(registry.context, registry.port, "HEAD", f"{base}/blobs/{digest}")
    assert status == 404, f"预置新鲜度 HEAD 期望 404，实际 {status}"
    status, headers, body = https_request(registry.context, registry.port, "POST", f"{base}/blobs/uploads/", body=b"")
    assert status == 202, f"预置 POST 期望 202，实际 {status} {body!r}"
    location = headers["location"]
    status, headers, body = https_request(
        registry.context, registry.port, "PATCH", location, body=blob, headers={"Content-Type": "application/octet-stream"}
    )
    assert status == 202, f"预置 PATCH 期望 202，实际 {status} {body!r}"
    location = headers.get("location", location)
    separator = "&" if "?" in location else "?"
    target = f"{location}{separator}digest={urllib.parse.quote(digest, safe='')}"
    status, _, body = https_request(registry.context, registry.port, "PUT", target, body=b"")
    assert status == 201, f"预置 PUT 期望 201，实际 {status} {body!r}"
    return digest


def seed_repository(registry: LocalRegistry, namespace: str) -> str:
    """在真实 registry 里预置一个内容合法的单层仓库，返回 seed tag。

    config 是真实配置 JSON，layer 是真实 gzip tar。这样 tags/list 返回 200，
    原始 HTTP 臂的验收就不再依赖“仓库不存在时是否继续创建”的分支。
    """
    full_repository = f"{namespace}/image-sync-perf"
    config_blob = json.dumps(
        {"architecture": "amd64", "os": "linux", "rootfs": {"type": "layers", "diff_ids": []}},
        separators=(",", ":"),
    ).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        content = b"seed\n"
        info = tarfile.TarInfo(name="seed.txt")
        info.size = len(content)
        info.mtime = 0
        archive.addfile(info, io.BytesIO(content))
    layer_blob = buffer.getvalue()
    config_digest = registry_push_blob(registry, full_repository, config_blob)
    layer_digest = registry_push_blob(registry, full_repository, layer_blob)
    document = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {
            "mediaType": "application/vnd.docker.container.image.v1+json",
            "size": len(config_blob),
            "digest": config_digest,
        },
        "layers": [
            {
                "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
                "size": len(layer_blob),
                "digest": layer_digest,
            }
        ],
    }
    status, _, body = https_request(
        registry.context,
        registry.port,
        "PUT",
        f"/v2/{full_repository}/manifests/seed",
        body=json.dumps(document).encode(),
        headers={"Content-Type": "application/vnd.docker.distribution.manifest.v2+json"},
    )
    assert status == 201, f"预置 manifest 失败: {status} {body!r}"
    return "seed"


def registry_tags(registry: LocalRegistry, full_repository: str) -> list[str]:
    """读取真实 registry 的 tags/list。"""
    status, _, body = https_request(registry.context, registry.port, "GET", f"/v2/{full_repository}/tags/list")
    assert status == 200, f"tags/list 期望 200，实际 {status}"
    return list(json.loads(body).get("tags") or [])


def registry_tags_status(registry: LocalRegistry, full_repository: str) -> tuple[int, list[str]]:
    """读取真实 registry 的 tags/list，返回 (状态码, tags)。

    空仓/无 tag 时 Distribution 返回 404 NAME_UNKNOWN，因此这里不把 404 当作异常。
    """
    status, _, body = https_request(registry.context, registry.port, "GET", f"/v2/{full_repository}/tags/list")
    if status != 200:
        return status, []
    try:
        document = json.loads(body)
    except json.JSONDecodeError:
        return status, []
    return status, list(document.get("tags") or [])


def repository_from_path(path: str) -> str:
    """从 /v2/<namespace>/<repository>/... 中取出 namespace/repository。"""
    parts = path.split("/")
    return "/".join(parts[2:4]) if len(parts) >= 4 else ""


# --------------------------------------------------------------------------------------
# 故障响应脚本
# --------------------------------------------------------------------------------------


def tags_list_spec() -> FaultSpec:
    """已存在测试仓的 tags/list 响应：默认必须是 200。"""
    return FaultSpec(
        status=200,
        body=b'{"name": "image-sync-perf", "tags": ["seed"]}',
        headers={"Content-Type": "application/json"},
    )


def repository_unknown_spec() -> FaultSpec:
    """仓库不存在：真实 registry 的 404 NAME_UNKNOWN 响应。"""
    return FaultSpec(
        status=404,
        body=b'{"errors":[{"code":"NAME_UNKNOWN","message":"repository name not known to registry"}]}',
        headers={"Content-Type": "application/json"},
    )


def fresh_head_responder(head_status: int) -> Responder:
    """只让仓库内 blob HEAD 返回指定状态，其余请求不应到达上传阶段。"""

    def respond(request: RecordedRequest) -> FaultSpec:
        path, _, _ = request.target.partition("?")
        if path.rstrip("/") == "/v2":
            return FaultSpec(status=200, body=b"{}")
        if path.endswith("/tags/list"):
            return tags_list_spec()
        if request.method == "HEAD" and "/blobs/" in path:
            if head_status == 200:
                digest = path.rsplit("/", 1)[-1]
                return FaultSpec(status=200, headers={"Docker-Content-Digest": digest, "Content-Length": str(PAYLOAD_BYTES)})
            return FaultSpec(status=head_status)
        return FaultSpec(status=500, body=b"unexpected request")

    return respond


def basic_challenge_responder() -> Responder:
    """第一次仓库请求强制 401 Basic challenge，其后必须带正确 Basic 凭据。"""
    seen = {"repository_requests": 0}

    def respond(request: RecordedRequest) -> FaultSpec:
        path, _, _ = request.target.partition("?")
        if path.rstrip("/") == "/v2":
            return FaultSpec(status=200, body=b"{}")
        if path.endswith("/tags/list"):
            return tags_list_spec()
        seen["repository_requests"] += 1
        authorization = request.headers.get("authorization")
        if seen["repository_requests"] == 1:
            return FaultSpec(status=401, headers={"WWW-Authenticate": 'Basic realm="push-perf"'})
        if authorization != basic_authorization():
            return FaultSpec(status=401, headers={"WWW-Authenticate": 'Basic realm="push-perf"'})
        if request.method == "HEAD" and "/blobs/" in path:
            return FaultSpec(status=404)
        if request.method == "POST":
            # 鉴权通过也拒绝开始上传：不得发送任何载荷字节。
            return FaultSpec(status=403, body=b"{}")
        if request.method == "DELETE":
            return FaultSpec(status=403, body=b"{}")
        return FaultSpec(status=500, body=b"unexpected request")

    return respond


def bearer_challenge_responder(
    *,
    token_payload: dict[str, Any] | None = None,
    token_status: int = 200,
    token_body: bytes | None = None,
    post_status: int = 403,
    upload_location: Callable[[RecordedRequest], str] | None = None,
    patch_status: int = 400,
    realm: str | None = None,
) -> Responder:
    """Bearer challenge：token 端点按参数回应，仓库请求必须带 Bearer token。

    realm 为空时使用仓库同源 realm；给定时按给定值下发，用于跨 origin 鉴权规则测试。
    """
    token_requests = {"count": 0}

    def respond(request: RecordedRequest) -> FaultSpec:
        path, _, _ = request.target.partition("?")
        host = request.headers.get("host", "")
        if path.rstrip("/") == "/v2":
            return FaultSpec(status=200, body=b"{}")
        if path == "/token":
            token_requests["count"] += 1
            if token_requests["count"] > 5:
                # 兜底：实现若在拿不到 token 时空转取 token，测试必须失败而不是挂住。
                return FaultSpec(status=500, body=b'{"errors":[{"code":"TOO_MANY_REQUESTS"}]}')
            if token_body is not None:
                return FaultSpec(status=token_status, body=token_body, headers={"Content-Type": "application/json"})
            payload = token_payload if token_payload is not None else {"token": TOKEN_SENTINEL, "expires_in": 300}
            return FaultSpec(status=token_status, body=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        if path.endswith("/tags/list"):
            return tags_list_spec()
        if request.headers.get("authorization") != f"Bearer {TOKEN_SENTINEL}":
            scope = f"repository:{repository_from_path(path)}:pull,push"
            challenge_realm = realm if realm is not None else f"https://{host}/token"
            challenge = f'Bearer realm="{challenge_realm}",service="registry",scope="{scope}"'
            return FaultSpec(status=401, headers={"WWW-Authenticate": challenge})
        if request.method == "HEAD" and "/blobs/" in path:
            return FaultSpec(status=404)
        if request.method == "POST" and "/blobs/uploads/" in path:
            headers = {"Location": upload_location(request)} if upload_location is not None else {}
            return FaultSpec(status=post_status, headers=headers, body=b"{}")
        if request.method == "PATCH":
            return FaultSpec(status=patch_status, read_limit=0)
        if request.method == "DELETE":
            return FaultSpec(status=403, body=b"{}")
        return FaultSpec(status=500, body=b"unexpected request")

    return respond


def auth_endpoint_responder(
    *,
    token: str = TOKEN_SENTINEL,
    status: int = 200,
    location: str | None = None,
    require_basic: bool = True,
    framing: str = "length",
    truncate_body_to: int | None = None,
) -> Responder:
    """跨 origin 鉴权端点：只回应 token 请求，且必须收到 Basic 凭据。

    framing 用于覆盖合法/非法的 HTTP 响应定界：length/chunked/close/truncated。
    truncate_body_to 给定时声明完整长度但只发送前 N 字节，模拟提前 EOF。
    """

    def respond(request: RecordedRequest) -> FaultSpec:
        path, _, _ = request.target.partition("?")
        if path != "/token":
            return FaultSpec(status=500, body=b"unexpected request")
        if require_basic and request.headers.get("authorization") != basic_authorization():
            return FaultSpec(status=401, body=b'{"errors":[{"code":"UNAUTHORIZED"}]}')
        if location is not None:
            return FaultSpec(status=status, headers={"Location": location})
        body = json.dumps({"token": token, "expires_in": 300}).encode()
        if truncate_body_to is not None:
            return FaultSpec(
                status=status,
                body=body[:truncate_body_to],
                headers={"Content-Type": "application/json"},
                framing="truncated",
                declared_content_length=len(body),
            )
        return FaultSpec(
            status=status,
            body=body,
            headers={"Content-Type": "application/json"},
            framing=framing,
        )

    return respond


def relative_location_responder() -> Responder:
    """上传会话 Location 使用同源相对路径，并带 opaque query。"""
    fixed_upload_id = "22222222-3333-4444-5555-666666666666"

    def respond(request: RecordedRequest) -> FaultSpec:
        path, _, _ = request.target.partition("?")
        if path.rstrip("/") == "/v2":
            return FaultSpec(status=200, body=b"{}")
        if path.endswith("/tags/list"):
            return tags_list_spec()
        if request.method == "HEAD" and "/blobs/" in path:
            return FaultSpec(status=404)
        if request.method == "POST" and "/blobs/uploads/" in path:
            location = f"{path.rstrip('/')}/{fixed_upload_id}?_state={SIGNED_QUERY_SENTINEL}"
            return FaultSpec(status=202, headers={"Location": location, "Docker-Upload-UUID": fixed_upload_id})
        if request.method == "PATCH":
            return FaultSpec(status=400, body=b'{"errors":[{"code":"BLOB_UPLOAD_INVALID"}]}', read_limit=0)
        if request.method == "DELETE":
            return FaultSpec(status=403, body=b"{}")
        return FaultSpec(status=500, body=b"unexpected request")

    return respond


def cross_origin_responder(location: Callable[[RecordedRequest], str], status: int = 202) -> Responder:
    """上传会话返回不允许跟随的 Location（外域/降级/302）。"""

    def respond(request: RecordedRequest) -> FaultSpec:
        path, _, _ = request.target.partition("?")
        if path.rstrip("/") == "/v2":
            return FaultSpec(status=200, body=b"{}")
        if path.endswith("/tags/list"):
            return tags_list_spec()
        if request.method == "HEAD" and "/blobs/" in path:
            return FaultSpec(status=404)
        if request.method == "POST" and "/blobs/uploads/" in path:
            return FaultSpec(status=status, headers={"Location": location(request)})
        if request.method == "DELETE":
            return FaultSpec(status=404, body=b"{}")
        return FaultSpec(status=500, body=b"unexpected request")

    return respond


def patch_fault_responder(
    patch_status: int,
    patch_headers: dict[str, str] | None = None,
    read_limit: int | None = 0,
    *,
    with_rotation: bool = False,
) -> Responder:
    """PATCH 阶段按参数失败；POST 返回可用的上传会话 Location。"""
    upload_id = "33333333-4444-5555-6666-777777777777"

    def respond(request: RecordedRequest) -> FaultSpec:
        path, _, _ = request.target.partition("?")
        host = request.headers.get("host", "")
        if path.rstrip("/") == "/v2":
            return FaultSpec(status=200, body=b"{}")
        if path.endswith("/tags/list"):
            return tags_list_spec()
        if request.method == "HEAD" and "/blobs/" in path:
            return FaultSpec(status=404)
        if request.method == "POST" and "/blobs/uploads/" in path:
            location = f"https://{host}{path.rstrip('/')}/{upload_id}?_state={SIGNED_QUERY_SENTINEL}"
            return FaultSpec(status=202, headers={"Location": location, "Docker-Upload-UUID": upload_id})
        if request.method == "PATCH":
            response_headers = dict(patch_headers or {})
            if with_rotation:
                response_headers["Location"] = f"https://{host}{path}?_state={ROTATED_QUERY_SENTINEL}"
            return FaultSpec(status=patch_status, headers=response_headers, read_limit=read_limit)
        if request.method == "DELETE":
            return FaultSpec(status=403, body=b"{}")
        return FaultSpec(status=500, body=b"unexpected request")

    return respond


def commit_fault_responder(put_status: int = 500, put_digest: str | None = None) -> Responder:
    """完整上传通道，但 blob PUT 提交按参数失败或返回错误 digest。"""
    upload_id = "44444444-5555-6666-7777-888888888888"

    def respond(request: RecordedRequest) -> FaultSpec:
        path, _, query = request.target.partition("?")
        host = request.headers.get("host", "")
        if path.rstrip("/") == "/v2":
            return FaultSpec(status=200, body=b"{}")
        if path.endswith("/tags/list"):
            return tags_list_spec()
        if "/blobs/uploads/" in path:
            if request.method == "POST":
                location = f"https://{host}{path.rstrip('/')}/{upload_id}?_state={SIGNED_QUERY_SENTINEL}"
                return FaultSpec(status=202, headers={"Location": location, "Docker-Upload-UUID": upload_id})
            if request.method == "PATCH":
                return FaultSpec(
                    status=202,
                    headers={"Location": f"https://{host}{path}?_state={ROTATED_QUERY_SENTINEL}", "Range": f"0-{declared_content_length(request) - 1}"},
                )
            if request.method == "PUT":
                if put_status == 201:
                    digest = put_digest or dict(urllib.parse.parse_qsl(query)).get("digest", "")
                    return FaultSpec(status=201, headers={"Docker-Content-Digest": digest})
                return FaultSpec(status=put_status, body=b'{"errors":[{"code":"BLOB_UPLOAD_INVALID"}]}')
            if request.method == "DELETE":
                return FaultSpec(status=403, body=b"{}")
        if request.method == "HEAD" and "/blobs/" in path:
            return FaultSpec(status=404)
        return FaultSpec(status=500, body=b"unexpected request")

    return respond


def verify_length_responder() -> Responder:
    """上传与提交都成功，但提交后的 blob HEAD 验证返回错误的 Content-Length。"""
    upload_id = "55555555-6666-7777-8888-999999999999"
    state = {"committed": False}

    def respond(request: RecordedRequest) -> FaultSpec:
        path, _, query = request.target.partition("?")
        host = request.headers.get("host", "")
        if path.rstrip("/") == "/v2":
            return FaultSpec(status=200, body=b"{}")
        if path.endswith("/tags/list"):
            return tags_list_spec()
        if "/blobs/uploads/" in path:
            if request.method == "POST":
                location = f"https://{host}{path.rstrip('/')}/{upload_id}?_state={SIGNED_QUERY_SENTINEL}"
                return FaultSpec(status=202, headers={"Location": location, "Docker-Upload-UUID": upload_id})
            if request.method == "PATCH":
                return FaultSpec(
                    status=202,
                    headers={"Location": f"https://{host}{path}?_state={ROTATED_QUERY_SENTINEL}", "Range": f"0-{declared_content_length(request) - 1}"},
                )
            if request.method == "PUT":
                state["committed"] = True
                digest = dict(urllib.parse.parse_qsl(query)).get("digest", "")
                return FaultSpec(status=201, headers={"Docker-Content-Digest": digest})
            if request.method == "DELETE":
                return FaultSpec(status=403, body=b"{}")
        if request.method == "HEAD" and "/blobs/" in path:
            if not state["committed"]:
                return FaultSpec(status=404)
            digest = path.rsplit("/", 1)[-1]
            return FaultSpec(
                status=200,
                headers={"Docker-Content-Digest": digest, "Content-Length": str(PAYLOAD_BYTES - 1)},
            )
        return FaultSpec(status=500, body=b"unexpected request")

    return respond


def repository_unknown_responder(post_status: int = 404) -> Responder:
    """空仓预检（404 NAME_UNKNOWN）但上传开始被拒：用于证明失败且零载荷。

    POST 返回 post_status（404 表示仓库仍未知，403 表示无上传权限）；
    若客户端在预检失败后仍继续，这里会看到 POST/PATCH/PUT。
    """

    def respond(request: RecordedRequest) -> FaultSpec:
        path, _, _ = request.target.partition("?")
        if path.rstrip("/") == "/v2":
            return FaultSpec(status=200, body=b"{}")
        if path.endswith("/tags/list"):
            return repository_unknown_spec()
        if request.method == "HEAD" and "/blobs/" in path:
            return FaultSpec(status=404)
        if request.method == "POST" and "/blobs/uploads/" in path:
            return FaultSpec(status=post_status, body=b'{"errors":[{"code":"NAME_UNKNOWN"}]}')
        return FaultSpec(status=500, body=b"unexpected request")

    return respond


def precheck_failure_responder(tags_status: int, tags_body: bytes) -> Responder:
    """预检返回非空仓语义的状态；不得被当作空仓放行（POST 仍可用，用于暴露错误放行）。"""

    def respond(request: RecordedRequest) -> FaultSpec:
        path, _, _ = request.target.partition("?")
        host = request.headers.get("host", "")
        if path.rstrip("/") == "/v2":
            return FaultSpec(status=200, body=b"{}")
        if path.endswith("/tags/list"):
            return FaultSpec(status=tags_status, body=tags_body, headers={"Content-Type": "application/json"})
        if request.method == "HEAD" and "/blobs/" in path:
            return FaultSpec(status=404)
        if request.method == "POST" and "/blobs/uploads/" in path:
            location = f"https://{host}{path.rstrip('/')}/cccccccc-dddd-eeee-ffff-000000000000?_state={SIGNED_QUERY_SENTINEL}"
            return FaultSpec(status=202, headers={"Location": location, "Docker-Upload-UUID": "cccccccc-dddd-eeee-ffff-000000000000"})
        if request.method == "PATCH":
            return FaultSpec(status=400, body=b"{}", read_limit=0)
        return FaultSpec(status=500, body=b"unexpected request")

    return respond


def commit_response_lost_responder() -> Responder:
    """PUT 提交请求体已收到，但服务端在回应前断开：提交结果未知。"""
    upload_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def respond(request: RecordedRequest) -> FaultSpec:
        path, _, _ = request.target.partition("?")
        host = request.headers.get("host", "")
        if path.rstrip("/") == "/v2":
            return FaultSpec(status=200, body=b"{}")
        if path.endswith("/tags/list"):
            return tags_list_spec()
        if "/blobs/uploads/" in path:
            if request.method == "POST":
                location = f"https://{host}{path.rstrip('/')}/{upload_id}?_state={SIGNED_QUERY_SENTINEL}"
                return FaultSpec(status=202, headers={"Location": location, "Docker-Upload-UUID": upload_id})
            if request.method == "PATCH":
                return FaultSpec(
                    status=202,
                    headers={"Location": f"https://{host}{path}?_state={ROTATED_QUERY_SENTINEL}", "Range": f"0-{declared_content_length(request) - 1}"},
                )
            if request.method == "PUT":
                # 服务端已经收到提交体并可能已提交，但客户端看不到任何响应。
                return FaultSpec(status=201, abort_connection=True)
            if request.method == "DELETE":
                return FaultSpec(status=404, body=b'{"errors":[{"code":"BLOB_UPLOAD_UNKNOWN"}]}')
        if request.method == "HEAD" and "/blobs/" in path:
            return FaultSpec(status=404)
        return FaultSpec(status=500, body=b"unexpected request")

    return respond


def rotated_bad_range_responder() -> Responder:
    """PATCH 返回 202 与轮换后的 Location，但 Range 与已发送字节不符。"""
    upload_id = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"

    def respond(request: RecordedRequest) -> FaultSpec:
        path, _, _ = request.target.partition("?")
        host = request.headers.get("host", "")
        if path.rstrip("/") == "/v2":
            return FaultSpec(status=200, body=b"{}")
        if path.endswith("/tags/list"):
            return tags_list_spec()
        if request.method == "HEAD" and "/blobs/" in path:
            return FaultSpec(status=404)
        if request.method == "POST" and "/blobs/uploads/" in path:
            location = f"https://{host}{path.rstrip('/')}/{upload_id}?_state={SIGNED_QUERY_SENTINEL}"
            return FaultSpec(status=202, headers={"Location": location, "Docker-Upload-UUID": upload_id})
        if request.method == "PATCH":
            rotated = f"https://{host}{path}?_state={ROTATED_QUERY_SENTINEL}"
            return FaultSpec(status=202, headers={"Location": rotated, "Range": "0-1023"})
        if request.method == "DELETE":
            return FaultSpec(status=204)
        return FaultSpec(status=500, body=b"unexpected request")

    return respond


def hold_patch_responder(hold_seconds: float, *, after_read: bool = False) -> Responder:
    """接受上传会话后对 PATCH 不作响应；after_read 表示先读完 body 再静默。"""
    upload_id = "66666666-7777-8888-9999-aaaaaaaaaaaa"

    def respond(request: RecordedRequest) -> FaultSpec:
        path, _, _ = request.target.partition("?")
        host = request.headers.get("host", "")
        if path.rstrip("/") == "/v2":
            return FaultSpec(status=200, body=b"{}")
        if path.endswith("/tags/list"):
            return tags_list_spec()
        if request.method == "HEAD" and "/blobs/" in path:
            return FaultSpec(status=404)
        if request.method == "POST" and "/blobs/uploads/" in path:
            location = f"https://{host}{path.rstrip('/')}/{upload_id}?_state={SIGNED_QUERY_SENTINEL}"
            return FaultSpec(status=202, headers={"Location": location, "Docker-Upload-UUID": upload_id})
        if request.method == "PATCH":
            if after_read:
                return FaultSpec(status=202, hold_after_read_seconds=hold_seconds)
            return FaultSpec(status=202, hold_seconds=hold_seconds)
        if request.method == "DELETE":
            return FaultSpec(status=204)
        return FaultSpec(status=500, body=b"unexpected request")

    return respond


def cancel_rejected_responder() -> Responder:
    """PATCH 失败后，取消上传与删除 manifest/tag 都被服务端拒绝。"""
    upload_id = "77777777-8888-9999-aaaa-bbbbbbbbbbbb"

    def respond(request: RecordedRequest) -> FaultSpec:
        path, _, _ = request.target.partition("?")
        host = request.headers.get("host", "")
        if path.rstrip("/") == "/v2":
            return FaultSpec(status=200, body=b"{}")
        if path.endswith("/tags/list"):
            return tags_list_spec()
        if request.method == "HEAD" and "/blobs/" in path:
            return FaultSpec(status=404)
        if request.method == "POST" and "/blobs/uploads/" in path:
            location = f"https://{host}{path.rstrip('/')}/{upload_id}?_state={SIGNED_QUERY_SENTINEL}"
            return FaultSpec(status=202, headers={"Location": location, "Docker-Upload-UUID": upload_id})
        if request.method == "PATCH":
            return FaultSpec(status=400, body=b'{"errors":[{"code":"BLOB_UPLOAD_INVALID"}]}', read_limit=0)
        if request.method == "DELETE":
            return FaultSpec(status=403, body=b'{"errors":[{"code":"DENIED"}]}')
        return FaultSpec(status=500, body=b"unexpected request")

    return respond


def upload_success_responder(
    *,
    docker_layer: tuple[str, int] | None = None,
    docker_config_digest: str | None = None,
    docker_manifest_mode: str = "valid",
    slow_read_delay: float = SLOW_READ_DELAY_SECONDS,
) -> Responder:
    """按 Distribution v3.1.2 形态完成 POST→PATCH→PUT 的原始 HTTP 上传脚本。

    原始 HTTP 臂止于 blob 提交与 HEAD 验证，不发布任何 manifest；docker_layer 给定时，
    只额外回应 Docker 交叉臂读回的 manifest（合法 schema2 文档）。
    """
    upload_id = "88888888-9999-aaaa-bbbb-cccccccccccc"
    committed: set[str] = set()
    blob_sizes: dict[str, int] = {}
    patch_bytes = {"n": PAYLOAD_BYTES}

    def respond(request: RecordedRequest) -> FaultSpec:
        path, _, query = request.target.partition("?")
        host = request.headers.get("host", "")
        if path.rstrip("/") == "/v2":
            return FaultSpec(status=200, body=b"{}")
        if path.endswith("/tags/list"):
            return tags_list_spec()
        if "/blobs/uploads/" in path:
            if request.method == "POST":
                location = f"https://{host}{path.rstrip('/')}/{upload_id}?_state={SIGNED_QUERY_SENTINEL}"
                return FaultSpec(status=202, headers={"Location": location, "Docker-Upload-UUID": upload_id, "Range": "0-0"})
            if request.method == "PATCH":
                # PATCH 返回轮换后的 opaque query，客户端必须按新 Location 继续。
                # 服务端按块节流读取，客户端必须处理发送背压，不能把阻塞当失败。
                rotated = f"https://{host}{path}?_state={ROTATED_QUERY_SENTINEL}"
                patch_bytes["n"] = declared_content_length(request)
                return FaultSpec(
                    status=202,
                    headers={"Location": rotated, "Range": f"0-{patch_bytes['n'] - 1}"},
                    read_delay_seconds=slow_read_delay,
                )
            if request.method == "PUT":
                digest = dict(urllib.parse.parse_qsl(query)).get("digest", "")
                committed.add(digest)
                blob_sizes[digest] = patch_bytes["n"]
                return FaultSpec(status=201, headers={"Docker-Content-Digest": digest})
            if request.method == "DELETE":
                return FaultSpec(status=204)
        if "/manifests/" in path:
            if docker_layer is not None and request.method in {"HEAD", "GET"}:
                if docker_manifest_mode == "broken-json":
                    return FaultSpec(status=200, body=b"{not-json", headers={"Content-Type": "application/json"})
                if docker_manifest_mode == "hung":
                    return FaultSpec(status=200, hold_after_read_seconds=8.0)
                document = docker_manifest_document(*docker_layer, config_digest=docker_config_digest)
                digest = "sha256:" + hashlib.sha256(document).hexdigest()
                return FaultSpec(
                    status=200,
                    headers={"Docker-Content-Digest": digest, "Content-Length": str(len(document))},
                    body=document if request.method == "GET" else b"",
                )
            if request.method == "DELETE":
                return FaultSpec(status=202)
            return FaultSpec(status=404, body=b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}')
        if request.method == "HEAD" and "/blobs/" in path:
            digest = path.rsplit("/", 1)[-1]
            if digest not in committed:
                return FaultSpec(status=404)
            return FaultSpec(status=200, headers={"Docker-Content-Digest": digest, "Content-Length": str(blob_sizes.get(digest, PAYLOAD_BYTES))})
        return FaultSpec(status=500, body=b"unexpected request")

    return respond


# --------------------------------------------------------------------------------------
# 真实 Registry 验收
# --------------------------------------------------------------------------------------


def test_real_registry_empty_repository_upload_uses_post_as_authority(
    local_registry: LocalRegistry,
) -> None:
    """真实 Distribution 空仓：tags/list 404 NAME_UNKNOWN 只作 unknown/empty 观察，上传由 POST 202 决定。

    不预置 seed manifest/tag：空仓必须通过 POST 开始上传，64 MiB 单次 PATCH 真实落地，
    且本样本不发布任何 tag，只留下精确的 blob 残留。
    """
    namespace = "perf-empty"
    full_repository = f"{namespace}/image-sync-perf"
    pre_status, pre_tags = registry_tags_status(local_registry, full_repository)
    assert pre_status == 404, f"空仓预检应观察到 404（unknown/empty），实际 {pre_status}"
    assert pre_tags == [], pre_tags
    mark = local_registry.log_mark()

    result = run_sample(
        registry=local_registry.registry,
        namespace=namespace,
        tls_context=local_registry.context,
        run_id="empty-repo-round-trip",
    )

    assert_status(result, "valid")
    digest = result["payload"]["digest"]
    payload_size = int(result["payload"]["size_bytes"])
    assert local_registry.request_count_since(mark, "POST") == 1, "空仓必须通过 POST 202 才能开始上传"
    assert local_registry.request_count_since(mark, "PATCH") == 1, "64 MiB 载荷必须一次持续 PATCH 送达"
    assert local_registry.request_count_since(mark, "PUT") == 1, "只提交 blob，不得发布 manifest"

    precheck_stages = [stage for stage in result["stages"] if stage.get("name") == "repository_precheck"]
    assert precheck_stages and precheck_stages[-1]["http_status"] == 404, result_json(result)

    blob_status, blob_headers, blob = https_request(
        local_registry.context, local_registry.port, "GET", f"/v2/{full_repository}/blobs/{digest}", timeout=300.0
    )
    assert blob_status == 200, f"真实 registry 未返回 blob: {blob_status}"
    assert blob.startswith(b"\x1f\x8b"), f"落地线体必须是 gzip: {blob[:2]!r}"
    assert len(blob) == payload_size, f"落地长度不符: {len(blob)}"
    assert blob_headers.get("docker-content-digest") == digest, blob_headers
    assert "sha256:" + hashlib.sha256(blob).hexdigest() == digest, "落地内容 hash 与声称 digest 不符"

    _, post_tags = registry_tags_status(local_registry, full_repository)
    assert post_tags == [], "本样本不得发布任何 tag"
    residuals = result["cleanup"]["residuals"]
    assert any(entry.startswith("blob:") and digest in entry for entry in residuals), result_json(result)
    assert result["cleanup"]["storage_reclaimed"] is False, "不得宣称服务端存储已释放"


def test_real_registry_seeded_repository_round_trip_preserves_seed_and_reports_orphan_blob(
    local_registry: LocalRegistry,
) -> None:
    """真实 Distribution 已有 tag 的仓库：仍走同一上传路径，且不得破坏预置 tag。

    这是与空仓用例的区别：预置 tag 时 tags/list 为 200，本样本依然只提交 blob、不发布 manifest。
    """
    assert seed_repository(local_registry, "perf-local") == "seed"
    pre_status, pre_tags = registry_tags_status(local_registry, "perf-local/image-sync-perf")
    assert pre_status == 200 and "seed" in pre_tags, (pre_status, pre_tags)
    mark = local_registry.log_mark()

    result = run_sample(
        registry=local_registry.registry,
        namespace="perf-local",
        tls_context=local_registry.context,
        run_id="local-round-trip",
    )

    assert_status(result, "valid")
    assert result["environment"] == "control"
    assert result["run_id"] == "local-round-trip"
    assert result["repository"], result_json(result)
    assert str(result["full_repository"]).startswith(f"{result['namespace']}/"), result_json(result)
    assert result["tag"], result_json(result)

    payload = result["payload"]
    assert int(payload["size_bytes"]) >= PAYLOAD_BYTES, result_json(result)
    digest = payload["digest"]
    assert isinstance(digest, str) and digest.startswith("sha256:"), result_json(result)

    # 只看本次样本的日志区间，避免预置数据污染计数。
    assert local_registry.request_count_since(mark, "PATCH") == 1, "64 MiB 载荷必须一次持续 PATCH 送达"
    assert local_registry.request_count_since(mark, "POST") == 1, "上传会话只能开始一次"
    assert local_registry.request_count_since(mark, "PUT") == 1, "原始臂只提交 blob，不得发布 manifest"

    blob_path = f"/v2/{result['full_repository']}/blobs/{digest}"
    blob_status, blob_headers, blob = https_request(local_registry.context, local_registry.port, "GET", blob_path, timeout=300.0)
    assert blob_status == 200, f"真实 registry 未返回 blob: {blob_status}"
    assert blob.startswith(b"\x1f\x8b"), f"落地线体必须是 gzip: {blob[:2]!r}"
    assert len(blob) == int(payload["size_bytes"]), f"落地长度不符: {len(blob)}"
    assert blob_headers.get("docker-content-digest") == digest, blob_headers
    assert "sha256:" + hashlib.sha256(blob).hexdigest() == digest, "落地内容 hash 与声称 digest 不符"

    residuals = result["cleanup"]["residuals"]
    assert isinstance(residuals, list), result_json(result)
    assert any(entry.startswith("blob:") and digest in entry for entry in residuals), result_json(result)
    assert result["cleanup"]["storage_reclaimed"] is False, "不得宣称服务端存储已释放"
    assert "seed" in registry_tags(local_registry, str(result["full_repository"])), "预置仓库与 tag 不得被本次样本破坏"

    patch_stages = stages_with_body_bytes(result, int(payload["size_bytes"]))
    assert len(patch_stages) == 1, result_json(result)
    assert patch_stages[0]["http_status"] == 202, result_json(result)
    assert result["http"]["body_bytes"] >= PAYLOAD_BYTES, result_json(result)


# --------------------------------------------------------------------------------------
# 故障端点：成功路径的线上契约
# --------------------------------------------------------------------------------------


def test_fault_success_path_records_wire_contract(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
) -> None:
    """故障端点完整成功路径：单次持续 PATCH、Location 轮换、PUT 在原 query 上追加 digest。"""
    endpoint = fault_endpoints(upload_success_responder())
    result = run_sample(registry=endpoint.registry, namespace="perf-wire", tls_context=tls_client_context, run_id="wire-run")

    assert_status(result, "valid")
    digest = result["payload"]["digest"]

    patches = endpoint.requests("PATCH")
    assert len(patches) == 1, "载荷必须一次持续 PATCH 送达，不能拆成多次请求"
    assert patches[0].body.startswith(b"\x1f\x8b"), f"线体必须是 gzip 包装: {patches[0].body[:2]!r}"
    assert patches[0].body_bytes >= PAYLOAD_BYTES, f"gzip 线体不应短于内层: {patches[0].body_bytes}"
    assert patches[0].headers.get("content-range") == f"bytes 0-{patches[0].body_bytes - 1}", (
        f"PATCH 必须使用 bytes START-END: {patches[0].headers.get('content-range')}"
    )
    assert SIGNED_QUERY_SENTINEL in patches[0].target, "PATCH 必须落在服务端下发的 opaque query 上"

    upload_puts = [request for request in endpoint.requests("PUT") if "/blobs/uploads/" in request.target]
    assert len(upload_puts) == 1, "载荷提交必须恰好一次"
    put_query = upload_puts[0].query()
    assert put_query.get("_state") == [ROTATED_QUERY_SENTINEL], f"PUT 未使用轮换后的 Location: {upload_puts[0].target}"
    assert put_query.get("digest") == [digest], f"PUT 未在原 query 上追加 digest: {upload_puts[0].target}"

    # 原始 HTTP 臂止于 blob 提交与 HEAD 验证：不得发布任何 manifest。
    assert [request for request in endpoint.requests("PUT") if "/manifests/" in request.target] == [], "原始臂不得推送 manifest"

    assert len(stages_with_body_bytes(result, int(result["payload"]["size_bytes"]))) == 1, result_json(result)
    assert result["http"]["body_bytes"] >= PAYLOAD_BYTES, result_json(result)
    assert SIGNED_QUERY_SENTINEL not in result_json(result), "签名 query 不得进入结果"
    assert ROTATED_QUERY_SENTINEL not in result_json(result), "签名 query 不得进入结果"


# --------------------------------------------------------------------------------------
# 故障端点：鉴权
# --------------------------------------------------------------------------------------


def test_fault_basic_challenge_uses_credentials_without_upload(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
) -> None:
    """Basic challenge：仓库请求先被 401 挑战，随后必须携带正确 Basic 凭据，且不上传载荷。"""
    endpoint = fault_endpoints(basic_challenge_responder())
    result = run_sample(registry=endpoint.registry, namespace="perf-basic", tls_context=tls_client_context, run_id="basic-run")

    assert_status(result, "failed")
    challenged = [request for request in endpoint.requests() if request.status == 401]
    assert challenged, "端点没有观察到 401 挑战"
    assert any(request.headers.get("authorization") == basic_authorization() for request in endpoint.requests()), "挑战后未携带 Basic 凭据"
    assert endpoint.requests("PATCH") == [], "鉴权失败路径不得开始载荷传输"
    assert PASSWORD not in result_json(result), "结果中不得出现密码"


def test_fault_bearer_challenge_requests_quoted_scope_token(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
) -> None:
    """Bearer challenge：quoted scope 原样用于 token 请求，仓库请求改用 Bearer token。"""
    endpoint = fault_endpoints(bearer_challenge_responder())
    result = run_sample(registry=endpoint.registry, namespace="perf-bearer", tls_context=tls_client_context, run_id="bearer-run")

    assert_status(result, "failed")
    token_requests = [request for request in endpoint.requests() if request.target.partition("?")[0] == "/token"]
    assert len(token_requests) >= 1, "未请求 token 端点"
    token_query = token_requests[0].query()
    assert token_query.get("service") == ["registry"], token_requests[0].target
    scope = token_query.get("scope", [""])[0]
    assert scope.startswith("repository:perf-bearer/"), scope
    # scope 的动作集合按集合语义判断，允许 pull,push 或 push,pull 任意顺序。
    actions = scope.rsplit(":", 1)[-1].split(",")
    assert "push" in actions, f"token scope 必须包含 push 权限: {scope}"
    assert token_requests[0].headers.get("authorization") == basic_authorization(), "token 请求必须携带 Basic 凭据"
    assert any(request.headers.get("authorization") == f"Bearer {TOKEN_SENTINEL}" for request in endpoint.requests()), "仓库请求未改用 Bearer token"
    assert endpoint.requests("PATCH") == [], "鉴权失败路径不得开始载荷传输"


def test_fault_missing_token_field_fails_without_upload(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
) -> None:
    """token 响应缺少 token 字段：判失败，且不发送空 Bearer、不开始上传。"""
    responder = bearer_challenge_responder(token_payload={"expires_in": 300, "issued_at": "2026-10-06T00:00:00Z"})
    endpoint = fault_endpoints(responder)
    result = run_sample(registry=endpoint.registry, namespace="perf-token-missing", tls_context=tls_client_context, run_id="token-missing")

    assert_status(result, "failed")
    assert endpoint.requests("PATCH") == [], "缺失 token 时不得开始载荷传输"
    assert not any((request.headers.get("authorization") or "").strip() in {"Bearer", "Bearer "} for request in endpoint.requests()), "空 Bearer 不得发出"


def test_fault_token_endpoint_rejection_fails_without_upload(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
) -> None:
    """token 端点拒绝：判失败、不上传，结果与输出不得包含密码。"""
    responder = bearer_challenge_responder(token_status=401, token_body=b'{"errors":[{"code":"UNAUTHORIZED"}]}')
    endpoint = fault_endpoints(responder)
    result = run_sample(registry=endpoint.registry, namespace="perf-token-denied", tls_context=tls_client_context, run_id="token-denied")

    assert_status(result, "failed")
    assert endpoint.requests("PATCH") == [], "token 被拒时不得开始载荷传输"
    assert PASSWORD not in result_json(result), "结果中不得出现密码"


# --------------------------------------------------------------------------------------
# 故障端点：跨 origin 鉴权目标（auth_origin 规则）
# --------------------------------------------------------------------------------------


def test_fault_cross_origin_realm_allowed_with_explicit_auth_origin(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
) -> None:
    """显式 auth_origin：token 凭据只发往该 origin，仓库请求改用 Bearer token。"""
    auth = fault_endpoints(auth_endpoint_responder())
    realm = f"https://localhost:{auth.port}/token"
    registry_endpoint = fault_endpoints(bearer_challenge_responder(realm=realm))
    result = run_sample(
        registry=registry_endpoint.registry,
        namespace="perf-auth-origin",
        tls_context=tls_client_context,
        run_id="auth-origin-run",
        auth_origin=f"https://localhost:{auth.port}",
    )

    assert_status(result, "failed")
    token_requests = [request for request in auth.requests() if request.target.partition("?")[0] == "/token"]
    assert token_requests, "未向显式 auth_origin 请求 token"
    assert token_requests[0].headers.get("authorization") == basic_authorization(), "token 请求必须带 Basic 凭据"
    assert any(request.headers.get("authorization") == f"Bearer {TOKEN_SENTINEL}" for request in registry_endpoint.requests()), "仓库请求未改用 Bearer token"
    assert registry_endpoint.requests("PATCH") == [], "鉴权失败路径不得开始载荷传输"


@pytest.mark.parametrize(
    "variant",
    ["missing-auth-origin", "port-drift", "http-realm", "token-redirect"],
    ids=str,
)
def test_fault_untrusted_auth_target_never_receives_credentials(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
    variant: str,
) -> None:
    """未声明或不匹配的跨 origin realm：不下发凭据、判失败、不跟随 token 重定向。"""
    realm: str
    auth_origin: str | None
    if variant == "http-realm":
        untrusted = fault_endpoints(auth_endpoint_responder(), secure=False)
        realm = f"http://localhost:{untrusted.port}/token"
        auth_origin = f"https://localhost:{untrusted.port}"
    elif variant == "token-redirect":
        attacker = fault_endpoints(auth_endpoint_responder())
        untrusted = attacker
        auth = fault_endpoints(auth_endpoint_responder(status=302, location=f"https://localhost:{attacker.port}/token-loot"))
        realm = f"https://localhost:{auth.port}/token"
        auth_origin = f"https://localhost:{auth.port}"
    elif variant == "port-drift":
        untrusted = fault_endpoints(auth_endpoint_responder())
        drift = fault_endpoints(auth_endpoint_responder())
        realm = f"https://localhost:{untrusted.port}/token"
        auth_origin = f"https://localhost:{drift.port}"
    else:
        untrusted = fault_endpoints(auth_endpoint_responder())
        realm = f"https://localhost:{untrusted.port}/token"
        auth_origin = None

    registry_endpoint = fault_endpoints(bearer_challenge_responder(realm=realm))
    result = run_sample(
        registry=registry_endpoint.registry,
        namespace="perf-auth-untrusted",
        tls_context=tls_client_context,
        run_id=f"auth-{variant}",
        auth_origin=auth_origin,
    )

    assert_status(result, "failed")
    leaked = [
        request
        for request in untrusted.requests()
        if (request.headers.get("authorization") or "").startswith("Basic ")
    ]
    assert leaked == [], f"密码不得发往未声明或不匹配的鉴权目标: {[request.target for request in leaked]}"
    if variant in {"http-realm", "token-redirect"}:
        assert untrusted.requests() == [], "不得跟随明文 realm 或 token 重定向"
    assert registry_endpoint.requests("PATCH") == [], "鉴权失败路径不得开始载荷传输"
    assert PASSWORD not in result_json(result)


# --------------------------------------------------------------------------------------
# 故障端点：新鲜度 HEAD
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("head_status", "expected_status"),
    [(200, "invalid"), (401, "failed"), (403, "failed"), (500, "failed")],
    ids=["exists-200", "unauthorized-401", "forbidden-403", "server-error-500"],
)
def test_fault_fresh_head_status_blocks_upload(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
    head_status: int,
    expected_status: str,
) -> None:
    """新鲜度 HEAD 非 404：200 判样本无效，401/403/500 判失败，且都不得开始上传。"""
    endpoint = fault_endpoints(fresh_head_responder(head_status))
    result = run_sample(registry=endpoint.registry, namespace="perf-fresh", tls_context=tls_client_context, run_id=f"fresh-{head_status}")

    assert_status(result, expected_status)
    assert_no_upload(endpoint)


# --------------------------------------------------------------------------------------
# 故障端点：上传 Location 与 redirect
# --------------------------------------------------------------------------------------


def test_fault_relative_same_origin_location_is_followed_verbatim(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
) -> None:
    """同源相对 Location：按同源解析并保留 opaque query，失败后只取消本次上传。"""
    endpoint = fault_endpoints(relative_location_responder())
    result = run_sample(registry=endpoint.registry, namespace="perf-relative", tls_context=tls_client_context, run_id="relative-run")

    assert_status(result, "failed")
    patches = endpoint.requests("PATCH")
    assert len(patches) == 1, "失败路径不得重放载荷传输"
    expected = f"/v2/perf-relative/{result['repository']}/blobs/uploads/22222222-3333-4444-5555-666666666666?_state={SIGNED_QUERY_SENTINEL}"
    assert patches[0].target == expected, patches[0].target
    cancels = endpoint.requests("DELETE")
    assert cancels, "失败后必须取消本次未完成上传"
    assert all(request.target.startswith("/v2/perf-relative/") for request in cancels), "取消只能作用于本次上传"
    assert SIGNED_QUERY_SENTINEL not in result_json(result), "opaque query 不得进入结果"


@pytest.mark.parametrize("variant", ["cross-origin", "scheme-downgrade", "redirect-302"], ids=str)
def test_fault_disallowed_upload_location_is_not_followed(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
    variant: str,
) -> None:
    """外域、HTTP 降级与 302 Location 都不得跟随，也不得把请求或 Authorization 送出去。"""
    external = fault_endpoints(fresh_head_responder(500))
    plaintext = fault_endpoints(fresh_head_responder(500), secure=False)
    namespace = "perf-location"

    def location(request: RecordedRequest) -> str:
        path = request.target.partition("?")[0]
        if variant == "cross-origin":
            return f"https://localhost:{external.port}{path}external-upload?_state={SIGNED_QUERY_SENTINEL}"
        if variant == "scheme-downgrade":
            return f"http://localhost:{plaintext.port}{path}plaintext-upload?_state={DOWNGRADE_QUERY_SENTINEL}"
        return f"/v2/{namespace}/external-image-sync-perf/blobs/uploads/redirected-upload?_state={REDIRECT_QUERY_SENTINEL}"

    status = 302 if variant == "redirect-302" else 202
    endpoint = fault_endpoints(cross_origin_responder(location, status=status))
    result = run_sample(registry=endpoint.registry, namespace=namespace, tls_context=tls_client_context, run_id=f"location-{variant}")

    assert_status(result, "failed")
    assert external.requests() == [], "不得跟随到其它 origin"
    assert plaintext.requests() == [], "不得向明文 Location 发出任何请求"
    assert not [request for request in endpoint.requests() if "external-upload" in request.target or "plaintext-upload" in request.target or "redirected-upload" in request.target], "不得跟随不允许的 Location"
    assert endpoint.requests("PATCH") == [], "不得向不允许的 Location 发送载荷"
    assert SIGNED_QUERY_SENTINEL not in result_json(result)
    assert DOWNGRADE_QUERY_SENTINEL not in result_json(result)
    assert REDIRECT_QUERY_SENTINEL not in result_json(result)


# --------------------------------------------------------------------------------------
# 故障端点：仓库存在性、token 响应定界
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("post_status", [404, 403], ids=["post-404", "post-403"])
def test_fault_rejected_upload_start_after_empty_precheck_sends_no_payload(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
    post_status: int,
) -> None:
    """空仓预检后 POST 被拒（404/403）：明确失败，且在拒绝前不得发送任何载荷字节。"""
    endpoint = fault_endpoints(repository_unknown_responder(post_status=post_status))
    result = run_sample(
        registry=endpoint.registry,
        namespace="perf-empty-rejected",
        tls_context=tls_client_context,
        run_id=f"empty-post-{post_status}",
    )

    assert_status(result, "failed")
    assert endpoint.requests("PATCH") == [], "上传开始被拒时不得发送载荷"
    assert endpoint.requests("PUT") == [], "上传开始被拒时不得提交任何对象"
    assert endpoint.received_body_bytes() == 0, "上传开始被拒前不得发送任何请求体字节"


@pytest.mark.parametrize(
    ("tags_status", "tags_body"),
    [
        pytest.param(404, b'{"errors":[{"code":"UNAUTHORIZED"}]}', id="404-foreign-body"),
        pytest.param(500, b"{}", id="500"),
        pytest.param(401, b"{}", id="401"),
        pytest.param(403, b"{}", id="403"),
    ],
)
def test_fault_unexpected_precheck_status_is_not_treated_as_empty_repository(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
    tags_status: int,
    tags_body: bytes,
) -> None:
    """预检返回非空仓语义的状态：不得当作空仓放行开始上传。"""
    endpoint = fault_endpoints(precheck_failure_responder(tags_status, tags_body))
    result = run_sample(
        registry=endpoint.registry,
        namespace="perf-precheck-semantics",
        tls_context=tls_client_context,
        run_id=f"precheck-{tags_status}",
    )

    assert_status(result, "failed")
    assert endpoint.requests("POST") == [], "非空仓语义的预检不得继续开始上传"
    assert endpoint.requests("PATCH") == [], "非空仓语义的预检不得发送载荷"
    assert endpoint.requests("PUT") == [], "非空仓语义的预检不得提交对象"
    assert endpoint.received_body_bytes() == 0, "非空仓语义的预检不得发送任何请求体字节"


@pytest.mark.parametrize("framing", ["chunked", "close"], ids=str)
def test_fault_token_response_framing_is_accepted(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
    framing: str,
) -> None:
    """chunked 与连接关闭定界都是合法 HTTP/1.1：必须取到 token 并在仓库请求使用它。"""
    auth = fault_endpoints(auth_endpoint_responder(framing=framing))
    registry_endpoint = fault_endpoints(bearer_challenge_responder(realm=f"https://localhost:{auth.port}/token"))
    result = run_sample(
        registry=registry_endpoint.registry,
        namespace="perf-framing",
        tls_context=tls_client_context,
        run_id=f"token-{framing}",
        auth_origin=f"https://localhost:{auth.port}",
    )

    assert_status(result, "failed")
    assert any(request.headers.get("authorization") == f"Bearer {TOKEN_SENTINEL}" for request in registry_endpoint.requests()), "未使用取到的 token"
    assert registry_endpoint.requests("PATCH") == [], "鉴权失败路径不得开始载荷传输"


def test_fault_token_response_truncated_body_fails_without_upload(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
) -> None:
    """声明长度但提前 EOF 的 token 响应不完整：判失败，不得当作有效 token 使用。"""
    auth = fault_endpoints(auth_endpoint_responder(truncate_body_to=8))
    registry_endpoint = fault_endpoints(bearer_challenge_responder(realm=f"https://localhost:{auth.port}/token"))
    result = run_sample(
        registry=registry_endpoint.registry,
        namespace="perf-truncated-token",
        tls_context=tls_client_context,
        run_id="truncated-token",
        auth_origin=f"https://localhost:{auth.port}",
    )

    assert_status(result, "failed")
    assert not any(
        (request.headers.get("authorization") or "").startswith("Bearer ") for request in registry_endpoint.requests()
    ), "不完整的 token 响应不得产生 Bearer 授权请求"
    assert registry_endpoint.requests("PATCH") == [], "鉴权失败路径不得开始载荷传输"


# --------------------------------------------------------------------------------------
# 故障端点：发送后等待、轮换 query、Range 证据、提交结果未知
# --------------------------------------------------------------------------------------


def test_fault_silent_server_after_full_body_records_patch_stage(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
) -> None:
    """服务端读完整个 64 MiB 后静默：仍必须记录 upload_patch 阶段（完整字节、截止失败）。"""
    endpoint = fault_endpoints(hold_patch_responder(hold_seconds=8.0, after_read=True))
    budget = 3.0
    with watchdog(budget + 25.0, "静默服务端用例未在有界时间内结束"):
        result = run_sample(
            registry=endpoint.registry,
            namespace="perf-silent",
            tls_context=tls_client_context,
            run_id="silent-after-body",
            upload_timeout=budget,
        )

    assert_status(result, "failed")

    patches = endpoint.requests("PATCH")
    assert len(patches) == 1, "超时后不得重放载荷传输"
    assert patches[0].body_bytes >= PAYLOAD_BYTES, f"服务端应已读完整个载荷: {patches[0].body_bytes}"

    patch_stages = [stage for stage in result["stages"] if stage.get("name") == "upload_patch"]
    assert len(patch_stages) == 1, result_json(result)
    assert patch_stages[0]["body_bytes"] == patches[0].body_bytes, result_json(result)
    assert patch_stages[0]["status"] == "deadline_exceeded", result_json(result)
    # 只看阶段自身是否守住预算；清理有独立预算，不计入本断言。
    assert patch_stages[0]["seconds"] <= budget + 0.5, result_json(result)
    assert [request for request in endpoint.requests("PUT") if "/blobs/uploads/" in request.target] == [], "截止失败不得提交"


def test_fault_rotated_location_with_bad_range_cancels_latest_query(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
) -> None:
    """202 带轮换 query 但 Range 不符：不得提交，且取消上传必须使用轮换后的最新 query。"""
    endpoint = fault_endpoints(rotated_bad_range_responder())
    result = run_sample(registry=endpoint.registry, namespace="perf-rotated", tls_context=tls_client_context, run_id="rotated-bad-range")

    assert_status(result, "failed")
    assert [request for request in endpoint.requests("PUT") if "/blobs/uploads/" in request.target] == [], "Range 不符不得提交"

    cancel_requests = endpoint.requests("DELETE")
    assert cancel_requests, "必须取消本次未完成上传"
    assert any(request.query().get("_state") == [ROTATED_QUERY_SENTINEL] for request in cancel_requests), "取消必须使用轮换后的最新 query"
    assert ROTATED_QUERY_SENTINEL not in result_json(result), "签名 query 不得进入结果"
    assert SIGNED_QUERY_SENTINEL not in result_json(result), "签名 query 不得进入结果"


@pytest.mark.parametrize(
    ("headers", "read_limit"),
    [
        pytest.param({"Range": "0-65535"}, 65536, id="premature-prefix-range"),
        pytest.param({}, None, id="missing-range"),
        pytest.param({"Range": "0-abc"}, None, id="malformed-range"),
        pytest.param({"Range": "1-67108863"}, None, id="nonzero-start-range"),
    ],
)
def test_fault_incomplete_patch_range_evidence_does_not_commit(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
    headers: dict[str, str],
    read_limit: int | None,
) -> None:
    """早响应、缺失/畸形 Range 或非 0 起点都不构成完整发送证据：不得提交、不得重放。"""
    endpoint = fault_endpoints(patch_fault_responder(202, patch_headers=headers, read_limit=read_limit, with_rotation=True))
    result = run_sample(registry=endpoint.registry, namespace="perf-range-evidence", tls_context=tls_client_context, run_id="range-evidence")

    assert_status(result, "failed")
    assert len(endpoint.requests("PATCH")) == 1, "失败的载荷传输不得重放"
    assert [request for request in endpoint.requests("PUT") if "/blobs/uploads/" in request.target] == [], "发送证据不完整时不得提交"


def test_fault_commit_response_lost_reports_unknown_blob_residual(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
) -> None:
    """提交响应丢失（服务端可能已存）：必须报告本次 blob 的未知残留，不写成 uncommitted。"""
    endpoint = fault_endpoints(commit_response_lost_responder())
    result = run_sample(registry=endpoint.registry, namespace="perf-lost", tls_context=tls_client_context, run_id="commit-response-lost")

    assert_status(result, "failed")
    digest = result["payload"]["digest"]
    residuals = result["cleanup"]["residuals"]
    assert isinstance(residuals, list), result_json(result)
    assert any(digest in entry for entry in residuals), "提交结果未知必须报告本次 blob 的精确残留"
    assert not any("uncommitted" in entry for entry in residuals), "不得把提交结果未知写成未提交"
    assert result["cleanup"]["storage_reclaimed"] is False, "不得宣称服务端存储已释放"


# --------------------------------------------------------------------------------------
# 故障端点：realm query 不得绕过本仓收窄
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("variant", ["foreign-scope", "duplicate-scope", "foreign-service"], ids=str)
def test_fault_realm_query_parameters_are_not_merged(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
    variant: str,
) -> None:
    """realm 自带 query 的 scope/service 不得绕过本仓收窄：拒绝或只发送本仓权限。"""
    auth = fault_endpoints(auth_endpoint_responder())
    base = f"https://localhost:{auth.port}/token"
    if variant == "foreign-scope":
        realm = f"{base}?scope=repository:other/repo:pull"
    elif variant == "duplicate-scope":
        realm = f"{base}?scope=repository:other/repo:pull&scope=repository:perf-realm/image-sync-perf:push"
    else:
        realm = f"{base}?service=evil"

    registry_endpoint = fault_endpoints(bearer_challenge_responder(realm=realm))
    result = run_sample(
        registry=registry_endpoint.registry,
        namespace="perf-realm",
        tls_context=tls_client_context,
        run_id=f"realm-{variant}",
        auth_origin=f"https://localhost:{auth.port}",
    )

    assert_status(result, "failed")
    for request in [*auth.requests(), *registry_endpoint.requests()]:
        assert "other/repo" not in request.target, f"异仓 scope 不得发出: {request.target}"
        assert all(scope.startswith("repository:perf-realm/") for scope in request.query().get("scope", [])), request.target
        assert all(service == "registry" for service in request.query().get("service", [])), request.target
    assert registry_endpoint.requests("PATCH") == [], "鉴权失败路径不得开始载荷传输"


# --------------------------------------------------------------------------------------
# 故障端点：字节完整性与提交校验
# --------------------------------------------------------------------------------------


def test_fault_patch_short_write_fails_once_without_replay(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
) -> None:
    """PATCH 被服务端提前拒收：判失败、载荷未完整送达，且不得重放这次上传。"""
    endpoint = fault_endpoints(patch_fault_responder(400))
    result = run_sample(registry=endpoint.registry, namespace="perf-short-write", tls_context=tls_client_context, run_id="short-write")

    assert_status(result, "failed")
    patches = endpoint.requests("PATCH")
    assert len(patches) == 1, "失败的载荷传输不得重放"
    assert patches[0].body_bytes < PAYLOAD_BYTES, f"服务端提前拒收，不应收到完整载荷: {patches[0].body_bytes}"
    assert [request for request in endpoint.requests("PUT") if "/blobs/uploads/" in request.target] == [], "载荷不完整时不得提交"


def test_fault_patch_range_mismatch_fails_before_commit(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
) -> None:
    """PATCH 返回的 Range 与已发送字节不符：必须在提交前失败。"""
    endpoint = fault_endpoints(patch_fault_responder(202, patch_headers={"Range": "0-1023"}, read_limit=None))
    result = run_sample(registry=endpoint.registry, namespace="perf-range", tls_context=tls_client_context, run_id="range-mismatch")

    assert_status(result, "failed")
    assert [request for request in endpoint.requests("PUT") if "/blobs/uploads/" in request.target] == [], "Range 不符时不得提交"


def test_fault_commit_digest_mismatch_fails(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
) -> None:
    """blob 提交返回的 digest 与本地载荷不符：判失败且不得继续推送 manifest。"""
    wrong = "sha256:" + "0" * 64
    endpoint = fault_endpoints(commit_fault_responder(put_status=201, put_digest=wrong))
    result = run_sample(registry=endpoint.registry, namespace="perf-digest", tls_context=tls_client_context, run_id="digest-mismatch")

    assert_status(result, "failed")
    assert result["payload"]["digest"] != wrong, result_json(result)


def test_fault_verify_length_mismatch_fails(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
) -> None:
    """提交后 HEAD 验证返回错误长度：判失败，不得判为有效样本。"""
    endpoint = fault_endpoints(verify_length_responder())
    result = run_sample(registry=endpoint.registry, namespace="perf-verify", tls_context=tls_client_context, run_id="verify-length")

    assert_status(result, "failed")


def test_fault_commit_put_failure_reports_failed_stage(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
) -> None:
    """PUT 提交返回 500：判失败，阶段结果保留真实 HTTP 状态。"""
    endpoint = fault_endpoints(commit_fault_responder(put_status=500))
    result = run_sample(registry=endpoint.registry, namespace="perf-put", tls_context=tls_client_context, run_id="put-failure")

    assert_status(result, "failed")
    failed_stages = [stage for stage in result["stages"] if stage.get("http_status") == 500]
    assert failed_stages, result_json(result)


def test_fault_upload_deadline_expiry_does_not_replay_upload(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
) -> None:
    """短 upload_timeout：端点不响应 PATCH，样本按失败结束且不重放载荷传输。"""
    endpoint = fault_endpoints(hold_patch_responder(hold_seconds=8.0))
    started = time.monotonic()
    result = run_sample(
        registry=endpoint.registry,
        namespace="perf-deadline",
        tls_context=tls_client_context,
        run_id="deadline",
        upload_timeout=2.0,
    )
    elapsed = time.monotonic() - started

    assert_status(result, "failed")
    assert elapsed < 30.0, f"短 deadline 未及时结束: {elapsed:.1f}s"
    assert len(endpoint.requests("PATCH")) == 1, "超时后不得重放载荷传输"


def test_fault_rejected_cleanup_reports_residuals_and_keeps_primary_error(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
) -> None:
    """取消上传与删除 manifest 都被拒：残留被单独报告，本地主错误不被清理覆盖。"""
    endpoint = fault_endpoints(cancel_rejected_responder())
    result = run_sample(registry=endpoint.registry, namespace="perf-cleanup", tls_context=tls_client_context, run_id="cleanup-rejected")

    assert_status(result, "failed")
    assert result["reason"], result_json(result)
    # 服务端在上传中途拒绝时，客户端可能先收到 400，也可能先遇到发送侧错误；
    # 无论哪种，主错误都必须留在阶段结果里（失败的/中止的阶段），不被清理失败覆盖。
    assert endpoint.requests("PATCH"), "取消失败用例必须先有一次真实的 PATCH 尝试"
    failed_stages = [
        stage
        for stage in result["stages"]
        if stage.get("status") in {"failed", "aborted", "deadline_exceeded"}
    ]
    assert failed_stages, result_json(result)

    residuals = result["cleanup"]["residuals"]
    assert isinstance(residuals, list) and residuals, f"清理受拒必须报告残留: {result_json(result)}"
    assert all(isinstance(item, str) and item for item in residuals), residuals
    cleanup_text = json.dumps(residuals, ensure_ascii=False)
    assert PASSWORD not in cleanup_text and SIGNED_QUERY_SENTINEL not in cleanup_text, "残留清单不得包含凭据或签名 query"


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def test_cli_reports_failure_without_leaking_password_token_or_signed_query(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_material: TlsMaterial,
    tmp_path: Path,
) -> None:
    """CLI 失败路径：退出非零、写出 JSON/summary、按 --auth-origin 取 token，且不泄漏密码/token/签名 query。"""
    def upload_location(request: RecordedRequest) -> str:
        host = request.headers.get("host", "")
        path = request.target.partition("?")[0]
        return f"https://{host}{path.rstrip('/')}/99999999-aaaa-bbbb-cccc-dddddddddddd?_state={SIGNED_QUERY_SENTINEL}"

    auth = fault_endpoints(auth_endpoint_responder())
    realm = f"https://localhost:{auth.port}/token"
    endpoint = fault_endpoints(
        bearer_challenge_responder(realm=realm, upload_location=upload_location, post_status=202, patch_status=400)
    )
    output_json = tmp_path / "perf-summary.json"
    output_markdown = tmp_path / "perf-summary.md"
    environment = dict(os.environ)
    environment.update(
        {
            "SSL_CERT_FILE": str(tls_material.ca_pem),
            "ALIYUN_REGISTRY": endpoint.registry,
            "ALIYUN_NAME_SPACE": "perf-cli",
            "ALIYUN_REGISTRY_USER": USERNAME,
            "ALIYUN_REGISTRY_PASSWORD": PASSWORD,
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(PUSH_PERF_PATH),
            "--http-only",
            "--environment",
            "control",
            "--run-id",
            "cli-failure-run",
            "--auth-origin",
            f"https://localhost:{auth.port}",
            "--output",
            str(output_json),
            "--summary",
            str(output_markdown),
        ],
        capture_output=True,
        text=True,
        timeout=300.0,
        env=environment,
        check=False,
    )

    assert completed.returncode != 0, f"失败样本必须非零退出\n{completed.stdout}\n{completed.stderr}"
    assert output_json.is_file(), "失败样本同样必须写出结构化结果"
    assert output_markdown.is_file(), "必须写出 summary"
    document = json.loads(output_json.read_text(encoding="utf-8"))
    assert document["status"] == "failed", json.dumps(document, ensure_ascii=False)

    # 先证明这些凭证确实上过线，脱敏断言才有意义。
    assert any(request.headers.get("authorization") == basic_authorization() for request in auth.requests()), "CLI 未按 --auth-origin 用进程环境凭据请求 token"
    assert any(request.headers.get("authorization") == f"Bearer {TOKEN_SENTINEL}" for request in endpoint.requests()), "CLI 未使用 Bearer token"
    assert any(SIGNED_QUERY_SENTINEL in request.target for request in endpoint.requests()), "CLI 未使用签名 upload Location"

    outputs = {
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "json": output_json.read_text(encoding="utf-8"),
        "summary": output_markdown.read_text(encoding="utf-8"),
    }
    secrets = {
        "密码": PASSWORD,
        "Basic 凭据": basic_authorization(),
        "token": TOKEN_SENTINEL,
        "Bearer 头": f"Bearer {TOKEN_SENTINEL}",
        "签名 query": SIGNED_QUERY_SENTINEL,
    }
    for label, text in outputs.items():
        for secret_label, secret in secrets.items():
            assert secret not in text, f"{label} 泄漏了{secret_label}"


# --------------------------------------------------------------------------------------
# Docker 交叉臂：只覆盖无效/失败消费分支（替身是外部边界，不是真实 daemon）
# --------------------------------------------------------------------------------------


def docker_arm_sample(
    endpoint: FaultEndpoint,
    tls_client_context: ssl.SSLContext,
    *,
    namespace: str,
    run_id: str,
    upload_timeout: float = 120.0,
) -> dict[str, Any]:
    """跑一次包含 Docker 臂的样本；原始 HTTP 臂用故障端点的成功脚本走通。"""
    return run_sample(
        registry=endpoint.registry,
        namespace=namespace,
        tls_context=tls_client_context,
        run_id=run_id,
        http_only=False,
        upload_timeout=upload_timeout,
    )


def test_docker_arm_exit_zero_without_transfer_evidence_is_not_valid(
    fault_endpoints: Callable[..., FaultEndpoint],
    docker_stub: DockerStub,
    tls_client_context: ssl.SSLContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """push 退出 0 且目标存在 layer descriptor，但没有任何传输证据：不得判 valid。"""
    monkeypatch.setenv("DOCKER_STUB_PUSH_STDOUT", "")
    endpoint = fault_endpoints(
        upload_success_responder(
            docker_layer=(DOCKER_LAYER_DIGEST, DOCKER_LAYER_SIZE),
            docker_config_digest=STUB_IMAGE_ID,
            slow_read_delay=0.0,
        )
    )
    result = docker_arm_sample(endpoint, tls_client_context, namespace="perf-docker-none", run_id="docker-no-evidence")

    assert result["status"] != "valid", result_json(result)
    assert result["docker"]["status"] != "pushed", result_json(result)


def test_docker_arm_deduplicated_layer_is_invalid(
    fault_endpoints: Callable[..., FaultEndpoint],
    docker_stub: DockerStub,
    tls_client_context: ssl.SSLContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """push 命中 layer 去重：没有本次真实上传，判 invalid。"""
    monkeypatch.setenv("DOCKER_STUB_PUSH_STDOUT", "The push refers to repository [x]\nLayer already exists\n")
    endpoint = fault_endpoints(
        upload_success_responder(
            docker_layer=(DOCKER_LAYER_DIGEST, DOCKER_LAYER_SIZE),
            docker_config_digest=STUB_IMAGE_ID,
            slow_read_delay=0.0,
        )
    )
    result = docker_arm_sample(endpoint, tls_client_context, namespace="perf-docker-dedup", run_id="docker-dedup")

    assert_status(result, "invalid")
    assert result["docker"]["deduplicated"] is True, result_json(result)


def test_docker_arm_push_manifest_digest_must_match_target(
    fault_endpoints: Callable[..., FaultEndpoint],
    docker_stub: DockerStub,
    tls_client_context: ssl.SSLContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """push 摘要行描述的目标 manifest 与本次目标不一致：不得判 valid。"""
    foreign = docker_manifest_document(DOCKER_LAYER_DIGEST, DOCKER_LAYER_SIZE, config_digest="sha256:" + "4" * 64)
    monkeypatch.setenv("DOCKER_STUB_PUSH_STDOUT", docker_push_output(foreign))
    endpoint = fault_endpoints(
        upload_success_responder(
            docker_layer=(DOCKER_LAYER_DIGEST, DOCKER_LAYER_SIZE),
            docker_config_digest=STUB_IMAGE_ID,
            slow_read_delay=0.0,
        )
    )
    result = docker_arm_sample(endpoint, tls_client_context, namespace="perf-docker-mismatch", run_id="docker-descriptor-mismatch")

    assert result["status"] != "valid", result_json(result)
    assert result["docker"]["status"] != "pushed", result_json(result)


def test_docker_arm_logout_failure_is_reported_consistently(
    fault_endpoints: Callable[..., FaultEndpoint],
    docker_stub: DockerStub,
    tls_client_context: ssl.SSLContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """logout 失败必须如实报告，且 cleanup.logout 与阶段、残留状态一致。"""
    monkeypatch.setenv("DOCKER_STUB_PUSH_STDOUT", consistent_push_output())
    monkeypatch.setenv("DOCKER_STUB_LOGOUT_EXIT", "1")
    endpoint = fault_endpoints(
        upload_success_responder(
            docker_layer=(DOCKER_LAYER_DIGEST, DOCKER_LAYER_SIZE),
            docker_config_digest=STUB_IMAGE_ID,
            slow_read_delay=0.0,
        )
    )
    result = docker_arm_sample(endpoint, tls_client_context, namespace="perf-docker-logout", run_id="docker-logout-failure")

    assert result["cleanup"]["logout"] == "failed", result_json(result)
    logout_stages = [stage for stage in result["stages"] if stage.get("name") == "docker_logout"]
    assert logout_stages and logout_stages[-1]["status"] == "failed", result_json(result)
    assert result["cleanup"]["status"] == "partial", result_json(result)
    assert_status(result, "failed")


def test_docker_arm_uses_independent_tag_and_tracks_docker_objects(
    fault_endpoints: Callable[..., FaultEndpoint],
    docker_stub: DockerStub,
    tls_client_context: ssl.SSLContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docker 臂必须使用独立 tag，并按精确 digest 清理、如实报告 Docker 层残留。"""
    monkeypatch.setenv("DOCKER_STUB_PUSH_STDOUT", consistent_push_output())
    endpoint = fault_endpoints(
        upload_success_responder(
            docker_layer=(DOCKER_LAYER_DIGEST, DOCKER_LAYER_SIZE),
            docker_config_digest=STUB_IMAGE_ID,
            slow_read_delay=0.0,
        )
    )
    result = docker_arm_sample(endpoint, tls_client_context, namespace="perf-docker-tag", run_id="docker-tag")

    push_calls = docker_stub.calls_with("push")
    assert len(push_calls) == 1, push_calls
    reference = push_calls[0][-1]
    docker_tag = reference.rsplit(":", 1)[-1]
    assert reference.startswith(f"{endpoint.registry}/{result['full_repository']}:"), reference
    assert docker_tag == result["docker_tag"], result_json(result)
    assert docker_tag != result["tag"], "Docker 臂必须使用独立 tag，不得覆盖原始臂 tag"

    manifest_digest = "sha256:" + hashlib.sha256(consistent_docker_manifest()).hexdigest()
    deleted = [request.target for request in endpoint.requests("DELETE")]
    assert any(manifest_digest in target for target in deleted), f"Docker manifest 必须按精确 digest 清理: {deleted}"

    residuals = result["cleanup"]["residuals"]
    assert any(entry.startswith("docker_layer:") for entry in residuals), result_json(result)
    assert result["cleanup"]["storage_reclaimed"] is False, "不得宣称服务端存储已释放"


def peak_growth_bytes(before: int, after: int) -> int:
    """把两次 ru_maxrss 读数换算成字节增量（macOS 为字节，Linux 为 KiB）。"""
    scale = 1 if sys.platform == "darwin" else 1024
    return max(0, after - before) * scale


def test_docker_arm_endless_output_is_bounded_and_deadline_enforced(
    fault_endpoints: Callable[..., FaultEndpoint],
    docker_stub: DockerStub,
    tls_client_context: ssl.SSLContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """push 持续输出且无换行：Docker 臂必须有独立预算、有界处理，并在超时后判失败。

    复用 Config.upload_timeout 作为每臂预算：HTTP 臂故意放慢到数秒，Docker 臂仍能用满
    接近完整的预算，说明两个臂各自独立计时；同时检查进程峰值内存不因缓冲输出而膨胀。
    """
    monkeypatch.setenv("DOCKER_STUB_PUSH_STREAM", "1")
    endpoint = fault_endpoints(
        upload_success_responder(
            docker_layer=(DOCKER_LAYER_DIGEST, DOCKER_LAYER_SIZE),
            docker_config_digest=STUB_IMAGE_ID,
            slow_read_delay=0.004,
        )
    )
    budget = 10.0
    peak_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    started = time.monotonic()
    result = docker_arm_sample(
        endpoint,
        tls_client_context,
        namespace="perf-docker-stream",
        run_id="docker-endless-output",
        upload_timeout=budget,
    )
    elapsed = time.monotonic() - started
    peak_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    assert_status(result, "failed")
    push_stages = [stage for stage in result["stages"] if stage.get("name") == "docker_push"]
    assert push_stages and push_stages[-1]["status"] == "deadline_exceeded", result_json(result)
    assert push_stages[-1]["seconds"] >= budget * 0.8, result_json(result)
    assert elapsed >= budget + 3.0, f"两个臂的预算未独立累加: {elapsed:.1f}s"
    assert peak_growth_bytes(peak_before, peak_after) < 200 * 1024 * 1024, "海量输出必须流式有界处理"


# --------------------------------------------------------------------------------------
# 真实 I/O 故障注入（公开 run_sample / main）
# --------------------------------------------------------------------------------------


def test_docker_tar_io_failure_reports_failed_stage(
    fault_endpoints: Callable[..., FaultEndpoint],
    docker_stub: DockerStub,
    tls_client_context: ssl.SSLContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tar 生成遇到真实 I/O 失败：必须记录 docker_prepare failed 阶段。

    gzip 线体略大于 64MiB，不能再用略高于内层的 RLIMIT_FSIZE 区分 tar；只让 rootfs tar 打开失败。
    """
    endpoint = fault_endpoints(upload_success_responder(slow_read_delay=0.0))
    original_open = tarfile.open

    def wrapped_open(*args: object, **kwargs: object) -> tarfile.TarFile:
        name = args[0] if args else kwargs.get("name")
        if isinstance(name, (str, Path)) and str(name).endswith("payload-rootfs.tar"):
            raise OSError(errno.EFBIG, "File too large")
        return original_open(*args, **kwargs)

    monkeypatch.setattr(tarfile, "open", wrapped_open)
    result = docker_arm_sample(endpoint, tls_client_context, namespace="perf-tar-io", run_id="tar-io-failure")

    assert_status(result, "failed")
    prepare_stages = [stage for stage in result["stages"] if stage.get("name") == "docker_prepare"]
    assert prepare_stages and prepare_stages[-1]["status"] == "failed", result_json(result)


def test_gzip_io_failure_reports_failed_stage_and_nonzero_exit(
    fault_endpoints: Callable[..., FaultEndpoint],
    docker_stub: DockerStub,
    tls_material: TlsMaterial,
    tls_client_context: ssl.SSLContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gzip 参考测量的真实 I/O 失败（载荷被外部删除）必须失败，且 CLI 非零退出。"""
    monkeypatch.setenv("DOCKER_STUB_PUSH_STDOUT", consistent_push_output())
    monkeypatch.setenv("DOCKER_STUB_DELETE_PAYLOAD", "1")
    endpoint = fault_endpoints(
        upload_success_responder(
            docker_layer=(DOCKER_LAYER_DIGEST, DOCKER_LAYER_SIZE),
            docker_config_digest=STUB_IMAGE_ID,
            slow_read_delay=0.0,
        )
    )

    result = docker_arm_sample(endpoint, tls_client_context, namespace="perf-gzip-io", run_id="gzip-io-failure")
    assert_status(result, "failed")
    gzip_stages = [stage for stage in result["stages"] if stage.get("name") == "gzip_reference"]
    assert gzip_stages and gzip_stages[-1]["status"] == "failed", result_json(result)

    output_path = tmp_path / "gzip-io.json"
    environment = dict(os.environ)
    environment.update(
        {
            "SSL_CERT_FILE": str(tls_material.ca_pem),
            "ALIYUN_REGISTRY": endpoint.registry,
            "ALIYUN_NAME_SPACE": "perf-gzip-io",
            "ALIYUN_REGISTRY_USER": USERNAME,
            "ALIYUN_REGISTRY_PASSWORD": PASSWORD,
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(PUSH_PERF_PATH),
            "--environment",
            "control",
            "--run-id",
            "gzip-io-failure",
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        timeout=300.0,
        env=environment,
        check=False,
    )
    assert completed.returncode != 0, f"gzip I/O 失败必须非零退出\n{completed.stdout}\n{completed.stderr}"
    assert output_path.is_file(), "失败样本同样必须写出结构化结果"
    assert json.loads(output_path.read_text(encoding="utf-8"))["status"] == "failed"


# --------------------------------------------------------------------------------------
# CLI 产物写出失败
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("variant", ["output-is-directory", "summary-under-file"], ids=str)
def test_cli_artifact_write_failure_exits_nonzero(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_material: TlsMaterial,
    tmp_path: Path,
    variant: str,
) -> None:
    """必需产物写出失败必须报告失败并非零退出，不得静默成功。"""
    endpoint = fault_endpoints(upload_success_responder())
    blocker = tmp_path / "blocker"
    summary_path = tmp_path / "perf-summary.md"
    if variant == "output-is-directory":
        blocker.mkdir()
        output_path = blocker
    else:
        blocker.write_text("x", encoding="utf-8")
        output_path = tmp_path / "perf-summary.json"
        summary_path = blocker / "summary.md"

    environment = dict(os.environ)
    environment.update(
        {
            "SSL_CERT_FILE": str(tls_material.ca_pem),
            "ALIYUN_REGISTRY": endpoint.registry,
            "ALIYUN_NAME_SPACE": "perf-artifact",
            "ALIYUN_REGISTRY_USER": USERNAME,
            "ALIYUN_REGISTRY_PASSWORD": PASSWORD,
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(PUSH_PERF_PATH),
            "--http-only",
            "--environment",
            "control",
            "--run-id",
            "artifact-write-failure",
            "--output",
            str(output_path),
            "--summary",
            str(summary_path),
        ],
        capture_output=True,
        text=True,
        timeout=300.0,
        env=environment,
        check=False,
    )

    assert completed.returncode != 0, f"产物写出失败必须非零退出\n{completed.stdout}\n{completed.stderr}"
    assert "无法写出" in completed.stderr, completed.stderr
    if variant == "summary-under-file":
        assert json.loads(output_path.read_text(encoding="utf-8"))["status"] == "valid", "样本本身必须成功，失败只能来自产物写出"


def trickle_raw_response(framing: str, *, pad: int = 64) -> tuple[bytes, int]:
    """构造滴流响应的原始字节与“立即发送”的前缀长度。

    framing 覆盖缓慢 header、缓慢 chunk-size、chunked + trailer 与裸 LF 行结束符四种形状。
    """
    filler = b"x" * pad
    if framing == "slow-header":
        head = b"HTTP/1.1 200 OK\r\nX-Slow: "
        return head + filler + b"\r\nContent-Length: 0\r\n\r\n", len(head)
    if framing == "slow-chunk-size":
        head = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1;slow="
        return head + filler + b"\r\nA\r\n0\r\n\r\n", len(head)
    if framing == "chunked-trailer":
        head = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nTrailer: X-Sum\r\n\r\n2\r\n{}\r\n0\r\nX-Sum: "
        return head + filler + b"\r\n\r\n", len(head)
    head = b"HTTP/1.1 200 OK\nContent-Length: 0\nX-Slow: "
    return head + filler + b"\n\n", len(head)


def trickle_responder(payload: bytes, *, gap: float, prefix: int, post_status: int = 403) -> Responder:
    """对 tags/list 返回原始字节响应（可按字节滴流），其余请求按失败脚本回应。"""

    def respond(request: RecordedRequest) -> FaultSpec:
        path, _, _ = request.target.partition("?")
        if path.rstrip("/") == "/v2":
            return FaultSpec(status=200, body=b"{}")
        if path.endswith("/tags/list"):
            return FaultSpec(status=200, trickle_response=payload, trickle_prefix_bytes=prefix, trickle_gap_seconds=gap)
        if request.method == "HEAD" and "/blobs/" in path:
            return FaultSpec(status=404)
        if request.method == "POST" and "/blobs/uploads/" in path:
            return FaultSpec(status=post_status, body=b"{}")
        return FaultSpec(status=500, body=b"unexpected request")

    return respond


@contextlib.contextmanager
def watchdog(seconds: float, message: str) -> Iterator[None]:
    """为可能阻塞的调用设置硬上限，超时抛出断言而不是让测试挂住（仅主线程可用）。"""

    def handler(signum: int, frame: object) -> None:
        raise AssertionError(message)

    previous = signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous)


def pid_alive(pid: int) -> bool:
    """判断进程是否仍存在；已被回收的子进程返回 False。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --------------------------------------------------------------------------------------
# 故障端点：缓慢滴流响应下的绝对截止时间（HTTP-10）
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("framing", ["slow-header", "slow-chunk-size", "chunked-trailer", "lf-only"], ids=str)
def test_fault_trickled_response_respects_request_deadline(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
    framing: str,
) -> None:
    """服务端按字节节奏发送 header/chunk-size/trailer：请求必须在绝对预算内结束。"""
    budget = 1.2
    payload, prefix = trickle_raw_response(framing)
    endpoint = fault_endpoints(trickle_responder(payload, gap=0.02, prefix=prefix))
    started = time.monotonic()
    result = run_sample(
        registry=endpoint.registry,
        namespace="perf-trickle",
        tls_context=tls_client_context,
        run_id=f"trickle-{framing}",
        upload_timeout=budget,
    )
    elapsed = time.monotonic() - started

    assert_status(result, "failed")
    precheck_stages = [stage for stage in result["stages"] if stage.get("name") == "repository_precheck"]
    assert precheck_stages, result_json(result)
    assert precheck_stages[-1]["status"] in {"failed", "deadline_exceeded"}, result_json(result)
    assert precheck_stages[-1]["seconds"] <= budget + 0.5, result_json(result)
    # 清理有独立预算：这里没有任何待清理对象，总耗时只允许比请求预算多出一个有界余量。
    assert elapsed <= budget + 5.0, f"请求未在预算内结束: {elapsed:.1f}s"


def test_fault_untrickled_response_completes_within_budget(
    fault_endpoints: Callable[..., FaultEndpoint],
    tls_client_context: ssl.SSLContext,
) -> None:
    """反向对照：同一端点在 gap=0 时必须快速完成预检并按后续语义失败，而不是一律超时。"""
    budget = 2.0
    payload = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n{}"
    endpoint = fault_endpoints(trickle_responder(payload, gap=0.0, prefix=0))
    result = run_sample(
        registry=endpoint.registry,
        namespace="perf-trickle-fast",
        tls_context=tls_client_context,
        run_id="trickle-fast",
        upload_timeout=budget,
    )

    precheck_stages = [stage for stage in result["stages"] if stage.get("name") == "repository_precheck"]
    assert precheck_stages and precheck_stages[-1]["status"] == "ok", result_json(result)
    assert precheck_stages[-1]["seconds"] <= budget, result_json(result)
    assert not [stage for stage in result["stages"] if stage.get("status") == "deadline_exceeded"], result_json(result)
    assert result["status"] != "valid", result_json(result)


# --------------------------------------------------------------------------------------
# Docker 交叉臂：真实输出语义与有界读取边界
# --------------------------------------------------------------------------------------


def test_docker_arm_manifest_digest_without_pushed_is_invalid(
    fault_endpoints: Callable[..., FaultEndpoint],
    docker_stub: DockerStub,
    tls_client_context: ssl.SSLContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只有正确的 manifest 摘要、没有 Pushed 正证据：不得判 valid。"""
    monkeypatch.setenv("DOCKER_STUB_PUSH_STDOUT", consistent_push_output(pushed=False))
    endpoint = fault_endpoints(
        upload_success_responder(
            docker_layer=(DOCKER_LAYER_DIGEST, DOCKER_LAYER_SIZE),
            docker_config_digest=STUB_IMAGE_ID,
            slow_read_delay=0.0,
        )
    )
    result = docker_arm_sample(endpoint, tls_client_context, namespace="perf-docker-pushed", run_id="docker-no-pushed")

    assert_status(result, "invalid")
    assert result["docker"]["status"] != "pushed", result_json(result)


def test_docker_arm_dedup_marker_after_large_prefix_is_detected(
    fault_endpoints: Callable[..., FaultEndpoint],
    docker_stub: DockerStub,
    tls_client_context: ssl.SSLContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """去重标记前面有大量输出时仍须判出去重：有界读取不能丢尚未解析的字节。"""
    pieces = ["y" * 20000 + "\nLayer already exists\n" + "z" * 40000]
    monkeypatch.setenv("DOCKER_STUB_PUSH_PIECES", json.dumps(pieces))
    endpoint = fault_endpoints(
        upload_success_responder(
            docker_layer=(DOCKER_LAYER_DIGEST, DOCKER_LAYER_SIZE),
            docker_config_digest=STUB_IMAGE_ID,
            slow_read_delay=0.0,
        )
    )
    result = docker_arm_sample(endpoint, tls_client_context, namespace="perf-docker-headmark", run_id="docker-dedup-head")

    assert_status(result, "invalid")
    assert result["docker"]["deduplicated"] is True, result_json(result)


def test_docker_arm_dedup_marker_split_across_reads_is_detected(
    fault_endpoints: Callable[..., FaultEndpoint],
    docker_stub: DockerStub,
    tls_client_context: ssl.SSLContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """去重标记跨读取块时也必须判出去重：解析不能只看单个块。"""
    pieces = ["y" * 6000, "Layer already exi", "sts\n"]
    monkeypatch.setenv("DOCKER_STUB_PUSH_PIECES", json.dumps(pieces))
    monkeypatch.setenv("DOCKER_STUB_PUSH_PIECE_GAP", "0.08")
    endpoint = fault_endpoints(
        upload_success_responder(
            docker_layer=(DOCKER_LAYER_DIGEST, DOCKER_LAYER_SIZE),
            docker_config_digest=STUB_IMAGE_ID,
            slow_read_delay=0.0,
        )
    )
    result = docker_arm_sample(endpoint, tls_client_context, namespace="perf-docker-splitmark", run_id="docker-dedup-split")

    assert_status(result, "invalid")
    assert result["docker"]["deduplicated"] is True, result_json(result)


def test_docker_arm_manifest_size_split_across_reads_is_parsed(
    fault_endpoints: Callable[..., FaultEndpoint],
    docker_stub: DockerStub,
    tls_client_context: ssl.SSLContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """摘要行的 size 数字跨读取块时不得把数字前缀当完整长度。"""
    document = consistent_docker_manifest()
    text = consistent_push_output()
    digits = str(len(document))
    head, separator, tail = text.partition(f"size: {digits}")
    assert separator, text
    pieces = ["q" * 6000 + head + f"size: {digits[:1]}", digits[1:] + tail]
    monkeypatch.setenv("DOCKER_STUB_PUSH_PIECES", json.dumps(pieces))
    monkeypatch.setenv("DOCKER_STUB_PUSH_PIECE_GAP", "0.08")
    endpoint = fault_endpoints(
        upload_success_responder(
            docker_layer=(DOCKER_LAYER_DIGEST, DOCKER_LAYER_SIZE),
            docker_config_digest=STUB_IMAGE_ID,
            slow_read_delay=0.0,
        )
    )
    result = docker_arm_sample(endpoint, tls_client_context, namespace="perf-docker-splitsize", run_id="docker-split-size")

    manifest_digest = "sha256:" + hashlib.sha256(document).hexdigest()
    evidence = result["docker"].get("evidence") or []
    assert any(entry.get("digest") == manifest_digest for entry in evidence), result_json(result)
    assert any(entry.get("size_bytes") == len(document) for entry in evidence), result_json(result)


def test_docker_arm_config_identity_mismatch_is_not_deleted(
    fault_endpoints: Callable[..., FaultEndpoint],
    docker_stub: DockerStub,
    tls_client_context: ssl.SSLContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读回 manifest 的 config 与本次 import imageId 不一致：不得 DELETE 该对象，且保留 tag 未知残留。"""
    foreign_config = "sha256:" + "9" * 64
    document = docker_manifest_document(DOCKER_LAYER_DIGEST, DOCKER_LAYER_SIZE, config_digest=foreign_config)
    manifest_digest = "sha256:" + hashlib.sha256(document).hexdigest()
    monkeypatch.setenv("DOCKER_STUB_PUSH_STDOUT", docker_push_output(document))
    endpoint = fault_endpoints(
        upload_success_responder(
            docker_layer=(DOCKER_LAYER_DIGEST, DOCKER_LAYER_SIZE),
            docker_config_digest=foreign_config,
            slow_read_delay=0.0,
        )
    )
    result = docker_arm_sample(endpoint, tls_client_context, namespace="perf-docker-config", run_id="docker-config-mismatch")

    push_calls = docker_stub.calls_with("push")
    assert len(push_calls) == 1, push_calls
    docker_tag = push_calls[0][-1].rsplit(":", 1)[-1]
    assert result["status"] != "valid", result_json(result)
    deletes = [request.target for request in endpoint.requests("DELETE")]
    assert not any(manifest_digest in target for target in deletes), f"身份不匹配不得删除未绑定 manifest: {deletes}"
    assert f"docker_tag:{docker_tag}:push_unknown" in result["cleanup"]["residuals"], result_json(result)


@pytest.mark.parametrize("mode", ["broken-json", "hung"], ids=str)
def test_docker_arm_manifest_read_failure_keeps_push_unknown_residual(
    fault_endpoints: Callable[..., FaultEndpoint],
    docker_stub: DockerStub,
    tls_client_context: ssl.SSLContext,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """push 退出 0 但 manifest 读回失败（坏 JSON / 超时）：必须保留本次 docker_tag 的未知残留。"""
    monkeypatch.setenv("DOCKER_STUB_PUSH_STDOUT", consistent_push_output())
    endpoint = fault_endpoints(
        upload_success_responder(
            docker_layer=(DOCKER_LAYER_DIGEST, DOCKER_LAYER_SIZE),
            docker_config_digest=STUB_IMAGE_ID,
            docker_manifest_mode=mode,
            slow_read_delay=0.0,
        )
    )
    result = docker_arm_sample(
        endpoint,
        tls_client_context,
        namespace="perf-docker-read",
        run_id=f"docker-read-{mode}",
        upload_timeout=8.0,
    )

    push_calls = docker_stub.calls_with("push")
    assert len(push_calls) == 1, push_calls
    docker_tag = push_calls[0][-1].rsplit(":", 1)[-1]
    assert result["status"] != "valid", result_json(result)
    assert f"docker_tag:{docker_tag}:push_unknown" in result["cleanup"]["residuals"], result_json(result)
    assert result["cleanup"]["storage_reclaimed"] is False, result_json(result)


@pytest.mark.parametrize("variant", ["never-reads-stdin", "closes-stdin"], ids=str)
def test_docker_login_stdin_faults_are_bounded_and_reaped(
    fault_endpoints: Callable[..., FaultEndpoint],
    docker_stub: DockerStub,
    tls_client_context: ssl.SSLContext,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    """docker login 的 stdin 写入必须受预算约束并回收子进程（不读大 stdin / 提前关闭 stdin）。"""
    endpoint = fault_endpoints(upload_success_responder(slow_read_delay=0.0))
    monkeypatch.setenv("DOCKER_STUB_PUSH_STDOUT", consistent_push_output())
    monkeypatch.setenv("DOCKER_STUB_LOGIN_SLEEP_SECONDS", "30")
    if variant == "never-reads-stdin":
        monkeypatch.setenv("DOCKER_STUB_LOGIN_DRAIN_STDIN", "0")
    else:
        monkeypatch.setenv("DOCKER_STUB_LOGIN_CLOSE_STDIN", "1")

    budget = 4.0
    started = time.monotonic()
    with watchdog(budget * 3 + 10.0, "docker login 的 stdin 写入未在有界时间内返回"):
        result = run_sample(
            registry=endpoint.registry,
            namespace="perf-docker-stdin",
            tls_context=tls_client_context,
            run_id=f"docker-stdin-{variant}",
            http_only=False,
            upload_timeout=budget,
            password="p" * (2 * 1024 * 1024),
        )
    elapsed = time.monotonic() - started

    assert result["status"] != "valid", result_json(result)
    login_stages = [stage for stage in result["stages"] if stage.get("name") == "docker_login"]
    assert login_stages and login_stages[-1]["status"] in {"failed", "deadline_exceeded"}, result_json(result)
    assert result["cleanup"]["login_attempted"] is True, result_json(result)
    assert elapsed <= budget * 2 + 10.0, f"stdin 写入未被预算约束: {elapsed:.1f}s"

    pids = docker_stub.pids_with("login")
    assert pids, "替身必须记录 login 调用"
    for pid in pids:
        deadline = time.monotonic() + 5.0
        while pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not pid_alive(pid), f"docker login 子进程未被回收: {pid}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
