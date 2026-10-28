"""把缺失的镜像条目追加到 images.yaml 尾部，供 ensure-and-sync.sh 与手工调用使用。

只做文本追加，不重排、不改注释：已有条目（按 source 或 local 命中）不再写入，因此可重复执行。
新条目写成 ``  - source: <name>``，与清单现有风格一致；写完用 ``load_images`` 复验，
格式错误立刻失败，不会把坏文件留给后续 push。

用法：
    python3 scripts/ensure_manifest_entries.py images.yaml python:3.11.9-slim-bookworm
    python3 scripts/ensure_manifest_entries.py images.yaml --check python:3.11.9-slim-bookworm

stdout 每行输出一个新增的 ``source``（`--check` 时不写文件，只输出仍缺失的名字）；
没有新增时输出为空。诊断信息走 stderr，失败退出码为 1。
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 与 tests 一致：通过 sys.path 注入导入同目录模块，pyright 无法跟踪运行期改动，故声明忽略。
from image_manifest import (  # noqa: E402  # pyright: ignore[reportMissingImports]
    Image,
    ImageManifestError,
    load_images,
)

TRAILING_ENTRY = "  - source: {source}\n"


def requested_sources(names: list[str]) -> list[str]:
    """校验请求名并归一化成 ``source`` 形式；非法引用直接抛 ImageManifestError。"""
    sources: list[str] = []
    for name in names:
        if not isinstance(name, str) or not name.strip():
            raise ImageManifestError("请求的镜像名必须是非空字符串")
        source = Image(name.strip()).source
        if source not in sources:
            sources.append(source)
    return sources


def missing_sources(images: list[Image], sources: list[str]) -> list[str]:
    """返回既不是任何 source、也不是任何 local 的请求名。"""
    known: set[str] = set()
    for image in images:
        known.add(image.source)
        if image.local:
            known.add(image.local)
    return [source for source in sources if source not in known]


def append_entries(manifest: Path, sources: list[str]) -> None:
    """把条目追加到清单末尾并复验；已有文件末尾无换行时先补一个。"""
    text = manifest.read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        text += "\n"
    text += "".join(TRAILING_ENTRY.format(source=source) for source in sources)
    manifest.write_text(text, encoding="utf-8")
    # 复验：写坏比不写更糟，必须在这里失败而不是等 push 之后。
    load_images(manifest)


def main(argv: list[str] | None = None) -> int:
    """按缺失情况追加条目；stdout 为新增 source 列表。"""
    parser = argparse.ArgumentParser(
        prog="ensure_manifest_entries.py",
        description="把缺失的镜像追加到 images.yaml 末尾，输出新增的 source。",
    )
    parser.add_argument("manifest", type=Path, help="images.yaml 路径")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只报告缺失的名字，不修改清单",
    )
    parser.add_argument("images", nargs="+", help="镜像名，注意写清 tag")
    args = parser.parse_args(argv)

    try:
        sources = requested_sources(args.images)
        missing = missing_sources(load_images(args.manifest), sources)
        if missing and not args.check:
            append_entries(args.manifest, missing)
    except (OSError, ImageManifestError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1

    for source in missing:
        print(source)
    return 0


if __name__ == "__main__":
    sys.exit(main())
