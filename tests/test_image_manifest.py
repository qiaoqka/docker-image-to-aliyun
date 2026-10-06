"""image_manifest 模块的行为测试：引用归一化、阿里云映射、架构选择、冲突与 CLI。

覆盖范围：

- 引用归一化：source/local 缺省 tag 补 ``latest``，1/2/3 段引用合法，4 段路径拒绝。
- 阿里云映射：命名空间扁平化、官方镜像无前缀、显式 platform 加 ``-linux-arm64`` 后缀。
- 清单加载：schema 校验、``platform`` 字符串列表逐项展开、重复 ``(source, platform)``、
  同一 platform 映射冲突。
- 条目选择：精确 architecture 优先、amd64 回退到无 platform 条目、其它架构无条目报错、
  按 local 匹配仍保留 source，多架构同 source 合法。
- CLI：四 TAB 字段输出、local 缺失输出 ``-``、失败时非零退出且不输出 rows。

用法：
    pytest tests/test_image_manifest.py -v
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 测试通过上面的 sys.path 注入导入 scripts 包；pyright 无法跟踪运行期 sys.path 变更，故在此声明忽略。
from scripts.image_manifest import (  # noqa: E402  # pyright: ignore[reportMissingImports]
    DEFAULT_PLATFORM,
    Image,
    ImageManifestError,
    load_images,
    select_images,
)

MODULE_PATH = REPO_ROOT / "scripts" / "image_manifest.py"
REAL_MANIFEST = REPO_ROOT / "images.yaml"
REAL_MANIFEST_ENTRY_COUNT = 40


def _write_manifest(tmp_path: Path, body: str) -> Path:
    """把 YAML 正文写到临时清单文件并返回路径。"""
    path = tmp_path / "images.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _run_cli(manifest: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    """在当前仓库根目录运行模块 CLI，返回完整进程结果。"""
    return subprocess.run(
        [sys.executable, str(MODULE_PATH), str(manifest), *extra],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


def _assert_clean_row(row: str) -> None:
    """断言一行输出是 4 个不含空白控制字符的 TAB 字段。"""
    fields = row.split("\t")
    assert len(fields) == 4, row
    for field in fields:
        assert field != "", row
        assert not any(character.isspace() or ord(character) < 32 for character in field), row


# --------------------------------------------------------------------------- #
# 引用归一化与 mirror 映射
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        (Image("postgres:18.6"), "postgres:18.6"),
        (Image("oven/bun:1.2.19-alpine"), "oven_bun:1.2.19-alpine"),
        (
            Image("alpine/minio:RELEASE.2025-10-15T17-29-55Z"),
            "alpine_minio:RELEASE.2025-10-15T17-29-55Z",
        ),
        (Image("alpine"), "alpine:latest"),
        (
            Image("continuumio/miniconda3:24.9.2-0", platform="linux/arm64"),
            "continuumio_miniconda3:24.9.2-0-linux-arm64",
        ),
        (Image("xiaoyaliu/alist", platform="linux/arm/v7"), "xiaoyaliu_alist:latest-linux-arm-v7"),
        (
            Image("k8s.gcr.io/kube-state-metrics/kube-state-metrics:v2.0.0"),
            "kube-state-metrics_kube-state-metrics:v2.0.0",
        ),
    ],
    ids=[
        "official",
        "namespace",
        "namespace-minio",
        "implicit-latest",
        "arm64-suffix",
        "armv7-suffix",
        "registry-drop",
    ],
)
def test_mirror_maps_to_aliyun_repository(image: Image, expected: str) -> None:
    assert image.mirror == expected


def test_source_without_tag_gets_latest() -> None:
    assert Image("alpine").source == "alpine:latest"


def test_local_without_tag_gets_latest() -> None:
    image = Image("alpine/minio:RELEASE.2025-10-15T17-29-55Z", local="minio/minio")
    assert image.local == "minio/minio:latest"


def test_missing_local_stays_empty() -> None:
    assert Image("alpine").local == ""


def test_effective_platform_defaults_to_amd64() -> None:
    assert Image("alpine").effective_platform == "linux/amd64"


def test_effective_platform_keeps_explicit_value() -> None:
    assert Image("alpine", platform="linux/arm64").effective_platform == "linux/arm64"


@pytest.mark.parametrize(
    "source",
    ["quay.io/minio/aistor/minio", "a/b/c/d:1", "quay.io/minio/aistor/minio:v1"],
)
def test_rejects_reference_with_four_segments(source: str) -> None:
    with pytest.raises(ImageManifestError, match="四段"):
        Image(source)


def test_rejects_empty_tag() -> None:
    with pytest.raises(ImageManifestError, match="tag"):
        Image("alpine:")


def test_rejects_malformed_platform() -> None:
    with pytest.raises(ImageManifestError, match="platform"):
        Image("alpine", platform="amd64")


def test_rejects_digest_reference() -> None:
    with pytest.raises(ImageManifestError):
        Image("alpine@sha256:" + "a" * 64)


# --------------------------------------------------------------------------- #
# load_images：schema 与冲突
# --------------------------------------------------------------------------- #


def test_load_images_reads_entries(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        """
images:
  - source: alpine
  - source: oven/bun:1.2.19-alpine
    local: minio/minio
