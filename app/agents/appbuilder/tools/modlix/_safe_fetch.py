"""Fetching an agent-supplied URL without opening an SSRF hole.

A server that fetches whatever URL a model hands it can be pointed at the
server's own network. "Any public URL" is the intended capability; reaching the
gateway on localhost:8080, a database on a private subnet, or the cloud metadata
endpoint at 169.254.169.254 is not, and that last one hands out instance
credentials to anyone who can make the server request it.

The guard is not a restriction on which 3D asset libraries are allowed. Every
genuinely public URL still works. What it blocks is the set of addresses that
only exist from inside.

Three things have to be true together, and dropping any one of them reopens the
hole:

1. **Resolve first, check the resolved ADDRESS.** Checking the hostname is
   useless: `metadata.example.com` can resolve to 169.254.169.254, and so can a
   hostname the attacker controls.
2. **Check the address actually connected to, before reading the body.** A
   pre-flight DNS check alone leaves a rebind window: DNS can answer differently
   between the check and the connection. The obvious fix -- connecting to the
   literal IP -- breaks TLS, because the certificate is issued for the hostname
   and verification uses the URL's host, not the `Host` header. So the
   connection is made normally, and httpx's `network_stream` is asked what peer
   it reached; a private one aborts the request with the body unread.
3. **Re-check every redirect.** A public URL that 302s to
   `http://169.254.169.254/` defeats a check done only on the first hop, so
   redirects are followed manually with the full check repeated each time.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Any

import httpx


MAX_REDIRECTS = 5
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_TIMEOUT = 30.0


class BlockedURL(Exception):
    """The URL resolves somewhere the server must not reach."""


@dataclass
class FetchedAsset:
    content: bytes
    content_type: str
    final_url: str
    """The URL actually fetched, after redirects. Recorded as provenance:
    where a file came from is not the same as where it was asked for."""
    redirects: list[str]


def _describe(ip: ipaddress._BaseAddress) -> str | None:
    """Why this address is off limits, or None if it is fine."""
    if ip.is_loopback:
        return "a loopback address (the server itself)"
    if ip.is_private:
        return "a private address (inside this network)"
    if ip.is_link_local:
        # 169.254.0.0/16 is link-local, and 169.254.169.254 inside it is the
        # cloud metadata endpoint that hands out instance credentials.
        return "a link-local address (this includes the cloud metadata endpoint)"
    if ip.is_multicast:
        return "a multicast address"
    if ip.is_reserved or ip.is_unspecified:
        return "a reserved address"
    if getattr(ip, "is_site_local", False):
        return "a site-local address"
    # An IPv4 address expressed as IPv6 (::ffff:127.0.0.1) passes every check
    # above while still being loopback, so it is unwrapped and re-checked.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        inner = _describe(mapped)
        if inner:
            return f"{inner}, written as an IPv6-mapped address"
    return None


def check_address(addr: str, *, context: str) -> None:
    """Raise if this literal address is one the server must not reach."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return
    reason = _describe(ip)
    if reason:
        raise BlockedURL(
            f"{context} {addr}, which is {reason}. Only public addresses can be "
            f"fetched. This is not a restriction on which asset sources are allowed -- "
            f"every genuinely public URL works."
        )


def resolve_and_check(host: str, port: int) -> list[str]:
    """Every address this host resolves to, or raise if ANY is off limits.

    All of them, not just the first: a host that resolves to one public and one
    private address would otherwise be allowed through and then connected to the
    private one on a retry.
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise BlockedURL(f"{host!r} does not resolve: {e}") from e

    addrs: list[str] = []
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        check_address(addr, context=f"{host!r} resolves to")
        if addr not in addrs:
            addrs.append(addr)
    if not addrs:
        raise BlockedURL(f"{host!r} resolved to no usable address.")
    return addrs


def _split(url: str) -> tuple[str, str, int, str]:
    """scheme, host, port, path-and-query — refusing anything but http(s)."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedURL(
            f"Only http and https can be fetched, not {parts.scheme!r}. "
            f"file:, gopher: and the rest are ways to read the server's own disk."
        )
    if not parts.hostname:
        raise BlockedURL(f"{url!r} has no host.")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    tail = parts.path or "/"
    if parts.query:
        tail += "?" + parts.query
    return parts.scheme, parts.hostname, port, tail


