# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Joshua Kimsey
import asyncio

import httpx
import pytest

from librewxr.data.retry import retry_get


class _ScriptedClient:
    """Fake httpx.AsyncClient.get() that raises/returns a scripted sequence.

    One entry per call to ``get()``; a callable entry is invoked (so a
    script can raise ``asyncio.CancelledError`` — a plain object in the
    list can't represent "raise this" cleanly for a BaseException).
    """

    def __init__(self, script: list):
        self._script = list(script)
        self.calls = 0

    async def get(self, url, **kwargs):
        self.calls += 1
        item = self._script.pop(0)
        if callable(item):
            item()
        # BaseException, not Exception: asyncio.CancelledError inherits
        # from BaseException (since 3.8, specifically so `except
        # Exception` can't accidentally swallow a cancellation) - an
        # Exception-only check here would silently `return` the
        # CancelledError instance instead of raising it.
        if isinstance(item, BaseException):
            raise item
        return item


class TestRetryGet:
    @pytest.mark.asyncio
    async def test_succeeds_first_try(self):
        client = _ScriptedClient(["response"])
        result = await retry_get(client, "https://example.test", retries=1)
        assert result == "response"
        assert client.calls == 1

    @pytest.mark.asyncio
    async def test_retries_on_transport_error(self):
        client = _ScriptedClient([httpx.ConnectError("refused"), "response"])
        result = await retry_get(client, "https://example.test", retries=1, delay=0)
        assert result == "response"
        assert client.calls == 2

    @pytest.mark.asyncio
    async def test_retries_on_decoding_error(self):
        """Regression test: this path previously raised AttributeError
        (``module 'httpx' has no attribute 'DecodeError'`` — the actual
        class in the pinned httpx 0.28.1 is ``DecodingError``) instead of
        retrying, so a genuinely truncated response was never retried —
        it crash-logged an unrelated AttributeError instead."""
        client = _ScriptedClient([httpx.DecodingError("truncated"), "response"])
        result = await retry_get(client, "https://example.test", retries=1, delay=0)
        assert result == "response"
        assert client.calls == 2

    @pytest.mark.asyncio
    async def test_gives_up_after_retries_exhausted(self):
        client = _ScriptedClient([
            httpx.ConnectError("refused"),
            httpx.ConnectError("refused"),
        ])
        result = await retry_get(client, "https://example.test", retries=1, delay=0)
        assert result is None
        assert client.calls == 2

    @pytest.mark.asyncio
    async def test_does_not_retry_http_status_error(self):
        """A real server response (e.g. 404/500) means retrying won't
        help — retry_get only catches TransportError/DecodingError."""
        def _raise():
            raise httpx.HTTPStatusError(
                "500", request=httpx.Request("GET", "https://example.test"),
                response=httpx.Response(500),
            )
        client = _ScriptedClient([_raise])
        with pytest.raises(httpx.HTTPStatusError):
            await retry_get(client, "https://example.test", retries=1, delay=0)
        assert client.calls == 1

    @pytest.mark.asyncio
    async def test_cancellation_propagates_cleanly(self):
        """The property radar_fetch_timeout_seconds (fetcher.py) depends
        on: when asyncio.wait_for cancels a fetch mid-request, retry_get's
        except clauses must not intercept or mask CancelledError - it has
        to propagate straight out so wait_for can turn it into a clean
        TimeoutError. This is exactly the interaction that exposed the
        DecodingError typo above: matching a real CancelledError against
        `except httpx.DecodeError` (the misspelled, nonexistent attribute)
        raised AttributeError instead, masking the timeout with a
        confusing, unrelated error message in production on 2026-08-12."""
        client = _ScriptedClient([asyncio.CancelledError()])
        with pytest.raises(asyncio.CancelledError):
            await retry_get(client, "https://example.test", retries=1, delay=0)
        assert client.calls == 1