""",
    )
    images = load_images(path)
    assert [image.source for image in images] == ["alpine:latest", "oven/bun:1.2.19-alpine"]
    assert images[1].local == "minio/minio:latest"


def test_load_images_reads_real_manifest() -> None:
    images = load_images(REAL_MANIFEST)
    assert len(images) == REAL_MANIFEST_ENTRY_COUNT
    assert Image("postgres:18.6") in images
    assert Image("eclipse-temurin:21-jre") in images
    assert Image("alpine/minio:RELEASE.2025-10-15T17-29-55Z", local="minio/minio:latest") in images
    assert Image("xiaoyaliu/alist", platform="linux/arm64") in images
    assert Image("xiaoyaliu/alist", platform="linux/arm/v7") in images


def test_real_manifest_merges_same_source_platforms() -> None:
    images = load_images(REAL_MANIFEST)
    alist = [image for image in images if image.source == "xiaoyaliu/alist:latest"]
    assert [image.platform for image in alist] == ["linux/arm64", "linux/arm/v7"]


def test_load_images_rejects_missing_images_key(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, "entries: []\n")
    with pytest.raises(ImageManifestError, match="images"):
        load_images(path)


def test_load_images_rejects_non_list_images(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, "images: {}\n")
    with pytest.raises(ImageManifestError, match="列表"):
        load_images(path)


def test_load_images_rejects_empty_file(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, "")
    with pytest.raises(ImageManifestError):
        load_images(path)


def test_load_images_rejects_missing_source(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, "images:\n  - platform: [linux/arm64]\n")
    with pytest.raises(ImageManifestError, match="source"):
        load_images(path)


def test_load_images_rejects_unknown_field(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, "images:\n  - source: alpine\n    platfrom: linux/arm64\n")
    with pytest.raises(ImageManifestError, match="platfrom"):
        load_images(path)


def test_load_images_rejects_duplicate_source_platform(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, "images:\n  - source: alpine\n  - source: alpine\n")
    with pytest.raises(ImageManifestError, match="重复"):
        load_images(path)


def test_load_images_allows_same_source_multiple_platforms(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        """
images:
  - source: xiaoyaliu/alist
    platform: [linux/arm64]
  - source: xiaoyaliu/alist
    platform: [linux/arm/v7]
""",
    )
    assert len(load_images(path)) == 2


def test_load_images_rejects_mirror_collision(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, "images:\n  - source: a/b:1\n  - source: a_b:1\n")
    with pytest.raises(ImageManifestError, match="映射冲突"):
        load_images(path)


def test_load_images_rejects_alias_collision_within_same_platform(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        """
images:
  - source: alpine/minio:RELEASE.2025-10-15T17-29-55Z
    local: minio/minio:latest
  - source: minio/minio:latest
""",
    )
    with pytest.raises(ImageManifestError, match="映射冲突"):
        load_images(path)


def test_load_images_allows_alias_across_different_platforms(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        """
images:
  - source: alpine/minio:RELEASE.2025-10-15T17-29-55Z
    platform: [linux/arm64]
    local: minio/minio:latest
  - source: minio/minio:latest
""",
    )
    assert len(load_images(path)) == 2


def test_load_images_allows_same_source_empty_and_explicit_amd64(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        """
images:
  - source: alpine
  - source: alpine
    platform: [linux/amd64]
