#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""打一个可以直接发给别人的发布包。

为什么用 ``git ls-files`` 取文件清单
------------------------------------
``config.json``（含用户的 API Key）、``logs/``（含视频字幕内容）、``_preview/``
都不该进发布包。手工列白名单迟早会漏，而 ``git ls-files`` 只会列出**被跟踪**的
文件——``.gitignore`` 已经把这些排除掉了，于是「不进包」是机制保证的，
不依赖我每次记得检查。

用法::

    python tools/make_release.py                 # 打包到 dist/
    python tools/make_release.py --check-only    # 只做安全检查，不写文件
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

# 出现这些名字就说明包装错了（都是本机私有文件）
FORBIDDEN = ("config.json", "_commit_msg.txt", ".comtypes_cache", "__pycache__")
FORBIDDEN_PREFIX = ("logs/", "_preview/", ".git/")


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=_ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [line.strip().replace("\\", "/") for line in out.splitlines() if line.strip()]


def read_version() -> str:
    path = os.path.join(_ROOT, "src", "__init__.py")
    for line in open(path, encoding="utf-8"):
        if "__version__" in line and "=" in line:
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return "0.0.0"


def check(files: list[str]) -> list[str]:
    """返回不该进包的文件。"""
    problems = []
    for name in files:
        base = os.path.basename(name)
        if base in FORBIDDEN or name.startswith(FORBIDDEN_PREFIX):
            problems.append(name)
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="打发布包")
    parser.add_argument("--outdir", default=os.path.join(_ROOT, "dist"))
    parser.add_argument("--check-only", action="store_true", help="只做安全检查")
    parser.add_argument("--name", default="ai-caption-translator", help="包名前缀")
    args = parser.parse_args()

    version = read_version()
    files = tracked_files()
    print(f"版本: v{version}")
    print(f"git 跟踪的文件: {len(files)} 个")

    problems = check(files)
    if problems:
        print("\n!! 安全检查未通过，以下文件不该出现在发布包里：")
        for name in problems:
            print("   ", name)
        return 1
    print("安全检查: 通过（不含 config.json / logs / _preview）")

    if args.check_only:
        return 0

    os.makedirs(args.outdir, exist_ok=True)
    inner = f"{args.name}-v{version}"
    archive = os.path.join(args.outdir, f"{inner}.zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name in files:
            bundle.write(os.path.join(_ROOT, name), f"{inner}/{name}")

    size = os.path.getsize(archive)
    print(f"\n已生成: {archive}")
    print(f"  大小: {size / 1024:.0f} KB   内含 {len(files)} 个文件")

    # 打包后再从 zip 里核一遍（防止写错路径）
    with zipfile.ZipFile(archive) as bundle:
        inside = bundle.namelist()
    leaked = [n for n in inside if check([n[len(inner) + 1 :]])]
    print("  包内复查:", "通过" if not leaked else f"!! 泄漏 {leaked}")
    return 0 if not leaked else 1


if __name__ == "__main__":
    raise SystemExit(main())
