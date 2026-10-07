#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""comtypes 缓存目录引导。

背景
----
`comtypes` 在**首次导入时**需要一个可写的目录来存放自动生成的 COM 包装模块：

1. 首选 ``<site-packages>/comtypes/gen``；你的 Python 环境里这个目录通常还不存在，
   comtypes 会现场创建它。
2. 若第 1 步不可写，退回 ``%APPDATA%\\Python\\PythonXY\\comtypes_cache``。
3. 若两者都不可写，直接抛 ``PermissionError``，此时连 ``import uiautomation`` 都会失败。

第 3 种情况会出现在：只读安装位置、受限的沙箱/权限环境、容器镜像等。
本模块在导入 ``comtypes.client`` 之前先注入一个位于**项目目录内**的可写
``comtypes.gen`` 包，从而让第 1 步的判断直接通过。

设计原则
--------
只在默认位置确实不可用时才介入，正常环境下完全无副作用。
"""

from __future__ import annotations

import os
import sys
import types

_CACHE_DIRNAME = ".comtypes_cache"


def _default_gen_dir_is_usable() -> bool:
    """判断 comtypes 默认的 gen 目录是否已经可写。"""
    try:
        import comtypes  # noqa: F401
    except Exception:
        return True  # comtypes 本身有问题，不归本模块处理
    default = os.path.join(os.path.dirname(comtypes.__file__), "gen")
    return os.path.isdir(default) and os.access(default, os.W_OK)


def ensure_writable_comtypes_cache() -> str | None:
    """必要时把 comtypes 的生成缓存重定向到项目目录。

    返回实际使用的缓存目录；若无需介入则返回 None。
    """
    if "comtypes.gen" in sys.modules:
        return None

    if _default_gen_dir_is_usable():
        return None

    import comtypes

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cache_dir = os.path.join(project_root, _CACHE_DIRNAME)
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError:
        return None  # 连项目目录都写不了，交给 comtypes 自己报错

    gen_pkg = types.ModuleType("comtypes.gen")
    gen_pkg.__path__ = [cache_dir]  # type: ignore[attr-defined]
    sys.modules["comtypes.gen"] = gen_pkg
    comtypes.gen = gen_pkg  # type: ignore[attr-defined]
    return cache_dir


def silence_library_log() -> None:
    """关掉 ``uiautomation`` 自带的 ``@AutomationLog.txt``。

    该库默认会往**当前工作目录**写一个日志文件（``Logger.FilePath`` 默认就是
    ``@AutomationLog.txt``），会在用户的项目目录里留下一堆无用文件。
    这里统一关闭，本项目的日志走 ``logs/``。
    """
    try:
        import uiautomation as _auto

        _auto.Logger.SetLogFile("")
    except Exception:
        pass  # 库不可用时不用管，真正的错误会在调用处暴露


# 作为模块导入时自动生效
ensure_writable_comtypes_cache()
silence_library_log()