""",
    )
    images = load_images(path)
    assert len(images) == 2
    explicit = Image("alpine", platform="linux/amd64")
    assert select_images(images, ["alpine"], "linux/amd64") == [explicit]


def test_load_images_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ImageManifestError, match="不存在"):
        load_images(tmp_path / "missing.yaml")


# --------------------------------------------------------------------------- #
# load_images：platform 字符串列表展开
# --------------------------------------------------------------------------- #


def test_load_images_expands_platform_list_in_order(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        """
images:
  - source: xiaoyaliu/alist
    platform: [linux/arm64, linux/arm/v7]
""",
    )
    images = load_images(path)
    assert [(image.source, image.platform) for image in images] == [
        ("xiaoyaliu/alist:latest", "linux/arm64"),
        ("xiaoyaliu/alist:latest", "linux/arm/v7"),
    ]


def test_load_images_single_element_platform_list(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        """
images:
  - source: continuumio/miniconda3:24.9.2-0
    platform: [linux/arm64]
""",
    )
    images = load_images(path)
    assert len(images) == 1
    assert images[0].platform == "linux/arm64"


def test_load_images_omitted_platform_keeps_default_amd64(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, "images:\n  - source: alpine\n")
    images = load_images(path)
    assert len(images) == 1
    assert images[0].platform == ""
    assert images[0].effective_platform == DEFAULT_PLATFORM


def test_load_images_applies_local_to_every_expanded_platform(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        """
images:
  - source: alpine/minio:RELEASE.2025-10-15T17-29-55Z
    platform: [linux/arm64, linux/arm/v7]
    local: minio/minio
""",
    )
    images = load_images(path)
    assert [image.platform for image in images] == ["linux/arm64", "linux/arm/v7"]
    assert {image.local for image in images} == {"minio/minio:latest"}


def test_select_from_expanded_platform_list_picks_each_target(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        """
images:
  - source: xiaoyaliu/alist
    platform: [linux/arm64, linux/arm/v7]
