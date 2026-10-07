#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DeepSeek 翻译客户端。

要点
----
* 用「多轮对话」的方式携带上文：把前几句的原文/译文作为历史消息传进去，
  这样模型能保持术语和代词一致，明显优于逐句独立翻译。
* 默认开启流式输出，译文边生成边显示，降低等待感。
* 对 401/402 这类不该重试的错误直接给出可操作的提示。
"""

from __future__ import annotations

import json
import re
import time
from typing import Callable, Iterable, Sequence

import requests

from .config import Config

DeltaCallback = Callable[[str], None]


class TranslationError(RuntimeError):
    """翻译失败，message 面向最终用户，可直接展示。"""


class RetryableTranslationError(TranslationError):
    """临时性失败（限流、服务端 5xx、网络抖动），退避重试后有希望成功。"""


# 已于 2026-07-24 UTC 15:59 完全退役的模型名，之后请求会直接失败。
# 映射到官方建议的替代名称，用于给出可操作的提示而不是让用户吃一个 HTTP 400。
RETIRED_MODELS = {
    "deepseek-chat": "deepseek-flash",
    "deepseek-reasoner": "deepseek-v4-pro",
}


# 模型偶尔会加上的前后缀，统一清掉
_CLEAN_PATTERNS = (
    re.compile(r"^\s*(译文|翻译|中文|翻译结果)\s*[:：]\s*"),
    re.compile(r"^```[a-zA-Z]*\s*"),
    re.compile(r"\s*```$"),
)


def clean_translation(text: str) -> str:
    """清理模型输出，只保留译文本身。"""
    text = (text or "").strip()
    for pattern in _CLEAN_PATTERNS:
        text = pattern.sub("", text)
    return text.strip().strip('"').strip("“”").strip("'").strip()


def probe_api_key(base_url: str, api_key: str, model: str = "") -> tuple[bool, str]:
    """用 ``GET /models`` 校验 Key，并顺带核对模型名是否可用。

    校验不是必须的（离线也能先存下来），但能立刻把「Key 打错」和「模型名不对」
    区分开，比事后对着一个笼统的报错猜要好得多。

    返回 ``(是否可用, 给用户看的说明)``。命令行与浮窗设置面板共用这一处逻辑。
    """
    import requests

    base = (base_url or "https://api.deepseek.com").rstrip("/")
    try:
        response = requests.get(
            f"{base}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
    except requests.RequestException as exc:
        return True, f"⚠ 暂时无法联网校验（{exc}），先按你输入的内容保存。"

    if response.status_code == 200:
        try:
            available = [str(item.get("id")) for item in response.json().get("data", [])]
        except Exception:
            available = []
        if model and available and model not in available:
            return False, f"✘ Key 有效，但模型 {model!r} 不在该账号的可用列表里：{', '.join(available)}"
        return True, "✔ Key 校验通过。"

    detail = ""
    try:
        detail = str(response.json().get("error", {}).get("message", ""))[:160]
    except Exception:
        detail = (response.text or "")[:160]

    if response.status_code == 401:
        return False, f"✘ Key 无效：{detail}"
    if response.status_code == 402:
        return False, f"✘ 账户余额不足：{detail}"
    return False, f"✘ 校验失败（HTTP {response.status_code}）：{detail}"


class DeepSeekTranslator:
    """调用 DeepSeek 的 chat/completions 接口做翻译。"""

    def __init__(self, cfg: Config) -> None:
        self.api_key = cfg.api_key
        self.base_url = str(cfg.get("deepseek.base_url", "https://api.deepseek.com")).rstrip("/")
        self.model = str(cfg.get("deepseek.model", "deepseek-flash"))
        # "disabled" / "enabled" / ""（留空则不发送该参数）
        self.thinking = str(cfg.get("deepseek.thinking", "disabled") or "").strip().lower()
        self.temperature = float(cfg.get("deepseek.temperature", 1.3))
        self.max_tokens = int(cfg.get("deepseek.max_tokens", 256) or 0)
        self.timeout = float(cfg.get("deepseek.timeout_seconds", 25))
        self.max_retries = int(cfg.get("deepseek.max_retries", 2))
        self.use_stream = bool(cfg.get("deepseek.stream", True))
        self.system_prompt = cfg.system_prompt
        self.context_size = int(cfg.get("translate.context_sentences", 3))
        self._session = requests.Session()

    # ------------------------------------------------------------------ #
    def _build_messages(self, text: str, context: Sequence[tuple[str, str]]) -> list[dict]:
        messages: list[dict] = [{"role": "system", "content": self.system_prompt}]
        if self.context_size > 0:
            for source, target in list(context)[-self.context_size :]:
                messages.append({"role": "user", "content": source})
                messages.append({"role": "assistant", "content": target})
        messages.append({"role": "user", "content": text})
        return messages

    def _payload(self, text: str, context: Sequence[tuple[str, str]]) -> dict:
        payload = {
            "model": self.model,
            "messages": self._build_messages(text, context),
            "temperature": self.temperature,
            "stream": self.use_stream,
        }
        # 现行模型默认走思考模式；字幕场景必须显式关闭（见 config.py 注释）
        if self.thinking in ("enabled", "disabled"):
            payload["thinking"] = {"type": self.thinking}
        # 一句字幕不可能很长，封顶可以避免偶发长输出拖慢整句
        if self.max_tokens > 0:
            payload["max_tokens"] = self.max_tokens
        return payload

    # ------------------------------------------------------------------ #
    def translate(
        self,
        text: str,
        context: Sequence[tuple[str, str]] = (),
        on_delta: DeltaCallback | None = None,
    ) -> str:
        """翻译一句文本。``on_delta`` 用于接收流式增量。"""
        text = (text or "").strip()
        if not text:
            return ""
        if not self.api_key:
            raise TranslationError(
                "未配置 DeepSeek API Key。请在 config.json 的 deepseek.api_key 填入，"
                "或设置环境变量 DEEPSEEK_API_KEY。"
            )
        if self.model in RETIRED_MODELS:
            raise TranslationError(
                f"模型 {self.model!r} 已于 2026-07-24 停用，请求会直接失败。"
                f"请把 config.json 里的 deepseek.model 改为 "
                f"{RETIRED_MODELS[self.model]!r}。"
            )

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = self._payload(text, context)

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                if self.use_stream:
                    return self._request_stream(url, headers, body, on_delta)
                return self._request_once(url, headers, body)
            except RetryableTranslationError as exc:
                # 限流 / 服务端临时故障：退避后重试
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(self._backoff(attempt))
            except TranslationError:
                # 401 / 402 这类确定性错误，重试没有意义
                raise
            except (requests.RequestException, ValueError, KeyError) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(self._backoff(attempt))

        if isinstance(last_error, TranslationError):
            raise last_error
        raise TranslationError(f"翻译请求失败：{last_error}")

    @staticmethod
    def _backoff(attempt: int) -> float:
        """指数退避，单次最多等 8 秒，避免限流时把请求打得更凶。"""
        return min(8.0, 0.6 * (2**attempt))

    # ------------------------------------------------------------------ #
    def _request_once(self, url: str, headers: dict, body: dict) -> str:
        response = self._session.post(url, headers=headers, json=body, timeout=self.timeout)
        self._raise_for_status(response)
        data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise TranslationError(f"接口返回格式异常：{str(data)[:200]}") from exc
        return clean_translation(content)

    def _request_stream(
        self,
        url: str,
        headers: dict,
        body: dict,
        on_delta: DeltaCallback | None,
    ) -> str:
        chunks: list[str] = []
        with self._session.post(
            url, headers=headers, json=body, timeout=self.timeout, stream=True
        ) as response:
            self._raise_for_status(response)
            for raw_line in response.iter_lines():
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    delta = json.loads(payload)["choices"][0].get("delta", {})
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                piece = delta.get("content") or ""
                if piece:
                    chunks.append(piece)
                    if on_delta:
                        on_delta(piece)
        return clean_translation("".join(chunks))

    # ------------------------------------------------------------------ #
    # 这些状态码代表「稍后可能成功」，值得重试；其余一律视为确定性错误
    RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

    @classmethod
    def _raise_for_status(cls, response: requests.Response) -> None:
        if response.status_code == 200:
            return
        detail = ""
        try:
            detail = str(response.json().get("error", {}).get("message", ""))[:200]
        except Exception:
            detail = (response.text or "")[:200]
        hints = {
            401: "API Key 无效或已失效，请检查 config.json 里的 deepseek.api_key。",
            402: "DeepSeek 账户余额不足，请先充值。",
            429: "请求过于频繁，已被限流。程序会自动退避重试；也可调大 captions.stable_ms 降低请求频率。",
        }
        hint = hints.get(response.status_code, "")
        message = f"DeepSeek 接口返回 HTTP {response.status_code}。{hint} {detail}".strip()
        if response.status_code in cls.RETRYABLE_STATUS:
            raise RetryableTranslationError(message)
        raise TranslationError(message)

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        self._session.close()


class MockTranslator:
    """离线占位翻译器，用于在没有 API Key 时验证整条链路。"""

    def __init__(self, target_language: str = "简体中文") -> None:
        self.target_language = target_language
        self.api_key = "mock"

    def translate(
        self,
        text: str,
        context: Sequence[tuple[str, str]] = (),
        on_delta: DeltaCallback | None = None,
    ) -> str:
        result = f"【模拟·{self.target_language}】{text}"
        if on_delta:
            on_delta(result)
        time.sleep(0.05)
        return result

    def close(self) -> None:
        pass


def create_translator(cfg: Config, mock: bool = False):
    """按配置创建翻译器；未配置 Key 且非 mock 时返回 None 并给出提示。"""
    if mock:
        return MockTranslator(str(cfg.get("translate.target_language", "简体中文")))
    return DeepSeekTranslator(cfg)
