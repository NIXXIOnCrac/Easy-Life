"""Self-signed TLS certificate for the phone PWA (opt-in HTTPS).

iOS Safari only hands the camera to a page in a *secure context*, so the in-app
QR scanner (and the phone PWA generally) needs `https://`. Easy Life therefore
serves a second, https-only listener next to its plain http one (`api.run`), and
this module owns the certificate that listener uses.

The certificate names every address the phone might reach the PC on — localhost,
127.0.0.1, the machine hostname and each LAN IP (`net.lan_ips`) — so a phone on
the same Wi-Fi never hits a name mismatch. It is self-signed, which means the
phone shows a warning until the user installs the certificate once
(`python -m pcrituals.cert --help` prints those steps).

Two generation backends, tried in order:
  * `cryptography` — the dependency shipped with the app, so the packaged
    Windows build works with no extra tools;
  * the `openssl` CLI — for source installs on Unix.
If neither is available the caller gets a `CertError` with a plain-language
message; HTTPS is opt-in, so that must never take the app down.
"""
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

from pcrituals.config import Config, load_config

# iOS refuses to trust user-installed certificates valid for more than 825 days,
# so a longer leaf would simply never be accepted.
VALIDITY_DAYS = 825

# Renew this long before expiry: a phone that already trusts the old certificate
# keeps working, and the user is not asked to re-install it mid-session.
RENEW_BEFORE = dt.timedelta(days=30)

# Shown as the certificate's subject (iOS lists the profile under this name).
SUBJECT_COMMON_NAME = "Easy Life"


class CertError(RuntimeError):
    """Certificate generation is impossible here (missing dependency/tool)."""


def cert_paths(cfg: Config) -> tuple[Path, Path]:
    return cfg.tls_cert_file, cfg.tls_key_file


def san_names() -> list[str]:
    """Every name the certificate must cover, in a stable order.

    The IPs come from `net.cert_ips()`, the SAME helper the pairing URL is
    built from, so the address the QR advertises is always one the certificate
    names. That includes any Tailscale address: without it, HTTPS over the
    tunnel would fail to validate and the phone's camera scanner would stop
    working exactly when the user leaves the house.
    """
    from pcrituals import net

    names: list[str] = ["localhost", "127.0.0.1"]
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = ""
    if hostname:
        names.append(hostname)
    for ip in net.cert_ips():
        if ip not in names:
            names.append(ip)
    return names


def split_names(names: Iterable[str]) -> tuple[list[str], list[str]]:
    """Split SAN entries into (dns names, IP addresses).

    A hostname that is not a valid IP still goes in as DNS: `socket.gethostname()`
    returns things like `DESKTOP-ABC123`, and a certificate that lists them as
    IPs would fail to be created at all.
    """
    dns: list[str] = []
    ips: list[str] = []
    for name in names:
        try:
            ipaddress.ip_address(name)
        except ValueError:
            if name not in dns:
                dns.append(name)
        else:
            if name not in ips:
                ips.append(name)
    return dns, ips


# ---- generation backends -------------------------------------------------------

