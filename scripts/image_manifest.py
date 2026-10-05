"""镜像同步清单（``images.yaml``）的加载、映射与选择共享模块。

职责边界：

- 唯一 owner：解析 ``images.yaml``，把清单条目映射成阿里云目标名（``Image.mirror``），并按
  目标架构选择要处理的条目。Action 与 30 机拉取脚本共用同一套命名规则，避免各自复制解析逻辑。
- 不做网络请求、不执行 ``docker``、不读写 registry；只做纯数据校验与命名计算。

核心工作流程：

1. ``load_images`` 用 ``yaml.safe_load`` 读取清单，校验 schema、镜像引用与 platform。每条 ``platform``
   是字符串列表（省略表示默认 amd64），按列表顺序展开成多条单架构 ``Image``，并检查重复条目和
   同一 platform 映射冲突。
2. 构造函数把 ``source``/``local`` 的缺省 tag 统一补成 ``latest``，形成不可变 ``Image``。
3. ``Image.mirror`` 把上游命名空间扁平化成阿里云 ``repository:tag``，显式 platform 追加
   ``-linux-arm64`` 形式后缀。
4. ``select_images`` 按 ``source`` 或 ``local`` 匹配请求名，优先精确架构，amd64 可回退到未显式
   指定 platform 的条目。

用法示例：

    python3 scripts/image_manifest.py images.yaml

CLI 每条镜像输出一行，4 个 TAB 分隔字段：``source``、``effective_platform``、``mirror``、``local``；
``local`` 为空时输出 ``-``。诊断信息走 stderr，失败退出码非 0。
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import yaml

# 缺省架构与 tag：省略 platform 时按 amd64 处理，且阿里云 tag 不带架构后缀。
DEFAULT_PLATFORM = "linux/amd64"
DEFAULT_TAG = "latest"

# 允许的清单字段，出现其它字段说明清单写错（例如把 platform 拼成 platfrom）。
_ALLOWED_FIELDS = frozenset({"source", "platform", "local"})

# 引用各段的合法字符：镜像名/命名空间段。
_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:(?:[._-])[a-z0-9]+)*$")
# registry 段额外允许点号与端口（如 k8s.gcr.io、registry.example.com:5000）。
_REGISTRY_PATTERN = re.compile(r"^[A-Za-z0-9]+(?:(?:[.-])[A-Za-z0-9]+)*(?::[0-9]+)?$")
# Docker tag 规则：字母/数字/下划线开头，随后可含点、短横线。
_TAG_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
# platform 形如 os/arch 或 os/arch/variant。
_PLATFORM_PATTERN = re.compile(r"^[a-z0-9]+/[a-z0-9]+(?:/[a-z0-9]+)?$")

# 最多 3 段：name、namespace/name、registry/namespace/name；四段及以上拒绝。
_MAX_SEGMENTS = 3


class ImageManifestError(ValueError):
    """清单文件或镜像引用不合法时抛出的错误。"""


class _Reference(NamedTuple):
    """已通过校验的镜像引用。

    Attributes:
        repository: 去掉 tag 后的仓库路径，如 ``oven/bun``。
        namespace: 上游命名空间；官方镜像为空串。
        name: 仓库最后一段的名字。
        tag: 已补全的 tag，恒为非空。
    """

    repository: str
    namespace: str
    name: str
    tag: str


def _split_tag(reference: str) -> tuple[str, str | None]:
    """按最后一个斜杠之后的冒号拆分 repository 与 tag。

    Args:
        reference: 待拆分的引用。

    Returns:
        ``(repository, tag)``；引用未写 tag 时 ``tag`` 为 ``None``，写成空 tag（``alpine:``）时
        为 ``""``，两者语义不同，由调用方分别处理。
    """
    slash = reference.rfind("/")
    colon = reference.find(":", slash + 1)
    if colon == -1:
        return reference, None
    return reference[:colon], reference[colon + 1 :]


def _parse_reference(reference: str) -> _Reference:
    """校验镜像引用并拆出仓库、命名空间与 tag。

    支持 1/2/3 段引用：

    - 1 段：官方镜像，如 ``alpine``；命名空间为空。
    - 2 段：``namespace/name``，如 ``oven/bun``。
    - 3 段：``registry/namespace/name``，如 ``k8s.gcr.io/kube-state-metrics/kube-state-metrics``；
      按现有规则把第 1 段当 registry 丢弃，第 2 段当命名空间。

    Args:
        reference: 上游镜像引用。

    Returns:
        已校验的 ``_Reference``，缺省 tag 已补成 ``latest``。

    Raises:
        ImageManifestError: 引用为空、段数超过 3、含空段、某段或 tag 非法、显式 tag 为空时。
    """
    if not isinstance(reference, str) or not reference:
        raise ImageManifestError(f"镜像引用必须是非空字符串: {reference!r}")

    repository, tag = _split_tag(reference)
    segments = repository.split("/")
    if len(segments) > _MAX_SEGMENTS:
        raise ImageManifestError(
            f"镜像引用 {reference!r} 是 {len(segments)} 段路径，暂不支持四段及以上"
        )
    if any(not segment for segment in segments):
        raise ImageManifestError(f"镜像引用 {reference!r} 含空路径段")

    if len(segments) == _MAX_SEGMENTS:
        if not _REGISTRY_PATTERN.match(segments[0]):
            raise ImageManifestError(
                f"镜像引用 {reference!r} 的 registry 段非法: {segments[0]!r}"
            )
        namespace, name = segments[1], segments[2]
        name_segments = segments[1:]
    elif len(segments) == 2:
        namespace, name, name_segments = segments[0], segments[1], segments
    else:
        namespace, name, name_segments = "", segments[0], segments

    for segment in name_segments:
        if not _NAME_PATTERN.match(segment):
            raise ImageManifestError(f"镜像引用 {reference!r} 的路径段非法: {segment!r}")

    if tag is None:
        tag = DEFAULT_TAG
    elif not _TAG_PATTERN.match(tag):
        raise ImageManifestError(f"镜像引用 {reference!r} 的 tag 非法: {tag!r}")

    return _Reference(repository=repository, namespace=namespace, name=name, tag=tag)


def _normalize_reference(reference: str) -> str:
    """校验引用并补全缺省 tag，返回 ``repository:tag`` 形式。"""
    parsed = _parse_reference(reference)
    return f"{parsed.repository}:{parsed.tag}"


@dataclass(frozen=True)
class Image:
    """清单中的单条镜像。

    构造时统一校验并归一化：``source`` 缺省 tag 补 ``latest``；``local`` 非空时缺省 tag 也补
    ``latest``。因此 ``Image("alpine").source == "alpine:latest"``。

    Attributes:
        source: 上游镜像引用，恒为 ``repository:tag`` 形式。
        platform: 显式架构，如 ``linux/arm64``；为空表示按 ``linux/amd64`` 处理且阿里云 tag
            不带后缀。
        local: 拉取到目标机后额外打的本地 tag；为空表示只保留 ``source`` 名。
    """

    source: str
    platform: str = ""
    local: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source:
            raise ImageManifestError(f"source 必须是非空字符串: {self.source!r}")
        if not isinstance(self.platform, str):
            raise ImageManifestError(f"platform 必须是字符串: {self.platform!r}")
        if not isinstance(self.local, str):
            raise ImageManifestError(f"local 必须是字符串: {self.local!r}")
        if self.platform and not _PLATFORM_PATTERN.match(self.platform):
            raise ImageManifestError(
                f"platform 非法: {self.platform!r}，应形如 linux/amd64 或 linux/arm/v7"
            )

        object.__setattr__(self, "source", _normalize_reference(self.source))
        if self.local:
            object.__setattr__(self, "local", _normalize_reference(self.local))

    @property
    def effective_platform(self) -> str:
        """返回实际使用的架构：显式 ``platform``，否则默认 ``linux/amd64``。"""
        return self.platform or DEFAULT_PLATFORM

    @property
    def mirror(self) -> str:
        """返回阿里云目标 ``repository:tag``，不含 registry 与阿里云命名空间前缀。

        上游命名空间按 ``_`` 扁平化（``oven/bun`` → ``oven_bun``，``alpine/minio`` →
        ``alpine_minio``）；官方镜像不加前缀。显式 platform 会在 tag 后追加
        ``-linux-arm64`` 形式后缀，省略时不加。
        """
        parsed = _parse_reference(self.source)
        repository = (
            f"{parsed.namespace}_{parsed.name}" if parsed.namespace else parsed.name
        )
        suffix = f"-{self.platform.replace('/', '-')}" if self.platform else ""
        return f"{repository}:{parsed.tag}{suffix}"


def _ensure_no_conflicts(images: list[Image]) -> None:
    """校验条目集合内没有重复条目、映射冲突与同名覆盖。

    检测三类冲突：

    1. 重复 ``(source, platform)`` 条目。
    2. 两个不同 source 生成同一 mirror，会在阿里云侧互相覆盖。
    3. 同一 ``effective_platform`` 下，同一个名字（source 或 local）属于两个不同 source；否则
       按名字匹配时会因条目顺序不同得到不同结果（例如 ``A.local == B.source``）。

    Args:
        images: 待校验的条目。

    Raises:
        ImageManifestError: 出现上述任一冲突时。
    """
    seen: set[tuple[str, str]] = set()
    mirrors: dict[str, Image] = {}
    aliases: dict[tuple[str, str], Image] = {}
    for image in images:
        key = (image.source, image.platform)
        if key in seen:
            raise ImageManifestError(
                f"重复条目: source={image.source} platform={image.effective_platform}"
            )
        seen.add(key)

        existing = mirrors.get(image.mirror)
        if existing is not None and existing.source != image.source:
            raise ImageManifestError(
                f"同一 platform 映射冲突: {existing.source} 与 {image.source} 都映射到 {image.mirror}"
            )
        mirrors[image.mirror] = image

        for name in {image.source, image.local} - {""}:
            alias_key = (name, image.effective_platform)
            owner = aliases.get(alias_key)
            if owner is not None and owner.source != image.source:
                raise ImageManifestError(
                    f"同一 platform 映射冲突: 名字 {name} 在同为 {image.effective_platform} 的 "
                    f"{owner.source} 与 {image.source} 之间重复"
                )
            aliases[alias_key] = image


def _parse_platform_field(item: dict[str, object], index: int) -> list[str]:
    """解析单条清单的 ``platform`` 字段，返回待展开的架构列表。

    - 省略：返回 ``[""]``，表示按默认 amd64 处理且阿里云 tag 不带后缀。
    - 显式：必须是非空字符串列表；逐项校验架构合法性并拒绝重复项，避免同一条目重复展开。

    Args:
        item: 单条清单映射。
        index: 条目序号，仅用于错误信息。

    Returns:
        架构字符串列表，至少一项；省略时唯一项为空串。

    Raises:
        ImageManifestError: platform 不是列表、列表为空、元素非字符串/非法架构或重复时。
    """
    if "platform" not in item:
        return [""]
    raw = item["platform"]
    if not isinstance(raw, list):
        raise ImageManifestError(
            f"images 第 {index} 条 platform 必须是列表，如 [linux/arm64]；"
            f"省略表示默认 {DEFAULT_PLATFORM}"
        )
    if not raw:
        raise ImageManifestError(
            f"images 第 {index} 条 platform 列表不能为空；省略表示默认 {DEFAULT_PLATFORM}"
        )
    seen: set[str] = set()
    validated: list[str] = []
    for element in raw:
        if not isinstance(element, str) or not _PLATFORM_PATTERN.match(element):
            raise ImageManifestError(
                f"images 第 {index} 条 platform 元素非法: {element!r}，"
                "应形如 linux/amd64 或 linux/arm/v7"
            )
        if element in seen:
            raise ImageManifestError(
                f"images 第 {index} 条 platform 列表存在重复架构: {element!r}"
            )
        seen.add(element)
        validated.append(element)
    return validated


def load_images(path: Path) -> list[Image]:
    """读取 YAML 镜像清单并校验为 ``Image`` 列表。

    每条清单条目的 ``platform`` 是字符串列表：省略表示默认 amd64；显式时按列表顺序展开成多条
    单架构 ``Image``，``local`` 原样应用到每一条展开结果。

    Args:
        path: 清单文件路径，根节点必须是含 ``images`` 列表的映射；每条为含 ``source``、可选
            ``platform`` 列表/``local`` 的映射。

    Returns:
        按清单原始顺序排列的镜像条目；一条含 N 个架构的条目展开成 N 条。

    Raises:
        ImageManifestError: 文件不存在、YAML 非法、根节点或条目 schema 不符、镜像引用或
            platform 非法（非列表、空列表、元素非法或重复）、存在重复条目或同一 platform
            映射冲突时。
    """
    manifest_path = Path(path)
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ImageManifestError(f"清单文件不存在: {manifest_path}") from error
    except IsADirectoryError as error:
        raise ImageManifestError(f"清单路径不是文件: {manifest_path}") from error
    except yaml.YAMLError as error:
        raise ImageManifestError(f"清单 YAML 解析失败: {manifest_path}: {error}") from error

    if not isinstance(raw, dict):
        raise ImageManifestError(f"清单根节点必须是映射（mapping）: {manifest_path}")
    if "images" not in raw:
        raise ImageManifestError(f"清单缺少 images 字段: {manifest_path}")
    unknown_root = set(raw) - {"images"}
    if unknown_root:
        raise ImageManifestError(
            "清单根节点含未知字段: " + ", ".join(sorted(map(str, unknown_root)))
        )
    items = raw["images"]
    if not isinstance(items, list):
        raise ImageManifestError("images 字段必须是列表")

    images: list[Image] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ImageManifestError(f"images 第 {index} 条必须是映射")
        unknown = set(item) - _ALLOWED_FIELDS
        if unknown:
            raise ImageManifestError(
                f"images 第 {index} 条含未知字段: " + ", ".join(sorted(map(str, unknown)))
            )
        if "source" not in item:
            raise ImageManifestError(f"images 第 {index} 条缺少 source 字段")
        # platform 已在此步整体校验；展开失败只会来自 source/local，首次构造即抛出，不会留下部分结果。
        platforms = _parse_platform_field(item, index)
        try:
            for platform in platforms:
                images.append(
                    Image(
                        source=item["source"],
                        platform=platform,
                        local=item.get("local", ""),
                    )
                )
        except ImageManifestError as error:
            raise ImageManifestError(f"images 第 {index} 条无效: {error}") from error

    _ensure_no_conflicts(images)
    return images


def _dedupe_images(images: Iterable[Image]) -> list[Image]:
    """按 ``(source, platform)`` 对条目去重，保留首次出现顺序。"""
    unique: dict[tuple[str, str], Image] = {}
    for image in images:
        unique.setdefault((image.source, image.platform), image)
    return list(unique.values())


def _pick_for_platform(candidates: list[Image], target: str, name: str) -> Image:
    """为请求名在候选条目中选出目标架构的条目。

    候选集已通过 ``_ensure_no_conflicts``：同一 ``effective_platform`` 下同名不会映射到两个不同
    source，因此精确层与回退层各自最多命中一条。

    Args:
        candidates: 同一请求名匹配到的条目，已按 ``(source, platform)`` 去重。
        target: 目标架构。
        name: 请求名，仅用于错误信息。

    Returns:
        选中的条目。

    Raises:
        ImageManifestError: 目标架构没有可用条目时。
    """
    for image in candidates:
        if image.platform == target:
            return image

    # amd64 可以回退到未显式指定 platform 的条目；其它架构不做回退。
    if target == DEFAULT_PLATFORM:
        for image in candidates:
            if not image.platform:
                return image

    available = ", ".join(sorted({image.effective_platform for image in candidates}))
    raise ImageManifestError(f"镜像 {name} 没有 {target} 架构条目（清单中可选: {available}）")


def select_images(images: list[Image], requested: list[str], platform: str) -> list[Image]:
    """按请求名与目标架构从清单中选择要处理的镜像条目。

    Args:
        images: 已加载的清单条目。
        requested: 命令行传入的镜像名，按 ``source`` 或 ``local`` 匹配；缺省 tag 视为 ``latest``。
        platform: 目标架构，如 ``linux/amd64``；为空时按默认 amd64 处理。

    Returns:
        命中的条目，按 ``images`` 原始顺序且去重；同一请求名同时命中 ``source`` 与 ``local``
        时只返回一条。

    Raises:
        ImageManifestError: 清单内本身存在重复/映射冲突、请求名非法、清单中没有该镜像、目标
            架构没有可用条目，或同一 platform 下请求名映射到多个条目时。
    """
    _ensure_no_conflicts(images)

    target = platform or DEFAULT_PLATFORM
    if not _PLATFORM_PATTERN.match(target):
        raise ImageManifestError(f"platform 非法: {target!r}")

    normalized: list[str] = []
    seen_requested: set[str] = set()
    for name in requested:
        if not isinstance(name, str) or not name:
            raise ImageManifestError(f"请求的镜像名必须是非空字符串: {name!r}")
        resolved = _normalize_reference(name)
        if resolved not in seen_requested:
            seen_requested.add(resolved)
            normalized.append(resolved)

    selected: dict[tuple[str, str], Image] = {}
    for name in normalized:
        candidates = _dedupe_images(
            image for image in images if image.source == name or image.local == name
        )
        if not candidates:
            raise ImageManifestError(f"清单中没有镜像: {name}")
        image = _pick_for_platform(candidates, target, name)
        selected[(image.source, image.platform)] = image

    return [image for image in images if (image.source, image.platform) in selected]


def _format_row(image: Image) -> str:
    """把条目格式化成 4 个 TAB 分隔字段。

    Args:
        image: 待输出的条目。

    Returns:
        形如 ``source\\teffective_platform\\tmirror\\tlocal`` 的一行，``local`` 为空时为 ``-``。

    Raises:
        ImageManifestError: 任一字段为空或含空白/控制字符时（保证输出可按 TAB 安全切分）。
    """
    fields = [image.source, image.effective_platform, image.mirror, image.local or "-"]
    for field in fields:
        if not field or any(character.isspace() or ord(character) < 32 for character in field):
            raise ImageManifestError(f"输出字段含空白或控制字符: {field!r}")
    return "\t".join(fields)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：输出清单每条的 4 个 TAB 分隔字段。

    Args:
        argv: 命令行参数，默认取 ``sys.argv[1:]``。

    Returns:
        进程退出码：成功为 0，清单读取或校验失败为 2。
    """
    parser = argparse.ArgumentParser(
        prog="image_manifest.py",
        description="读取 images.yaml，每条镜像输出 source、effective_platform、mirror、local 四个 TAB 字段。",
    )
    parser.add_argument("manifest", type=Path, help="images.yaml 清单文件路径")
    args = parser.parse_args(argv)

    try:
        images = load_images(args.manifest)
        rows = [_format_row(image) for image in images]
    except ImageManifestError as error:
        print(f"错误: {error}", file=sys.stderr)
        return 2

    for row in rows:
        print(row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
