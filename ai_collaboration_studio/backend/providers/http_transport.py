"""Bounded transport primitives for text-generation providers."""
from __future__ import annotations

import urllib.request
from typing import Any


MAX_PROVIDER_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PROVIDER_ERROR_BYTES = 64 * 1024


class NoProviderRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A credentialed inference/probe is exactly one authorized HTTP request.
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(NoProviderRedirectHandler())


def open_provider_request(request: urllib.request.Request, *, timeout: float) -> Any:
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


class ProviderResponseLimitError(ValueError):
    """An upstream response cannot be safely consumed as bounded bytes."""


def read_provider_response(response: Any, maximum: int) -> bytes:
    """Read at most one byte beyond the limit, before decoding or parsing.

    Do not fall back to an unbounded read for non-conforming response wrappers.
    HTTPResponse and HTTPError both support the bounded read protocol.
    """
    raw = response.read(maximum + 1)
    if not isinstance(raw, bytes) or len(raw) > maximum:
        raise ProviderResponseLimitError("provider response exceeds the allowed size")
    return raw