def _generate_with_cryptography(cert_path: Path, key_path: Path,
                                names: list[str]) -> None:
    """Write a self-signed cert/key pair with the `cryptography` package."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    dns, ips = split_names(names)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    alt: list[x509.GeneralName] = [x509.DNSName(n) for n in dns]
    alt += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips]

    now = dt.datetime.now(dt.timezone.utc)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, SUBJECT_COMMON_NAME),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, SUBJECT_COMMON_NAME),
    ])
    # CA:TRUE so the same certificate can be the trust anchor the phone installs
    # *and* the leaf the server presents; that is the one-certificate flow the
    # iPhone instructions describe. Browsers only accept it once it is trusted.
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))  # tolerate clock skew
        .not_valid_after(now + dt.timedelta(days=VALIDITY_DAYS))
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_encipherment=True, key_cert_sign=True,
            content_commitment=False, data_encipherment=False,
            key_agreement=False, crl_sign=False, encipher_only=False,
            decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                       critical=False)
    )
    cert = builder.sign(key, hashes.SHA256())

    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _openssl_config(names: list[str]) -> str:
    """An openssl config carrying the SANs (the only place `req -x509` takes
    them; a command line of `-addext` is not available on older 1.x builds)."""
    dns, ips = split_names(names)
    lines = [
        "[req]",
        "distinguished_name = dn",
        "x509_extensions = v3",
        "prompt = no",
        "[dn]",
        f"CN = {SUBJECT_COMMON_NAME}",
        f"O = {SUBJECT_COMMON_NAME}",
        "[v3]",
        "basicConstraints = critical,CA:TRUE",
        "keyUsage = critical,digitalSignature,keyEncipherment,keyCertSign",
        "extendedKeyUsage = serverAuth",
        "subjectAltName = @alt",
        "[alt]",
    ]
    lines += [f"DNS.{i} = {n}" for i, n in enumerate(dns, 1)]
    lines += [f"IP.{i} = {n}" for i, n in enumerate(ips, 1)]
    return "\n".join(lines) + "\n"


def _generate_with_openssl(cert_path: Path, key_path: Path,
                           names: list[str]) -> None:
    """Write a self-signed cert/key pair using the `openssl` CLI."""
    openssl = shutil.which("openssl")
    if not openssl:
        raise CertError(
            "Found neither the 'cryptography' Python package nor an 'openssl' "
            "command. Install one of them (pip install cryptography) to use "
            "HTTPS."
        )
    workdir = Path(tempfile.mkdtemp(prefix="pcrituals_tls_"))
    try:
        conf = workdir / "openssl.cnf"
        conf.write_text(_openssl_config(names))
        proc = subprocess.run(
            [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256",
             "-days", str(VALIDITY_DAYS), "-config", str(conf),
             "-keyout", str(key_path), "-out", str(cert_path)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0 or not cert_path.exists():
            raise CertError(
                "openssl could not create the certificate: "
                + (proc.stderr or proc.stdout or "unknown error").strip()
            )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    _restrict(key_path)


def _restrict(path: Path) -> None:
    """Best effort: the key is a secret, keep it owner-only where that exists."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def generate(cert_path: Path, key_path: Path, names: list[str] | None = None) -> None:
    """Create a fresh self-signed certificate, replacing any existing one."""
    names = list(san_names() if names is None else names)
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import cryptography  # noqa: F401
    except ImportError:
        _generate_with_openssl(cert_path, key_path, names)
    else:
        _generate_with_cryptography(cert_path, key_path, names)
    _restrict(key_path)


# ---- inspection -----------------------------------------------------------------

def cert_expiry(cert_path: Path) -> dt.datetime | None:
    """When the certificate stops being valid, or None if that cannot be read."""
    try:
        from cryptography import x509

        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        return cert.not_valid_after_utc
    except ImportError:
        pass
    except Exception:
        return None
    # No `cryptography`: ask openssl, which is what generated it in that case.
    openssl = shutil.which("openssl")
    if not openssl:
        return None
    try:
        proc = subprocess.run(
            [openssl, "x509", "-in", str(cert_path), "-noout", "-enddate"],
            capture_output=True, text=True,
        )
        raw = proc.stdout.strip().split("=", 1)[1]
        # e.g. "notAfter=Aug  1 12:00:00 2027 GMT"
        return dt.datetime.strptime(raw, "%b %d %H:%M:%S %Y %Z").replace(
            tzinfo=dt.timezone.utc)
    except Exception:
        return None


def cert_san_names(cert_path: Path) -> list[str] | None:
    """The SANs actually in the certificate, or None when unreadable."""
    try:
        from cryptography import x509

        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        try:
            san = cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName).value
        except x509.ExtensionNotFound:
            return []
        return [str(n.value) for n in san]  # DNSName and IPAddress both stringify
    except ImportError:
        pass
    except Exception:
        return None
    openssl = shutil.which("openssl")
    if not openssl:
        return None
    try:
        # `-text` rather than `-ext`: older openssl builds have no `-ext`, and
        # the readable dump is the one format both of them share.
        proc = subprocess.run(
            [openssl, "x509", "-in", str(cert_path), "-noout", "-text"],
            capture_output=True, text=True,
        )
        text = proc.stdout
        if "Subject Alternative Name" not in text:
            return []
        names: list[str] = []
        for name in re.findall(r"(?:DNS|IP Address):\s*([^,\s]+)", text):
            if name not in names:
                names.append(name)
        return names
    except Exception:
        return None


def is_usable(cert_path: Path, key_path: Path, names: list[str] | None = None) -> bool:
    """True when an existing pair can be reused instead of regenerated.

    Missing or unreadable files are not reusable. So is a certificate that is
    within `RENEW_BEFORE` of expiry, or one whose SANs no longer cover the
    addresses this machine has today — a user who moves the PC to another
    network, or whose router hands out a new address, would otherwise get a
    certificate error on the phone with no way to understand why.
    """
    if not cert_path.exists() or not key_path.exists():
        return False
    expiry = cert_expiry(cert_path)
    if expiry is None:
        return False
    now = dt.datetime.now(dt.timezone.utc)
    if expiry - now <= RENEW_BEFORE:
        return False
    covered = cert_san_names(cert_path)
    if covered is None:
        return True  # unreadable SANs, but the pair exists and is in date
    wanted = set(san_names() if names is None else names)
    return wanted.issubset(set(covered))


