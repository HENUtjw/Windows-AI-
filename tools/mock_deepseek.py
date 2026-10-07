#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""假装成 DeepSeek 接口的本地 HTTP 服务器（测试与自检专用）。

用途
----
翻译器最容易出错的地方不是提示词，而是**协议细节**：SSE 分片拼接、HTTP 错误码
映射、哪些错误该重试。用这台本地服务器就能把这些路径全覆盖，不需要 API Key、
不花钱、不联网。

能力
----
* ``stream=true``  → 按 SSE 逐片返回；``stream=false`` → 一次性返回 JSON
* ``fail_first_n`` → 前 N 次请求故意失败，用来验证限流 / 5xx 之后能否自动恢复
* ``echo``         → 把收到的待翻译文本回显进译文，便于端到端断言「哪句对应哪个请求」

被 ``tests/test_translator.py`` 与 ``tools/e2e_selftest.py`` 共用。
"""

from __future__ import annotations

import http.server
import json
import threading


def _last_user_message(messages: list) -> str:
    for message in reversed(messages or []):
        if isinstance(message, dict) and message.get("role") == "user":
            return str(message.get("content", ""))
    return ""


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):  # 静音，避免污染测试输出
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {}

        self.server.requests.append(  # type: ignore[attr-defined]
            {"path": self.path, "headers": dict(self.headers), "body": body}
        )

        scene = self.server.scene  # type: ignore[attr-defined]

        # 前 fail_first_n 次请求返回失败，用于测试「失败后自动恢复」
        index = len(self.server.requests)  # type: ignore[attr-defined]
        fail_first = scene.get("fail_first_n", 0)
        status = scene.get("fail_status", 429) if index <= fail_first else scene.get("status", 200)

        if status != 200:
            payload = json.dumps(
                {"error": {"message": scene.get("error_message", "mock failure")}},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        # 计算译文：echo 模式下回显待翻译文本，便于端到端核对一一对应关系
        if scene.get("echo"):
            source = _last_user_message(body.get("messages"))
            content = f"{scene.get('echo_prefix', '【译】')}{source}"
        else:
            content = str(scene.get("content", "译文"))

        if body.get("stream"):
            pieces = scene.get("stream_pieces")
            if pieces is None:
                pieces = list(content)  # 逐字分片，顺带压力测试 SSE 拼接
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for piece in pieces:
                frame = {"choices": [{"delta": {"content": piece}}]}
                self.wfile.write(f"data: {json.dumps(frame, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            payload = json.dumps(
                {"choices": [{"message": {"content": content}}]}, ensure_ascii=False
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)


class MockDeepSeekServer:
    """上下文管理器：起一台 mock 服务器，并按场景切换响应。"""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.httpd = http.server.ThreadingHTTPServer((host, 0), _Handler)
        self.httpd.scene = {"content": "译文"}  # type: ignore[attr-defined]
        self.httpd.requests = []  # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "MockDeepSeekServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def stop(self) -> None:
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def requests(self) -> list[dict]:
        return self.httpd.requests  # type: ignore[attr-defined]

    @property
    def request_count(self) -> int:
        return len(self.requests)

    def set_scene(self, **kwargs) -> None:
        self.httpd.scene = kwargs  # type: ignore[attr-defined]

    def reset_requests(self) -> None:
        self.httpd.requests.clear()  # type: ignore[attr-defined]