async def fetch_public_url(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: float = DEFAULT_TIMEOUT,
) -> FetchedAsset:
    """GET a public URL, refusing anything that resolves inside the network.

    Redirects are followed BY HAND, with the whole check repeated on each hop.
    httpx's own redirect following would connect to the new location without
    re-checking, so a public URL that 302s to 169.254.169.254 would walk
    straight past a check done only on the first request.
    """
    from urllib.parse import urljoin

    seen: list[str] = []
    current = url

    for _hop in range(MAX_REDIRECTS + 1):
        scheme, host, port, _tail = _split(current)
        # Pre-flight: refuse before a single packet leaves, so an obviously
        # internal target never reaches the network at all.
        resolve_and_check(host, port)

        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": "Modlix-AppBuilder/1.0"},
        ) as client:
            try:
                async with client.stream("GET", current) as resp:
                    # Post-connect: what did we ACTUALLY reach? This is what
                    # closes the rebind window the pre-flight leaves open, and
                    # it runs before a single byte of the body is read.
                    stream = resp.extensions.get("network_stream")
                    peer = stream.get_extra_info("server_addr") if stream else None
                    if peer:
                        check_address(str(peer[0]), context=f"{current} connected to")
                    else:
                        # Loud, not silent: without the peer address the only
                        # protection left is the pre-flight, and saying so is
                        # better than implying a guarantee that is not there.
                        seen.append(f"(peer address unavailable for {current})")

                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("location")
                        if not loc:
                            raise BlockedURL(f"{current} redirected with no Location header.")
                        seen.append(current)
                        current = urljoin(current, loc)
                        continue
                    if resp.status_code >= 400:
                        raise BlockedURL(f"{current} answered {resp.status_code}.")

                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise BlockedURL(
                            f"{current} declares {int(declared):,} bytes, over the "
                            f"{max_bytes:,} cap."
                        )

                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        # Counted as it arrives, not taken from Content-Length:
                        # a server can understate that header, and a cap that
                        # trusts it is not a cap.
                        if total > max_bytes:
                            raise BlockedURL(
                                f"{current} went over the {max_bytes:,} byte cap while "
                                f"downloading."
                            )
                        chunks.append(chunk)

                    return FetchedAsset(
                        content=b"".join(chunks),
                        content_type=(resp.headers.get("content-type") or "").split(";")[0].strip(),
                        final_url=current,
                        redirects=seen,
                    )
            except httpx.HTTPError as e:
                raise BlockedURL(f"Could not fetch {current}: {type(e).__name__}: {e}") from e

    raise BlockedURL(f"More than {MAX_REDIRECTS} redirects starting from {url}.")


# ── Format validation ────────────────────────────────────────────────────


def looks_like_gltf(data: bytes) -> tuple[bool, str]:
    """Is this really a glTF 2.0 binary or JSON?

    The HEADER is parsed, not the extension. A file called `model.glb` that is
    actually an HTML error page loads as nothing, and the author's only clue is
    an empty canvas.
    """
    if len(data) < 12:
        return False, "the file is too short to be a model."
    if data[:4] == b"glTF":
        import struct
        version = struct.unpack("<I", data[4:8])[0]
        if version != 2:
            return False, f"this is glTF version {version}; only 2.0 is supported."
        return True, "glTF 2.0 binary (.glb)"
    head = data[:200].lstrip()
    if head.startswith(b"{") and b'"asset"' in data[:4000]:
        return True, "glTF 2.0 JSON (.gltf)"
    if head[:15].lower().startswith(b"<!doctype html") or head[:5].lower() == b"<html":
        return False, (
            "this is an HTML page, not a model. The URL probably needs a login, or it is "
            "a viewer page rather than the file itself."
        )
    return False, "this does not look like a glTF file (no 'glTF' magic and no JSON asset block)."


def looks_like_hdr(data: bytes) -> tuple[bool, str]:
    if data[:10].startswith(b"#?RADIANCE") or data[:6].startswith(b"#?RGBE"):
        return True, "Radiance HDR"
    return False, "this does not look like a Radiance .hdr file."


def looks_like_image(data: bytes) -> tuple[bool, str]:
    sigs = [
        (b"\x89PNG\r\n\x1a\n", "PNG"),
        (b"\xff\xd8\xff", "JPEG"),
        (b"GIF87a", "GIF"),
        (b"GIF89a", "GIF"),
        (b"RIFF", "WebP"),
    ]
    for sig, label in sigs:
        if data.startswith(sig):
            return True, label
    if data[:100].lstrip().startswith(b"<svg") or b"<svg" in data[:400]:
        return True, "SVG"
    return False, "this does not look like an image."


VALIDATORS = {
    "model": looks_like_gltf,
    "hdri": looks_like_hdr,
    "image": looks_like_image,
}
