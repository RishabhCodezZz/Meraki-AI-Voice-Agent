"""Request-origin checks for the WebSocket.

Browsers do not apply the same-origin policy to WebSockets: any page a visitor
opens can dial `wss://this-host/ws` from their browser. Here that would spend
the visitor's own keys (or the server's, on a deployment that sets them) on a
page they never meant to talk to, so the upgrade checks who is asking.
"""

from __future__ import annotations

from urllib.parse import urlsplit


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
    if origin in extra:
        return True
    netloc = urlsplit(origin).netloc.lower()
    # An origin with no host ("null", "") must not match an empty Host header.
    return bool(netloc) and netloc == host.lower()
