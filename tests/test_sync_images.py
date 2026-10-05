"""scripts/sync-images.sh 的行为测试。

用真实的 bash 进程执行同步脚本：复制真实的 scripts/image_manifest.py 到临时仓库，
写入真实 YAML 清单（source / platform / local）驱动脚本，外部 `docker` 边界用替身模拟。
因此断言的是消费者脚本对真实清单模块输出的处理结果，而不是回显的测试数据。

覆盖的行为：

- digest 相同则跳过，不同则 pull/tag/push，push 目标带 registry/namespace 前缀；
- 多平台清单按 platform 选出子 manifest 的 config.digest（选错平台会导致误判）；
- 同一 source 的 platform 列表展开成多条，每条按各自架构推送到带对应后缀的 target；
- 未知 digest 不当作相等；
- 单条 pull/tag/push 失败会累计并使最终退出非零，同时继续处理其余镜像；
- 清单解析失败时不执行 docker login / pull；
- 密码只通过 stdin 传给 docker login；
- 结束 logout；logout 失败时，原成功返回非零，原失败保留原状态；
- local 字段不由 Action 使用；
- 输入来自清单模块 stdout，不会被 docker 命令消费掉。

用法：
    pytest tests/test_sync_images.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync-images.sh"
MANIFEST_MODULE = REPO_ROOT / "scripts" / "image_manifest.py"

REGISTRY = "reg.example.com"
NAMESPACE = "ns"
PASSWORD = "s3cret-pass"


# docker 替身：按 policy 模拟 manifest/login/logout/pull/tag/push/rmi，并记录全部 argv。
FAKE_DOCKER = '''#!/usr/bin/env python3
"""测试用 docker 替身。"""
import json
import os
import sys


def main() -> int:
    argv = sys.argv[1:]
    log_path = os.environ.get("FAKE_DOCKER_LOG")
    if log_path and argv:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(argv) + "\\n")

    policy_path = os.environ.get("FAKE_DOCKER_POLICY")
    policy = {}
    if policy_path and os.path.exists(policy_path):
        with open(policy_path, encoding="utf-8") as handle:
            policy = json.load(handle)

    sub = argv[0] if argv else ""
    if sub == "login":
        return 0 if "--password-stdin" in argv else 5
    if sub == "logout":
        if policy.get("logout_error"):
            print(policy["logout_error"], file=sys.stderr)
        return int(policy.get("logout_rc", 0))
    if sub == "manifest":
        if policy.get("manifest_error"):
            print(policy["manifest_error"], file=sys.stderr)
            return 1
        manifests = policy.get("manifests", {})
        if argv[-1] in manifests:
            json.dump(manifests[argv[-1]], sys.stdout)
            return 0
        return 1
    if sub == "pull":
        return 1 if argv[-1] in policy.get("pull_fail", []) else 0
    if sub == "tag":
        return 1 if argv[-1] in policy.get("tag_fail", []) else 0
    if sub == "push":
        return 1 if argv[-1] in policy.get("push_fail", []) else 0
    if sub == "rmi":
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _target(mirror: str) -> str:
    """阿里云镜像全名：registry/namespace/mirror。"""
    return f"{REGISTRY}/{NAMESPACE}/{mirror}"


def _manifest(digest: str) -> dict[str, Any]:
    """单平台镜像的 `docker manifest inspect` 响应。"""
    return {"config": {"digest": digest}}


def _manifest_list(*platform_digests: tuple[str, str]) -> dict[str, Any]:
    """多平台 manifest list 响应。参数为 (architecture, digest)。"""
    return {
        "manifests": [
            {"platform": {"architecture": architecture, "os": "linux"}, "digest": digest}
            for architecture, digest in platform_digests
        ]
    }


def _yaml(*entries: str) -> str:
    """把若干 YAML 条目拼成 images.yaml 内容。"""
    return "images:\n" + "".join(f"  - {entry}\n" for entry in entries)


def _run_sync(
    tmp_path: Path,
    yaml_text: str,
    *,
    manifests: dict[str, Any] | None = None,
    pull_fail: list[str] | None = None,
    tag_fail: list[str] | None = None,
    push_fail: list[str] | None = None,
    logout_rc: int = 0,
    manifest_error: str = "",
    logout_error: str = "",
    stdin_data: str = "",
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    """执行同步脚本，返回 (进程结果, docker 调用记录)。

    临时仓库里复制真实脚本与真实清单模块，写入真实 YAML 清单。
    """
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "sync-images.sh").write_text(
        SYNC_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (scripts / "image_manifest.py").write_text(
        MANIFEST_MODULE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    manifest = repo / "images.yaml"
    manifest.write_text(yaml_text, encoding="utf-8")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)

    policy = {
        "manifests": manifests or {},
        "pull_fail": pull_fail or [],
        "tag_fail": tag_fail or [],
        "push_fail": push_fail or [],
        "logout_rc": logout_rc,
        "manifest_error": manifest_error,
        "logout_error": logout_error,
    }
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    log_path = tmp_path / "docker.log"

    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "PYTHON": sys.executable,
        "ALIYUN_REGISTRY": REGISTRY,
        "ALIYUN_NAME_SPACE": NAMESPACE,
        "ALIYUN_REGISTRY_USER": "user",
        "ALIYUN_REGISTRY_PASSWORD": PASSWORD,
        "FAKE_DOCKER_LOG": str(log_path),
        "FAKE_DOCKER_POLICY": str(policy_path),
    }
    result = subprocess.run(
        ["bash", str(scripts / "sync-images.sh"), str(manifest)],
        cwd=repo,
        env=env,
        input=stdin_data,
        capture_output=True,
        text=True,
    )
    calls = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ] if log_path.exists() else []
    return result, calls


def _subcommands(calls: list[list[str]]) -> list[str]:
    """提取每次 docker 调用的子命令。"""
    return [call[0] for call in calls]


def test_digest_equal_skips_pull_and_push(tmp_path: Path) -> None:
    """源与阿里云 digest 相同：只查询 manifest，不 pull/push，退出 0。"""
    manifests = {
        "alpine:latest": _manifest("sha256:aaa"),
        _target("alpine:latest"): _manifest("sha256:aaa"),
    }

    result, calls = _run_sync(tmp_path, _yaml("source: alpine:latest"), manifests=manifests)

    assert result.returncode == 0, result.stderr
    assert "pull" not in _subcommands(calls)
    assert "push" not in _subcommands(calls)


def test_digest_differs_runs_full_pipeline_with_registry_prefix(tmp_path: Path) -> None:
    """digest 不同：pull/tag/push 全走，push 目标是 registry/namespace/mirror。"""
    manifests = {
        "alpine:latest": _manifest("sha256:new"),
        _target("alpine:latest"): _manifest("sha256:old"),
    }

    result, calls = _run_sync(tmp_path, _yaml("source: alpine:latest"), manifests=manifests)

    assert result.returncode == 0, result.stderr
    pull = next(call for call in calls if call[0] == "pull")
    assert pull == ["pull", "--platform", "linux/amd64", "alpine:latest"]
    tag = next(call for call in calls if call[0] == "tag")
    assert tag == ["tag", "alpine:latest", _target("alpine:latest")]
    push = next(call for call in calls if call[0] == "push")
    assert push == ["push", _target("alpine:latest")]


def test_multiarch_selects_matching_submanifest_digest(tmp_path: Path) -> None:
    """多平台清单按显式 platform 选子 manifest；选错平台会因 digest 不等而误触发 pull。"""
    manifests = {
        "alpine:latest": _manifest_list(("amd64", "sha256:amd64"), ("arm64", "sha256:arm64")),
        "alpine@sha256:arm64": _manifest("sha256:config-arm64"),
        "alpine@sha256:amd64": _manifest("sha256:config-amd64"),
        _target("alpine:latest-linux-arm64"): _manifest("sha256:config-arm64"),
    }
    yaml_text = _yaml("source: alpine:latest\n    platform: [linux/arm64]")

    result, calls = _run_sync(tmp_path, yaml_text, manifests=manifests)

    assert result.returncode == 0, result.stderr
    assert "pull" not in _subcommands(calls)
    assert "push" not in _subcommands(calls)


def test_multiarch_platform_mismatch_pulls_that_platform(tmp_path: Path) -> None:
    """多平台 digest 不匹配时，按显式 platform 拉取并推送带后缀的阿里云 tag。"""
    manifests = {
        "alpine:latest": _manifest_list(("amd64", "sha256:amd64"), ("arm64", "sha256:arm64")),
        "alpine@sha256:arm64": _manifest("sha256:config-arm64"),
        "alpine@sha256:amd64": _manifest("sha256:config-amd64"),
        _target("alpine:latest-linux-arm64"): _manifest("sha256:config-amd64"),
    }
    yaml_text = _yaml("source: alpine:latest\n    platform: [linux/arm64]")

    result, calls = _run_sync(tmp_path, yaml_text, manifests=manifests)

    assert result.returncode == 0, result.stderr
    pull = next(call for call in calls if call[0] == "pull")
    assert pull == ["pull", "--platform", "linux/arm64", "alpine:latest"]
    push = next(call for call in calls if call[0] == "push")
    assert push == ["push", _target("alpine:latest-linux-arm64")]


def test_platform_list_entry_syncs_each_platform_to_its_own_target(tmp_path: Path) -> None:
    """同一 source 的 platform 列表展开成多条：每架构按各自 platform 拉取并推送对应后缀 tag。"""
    manifests = {
        "alpine:latest": {
            "manifests": [
                {"platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}, "digest": "sha256:arm64"},
                {"platform": {"os": "linux", "architecture": "arm", "variant": "v7"}, "digest": "sha256:armv7"},
            ]
        },
        "alpine@sha256:arm64": _manifest("sha256:cfg-arm64"),
        "alpine@sha256:armv7": _manifest("sha256:cfg-armv7"),
        _target("alpine:latest-linux-arm64"): _manifest("sha256:old-arm64"),
        _target("alpine:latest-linux-arm-v7"): _manifest("sha256:old-armv7"),
    }
    yaml_text = _yaml("source: alpine:latest\n    platform: [linux/arm64, linux/arm/v7]")

    result, calls = _run_sync(tmp_path, yaml_text, manifests=manifests)

    assert result.returncode == 0, result.stderr
    pulls = [call for call in calls if call[0] == "pull"]
    assert pulls == [
        ["pull", "--platform", "linux/arm64", "alpine:latest"],
        ["pull", "--platform", "linux/arm/v7", "alpine:latest"],
    ]
    pushes = [call[-1] for call in calls if call[0] == "push"]
    assert pushes == [
        _target("alpine:latest-linux-arm64"),
        _target("alpine:latest-linux-arm-v7"),
    ]


def test_unknown_source_digest_is_not_treated_as_equal(tmp_path: Path) -> None:
    """源 manifest 查询失败时 digest 未知，不能当作相等，必须尝试 pull。"""
    manifests = {_target("ghost:1.0"): _manifest("sha256:existing")}

    result, calls = _run_sync(tmp_path, _yaml("source: ghost:1.0"), manifests=manifests)

    assert result.returncode == 0, result.stderr
    assert "pull" in _subcommands(calls)
    assert "push" in _subcommands(calls)


def test_push_failure_accumulates_and_continues_others(tmp_path: Path) -> None:
    """push 失败：该条计入失败，后续镜像仍然继续，最终退出非零。"""
    manifests = {
        "bad:1": _manifest("sha256:sx"),
        "good:1": _manifest("sha256:sy"),
        _target("bad:1"): _manifest("sha256:tx"),
        _target("good:1"): _manifest("sha256:ty"),
    }

    result, calls = _run_sync(
        tmp_path,
        _yaml("source: bad:1", "source: good:1"),
        manifests=manifests,
        push_fail=[_target("bad:1")],
    )

    assert result.returncode != 0
    pushes = [call for call in calls if call[0] == "push"]
    assert [call[-1] for call in pushes] == [_target("bad:1"), _target("good:1")]


def test_pull_and_tag_failures_accumulate(tmp_path: Path) -> None:
    """pull 与 tag 失败同样计入失败并使退出非零，且不影响其余条目的 tag 尝试。"""
    manifests = {
        "tagme:1": _manifest("sha256:st"),
        _target("tagme:1"): _manifest("sha256:tt"),
    }

    result, calls = _run_sync(
        tmp_path,
        _yaml("source: pullme:1", "source: tagme:1"),
        manifests=manifests,
        pull_fail=["pullme:1"],
        tag_fail=[_target("tagme:1")],
    )

    assert result.returncode != 0
    pulls = [call[-1] for call in calls if call[0] == "pull"]
    assert pulls == ["pullme:1", "tagme:1"]
    tags = [call[-1] for call in calls if call[0] == "tag"]
    assert tags == [_target("tagme:1")]


def test_logout_failure_after_successful_sync_exits_nonzero(tmp_path: Path) -> None:
    """同步成功但 logout 失败：最终退出非零。"""
    manifests = {
        "alpine:latest": _manifest("sha256:same"),
        _target("alpine:latest"): _manifest("sha256:same"),
    }

    result, _ = _run_sync(
        tmp_path, _yaml("source: alpine:latest"), manifests=manifests, logout_rc=7
    )

    assert result.returncode != 0


def test_logout_failure_preserves_original_failure_status(tmp_path: Path) -> None:
    """同步已有失败时，logout 失败不得覆盖原失败状态。"""
    manifests = {"bad:1": _manifest("sha256:sx")}

    result, calls = _run_sync(
        tmp_path,
        _yaml("source: bad:1"),
        manifests=manifests,
        push_fail=[_target("bad:1")],
        logout_rc=7,
    )

    assert result.returncode == 1
    assert "logout" in _subcommands(calls)


def test_password_only_passed_via_stdin(tmp_path: Path) -> None:
    """登录使用 --password-stdin，密码不出现在任何 docker 命令行参数里。"""
    manifests = {"alpine:latest": _manifest("sha256:new")}

    result, calls = _run_sync(tmp_path, _yaml("source: alpine:latest"), manifests=manifests)

    assert result.returncode == 0, result.stderr
    login = next(call for call in calls if call[0] == "login")
    assert "--password-stdin" in login
    assert all(PASSWORD not in arg for call in calls for arg in call)


def test_parse_failure_prevents_login_and_pull(tmp_path: Path) -> None:
    """真实坏 YAML：清单解析失败，不执行任何 docker 命令，退出非零。"""
    result, calls = _run_sync(tmp_path, "images: [unterminated\n")

    assert result.returncode != 0
    assert calls == []


def test_local_field_is_not_tagged_by_action(tmp_path: Path) -> None:
    """Action 不使用 local 字段：只 tag 阿里云目标名，不额外 tag local 名。"""
    manifests = {
        "alpine/minio:RELEASE.1": _manifest("sha256:s"),
        _target("alpine_minio:RELEASE.1"): _manifest("sha256:t"),
    }
    yaml_text = _yaml("source: alpine/minio:RELEASE.1\n    local: minio/minio:latest")

    result, calls = _run_sync(tmp_path, yaml_text, manifests=manifests)

    assert result.returncode == 0, result.stderr
    tag_targets = [call[-1] for call in calls if call[0] == "tag"]
    assert _target("alpine_minio:RELEASE.1") in tag_targets
    assert "minio/minio:latest" not in tag_targets


def test_rows_come_from_module_not_stdin(tmp_path: Path) -> None:
    """rows 来自清单模块 stdout，外部 stdin 内容不得被当作镜像处理。"""
    manifests = {"alpine:latest": _manifest("sha256:new")}
    stdin_row = "stdin-only:1\tlinux/amd64\tstdin-only:1\t-"

    result, calls = _run_sync(
        tmp_path, _yaml("source: alpine:latest"), manifests=manifests, stdin_data=stdin_row
    )

    assert result.returncode == 0, result.stderr
    assert "stdin-only:1" not in {arg for call in calls for arg in call}


@pytest.mark.parametrize(("platform", "descriptor", "mirror"), [
    ("linux/amd64", {"os": "linux", "architecture": "amd64"}, "testmulti:1-linux-amd64"),
    ("linux/arm64", {"os": "linux", "architecture": "arm64", "variant": "v8"}, "testmulti:1-linux-arm64"),
    ("linux/arm64/v8", {"os": "linux", "architecture": "arm64", "variant": "v8"}, "testmulti:1-linux-arm64-v8"),
])
def test_manifest_descriptor_matches_full_platform(tmp_path: Path, platform: str, descriptor: dict[str, str], mirror: str) -> None:
    """抓住误选 Windows 或遗漏 ARM64 默认 v8 导致错误更新的回归。"""
    desired = "sha256:" + "1" * 64
    child = "sha256:" + "2" * 64
    wrong_child = "sha256:" + "3" * 64
    manifests = {
        "testmulti:1": {"manifests": [
            {"platform": {"os": "windows", "architecture": "amd64"}, "digest": wrong_child},
            {"platform": descriptor, "digest": child},
        ]},
        "testmulti@" + child: _manifest(desired),
        "testmulti@" + wrong_child: _manifest("sha256:" + "4" * 64),
        _target(mirror): _manifest(desired),
    }
    result, calls = _run_sync(tmp_path, _yaml("source: testmulti:1\n    platform: [" + platform + "]"), manifests=manifests)
    assert result.returncode == 0, result.stderr
    assert "pull" not in _subcommands(calls)
    assert "push" not in _subcommands(calls)


def test_manifest_query_keeps_failure_reason_and_continues(tmp_path: Path) -> None:
    """manifest 失败可以继续 pull，但不能丢掉 registry 的诊断原因。"""
    result, calls = _run_sync(tmp_path, _yaml("source: alpine"), manifest_error="registry rate limit exceeded")
    assert result.returncode == 0, result.stderr
    assert "pull" in _subcommands(calls)
    assert "registry rate limit exceeded" in result.stderr


def test_logout_failure_keeps_credential_store_diagnostic(tmp_path: Path) -> None:
    """logout 失败必须非零，并保留凭据存储故障的原始诊断。"""
    result, _ = _run_sync(tmp_path, _yaml("source: alpine"), logout_rc=7, logout_error="credential store unavailable")
    assert result.returncode == 1
    assert "credential store unavailable" in result.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
