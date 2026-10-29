"""Minimal stdlib HTTP helpers (honours HTTPS_PROXY, system CA store)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

USER_AGENT = "tennisscout/0.1 (research; non-commercial)"


@dataclass
class Response:
    url: str
    status: int | None
    body: bytes
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    def json(self):
        return json.loads(self.body.decode("utf-8"))


def get(url: str, params: dict | None = None, timeout: float = 20.0) -> Response:
    if params:
        url = f"{url}?{urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return Response(url, resp.status, resp.read())
    except urllib.error.HTTPError as e:
        return Response(url, e.code, e.read() or b"", f"HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        return Response(url, None, b"", f"{type(e).__name__}: {reason}")


def probe(url: str) -> dict:
    """Record whether a host is reachable from here, as a measured fact for the audit."""
    r = get(url, timeout=15.0)
    return {"url": url, "reachable": r.status is not None, "status": r.status, "error": r.error}
