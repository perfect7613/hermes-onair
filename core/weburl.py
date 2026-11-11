"""Which web addresses the agent may hand to OBS's browser.

OBS's browser runs on the user's machine and inside their network, and the model can see what it
renders through a screenshot. So a link the model picks must be a public web page: ``http`` or
``https``, with a host that resolves only to public addresses. Loopback, private ranges,
link-local (including the cloud metadata address 169.254.169.254) and everything else that is not
globally routable is refused.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Callable, Iterable, Optional
from urllib.parse import urlsplit

from .errors import InvalidInput

_HINT = "Use a public http(s) address. To show a local page, add it in OBS yourself."


def _resolve(host: str) -> Iterable[str]:
    return {info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    mapped = getattr(ip, "ipv4_mapped", None)
    return (mapped or ip).is_global


def public_web_url(url: str, resolve: Optional[Callable[[str], Iterable[str]]] = None) -> str:
    """Return ``url`` if it is a public web page, else raise :class:`InvalidInput`."""
    url = url.strip()
    # Browsers and Python disagree on odd addresses: Chromium reads "\\" as "/" and drops tabs and
    # newlines, so "http://127.0.0.1\\@example.com/" is example.com here and 127.0.0.1 there.
    # Anything the two could read differently is refused rather than interpreted.
    if "\\" in url or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url):
        raise InvalidInput("Web addresses with backslashes, spaces or control characters are not accepted.", hint=_HINT)
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        parts.port  # raises on a malformed port
    except ValueError as exc:
        raise InvalidInput(f"'{url}' is not a web address.", hint=_HINT) from exc
    if "@" in parts.netloc:
        raise InvalidInput("Web addresses with a user name or password in them are not accepted.", hint=_HINT)
    if parts.scheme.lower() not in ("http", "https"):
        raise InvalidInput(f"Only http and https links can be opened in OBS, not '{parts.scheme or url}'.", hint=_HINT)
    if not host:
        raise InvalidInput(f"'{url}' has no host name.", hint=_HINT)
    try:
        addresses = [host] if _is_literal(host) else list((resolve or _resolve)(host))
    except (OSError, UnicodeError) as exc:
        raise InvalidInput(f"Could not look up '{host}'.", hint="Check the address and your connection.") from exc
    if not addresses or not all(_is_public(address) for address in addresses):
        raise InvalidInput(f"'{host}' is on this machine or a private network, so Hermes will not open it in OBS.", hint=_HINT)
    return url


def _is_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False
