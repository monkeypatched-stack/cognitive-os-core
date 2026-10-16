"""ModelBackend's Ollama HTTP timeouts: 120s by default for every caller; a caller with a
longer generation budget opts in per call (request_timeout / connect_timeout)."""

from __future__ import annotations

import asyncio

import httpx

from src.monkey_brain.kernel.execute.provider import model_backend as mb


class _Client:
    seen: list[httpx.Timeout] = []

    def __init__(self, timeout):
        _Client.seen.append(timeout)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json):
        return httpx.Response(200, json={"message": {"content": "ok"}}, request=httpx.Request("POST", url))


def test_ollama_read_timeout_is_120s_unless_the_caller_opts_in(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    _Client.seen.clear()
    backend = mb.ModelBackend(provider="ollama", model="m")
    assert asyncio.run(backend.complete("p")) == "ok"
    assert asyncio.run(backend.complete("p", request_timeout=255, connect_timeout=10)) == "ok"
    default, longer = _Client.seen
    assert default == httpx.Timeout(120.0)                                 # unchanged for every other caller
    assert longer.read == 255.0 and longer.connect == 10.0 and longer.write == 255.0
