"""fetch_page: the only local network tool, guarded against SSRF and prompt injection.

A URL is fetched only if it is https on port 443 with no userinfo, it is allowed for this run
(a static-allowlist domain or its subdomains, the posting's own exact host, or an exact URL that
web_search returned in this run), and every address its host resolves to is public. The request
then connects to the address the guard approved (pinned, with SNI and Host set to the real name and
the certificate checked against it), so there is no second DNS lookup to rebind. Connections are
never kept alive: the pool is keyed by the pinned IP, so a reused connection could carry a TLS
session made for another name at the same address. The peer address is re-checked after
connecting as defence in depth. Redirects are followed by hand and every hop is re-checked.
Compressed bodies are refused, bodies and total time are capped, HTML becomes text, and the result
is wrapped as untrusted data. Every failure surfaces as a generic FetchRefused.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from ..ingest.models import sanitize
from ..timeouts import run_with_timeout

MAX_REDIRECTS = 3
MAX_TEXT_CHARS = 12_000
ALLOWED_TYPES = ("text/html", "text/plain", "application/xhtml+xml")
USER_AGENT = "Mozilla/5.0 (compatible; research-fetch/1.0)"  # neutral: reveals nothing about the owner
_CGNAT = ipaddress.ip_network("100.64.0.0/10")  # the tailnet lives here
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_ANGLES = str.maketrans({c: "(" for c in "<‹＜〈⟨"} | {c: ")" for c in ">›＞〉⟩"})


class FetchRefused(Exception):
    """The guard refused a URL or the fetch failed. The message is safe to show to the model."""


def _apex(host: str) -> str:
    """'www.acme.io' and 'acme.io' are the same site for the homepage rule."""
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _strip_fragment(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, p.query, ""))


def _public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%")[0])
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        elif addr in _NAT64:
            addr = ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    return addr.is_global and addr not in _CGNAT


Resolver = Callable[[str], list[str]]


def system_resolver(host: str) -> list[str]:
    return sorted({ai[4][0] for ai in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})


@dataclass
class UrlGuard:
    """Per-run state: what may be fetched, and what was fetched (with its text)."""

    static_allowlist: tuple[str, ...]
    resolver: Resolver = system_resolver
    exact_hosts: set[str] = field(default_factory=set)  # the posting's own host (no subdomains)
    search_urls: set[str] = field(default_factory=set)  # exact URLs returned by web_search
    fetched: list[str] = field(default_factory=list)  # requested and final URLs that returned a page
    pages: dict[str, str] = field(default_factory=dict)  # url -> extracted text, for claim checks
    refused: list[tuple[str, str]] = field(default_factory=list)
    searches: int = 0

    def allow_host(self, host: str) -> None:
        if host:
            self.exact_hosts.add(host.lower().rstrip("."))

    def add_search_results(self, urls: Iterable[str]) -> None:
        self.search_urls.update(_strip_fragment(u) for u in urls)

    def _allowed(self, url: str, host: str) -> bool:
        if any(host == d or host.endswith("." + d) for d in self.static_allowlist):
            return True
        if host in self.exact_hosts or _strip_fragment(url) in self.search_urls:
            return True
        # The bare homepage of a host that search returned (no path, no query: nothing to smuggle out).
        parts = urlsplit(url)
        search_hosts = {_apex(urlsplit(u).hostname or "") for u in self.search_urls}
        return parts.path in ("", "/") and not parts.query and _apex(host) in search_hosts

    def check(self, url: str) -> tuple[str, str]:
        """Return (host, approved_ip) if the URL may be fetched; raise FetchRefused otherwise."""
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as e:
            raise FetchRefused("malformed URL") from e
        host = (parts.hostname or "").lower().rstrip(".")
        if parts.scheme != "https":
            raise FetchRefused("only https URLs may be fetched")
        if parts.username or parts.password or "\\" in parts.netloc:
            raise FetchRefused("URLs with credentials are not allowed")
        if port not in (None, 443):
            raise FetchRefused("only the default https port is allowed")
        if not host or not self._allowed(url, host):
            raise FetchRefused(f"host {host!r} is not allowed for this run")
        try:
            ips = self.resolver(host)
        except (OSError, UnicodeError, ValueError) as e:
            raise FetchRefused("host does not resolve") from e
        try:
            ok = bool(ips) and all(_public(ip) for ip in ips)
        except ValueError as e:
            raise FetchRefused("host resolves to an invalid address") from e
        if not ok:
            raise FetchRefused("host resolves to a private or internal address")
        return host, ips[0]


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "title", "iframe", "object"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self.depth = 0  # never let an unclosed skipped element swallow the body
        elif tag in self.SKIP:
            self.depth += 1
        elif tag in ("p", "br", "li", "div", "h1", "h2", "h3", "h4", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if not self.depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    p.close()
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.parts))
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def wrap_page(url: str, text: str) -> str:
    """Quote fetched text as data. All angle-like characters become parentheses inside the body,
    so page text can neither close the wrapper nor imitate it."""
    body = sanitize(text, MAX_TEXT_CHARS, multiline=True).translate(_ANGLES)
    safe_url = sanitize(url, 500).replace('"', "%22").translate(_ANGLES)
    return f'<web_page url="{safe_url}" trust="untrusted">\n{body}\n</web_page>'


def _peer_ip(response: httpx.Response) -> str | None:
    stream = response.extensions.get("network_stream")
    if stream is None:
        return None
    addr = stream.get_extra_info("server_addr")
    return addr[0] if addr else None


@dataclass
class Page:
    requested: str
    final: str
    text: str


def _fetch_once(
    url: str, guard: UrlGuard, client: httpx.Client, max_bytes: int, deadline: float, require_peer_check: bool
) -> Page:
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchRefused("page took too long")
        host, ip = guard.check(current)
        parts = urlsplit(current)
        ip_host = f"[{ip}]" if ":" in ip else ip
        pinned = urlunsplit(("https", ip_host, parts.path or "/", parts.query, ""))
        request = client.build_request(
            "GET",
            pinned,
            headers={"host": host},  # real name for the server...
            extensions={"sni_hostname": host},  # ...and for TLS SNI + certificate verification
            timeout=httpx.Timeout(min(remaining, 15.0)),
        )
        r = client.send(request, stream=True)
        try:
            peer = _peer_ip(r)
            if (peer is None and require_peer_check) or (peer is not None and not _public(peer)):
                raise FetchRefused("connected to a non-public address")
            if r.is_redirect:
                current = urljoin(current, r.headers.get("location", ""))
                continue
            if r.status_code >= 400:
                raise FetchRefused(f"HTTP {r.status_code}")
            if r.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                raise FetchRefused("compressed responses are not accepted")
            ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype not in ALLOWED_TYPES:
                raise FetchRefused(f"content type {ctype or 'unknown'} is not text")
            declared = r.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > 20 * max_bytes:
                raise FetchRefused("page too large")
            buf = bytearray()
            for chunk in r.iter_raw():  # raw: no decoder runs, so no decompression bomb
                buf += chunk
                if len(buf) >= max_bytes:
                    break  # a truncated page is still useful text
                if time.monotonic() > deadline:
                    raise FetchRefused("page took too long")
        finally:
            r.close()
        try:
            raw = bytes(buf[:max_bytes]).decode(r.encoding or "utf-8", errors="replace")
        except (LookupError, UnicodeError):
            raw = bytes(buf[:max_bytes]).decode("utf-8", errors="replace")
        text = html_to_text(raw) if ctype != "text/plain" else raw
        return Page(requested=url, final=current, text=text)
    raise FetchRefused("too many redirects")


def fetch_page_text(
    url: str,
    guard: UrlGuard,
    client: httpx.Client,
    *,
    max_bytes: int,
    deadline_s: float,
    require_peer_check: bool = True,
) -> Page:
    """Fetch with one hard wall-clock deadline over all hops, headers and body."""
    deadline = time.monotonic() + deadline_s
    try:
        page = run_with_timeout(
            lambda: _fetch_once(url, guard, client, max_bytes, deadline, require_peer_check), deadline_s + 1
        )
    except TimeoutError as e:  # before OSError: TimeoutError is a subclass of it
        raise FetchRefused("page took too long") from e
    except FetchRefused:
        raise
    except (httpx.HTTPError, ValueError, UnicodeError, OSError) as e:
        raise FetchRefused("fetch failed") from e  # never leak transport details to the model
    return page


def make_client(timeout_s: float) -> httpx.Client:
    return httpx.Client(
        timeout=timeout_s,
        follow_redirects=False,  # every hop is re-checked
        trust_env=False,  # no proxies or certs from the environment
        # Pinned requests share a pool key per IP, not per name: never reuse a connection (or its TLS session).
        limits=httpx.Limits(max_keepalive_connections=0),
        headers={"user-agent": USER_AGENT, "accept": "text/html,text/plain", "accept-encoding": "identity"},
    )
