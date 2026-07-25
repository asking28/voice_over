"""Tiny urllib wrapper with retries — keeps the pipeline SDK-free.

The Cartesia/Deepgram Python SDKs are code-generated and their call shapes churn between
releases; the REST endpoints don't. Using urllib also means the whole pipeline runs on a
stock interpreter with no install step.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid


# Statuses where retrying — or continuing with the other 200 segments — is pointless:
# the key is bad, the plan is out of credits, or the account is blocked.
FATAL_STATUSES = (401, 402, 403)


class ApiError(RuntimeError):
    def __init__(self, service: str, status: int, detail: str):
        self.service, self.status, self.detail = service, status, detail
        super().__init__(f"{service} HTTP {status}: {detail[:600]}")

    @property
    def fatal(self) -> bool:
        return self.status in FATAL_STATUSES

    def brief(self) -> str:
        """One-line, human-facing version — API errors arrive as JSON blobs."""
        detail = self.detail.strip()
        try:
            data = json.loads(detail)
            if isinstance(data, dict):
                detail = str(
                    data.get("error") or data.get("message") or data.get("detail") or detail
                )
        except (ValueError, TypeError):
            pass
        return f"{self.service} HTTP {self.status}: {' '.join(detail.split())[:220]}"


def request(
    url: str,
    *,
    service: str,
    data: bytes | None = None,
    headers: dict | None = None,
    method: str = "GET",
    timeout: int = 180,
    retries: int = 3,
    backoff: float = 1.5,
) -> bytes:
    """Perform an HTTP request, retrying on 429/5xx and transient network errors."""
    last: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            last = ApiError(service, exc.code, detail)
            if exc.code not in (408, 429, 500, 502, 503, 504):
                raise last from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = RuntimeError(f"{service} network error: {exc}")
        if attempt < retries - 1:
            time.sleep(backoff * (2**attempt))
    raise last  # type: ignore[misc]


def request_json(url: str, **kwargs) -> dict:
    return json.loads(request(url, **kwargs).decode())


def multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    """Encode a multipart/form-data body. files maps name → (filename, content, mime)."""
    boundary = f"----revoice{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    for name, (filename, content, mime) in files.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n".encode()
        )
        parts.append(content)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
