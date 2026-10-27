"""Bounded inference diagnostics without request objects, headers, or credentials.

Normal errors expose exception classes and HTTP status, never arbitrary provider
bodies. Explicit level-three debugging may show selected, redacted provider error
fields or model text; these diagnostics can still contain the caller's input data.
"""

from importlib import metadata
import json
import platform
import re
from urllib.parse import urlsplit


_SECRET_KEY = (
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|token|"
    r"password|passwd|secret|authorization|proxy-authorization|cookie|set-cookie)"
)
_QUOTED_SECRET = re.compile(
    rf'''(?i)(["']?\b{_SECRET_KEY}["']?\s*[:=]\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')'''
)
_BARE_SECRET = re.compile(rf'''(?i)(\b{_SECRET_KEY}\s*[:=]\s*)(?!["'])([^\s,;}}]+)''')
_AUTH_TOKEN = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/-]+={0,2}")
_URL = re.compile(r'''https?://[^\s<>"']+''', re.IGNORECASE)


def _url_origin(match: re.Match[str]) -> str:
    """Retain a useful endpoint origin, never userinfo, paths, or query secrets."""
    try:
        value = urlsplit(match.group())
        host = value.hostname
        if host is None:
            return "[URL redacted]"
        if ":" in host:
            host = f"[{host}]"
        port = f":{value.port}" if value.port is not None else ""
        return f"{value.scheme}://{host}{port}/[path redacted]"
    except ValueError:
        return "[URL redacted]"


def bounded_visible(value: object, *, limit: int = 2000) -> str:
    """Redact recognizable credentials, escape terminal controls, and cap text.

    This is intended for diagnostics, not for rendering executable commands.
    Redaction cannot recognize arbitrary secrets embedded in natural-language data.
    """
    text = str(value)
    text = _AUTH_TOKEN.sub(lambda match: match[1] + " [REDACTED]", text)
    text = _QUOTED_SECRET.sub(lambda match: match[1] + '"[REDACTED]"', text)
    text = _BARE_SECRET.sub(lambda match: match[1] + "[REDACTED]", text)
    text = _URL.sub(_url_origin, text)
    text = "".join(
        char if char in "\n\t" or (ord(char) >= 32 and not 127 <= ord(char) <= 159)
        else f"\\x{ord(char):02x}" for char in text
    )
    if len(text) > limit:
        text = text[:limit] + "\n[diagnostic truncated]"
    return text


def exception_chain(error: BaseException) -> list[BaseException]:
    """Inspect a short cause chain without materializing request/response objects."""
    chain = []
    seen = set()
    current = error
    while current is not None and id(current) not in seen and len(chain) < 5:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def safe_exception_detail(error: BaseException) -> str:
    """Only exception types and numeric HTTP statuses are safe at normal verbosity."""
    result = []
    for current in exception_chain(error):
        name = type(current).__name__
        status = getattr(current, "status_code", None)
        if type(status) is int and 100 <= status <= 599:
            name += f" (HTTP {status})"
        result.append(name)
    return " <- ".join(result)


def transport_hint(error: BaseException) -> str:
    """Provide actionable advice without echoing a potentially sensitive body."""
    chain = exception_chain(error)
    statuses = [getattr(item, "status_code", None) for item in chain]
    if any(status in {401, 403} for status in statuses):
        return "Check this Sheetbend source's configured authentication and access."
    if 404 in statuses:
        return "Check the configured endpoint and selected model identifier."
    if any(status in {400, 422} for status in statuses):
        return (
            "The endpoint rejected the structured request; check json_schema support and "
            "configured reasoning options. -vvv includes redacted provider error fields."
        )
    if 429 in statuses:
        return "The selected endpoint rejected the request for rate or capacity limits."
    if any(type(status) is int and status >= 500 for status in statuses):
        return "The selected endpoint reported a server error."
    names = " ".join(type(item).__name__.lower() for item in chain)
    if "timeout" in names:
        return "The request timed out; check endpoint health and Sheetbend's configured timeout."
    if "connect" in names:
        return "Check that the configured endpoint is reachable and its TLS settings are correct."
    return "Use -vv for stage/version diagnostics; -vvv may include input data in diagnostics."


def provider_error_detail(error: BaseException) -> str | None:
    """Selected SDK error fields for explicit debugging; never headers or requests."""
    for current in exception_chain(error):
        body = getattr(current, "body", None)
        if isinstance(body, dict):
            body = body.get("error", body)
            if isinstance(body, dict):
                body = {key: body[key] for key in ("message", "type", "code", "param") if key in body}
                if not body:
                    continue
                return bounded_visible(json.dumps(body, ensure_ascii=False, default=str), limit=3000)
        if isinstance(body, str) and body:
            return bounded_visible(body, limit=3000)
    return None


def runtime_versions(*, sheetbend_version: str) -> dict[str, str]:
    """Read installed distribution metadata only; do not probe any service."""
    versions = {"python": platform.python_version(), "sheetbend": sheetbend_version}
    for package in ("llm", "openai"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = "not installed"
    return versions