""",
    )
    images = load_images(path)
    arm64 = select_images(images, ["xiaoyaliu/alist"], "linux/arm64")
    armv7 = select_images(images, ["xiaoyaliu/alist"], "linux/arm/v7")
    assert [image.platform for image in arm64] == ["linux/arm64"]
    assert [image.platform for image in armv7] == ["linux/arm/v7"]
    assert arm64[0].mirror == "xiaoyaliu_alist:latest-linux-arm64"
    assert armv7[0].mirror == "xiaoyaliu_alist:latest-linux-arm-v7"


@pytest.mark.parametrize(
    "platform_line",
    [
        "platform: []",
        "platform:",
        "platform: linux/arm64",
        "platform: [linux/arm64, 3]",
        "platform: [linux/arm64, null]",
        "platform: [amd64]",
        "platform: [linux/arm64, linux/arm64]",
    ],
    ids=[
        "empty-list",
        "null",
        "scalar",
        "non-string-element",
        "null-element",
        "invalid-arch",
        "duplicate-arch",
    ],
)
def test_load_images_rejects_invalid_platform_field(tmp_path: Path, platform_line: str) -> None:
    path = _write_manifest(tmp_path, f"images:\n  - source: alpine\n    {platform_line}\n")
    with pytest.raises(ImageManifestError, match="platform"):
        load_images(path)


# --------------------------------------------------------------------------- #
# select_images：架构选择
# --------------------------------------------------------------------------- #


def test_select_matches_source_and_implicit_latest() -> None:
    images = [Image("nginx")]
    assert select_images(images, ["nginx"], DEFAULT_PLATFORM) == images


def test_select_amd64_falls_back_to_entry_without_platform() -> None:
    plain = Image("alpine")
    pinned_arm = Image("alpine", platform="linux/arm64")
    assert select_images([plain, pinned_arm], ["alpine"], "linux/amd64") == [plain]


def test_select_prefers_exact_platform_over_fallback() -> None:
    plain = Image("alpine")
    pinned = Image("alpine", platform="linux/amd64")
    assert select_images([plain, pinned], ["alpine"], "linux/amd64") == [pinned]


def test_select_picks_requested_arm_platform() -> None:
    arm64 = Image("xiaoyaliu/alist", platform="linux/arm64")
    armv7 = Image("xiaoyaliu/alist", platform="linux/arm/v7")
    assert select_images([arm64, armv7], ["xiaoyaliu/alist"], "linux/arm/v7") == [armv7]
    assert select_images([arm64, armv7], ["xiaoyaliu/alist"], "linux/arm64") == [arm64]


def test_select_errors_when_platform_has_no_entry() -> None:
    entry = Image("continuumio/miniconda3:24.9.2-0", platform="linux/arm64")
    with pytest.raises(ImageManifestError, match="linux/amd64"):
        select_images([entry], ["continuumio/miniconda3:24.9.2-0"], "linux/amd64")


def test_select_matches_local_and_keeps_source() -> None:
    entry = Image("alpine/minio:RELEASE.2025-10-15T17-29-55Z", local="minio/minio:latest")
    selected = select_images([entry], ["minio/minio:latest"], DEFAULT_PLATFORM)
    assert selected == [entry]
    assert selected[0].source == "alpine/minio:RELEASE.2025-10-15T17-29-55Z"
    assert selected[0].local == "minio/minio:latest"


def test_select_deduplicates_and_keeps_manifest_order() -> None:
    other = Image("nginx:1.25.5")
    entry = Image("alpine/minio:RELEASE.2025-10-15T17-29-55Z", local="minio/minio:latest")
    selected = select_images(
        [other, entry],
        ["minio/minio:latest", "nginx:1.25.5", "alpine/minio:RELEASE.2025-10-15T17-29-55Z"],
        DEFAULT_PLATFORM,
    )
    assert selected == [other, entry]


def test_select_errors_for_unknown_name() -> None:
    with pytest.raises(ImageManifestError, match="没有"):
        select_images([Image("alpine")], ["nginx:1.25.5"], DEFAULT_PLATFORM)


def test_select_errors_on_platform_mapping_conflict() -> None:
    first = Image("a/one:1", local="shared:1")
    second = Image("a/two:1", local="shared:1")
    with pytest.raises(ImageManifestError, match="映射冲突"):
        select_images([first, second], ["shared:1"], DEFAULT_PLATFORM)


def test_select_rejects_invalid_requested_reference() -> None:
    with pytest.raises(ImageManifestError, match="四段"):
        select_images([Image("alpine")], ["quay.io/minio/aistor/minio"], DEFAULT_PLATFORM)


# --------------------------------------------------------------------------- #
# CLI 契约
# --------------------------------------------------------------------------- #


def test_cli_outputs_four_tab_fields(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        """
images:
  - source: alpine
  - source: continuumio/miniconda3:24.9.2-0
    platform: [linux/arm64]
  - source: alpine/minio:RELEASE.2025-10-15T17-29-55Z
    local: minio/minio
""",
    )
    result = _run_cli(path)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines == [
        "alpine:latest\tlinux/amd64\talpine:latest\t-",
        "continuumio/miniconda3:24.9.2-0\tlinux/arm64\tcontinuumio_miniconda3:24.9.2-0-linux-arm64\t-",
        "alpine/minio:RELEASE.2025-10-15T17-29-55Z\tlinux/amd64\talpine_minio:RELEASE.2025-10-15T17-29-55Z\tminio/minio:latest",
    ]
    for line in lines:
        _assert_clean_row(line)
    assert result.stderr == ""


def test_cli_emits_every_real_manifest_entry() -> None:
    result = _run_cli(REAL_MANIFEST)
    assert result.returncode == 0, result.stderr
    rows = result.stdout.splitlines()
    assert len(rows) == REAL_MANIFEST_ENTRY_COUNT
    for row in rows:
        _assert_clean_row(row)
    assert "alpine/minio:RELEASE.2025-10-15T17-29-55Z\tlinux/amd64\talpine_minio:RELEASE.2025-10-15T17-29-55Z\tminio/minio:latest" in rows
    assert "eclipse-temurin:21-jre\tlinux/amd64\teclipse-temurin:21-jre\t-" in rows


def test_cli_fails_without_rows_for_invalid_manifest(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, "images:\n  - source: quay.io/minio/aistor/minio\n")
    result = _run_cli(path)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "四段" in result.stderr


def test_cli_fails_for_missing_file(tmp_path: Path) -> None:
    result = _run_cli(tmp_path / "missing.yaml")
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr != ""


def test_cli_help_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--help"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 0
    assert "manifest" in result.stdout
