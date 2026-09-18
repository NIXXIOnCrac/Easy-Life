"""Opt-in HTTPS: the phone PWA needs a secure origin for its QR camera scanner.

iOS Safari refuses `getUserMedia` outside a secure context, so the in-app QR
scanner only works when the phone opened the app over `https://`. Easy Life
therefore grew a *second*, opt-in https listener next to the plain http one that
the desktop window keeps using. Each test below pins one rule of that feature:

  * HTTPS is off by default, and turning it off leaves the pairing URL exactly
    as it was before the feature existed (http, plain port);
  * turning it on makes the pairing URL and the QR code advertise the secure
    origin on the TLS port, so scanning the code lands the phone on https;
  * the self-signed certificate covers every address the phone might use
    (localhost, 127.0.0.1, the hostname, every LAN IP) and lives in the app data
    dir, where both the source install and the packaged build look;
  * an existing certificate is reused, so the phone only trusts it once, and a
    machine without `cryptography` still gets a certificate via the openssl CLI
    — or a plain-language error when it has neither.

The names are faked here (LAN IPs, hostname) so the assertions do not depend on
the machine running the tests; nothing binds to anything but 127.0.0.1.
"""
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pcrituals import cert as cert_mod
from pcrituals import net
from pcrituals.config import Config, load_config