def ensure_certificate(cfg: Config, *, force: bool = False) -> tuple[Path, Path]:
    """Return (cert, key) for `cfg`, generating them the first time.

    Later runs reuse the files, so the phone only ever has to trust the
    certificate once.
    """
    cert_path, key_path = cert_paths(cfg)
    if force or not is_usable(cert_path, key_path):
        cfg.ensure_dirs()
        generate(cert_path, key_path)
    return cert_path, key_path


# ---- turning it on --------------------------------------------------------------

def set_enabled(cfg: Config, enabled: bool) -> dict:
    """Persist the opt-in switch in settings.json, keeping other keys.

    The certificate is generated straight away when switching on, so the user
    can install it on the phone before starting the app.
    """
    current: dict = {}
    if cfg.settings_file.exists():
        try:
            current = json.loads(cfg.settings_file.read_text()) or {}
        except Exception:
            current = {}
    current["tls_enabled"] = bool(enabled)
    cfg.ensure_dirs()
    cfg.settings_file.write_text(json.dumps(current, indent=2))
    cfg.tls_enabled = bool(enabled)
    return current


def https_urls(cfg: Config) -> list[str]:
    from pcrituals import net

    return net.lan_urls(cfg.tls_port, scheme="https")


# ---- CLI ------------------------------------------------------------------------

_TRUST_STEPS = """\
  1. Copy the certificate to the iPhone (AirDrop, or mail it to yourself):
       python -m pcrituals.cert --export ~/easylife.crt
  2. Tap the file on the phone -> Install Profile (enter the passcode).
  3. Settings > General > About > Certificate Trust Settings.
  4. Turn ON full trust for "Easy Life".
  5. Open the https:// address printed above in Safari and allow the camera."""


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pcrituals.cert",
        description="Easy Life HTTPS certificate: enable it, inspect it, "
                    "or export it for the phone.",
    )
    parser.add_argument("--print-ip", action="store_true",
                        help="show the detected LAN address(es) and the https URL")
    parser.add_argument("--enable", action="store_true",
                        help="turn HTTPS on persistently (settings.json)")
    parser.add_argument("--disable", action="store_true",
                        help="turn HTTPS off again")
    parser.add_argument("--generate", action="store_true",
                        help="(re)create the certificate now")
    parser.add_argument("--force", action="store_true",
                        help="with --generate: replace an existing certificate")
    parser.add_argument("--export", metavar="PATH",
                        help="copy the certificate to PATH for the phone")
    args = parser.parse_args(argv)

    cfg = load_config()
    cert_path, key_path = cert_paths(cfg)

    if args.enable:
        set_enabled(cfg, True)
        print("HTTPS is now ON (saved in settings.json). Next restart of Easy "
              "Life will serve it on port "
              f"{cfg.tls_port}.")
    if args.disable:
        set_enabled(cfg, False)
        print("HTTPS is OFF. The app serves plain http again.")

    if args.generate or (args.enable and not cert_path.exists()):
        try:
            ensure_certificate(cfg, force=args.force)
        except CertError as exc:
            print(f"Could not create the certificate: {exc}", file=sys.stderr)
            return 1
        print(f"Certificate: {cert_path}")

    if args.export:
        if not cert_path.exists():
            try:
                ensure_certificate(cfg)
            except CertError as exc:
                print(f"Could not create the certificate: {exc}", file=sys.stderr)
                return 1
        dest = Path(args.export).expanduser()
        shutil.copyfile(cert_path, dest)
        print(f"Copied the certificate to {dest} — transfer it to the iPhone "
              "and install it as a profile.")

    if args.print_ip or not any([args.enable, args.disable, args.generate,
                                 args.export]):
        from pcrituals import __version__, net

        print(f"Easy Life {__version__}")
        ips = net.lan_ips()
        print("  LAN IP  : " + (", ".join(ips) if ips else "(not detected)"))
        urls = https_urls(cfg)
        print("  HTTPS   : " + (", ".join(urls) if urls else "(no LAN address)"))
        print(f"  State   : {'ON' if cfg.tls_enabled else 'OFF'} "
              f"(port {cfg.tls_port})")
        print(f"  Cert    : {cert_path}"
              + ("" if cert_path.exists() else "  (not created yet)"))
        if cert_path.exists():
            expiry = cert_expiry(cert_path)
            print(f"  Expires : {expiry.date().isoformat() if expiry else 'unknown'}")
        print("\n  Turn HTTPS on : python -m pcrituals.cert --enable")
        print("  (phone side, once per phone)")
        print(_TRUST_STEPS)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
