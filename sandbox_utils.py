"""Shared OpenSandbox helpers.

`_parse_sandbox_endpoint` bridges the gap between OPENSANDBOX_SERVER
(a full URL env var, used by lifecycle HTTP calls) and the SDK's
ConnectionConfig (which takes domain + protocol separately).
"""

from urllib.parse import urlparse


def _parse_sandbox_endpoint(url: str) -> tuple[str, str]:
    """Parse an OpenSandbox server URL into (domain, protocol).

    Falls back to ``127.0.0.1:8080`` / ``http`` for empty input so callers
    don't need their own guard.
    """
    parsed = urlparse(url or "http://127.0.0.1:8080")
    protocol = parsed.scheme or "http"
    domain = parsed.netloc or "127.0.0.1:8080"
    return domain, protocol
