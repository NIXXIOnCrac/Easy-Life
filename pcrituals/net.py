"""Local network helpers: detect the host's LAN IP address(es)."""
from __future__ import annotations

import socket

PRIVATE_PREFIXES = ("192.168.", "10.", "172.")


def lan_ip() -> str | None:
    """Return the host's most likely LAN IPv4 address, or None if undetectable.

    Strategy: open a UDP socket to a public address (no packets actually sent —
    this just makes the OS pick the outbound interface) and read the source IP.
    Falls back to enumerating host interfaces.
    """
    # Fast, reliable trick: connect() to a public IP with UDP picks the route.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            return ip
        finally:
            s.close()
    except Exception:
        pass

    # Fallback: enumerate interfaces, prefer private-range IPs.
    try:
        candidates = socket.gethostbyname_ex(socket.gethostname())[2]
    except Exception:
        candidates = []
    for ip in candidates:
        if ip.startswith(PRIVATE_PREFIXES):
            return ip
    return candidates[0] if candidates else None


def lan_ips() -> list[str]:
    """Return each private LAN IPv4 address this host answers on.

    A self-signed certificate has to name every address the phone might use, so
    the addresses are collected once here and both the URLs and the certificate
    Subject Alternative Names are built from the same list.
    """
    ips: list[str] = []
    try:
        host = socket.gethostname()
        for ip in socket.gethostbyname_ex(host)[2]:
            if ip.startswith(PRIVATE_PREFIXES) and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    ip = lan_ip()
    if ip and ip not in ips:
        ips.append(ip)
    return ips


def lan_urls(port: int, scheme: str = "http") -> list[str]:
    """Return ['<scheme>://<ip>:<port>'...] for each private LAN address."""
    return [f"{scheme}://{ip}:{port}" for ip in lan_ips()]


# --------------------------------------------------------------------------
# Who is the caller, really?
# --------------------------------------------------------------------------
# A tunnel or reverse proxy runs ON this machine (cloudflared, a local nginx),
# so traffic that arrived from the internet has a loopback socket address. The
# desktop UI is trusted implicitly, so treating such a request as "local" would
# hand the entire API — running Plays, power control — to an anonymous caller
# with no credential at all. The socket address alone can therefore never mean
# "this is the desktop UI".
FORWARDING_HEADERS = (
    "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
    "cf-connecting-ip", "cf-ray", "x-real-ip", "forwarded",
)

LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost", "0.0.0.0")


def is_loopback_host(host: str) -> bool:
    return str(host or "").strip() in LOOPBACK_HOSTS


def _header(headers, name: str) -> str:
    """Case-insensitive header lookup that also works on a plain dict.

    Starlette's Headers is case-insensitive, but the trust check must not
    DEPEND on that: handed a plain dict, a naive `.get("x-forwarded-for")`
    silently finds nothing, reads as "no proxy", and re-opens the loopback
    bypass this module exists to close.
    """
    try:
        value = headers.get(name)
        if value:
            return str(value)
    except Exception:
        pass
    try:
        for key, value in headers.items():
            if str(key).lower() == name and value:
                return str(value)
    except Exception:
        pass
    return ""


def came_through_a_proxy(headers) -> bool:
    """True when a proxy/tunnel handled this request."""
    return any(_header(headers, h) for h in FORWARDING_HEADERS)


def client_is_local(request) -> bool:
    """True only when the request genuinely originated on this machine.

    Any forwarding header disqualifies a request from counting as local. Note
    the direction: a caller cannot spoof their way IN with this rule, because
    adding a header can only ever make a request look MORE remote — the check
    fails closed.
    """
    if came_through_a_proxy(getattr(request, "headers", {})):
        return False
    client = getattr(request, "client", None)
    return is_loopback_host(getattr(client, "host", ""))


