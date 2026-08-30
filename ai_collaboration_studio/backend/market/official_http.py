"""Pre-connect redirect policy for fixed official HTTPS sources."""

from __future__ import annotations

import string
from collections.abc import Callable, Collection
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urljoin, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class OfficialHttpsRedirectHandler(HTTPRedirectHandler):
    """Reject an unsafe redirect before urllib sends the redirected request."""

    max_redirections = 5
    max_repeats = 2

    def __init__(
        self,
        allowed_hosts: Collection[str],
        *,
        url_validator: Callable[[str], bool] | None = None,
    ) -> None:
        super().__init__()
        if type(allowed_hosts) not in {set, frozenset, list, tuple}:
            raise TypeError("allowed_hosts must be a native collection")
        hosts: list[str] = []
        for host in allowed_hosts:
            if (
                type(host) is not str
                or not host
                or host != host.lower()
                or urlparse(f"https://{host}").hostname != host
                or host in hosts
            ):
                raise ValueError("allowed_hosts contains a noncanonical host")
            hosts.append(host)
        if not hosts:
            raise ValueError("allowed_hosts must not be empty")
        if url_validator is not None and not callable(url_validator):
            raise TypeError("url_validator must be callable")
        self._allowed_hosts = frozenset(hosts)
        self._url_validator = url_validator

    def validate_url(self, value: Any) -> str:
        if type(value) is not str or not value or value != value.strip():
            raise ValueError("official source URL must be a canonical native string")
        try:
            parsed = urlparse(value)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("official source URL has an invalid port") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname not in self._allowed_hosts
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or not parsed.path.startswith("/")
            or parsed.fragment
        ):
            raise ValueError("official source URL is outside the fixed HTTPS policy")
        if self._url_validator is not None and self._url_validator(value) is not True:
            raise ValueError("official source URL is outside the fixed endpoint policy")
        return value

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

    @staticmethod
    def _close_redirect_response(fp: Any) -> None:
        try:
            fp.close()
        except Exception:
            pass

    def http_error_302(self, req, fp, code, msg, headers):
        """Follow one checked redirect without draining its unbounded body."""

        try:
            if "location" in headers:
                newurl = headers["location"]
            elif "uri" in headers:
                newurl = headers["uri"]
            else:
                raise ValueError("official source redirect is missing Location")
            if type(newurl) is not str:
                raise ValueError("official source redirect Location must be text")
            urlparts = urlparse(newurl)
            if not urlparts.path and urlparts.netloc:
                mutable_parts = list(urlparts)
                mutable_parts[2] = "/"
                urlparts = urlparse(urlunparse(mutable_parts))
            normalized = quote(
                urlunparse(urlparts),
                encoding="iso-8859-1",
                safe=string.punctuation,
            )
            normalized = urljoin(req.full_url, normalized)
            self.validate_url(normalized)
            new_request = self.redirect_request(
                req,
                fp,
                code,
                msg,
                headers,
                normalized,
            )
            if new_request is None:
                raise ValueError("official source redirect could not be represented")
            if hasattr(req, "redirect_dict"):
                visited = new_request.redirect_dict = req.redirect_dict
                if (
                    visited.get(normalized, 0) >= self.max_repeats
                    or len(visited) >= self.max_redirections
                ):
                    raise HTTPError(
                        req.full_url,
                        code,
                        self.inf_msg + msg,
                        headers,
                        fp,
                    )
            else:
                visited = new_request.redirect_dict = req.redirect_dict = {}
            visited[normalized] = visited.get(normalized, 0) + 1
        except Exception:
            self._close_redirect_response(fp)
            raise

        self._close_redirect_response(fp)
        return self.parent.open(new_request, timeout=req.timeout)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def open_official_https(
    request: Request,
    *,
    allowed_hosts: Collection[str],
    timeout: float,
    url_validator: Callable[[str], bool] | None = None,
):
    """Open one fixed official request with every redirect checked first."""

    if type(request) is not Request:
        raise TypeError("request must be an exact urllib.request.Request")
    if type(timeout) not in {int, float} or type(timeout) is bool or timeout <= 0:
        raise ValueError("timeout must be a positive native number")
    redirect_policy = OfficialHttpsRedirectHandler(
        allowed_hosts,
        url_validator=url_validator,
    )
    redirect_policy.validate_url(request.full_url)
    opener = build_opener(redirect_policy)
    response = opener.open(request, timeout=timeout)
    try:
        redirect_policy.validate_url(response.geturl())
    except Exception:
        response.close()
        raise
    return response


__all__ = ["OfficialHttpsRedirectHandler", "open_official_https"]
