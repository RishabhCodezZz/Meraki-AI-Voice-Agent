"""Request-origin checks for the WebSocket, and response hardening for HTTP.

Browsers do not apply the same-origin policy to WebSockets: any page a visitor
opens can dial `wss://this-host/ws` from their browser. Here that would spend
the visitor's own keys (or the server's, on a deployment that sets them) on a
page they never meant to talk to, so the upgrade checks who is asking.

The middleware below are plain ASGI rather than `BaseHTTPMiddleware`, which wraps
the response in a task and queue (it interferes with streaming and cancellation)
and would also sit in front of the WebSocket for no benefit. Anything that is not
an HTTP request passes through untouched.
"""

from __future__ import annotations

from urllib.parse import urlsplit

# Everything is served from this origin except the font CSS and files, and the
# page dials its own WebSocket (ws: for local http, wss: when deployed). The
# capture worklet loads as a same-origin module, which script-src and worker-src
# 'self' cover. No inline script or style is needed, so 'unsafe-inline' is absent.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self' ws: wss:; "
    "media-src 'self' blob:; "
    "worker-src 'self'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    # The page needs the microphone and nothing else.
    (b"permissions-policy", b"microphone=(self), camera=(), geolocation=()"),
)


def origin_allowed(origin: str | None, host: str, extra: set[str]) -> bool:
    """Is this WebSocket upgrade from us, or from somewhere we have approved?

    No Origin header means a non-browser client (curl, a test, a script). It
    cannot be a hijacked visitor, so it is let through - the check protects
    browsers, not the endpoint.

    Otherwise the Origin's host (and port) must equal the Host the request was
    sent to, or the whole origin must be listed in `extra`
    (`MERAKI_ALLOWED_ORIGINS`), for a front end hosted elsewhere.
    """
    if origin is None:
        return True
    # Compared case-insensitively and without a trailing slash, as config.py
    # normalises the list: an operator's "https://App.example.org/" must match
    # the lowercase, slashless Origin a browser sends.
    if origin.lower().rstrip("/") in {e.lower().rstrip("/") for e in extra}:
        return True
    netloc = urlsplit(origin).netloc.lower()
    # An origin with no host ("null", "") must not match an empty Host header.
    return bool(netloc) and netloc == host.lower()


class SecurityHeadersMiddleware:
    """Add the standard hardening headers to every HTTP response.

    Added only when absent, so a route that sets its own policy keeps it.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers.extend(h for h in SECURITY_HEADERS if h[0] not in present)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class StaticCacheMiddleware:
    """Make browsers revalidate /static instead of guessing a freshness window.

    Only the entry points carry `?v=`; the ES modules they import and the audio
    worklet do not, and with no Cache-Control a browser may reuse them for days
    after a deploy. `no-cache` still allows a cheap 304 via ETag/Last-Modified.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/static/"):
            await self.app(scope, receive, send)
            return

        async def send_with_cache_control(message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if not any(name.lower() == b"cache-control" for name, _ in headers):
                    headers.append((b"cache-control", b"no-cache"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_cache_control)