def client_ip_for_limits(request) -> str:
    """The key a rate limit or lockout should be counted against.

    Through a tunnel every visitor shares the loopback socket address, so the
    forwarded address is used when — and only when — the request arrived over
    loopback. On a direct connection the header is attacker-controlled, and
    honouring it would let someone rotate a spoofed value to dodge the
    pairing-code rate limit.
    """
    client = getattr(request, "client", None)
    host = str(getattr(client, "host", "") or "unknown")
    if is_loopback_host(host):
        headers = getattr(request, "headers", {})
        forwarded = (_header(headers, "cf-connecting-ip")
                     or _header(headers, "x-forwarded-for"))
        first = str(forwarded).split(",")[0].strip()
        if first:
            return first
    return host


# --------------------------------------------------------------------------
# Remote access (Tailscale)
# --------------------------------------------------------------------------
# Tailscale hands out addresses from 100.64.0.0/10 — the CGNAT range, NOT
# RFC1918 space. That is precisely why a plain "is this a private IP?" test
# missed the tunnel: the address that reaches this PC from anywhere looked
# like a public one and got filtered out, so the phone never learned it.
TAILSCALE_EXE_CANDIDATES = (
    r"C:\Program Files\Tailscale\tailscale.exe",
    r"C:\Program Files (x86)\Tailscale\tailscale.exe",
)


def _ipv4(value: str) -> str | None:
    """Normalise an IPv4 string, or None if it isn't one."""
    parts = str(value or "").strip().split(".")
    if len(parts) != 4:
        return None
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if any(n < 0 or n > 255 for n in nums):
        return None
    return ".".join(str(n) for n in nums)


def is_tailscale_ip(ip: str) -> bool:
    """True for Tailscale/CGNAT addresses (100.64.0.0 - 100.127.255.255)."""
    norm = _ipv4(ip)
    if not norm:
        return False
    first, second = (int(x) for x in norm.split(".")[:2])
    return first == 100 and 64 <= second <= 127


def _candidate_ips() -> list[str]:
    """Every IPv4 address this host answers on, LAN and otherwise."""
    ips: list[str] = []
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            norm = _ipv4(ip)
            if norm and norm not in ips:
                ips.append(norm)
    except Exception:
        pass
    routed = _ipv4(lan_ip() or "")
    if routed and routed not in ips:
        ips.append(routed)
    return ips


def _tailscale_cli_ips() -> list[str]:
    """Ask the Tailscale CLI, which is authoritative.

    Interface enumeration can miss the adapter on some Windows installs, so the
    CLI is consulted as well. Absent/slow/failing is normal and not an error —
    the user simply may not have Tailscale.
    """
    import os
    import shutil
    import subprocess

    exe = shutil.which("tailscale") or shutil.which("tailscale.exe")
    if not exe:
        for cand in TAILSCALE_EXE_CANDIDATES:
            if os.path.exists(cand):
                exe = cand
                break
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True,
                             timeout=3)
    except Exception:
        return []
    if out.returncode != 0:
        return []
    return [n for n in (_ipv4(line) for line in out.stdout.splitlines()) if n]


def tailscale_ips() -> list[str]:
    """Addresses that reach this PC from anywhere, empty when Tailscale is off."""
    ips = [ip for ip in _candidate_ips() if is_tailscale_ip(ip)]
    for ip in _tailscale_cli_ips():
        if ip not in ips:
            ips.append(ip)
    return ips


def cert_ips() -> list[str]:
    """Every IP the TLS certificate must cover."""
    ips = list(lan_ips())
    for ip in tailscale_ips():
        if ip not in ips:
            ips.append(ip)
    return ips


def pairing_urls(port: int, scheme: str = "http") -> list[str]:
    """Phone-facing URLs, best first.

    A Tailscale address works both at home and away, so it is advertised ahead
    of the LAN address: the phone then stores ONE address and keeps working when
    it leaves the house. LAN addresses follow as a fallback for a phone that has
    not joined the tailnet yet.
    """
    tail = [f"{scheme}://{ip}:{port}" for ip in tailscale_ips()]
    return tail + [u for u in lan_urls(port, scheme=scheme) if u not in tail]