LAN_A = "192.168.7.31"
LAN_B = "10.0.0.9"
HOSTNAME = "DESKTOP-EASYLIFE"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A config with its own data dir and a deterministic set of names."""
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PCRITUALS_TLS", raising=False)
    monkeypatch.delenv("PCRITUALS_TLS_PORT", raising=False)
    monkeypatch.setattr(net, "lan_ips", lambda: [LAN_A, LAN_B])
    monkeypatch.setattr(socket, "gethostname", lambda: HOSTNAME)
    return Config(data_dir=tmp_path)


def _without_cryptography(monkeypatch):
    """Make `import cryptography` fail, as it does on a bare source install."""
    monkeypatch.setitem(sys.modules, "cryptography", None)


# ---- defaults and opt-in wiring ----------------------------------------------

def test_https_is_off_by_default():
    """Requirement 1: no existing install changes behaviour on upgrade."""
    c = Config()
    assert c.tls_enabled is False
    assert c.tls_port == 8443
    assert c.tls_host == ""


def test_env_var_turns_https_on(cfg, monkeypatch):
    monkeypatch.setenv("PCRITUALS_TLS", "1")
    monkeypatch.setenv("PCRITUALS_TLS_PORT", "9443")
    monkeypatch.delenv("PCRITUALS_HOST", raising=False)
    loaded = load_config()
    assert loaded.tls_enabled is True
    assert loaded.tls_port == 9443


def test_saved_settings_turn_https_on_and_keep_other_keys(cfg, monkeypatch):
    """`python -m pcrituals.cert --enable` is the non-technical switch."""
    (cfg.data_dir / "settings.json").write_text('{"update_url": "http://x/y"}')
    cert_mod.set_enabled(cfg, True)
    saved = (cfg.data_dir / "settings.json").read_text()
    assert '"tls_enabled": true' in saved
    assert '"update_url": "http://x/y"' in saved
    loaded = load_config()
    assert loaded.tls_enabled is True
    assert loaded.update_url == "http://x/y"

    cert_mod.set_enabled(cfg, False)
    assert load_config().tls_enabled is False


# ---- certificate generation ----------------------------------------------------

def test_certificate_lives_in_the_data_dir(cfg):
    cert_file, key_file = cert_mod.ensure_certificate(cfg)
    assert cert_file == cfg.tls_cert_file
    assert key_file == cfg.tls_key_file
    # Same place as the database, never the install directory.
    assert cert_file.parent == cfg.data_dir == cfg.db_file.parent
    assert cert_file.exists() and key_file.exists()


@pytest.mark.parametrize("backend", ["cryptography", "openssl"])
def test_certificate_covers_every_address_the_phone_might_use(cfg, monkeypatch,
                                                             backend):
    """Every LAN IP plus localhost/127.0.0.1/the hostname must be in the SANs —
    a name mismatch is an iOS Safari certificate error, not a warning."""
    if backend == "openssl":
        if not shutil.which("openssl"):
            pytest.skip("no openssl CLI on this machine")
        _without_cryptography(monkeypatch)

    cert_file, _ = cert_mod.ensure_certificate(cfg)
    names = set(cert_mod.cert_san_names(cert_file) or [])
    assert {LAN_A, LAN_B, HOSTNAME, "localhost", "127.0.0.1"} <= names


@pytest.mark.parametrize("backend", ["cryptography", "openssl"])
def test_certificate_is_a_real_certificate(cfg, monkeypatch, backend):
    """Readable by the TLS stack at all: in date, server-auth, self-consistent."""
    if backend == "openssl":
        if not shutil.which("openssl"):
            pytest.skip("no openssl CLI on this machine")
        _without_cryptography(monkeypatch)

    cert_file, key_file = cert_mod.ensure_certificate(cfg)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    expiry = cert_mod.cert_expiry(cert_file)
    assert expiry is not None and expiry.year >= time.gmtime().tm_year


def test_existing_certificate_is_reused(cfg):
    """The phone trusts the certificate once; a restart must not replace it."""
    cert_file, key_file = cert_mod.ensure_certificate(cfg)
    first = (cert_file.read_bytes(), key_file.read_bytes())
    before = (cert_file.stat().st_mtime_ns, key_file.stat().st_mtime_ns)

    again = cert_mod.ensure_certificate(cfg)

    assert again == (cert_file, key_file)
    assert (cert_file.read_bytes(), key_file.read_bytes()) == first
    assert (cert_file.stat().st_mtime_ns,
            key_file.stat().st_mtime_ns) == before


def test_certificate_is_replaced_when_it_is_near_expiry(cfg, monkeypatch):
    cert_file, _ = cert_mod.ensure_certificate(cfg)
    first = cert_file.read_bytes()
    soon = cert_mod.dt.datetime.now(cert_mod.dt.timezone.utc) + cert_mod.dt.timedelta(days=1)
    monkeypatch.setattr(cert_mod, "cert_expiry", lambda path: soon)
    cert_mod.ensure_certificate(cfg)
    assert cert_file.read_bytes() != first


def test_certificate_is_replaced_when_a_lan_ip_is_not_covered(cfg, monkeypatch):
    """A new address (new router, new network) must not silently break TLS."""
    cert_file, _ = cert_mod.ensure_certificate(cfg)
    first = cert_file.read_bytes()
    monkeypatch.setattr(net, "lan_ips", lambda: [LAN_A, LAN_B, "192.168.7.99"])
    cert_mod.ensure_certificate(cfg)
    assert cert_file.read_bytes() != first
    assert "192.168.7.99" in (cert_mod.cert_san_names(cert_file) or [])


def test_missing_key_regenerates_the_pair(cfg):
    cert_file, key_file = cert_mod.ensure_certificate(cfg)
    key_file.unlink()
    cert_mod.ensure_certificate(cfg)
    assert cert_file.exists() and key_file.exists()


def test_missing_openssl_and_no_cryptography_is_a_clear_error(cfg, monkeypatch):
    """Windows has no openssl; the message has to say what to install."""
    _without_cryptography(monkeypatch)
    monkeypatch.setattr(cert_mod.shutil, "which", lambda name: None)
    with pytest.raises(cert_mod.CertError) as exc:
        cert_mod.generate(cfg.tls_cert_file, cfg.tls_key_file)
    assert "cryptography" in str(exc.value)
    assert not cfg.tls_cert_file.exists()


def test_san_names_split_dns_from_ips():
    """`gethostname()` returns names like DESKTOP-ABC; those are DNS, not IPs,
    and feeding one to an IPAddress SAN makes generation fail outright."""
    assert cert_mod.split_names(["localhost", "127.0.0.1", HOSTNAME, LAN_A]) == (
        ["localhost", HOSTNAME], ["127.0.0.1", LAN_A])


# ---- pairing URL / QR ----------------------------------------------------------

def _pairing_app(monkeypatch, tmp_path):
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PCRITUALS_TLS", raising=False)
    monkeypatch.setattr(net, "lan_ips", lambda: [LAN_A, LAN_B])
    from pcrituals.api import create_app

    return create_app()


def test_pairing_url_is_unchanged_while_https_is_off(monkeypatch, tmp_path):
    """Requirement 7: with TLS off the advertised address is byte-identical to
    the plain-http behaviour that ships today."""
    app = _pairing_app(monkeypatch, tmp_path)
    ap = app.state.pcrituals
    assert ap.config.tls_enabled is False

    url = ap.pairing_base_url()
    assert url == f"http://{LAN_A}:{ap.config.port}"
    assert url == net.lan_urls(ap.config.port)[0]
    assert ap.create_pairing_code()["lan_url"] == url


def test_pairing_url_and_qr_switch_to_https_with_the_tls_port(monkeypatch, tmp_path):
    """Requirement 7: the QR must open the secure origin, or the phone never
    gets camera access and the scanner stays broken."""
    app = _pairing_app(monkeypatch, tmp_path)
    ap = app.state.pcrituals
    ap.config.tls_enabled = True
    ap.config.tls_port = 8443

    url = ap.pairing_base_url()
    assert url == f"https://{LAN_A}:8443"

    code = ap.create_pairing_code()
    assert code["lan_url"] == url
    # The QR payload carries the same https URL, not just the code.
    assert code["qr_png"]  # a PNG data URL was produced
    assert f"https://{LAN_A}:8443" in url


# ---- the server really serves the PWA over https -------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_app_is_reachable_over_https(monkeypatch, tmp_path):
    """End to end: the same FastAPI app, served over TLS on 127.0.0.1, answers
    an https request validated against the certificate it generated."""
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PCRITUALS_TLS", raising=False)
    from pcrituals.api import create_app, start_https_listener

    cfg = Config(data_dir=tmp_path, tls_enabled=True, tls_host="127.0.0.1",
                 tls_port=_free_port())
    app = create_app()
    server = start_https_listener(cfg, app)
    assert server is not None
    try:
        _assert_https_serves_the_pwa(cfg)
    finally:
        server.should_exit = True


def _assert_https_serves_the_pwa(cfg) -> None:
    """Fetch the PWA over https, verifying against the generated certificate."""
    ctx = ssl.create_default_context(cafile=str(cfg.tls_cert_file))
    deadline = time.time() + 20
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"https://127.0.0.1:{cfg.tls_port}/",
                                        context=ctx, timeout=5) as resp:
                assert resp.status == 200
                body = resp.read()
                assert b"<html" in body.lower() or b"<!doctype" in body.lower()
                return
        except Exception as exc:  # noqa: BLE001 - the server may still be binding
            last_error = exc
            time.sleep(0.2)
    pytest.fail(f"https listener never answered: {last_error}")


def test_desktop_window_entry_point_also_serves_https(monkeypatch, tmp_path):
    """The packaged .exe starts its server in desktop.py, not api.run(), so the
    opt-in https listener has to be wired in there too — otherwise the phone
    opens the https URL from the QR code and gets nothing."""
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PCRITUALS_PORT", str(_free_port()))
    monkeypatch.setenv("PCRITUALS_TLS", "1")
    monkeypatch.setenv("PCRITUALS_TLS_HOST", "127.0.0.1")
    monkeypatch.setenv("PCRITUALS_TLS_PORT", str(_free_port()))
    import desktop

    server, _port = desktop._start_server()
    cfg = load_config()
    assert cfg.tls_enabled is True
    try:
        _assert_https_serves_the_pwa(cfg)
    finally:
        server.should_exit = True


def test_the_plain_http_listener_is_unaffected_by_the_https_setting():
    """Requirement 1/2: the desktop window keeps its own port."""
    cfg = Config()
    assert cfg.port == 8765  # the desktop URL is built from this, unchanged
    assert cfg.tls_port != cfg.port


# ---- the CLI a non-technical user runs -----------------------------------------

def test_cli_enable_generates_the_certificate_and_prints_next_steps(cfg, capsys):
    rc = cert_mod._main(["--enable"])
    assert rc == 0
    assert cfg.tls_cert_file.exists()
    out = capsys.readouterr().out
    assert "HTTPS is now ON" in out


def test_cli_print_ip_shows_the_address_to_open_and_trust_steps(cfg, capsys):
    cert_mod.ensure_certificate(cfg)
    assert cert_mod._main(["--print-ip"]) == 0
    out = capsys.readouterr().out
    assert f"https://{LAN_A}:8443" in out
    assert "Certificate Trust Settings" in out


def test_cli_generate_fails_loudly_without_a_backend(cfg, monkeypatch, capsys):
    _without_cryptography(monkeypatch)
    monkeypatch.setattr(cert_mod.shutil, "which", lambda name: None)
    assert cert_mod._main(["--generate"]) == 1
    assert "cryptography" in capsys.readouterr().err


def test_cli_reports_the_detected_ip(cfg, capsys):
    """`python -m pcrituals.cert --print-ip` is the documented way for a user to
    learn which address the phone must open."""
    assert cert_mod._main(["--print-ip"]) == 0
    out = capsys.readouterr().out
    assert LAN_A in out and LAN_B in out
    assert "python -m pcrituals.cert --enable" in out
    assert "not created yet" in out  # honest about the missing certificate


def test_openssl_cli_backend_works_without_cryptography(cfg, monkeypatch):
    """Source installs on Unix without the `cryptography` wheel must still get
    HTTPS, because the fallback is the openssl CLI (never on Windows, where the
    wheel above is what ships)."""
    if not shutil.which("openssl"):
        pytest.skip("no openssl CLI on this machine")
    _without_cryptography(monkeypatch)
    cert_file, key_file = cert_mod.ensure_certificate(cfg)
    assert cert_file.exists() and key_file.exists()
    # And the certificate is usable by a real TLS stack.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    assert subprocess.run(["openssl", "x509", "-in", str(cert_file), "-noout"],
                          capture_output=True).returncode == 0


# ---- /cert endpoint and the tls block on /pair/state ------------------------
# The phone fetches the certificate over plain http (it has not trusted the
# https cert yet, so it cannot load the https origin). The endpoint is open on
# purpose: a certificate is public — it is sent in every TLS handshake.

def _tls_client(tmp_path, monkeypatch, tls_on=True):
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PCRITUALS_TLS", raising=False)
    if tls_on:
        monkeypatch.setenv("PCRITUALS_TLS", "1")
    monkeypatch.setattr(net, "lan_ips", lambda: [LAN_A])
    from pcrituals import cert as cert_mod
    cert_mod.ensure_certificate(Config(data_dir=tmp_path))
    from pcrituals.api import create_app
    from fastapi.testclient import TestClient
    app = create_app()
    token = app.state.pcrituals.security.register_local()
    return TestClient(app), token


def test_cert_endpoint_serves_the_certificate(monkeypatch, tmp_path):
    client, _ = _tls_client(tmp_path, monkeypatch)
    r = client.get("/cert")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/x-x509-ca-cert"
    assert "easylife.crt" in r.headers.get("content-disposition", "")
    assert b"BEGIN CERTIFICATE" in r.content


def test_cert_endpoint_404s_without_a_certificate(monkeypatch, tmp_path):
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PCRITUALS_TLS", raising=False)
    from pcrituals.api import create_app
    from fastapi.testclient import TestClient
    r = TestClient(create_app()).get("/cert")
    assert r.status_code == 404


def test_pair_state_reports_tls_and_the_cert_url(monkeypatch, tmp_path):
    client, token = _tls_client(tmp_path, monkeypatch)
    r = client.get("/api/pair/state", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    tls = r.json()["tls"]
    assert tls["enabled"] is True
    assert tls["cert_ready"] is True
    assert tls["cert_url"].endswith("/cert")
    assert f"http://{LAN_A}" in tls["cert_url"]
