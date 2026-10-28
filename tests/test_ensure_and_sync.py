"""ensure-and-sync.sh 与 ensure_manifest_entries.py 的行为测试。

用临时仓库副本执行真实脚本：复制真实的 ensure-and-sync.sh、ensure_manifest_entries.py、
image_manifest.py、images.yaml 与 tests/test_image_manifest.py，断言只改清单与条目数常量，
不碰 git 与 Actions。因此覆盖的是「补条目 → 常量同步 → 幂等」这条本地链路。

用法：
    pytest tests/test_ensure_and_sync.py -v
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"

MANIFEST_BODY = """images:
  - source: alpine:3.21
  - source: xiaoyaliu/alist
    platform: [linux/arm64, linux/arm/v7]
  - source: alpine/minio:RELEASE.2025-10-15T17-29-55Z
    local: minio/minio:latest
"""

TEST_FILE_BODY = """REAL_MANIFEST_ENTRY_COUNT = 4
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """最小仓库副本：只含脚本、清单与条目数常量。"""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "tests").mkdir()
    for name in ("ensure-and-sync.sh", "ensure_manifest_entries.py", "image_manifest.py"):
        shutil.copy2(SCRIPTS / name, root / "scripts" / name)
    (root / "images.yaml").write_text(MANIFEST_BODY, encoding="utf-8")
    (root / "tests" / "test_image_manifest.py").write_text(TEST_FILE_BODY, encoding="utf-8")
    return root


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """在副本仓库里执行 ensure-and-sync.sh。"""
    return subprocess.run(
        ["bash", str(root / "scripts" / "ensure-and-sync.sh"), *args],
        cwd=root,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHON": sys.executable},
        check=False,
    )


def _entries(root: Path) -> str:
    """返回清单里新增的条目文本（按行）。"""
    return (root / "images.yaml").read_text(encoding="utf-8")


def _count(root: Path) -> str:
    """返回测试文件里的条目数常量值。"""
    for line in (root / "tests" / "test_image_manifest.py").read_text(encoding="utf-8").splitlines():
        if line.startswith("REAL_MANIFEST_ENTRY_COUNT"):
            return line.split("=")[1].strip()
    raise AssertionError("常量缺失")


def test_local_only_appends_entry_and_bumps_count(repo: Path) -> None:
    """缺条目时追加到清单末尾，并把条目数常量 +1。"""
    result = _run(repo, "--local-only", "python:3.11.9-slim-bookworm")

    assert result.returncode == 0, result.stderr
    assert "  - source: python:3.11.9-slim-bookworm" in _entries(repo)
    assert _count(repo) == "5"
    assert "跳过提交" in result.stderr


def test_local_only_is_idempotent(repo: Path) -> None:
    """重复执行不再追加，也不再抬常量。"""
    assert _run(repo, "--local-only", "python:3.11.9-slim-bookworm").returncode == 0
    first = _entries(repo)
    assert _run(repo, "--local-only", "python:3.11.9-slim-bookworm").returncode == 0

    assert _entries(repo) == first
    assert _count(repo) == "5"


def test_dry_run_changes_nothing(repo: Path) -> None:
    """--dry-run 只报告计划，不改清单也不改常量。"""
    before = _entries(repo)
    result = _run(repo, "--dry-run", "python:3.11.9-slim-bookworm")

    assert result.returncode == 0, result.stderr
    assert "dry-run" in result.stderr
    assert _entries(repo) == before
    assert _count(repo) == "4"


def test_existing_local_alias_counts_as_present(repo: Path) -> None:
    """名字命中 local 别名时说明 source 已存在，不应新增条目。"""
    before = _entries(repo)
    result = _run(repo, "--local-only", "minio/minio:latest")

    assert result.returncode == 0, result.stderr
    assert _entries(repo) == before
    assert _count(repo) == "4"


def test_invalid_reference_fails_without_touching_manifest(repo: Path) -> None:
    """四段引用必须在写文件前失败，避免把坏条目留在清单里。"""
    before = _entries(repo)
    result = _run(repo, "--local-only", "quay.io/minio/aistor/minio")

    assert result.returncode != 0
    assert _entries(repo) == before
    assert _count(repo) == "4"


def test_check_mode_reports_without_writing(repo: Path) -> None:
    """--check 只输出缺失名字。"""
    result = subprocess.run(
        [sys.executable, str(repo / "scripts" / "ensure_manifest_entries.py"),
         "--check", str(repo / "images.yaml"), "python:3.11.9-slim-bookworm"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "python:3.11.9-slim-bookworm"
    assert "python:3.11.9-slim-bookworm" not in _entries(repo)


def test_dispatch_requires_github_origin(repo: Path, tmp_path: Path) -> None:
    """origin 不是 GitHub 仓库时，必须在改清单前失败，并提示用 --repo。"""
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(tmp_path / "local.git")],
        check=True,
    )
    before = _entries(repo)
    # 守卫顺序是「gh 存在 → origin 是 GitHub 仓库」，这里提供假 gh 以便走到 origin 校验。
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    (bindir / "gh").chmod(0o700)

    result = subprocess.run(
        ["bash", str(repo / "scripts" / "ensure-and-sync.sh"), "--wait", "--timeout", "5",
         "python:3.11.9-slim-bookworm"],
        cwd=repo,
        capture_output=True,
        text=True,
        env={"PATH": f"{bindir}:/usr/bin:/bin:/usr/sbin:/sbin", "PYTHON": sys.executable},
        check=False,
    )

    assert result.returncode != 0
    assert "origin 不是 GitHub 仓库" in result.stderr
    assert "--repo" in result.stderr
    assert _entries(repo) == before
