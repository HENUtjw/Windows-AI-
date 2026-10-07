#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DeepSeek 协议层测试：用本地 mock 服务器验证 HTTP / SSE / 错误 / 重试。

为什么这样测
------------
翻译器最容易出错的地方不是提示词，而是**协议细节**：SSE 分片拼接、
HTTP 错误码映射、哪些错误该重试、哪些不该。这些都能在本地用一台
假装成 DeepSeek 的 HTTP 服务器完整复现，不需要 API Key、不花钱、不联网。

运行::

    python tests/test_translator.py
"""

from __future__ import annotations

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tools"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

from mock_deepseek import MockDeepSeekServer  # noqa: E402
from src.config import Config  # noqa: E402
from src.translator import (  # noqa: E402
    DeepSeekTranslator,
    MockTranslator,
    TranslationError,
    clean_translation,
)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name}   {detail}")
        FAILURES.append(name)


def make_translator(server: MockDeepSeekServer, **overrides) -> DeepSeekTranslator:
    data = {
        "deepseek": {
            "api_key": "sk-test-key",
            "base_url": server.base_url,
            "model": "deepseek-flash",
            "thinking": "disabled",
            "timeout_seconds": 5,
            "max_retries": 2,
        },
        "translate": {"context_sentences": 2},
    }
    for key, value in overrides.items():
        section, _, field = key.partition(".")
        data.setdefault(section, {})[field] = value
    return DeepSeekTranslator(Config(data))


# --------------------------------------------------------------------------- #
def test_clean_translation() -> None:
    print("\n[输出清理]")
    check("去掉「译文：」前缀", clean_translation("译文：你好世界") == "你好世界", clean_translation("译文：你好世界"))
    check("去掉「翻译:」前缀", clean_translation("翻译: 你好") == "你好", clean_translation("翻译: 你好"))
    check("去掉代码围栏", clean_translation("```\n你好\n```") == "你好", repr(clean_translation("```\n你好\n```")))
    check("去掉包裹引号", clean_translation('"你好"') == "你好", repr(clean_translation('"你好"')))
    check("保留正常译文", clean_translation("今天天气很好。") == "今天天气很好。")


def test_non_stream(server: MockServer) -> None:
    print("\n[非流式请求]")
    server.set_scene(status=200, content="译文：经济学不只是关于金钱。")
    translator = make_translator(server, **{"deepseek.stream": False})
    server.reset_requests()

    result = translator.translate("Economics is not just about money.")
    check("返回清理后的译文", result == "经济学不只是关于金钱。", repr(result))
    check("只发一次请求", len(server.requests) == 1, str(len(server.requests)))

    request = server.requests[0]
    check("URL 路径正确", request["path"] == "/chat/completions", request["path"])
    check(
        "Authorization 头正确",
        request["headers"].get("Authorization") == "Bearer sk-test-key",
        str(request["headers"].get("Authorization")),
    )
    check("请求体 stream=False", request["body"].get("stream") is False)
    check("请求体带 model", request["body"].get("model") == "deepseek-flash")
    translator.close()


def test_payload_shape(server: MockDeepSeekServer) -> None:
    """回归：模型名与思考模式开关必须按现行 API 规范发送。

    背景：``deepseek-chat`` / ``deepseek-reasoner`` 已于 2026-07-24 完全退役，
    且新模型**默认开启思考模式**。若沿用旧名或忘记关闭思考，字幕翻译会直接
    失败或变得极慢极贵。
    """
    print("\n[请求体规范]")
    translator = make_translator(server, **{"deepseek.stream": False})
    server.set_scene(status=200, content="ok")
    server.reset_requests()
    translator.translate("hello")
    body = server.requests[0]["body"]
    check("发送了 thinking 开关", body.get("thinking") == {"type": "disabled"}, str(body.get("thinking")))
    check("模型名不是已退役的旧名", body.get("model") != "deepseek-chat", str(body.get("model")))
    check("带上了 max_tokens 上限", int(body.get("max_tokens", 0)) > 0, str(body.get("max_tokens")))
    translator.close()

    # thinking 留空时应完全不发送该参数
    blank = make_translator(server, **{"deepseek.stream": False, "deepseek.thinking": ""})
    server.reset_requests()
    blank.translate("hello")
    check("留空时不发送 thinking", "thinking" not in server.requests[0]["body"], str(server.requests[0]["body"].keys()))
    blank.close()


def test_retired_model_guard(server: MockDeepSeekServer) -> None:
    """回归：配置里残留已退役的模型名时，必须给出可操作的提示而不是等 400。"""
    print("\n[已退役模型名的提示]")
    server.reset_requests()
    for retired, replacement in (("deepseek-chat", "deepseek-flash"), ("deepseek-reasoner", "deepseek-v4-pro")):
        translator = make_translator(server, **{"deepseek.model": retired})
        try:
            translator.translate("hello")
            check(f"{retired} 应被拦下", False)
        except TranslationError as exc:
            check(f"{retired} 提示改用 {replacement}", replacement in str(exc) and "停用" in str(exc), str(exc))
        check(f"{retired} 不会发起请求", len(server.requests) == 0, str(len(server.requests)))
        translator.close()


def test_default_model_is_current() -> None:
    """回归：默认配置不能指向已停用的模型名。"""
    print("\n[默认配置]")
    cfg = Config({})
    model = cfg.get("deepseek.model")
    check("默认模型不是已退役名称", model not in ("deepseek-chat", "deepseek-reasoner"), str(model))
    check("默认模型是现行名称", model in ("deepseek-flash", "deepseek-v4-pro"), str(model))
    check("默认关闭思考模式", cfg.get("deepseek.thinking") == "disabled", str(cfg.get("deepseek.thinking")))
    check("默认 stream 开启", cfg.get("deepseek.stream") is True)


def test_stream(server: MockServer) -> None:
    print("\n[流式请求]")
    server.set_scene(status=200, stream_pieces=["经济学", "不只是", "关于金钱。"])
    translator = make_translator(server, **{"deepseek.stream": True})
    server.reset_requests()

    deltas: list[str] = []
    result = translator.translate("Economics is not just about money.", on_delta=deltas.append)

    check("流式结果拼接正确", result == "经济学不只是关于金钱。", repr(result))
    check("on_delta 收到 3 个分片", len(deltas) == 3, str(deltas))
    check("分片内容正确", "".join(deltas) == result, str(deltas))
    check("请求体 stream=True", server.requests[0]["body"].get("stream") is True)
    translator.close()


def test_context_messages(server: MockServer) -> None:
    print("\n[上文携带]")
    server.set_scene(status=200, content="好的。")
    translator = make_translator(server, **{"deepseek.stream": False})
    server.reset_requests()

    context = [("First sentence.", "第一句。"), ("Second one.", "第二句。"), ("Third.", "第三句。")]
    translator.translate("Current sentence.", context)

    messages = server.requests[0]["body"]["messages"]
    roles = [m["role"] for m in messages]
    check("首条为 system", roles[0] == "system", str(roles))
    check("末条为当前待翻译文本", messages[-1]["content"] == "Current sentence.", messages[-1]["content"])
    # context_sentences=2，应只带最近 2 句 → system + 2*2 + 1 = 6 条
    check("只携带最近 2 句上文", len(messages) == 6, f"{len(messages)}: {roles}")
    check("上文按 用户/助手 交替", roles[1:5] == ["user", "assistant", "user", "assistant"], str(roles))
    check("最老的一句被丢弃", all("First sentence." != m["content"] for m in messages), str(messages))
    translator.close()


def test_errors(server: MockServer) -> None:
    print("\n[错误映射]")
    translator = make_translator(server, **{"deepseek.stream": False})

    server.set_scene(status=401, error_message="invalid key")
    server.reset_requests()
    try:
        translator.translate("hello")
        check("401 应抛异常", False)
    except TranslationError as exc:
        check("401 提示 API Key 无效", "API Key" in str(exc), str(exc))
    check("401 不重试", len(server.requests) == 1, str(len(server.requests)))

    server.set_scene(status=402, error_message="insufficient balance")
    server.reset_requests()
    try:
        translator.translate("hello")
        check("402 应抛异常", False)
    except TranslationError as exc:
        check("402 提示余额不足", "余额" in str(exc), str(exc))
    check("402 不重试", len(server.requests) == 1, str(len(server.requests)))
    translator.close()


def test_retry(server: MockServer) -> None:
    print("\n[可重试错误]")
    translator = make_translator(server, **{"deepseek.stream": False, "deepseek.max_retries": 2})

    server.set_scene(status=429, error_message="rate limited")
    server.reset_requests()
    started = time.monotonic()
    try:
        translator.translate("hello")
        check("429 重试耗尽后应抛异常", False)
    except TranslationError as exc:
        check("429 抛出可读错误", "限流" in str(exc) or "429" in str(exc), str(exc))
    elapsed = time.monotonic() - started
    # max_retries=2 → 共 3 次尝试
    check("429 重试到 max_retries 次", len(server.requests) == 3, str(len(server.requests)))
    check("重试之间有退避等待", elapsed >= 0.5, f"{elapsed:.2f}s")
    translator.close()


def test_retry_then_recover(server: MockServer) -> None:
    print("\n[限流后自动恢复]")
    translator = make_translator(server, **{"deepseek.stream": False, "deepseek.max_retries": 2})
    server.set_scene(fail_first_n=1, fail_status=429, status=200, content="限流后成功翻译")
    server.reset_requests()

    result = translator.translate("hello")
    check("限流后自动恢复并返回译文", result == "限流后成功翻译", repr(result))
    check("共 2 次请求（1 次限流 + 1 次成功）", len(server.requests) == 2, str(len(server.requests)))
    translator.close()


def test_server_error_retry(server: MockServer) -> None:
    print("\n[服务端 5xx 重试]")
    translator = make_translator(server, **{"deepseek.stream": False, "deepseek.max_retries": 2})
    server.set_scene(fail_first_n=2, fail_status=503, status=200, content="5xx 后成功")
    server.reset_requests()

    result = translator.translate("hello")
    check("连续 503 后仍恢复", result == "5xx 后成功", repr(result))
    check("共 3 次请求", len(server.requests) == 3, str(len(server.requests)))
    translator.close()


def test_retry_then_success(server: MockServer) -> None:
    print("\n[正常返回]")
    server.set_scene(status=200, content="成功译文")
    translator = make_translator(server, **{"deepseek.stream": False})
    server.reset_requests()
    check("正常返回", translator.translate("hello") == "成功译文")
    translator.close()


def test_guardrails(server: MockServer) -> None:
    print("\n[边界与守卫]")
    translator = make_translator(server, **{"deepseek.stream": False})
    server.reset_requests()
    check("空文本直接返回空", translator.translate("") == "")
    check("空文本不发请求", len(server.requests) == 0, str(len(server.requests)))

    blank = DeepSeekTranslator(Config({"deepseek": {"api_key": "", "base_url": server.base_url}}))
    try:
        blank.translate("hello")
        check("缺 Key 应抛异常", False)
    except TranslationError as exc:
        check("缺 Key 提示可操作", "config.json" in str(exc) or "DEEPSEEK_API_KEY" in str(exc), str(exc))
    translator.close()

    server.set_scene(status=200, content="正常")
    broken = make_translator(server, **{"deepseek.stream": False})


def test_mock_translator() -> None:
    print("\n[模拟翻译器]")
    mock = MockTranslator("简体中文")
    deltas: list[str] = []
    result = mock.translate("Hello world.", on_delta=deltas.append)
    check("返回非空", bool(result))
    check("包含原文", "Hello world." in result, result)
    check("回调被调用", len(deltas) == 1, str(deltas))


# --------------------------------------------------------------------------- #
def main() -> int:
    print("=" * 74)
    print("DeepSeek 协议层测试（本地 mock 服务器）")
    print("=" * 74)

    test_clean_translation()
    test_mock_translator()
    test_default_model_is_current()

    with MockDeepSeekServer() as server:
        print(f"\n[mock 服务器] 已启动于 {server.base_url}")
        test_non_stream(server)
        test_payload_shape(server)
        test_retired_model_guard(server)
        test_stream(server)
        test_context_messages(server)
        test_errors(server)
        test_retry(server)
        test_retry_then_recover(server)
        test_server_error_retry(server)
        test_retry_then_success(server)
        test_guardrails(server)

    print("\n" + "=" * 74)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for name in FAILURES:
            print(f"  · {name}")
        return 1
    print("全部通过 (ALL PASS)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
