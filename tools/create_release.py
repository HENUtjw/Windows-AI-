# -*- coding: utf-8 -*-
"""创建 GitHub Release 并上传附件。

`gh` CLI 没装，所以直接调 GitHub REST API。凭据从 git 的凭据管理器取
（推送能成功就说明那里有），**全程不打印 token**。
"""

import json
import os
import subprocess
import sys

import requests

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

OWNER_REPO = "HENUtjw/Windows-AI-"
TAG = "v0.1.0"
NAME = "v0.1.0 · 首个公开版本"
ARCHIVE = os.path.join(_ROOT, "dist", "ai-caption-translator-v0.1.0.zip")

BODY = """\
把 Windows 系统「实时字幕」的文本取出来，用 DeepSeek 翻译后显示在置顶浮窗上。
面向看英文视频的场景——重点是端到端延迟，以及一个能长期挂在屏幕上的界面。

## 怎么用

1. 装 Python 3.8+（本机在 3.13.7 上实测）
2. `pip install -r requirements.txt`
3. `python main.py`
4. 第一次运行会**弹窗口让你填 DeepSeek API Key**（[申请地址](https://platform.deepseek.com/api_keys)），
   不用改配置文件

Windows 实时字幕不用你手动开，程序会拉起来；退出时也会一并关掉。

## 这个版本有什么

- **置顶浮窗**：仿 Windows 实时字幕的外观，可拖动、圆角、不抢焦点（点按钮不会抢走播放器的快捷键）
- **三页设置面板**：21 项可调，**外观类改动保存后立即生效**，不用重启
- **双层提速**：内置短语包（111 条高频套话，零延迟）+ AI 学到的短句缓存
- **推测翻译**：在句子定稿前先翻，实测命中率 86%，等于把断句等待藏起来
- **自动重连**：实时字幕被关掉或崩了会自动重新拉起并继续

## 实测数据（不是估计）

| 项目 | 实测 |
|---|---|
| 模型响应延迟 | 0.9–4.6s 波动，**与句子长短无关**（8 词 0.83s / 42 词 0.92s） |
| 推测翻译命中 | 86% |
| 逗号从句提前 | 长句平均提前 3 秒出译文 |
| 实时字幕翻页 | 25.7 分钟里 113 次 |

## 已知限制

- **只支持英文 → 中文**（识别语言取决于系统实时字幕的设置）
- 实时字幕的转录是**滚动窗口**，历史内容会被裁掉；程序做了防丢处理，但极短的残句仍可能被丢弃（实测 6.8–8.9%）
- 数字的多词写法（`Seventeen seventy six` ↔ `1776`）尚未归一化，可能偶尔重复翻译一次
- 需要 Windows 10/11（依赖系统自带的实时字幕组件）

## 验证情况

257 项单元测试 + 92 项端到端（含真实 DeepSeek 服务与真实实时字幕、以及中途强杀实时字幕的混沌测试）全部通过。

> 压缩包内**不含** `config.json`——你的 API Key 只在本机。别人下载后第一次运行会看到配置窗口。
"""


def github_token() -> str:
    """从 git 凭据管理器取 token（不打印）。"""
    proc = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    print("!! 没能从凭据管理器拿到 token")
    print("   git 的返回：", proc.stdout.replace("\n", " | ")[:200])
    raise SystemExit(1)


def main() -> int:
    token = github_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    base = f"https://api.github.com/repos/{OWNER_REPO}"

    # 已经建过就先删掉重建，避免重复运行时报 422
    existing = requests.get(f"{base}/releases/tags/{TAG}", headers=headers, timeout=30)
    if existing.status_code == 200:
        release_id = existing.json()["id"]
        print(f"已存在 {TAG} 的 release，先删除 (id={release_id})")
        requests.delete(f"{base}/releases/{release_id}", headers=headers, timeout=30)

    print("创建 release …")
    response = requests.post(
        f"{base}/releases",
        headers=headers,
        data=json.dumps(
            {
                "tag_name": TAG,
                "target_commitish": "main",
                "name": NAME,
                "body": BODY,
                "draft": False,
                "prerelease": False,
            }
        ).encode("utf-8"),
        timeout=60,
    )
    if response.status_code not in (200, 201):
        print(f"!! 创建失败 {response.status_code}: {response.text[:400]}")
        return 1
    release = response.json()
    print("   release:", release["html_url"])

    print("上传附件 …")
    upload_url = release["upload_url"].split("{")[0]
    with open(ARCHIVE, "rb") as handle:
        payload = handle.read()
    upload = requests.post(
        f"{upload_url}?name={ARCHIVE.split('/')[-1]}",
        headers={**headers, "Content-Type": "application/zip"},
        data=payload,
        timeout=300,
    )
    if upload.status_code not in (200, 201):
        print(f"!! 上传失败 {upload.status_code}: {upload.text[:400]}")
        return 1

    print(f"   附件: {upload.json()['name']}  ({upload.json()['size'] / 1024:.0f} KB)")
    print(f"   {upload.json()['browser_download_url']}")
    print()
    print("发布页:", release["html_url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
