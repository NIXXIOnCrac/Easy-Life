"""Reaching the PC from outside the house (Tailscale).

The phone originally only knew the PC's LAN address, so remote control died the
moment it left the Wi-Fi. Tailscale fixes that — but only if the app can SEE the
tunnel address, and it could not: Tailscale uses 100.64.0.0/10, the CGNAT range,
which is not RFC1918 space, so the "is this a private IP?" filter dropped it.

These tests pin the three things that have to line up or remote access silently
half-works:

  * an address that reaches the PC from anywhere is preferred over one that only
    works at home, because the phone stores ONE address;
  * the TLS certificate covers it, or HTTPS over the tunnel fails to validate
    and the camera scanner breaks precisely when the user is away;
  * no Tailscale installed means byte-identical behaviour to before.

Tailscale is faked throughout — nothing here talks to a real tunnel.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pcrituals import net

TS_IP = "100.101.102.103"
TS_IP_2 = "100.64.0.9"
LAN_IP = "192.168.7.31"


# ---- address classification ----------------------------------------------

@pytest.mark.parametrize("ip,expected", [
    (TS_IP, True),
    ("100.64.0.1", True),        # first address of the range
    ("100.127.255.255", True),   # last address of the range
    ("100.63.255.255", False),   # one below the range
    ("100.128.0.0", False),      # one above the range
    (LAN_IP, False),
    ("8.8.8.8", False),
    ("127.0.0.1", False),
    ("", False),
    ("not-an-ip", False),
    ("100.101.102", False),
    ("100.101.102.103.104", False),
    ("100.999.1.1", False),
])
def test_tailscale_range_detection(ip, expected):
    assert net.is_tailscale_ip(ip) is expected


# ---- discovery ------------------------------------------------------------

@pytest.fixture
def fake_host(monkeypatch):
    """A host with one LAN address and one Tailscale address."""
    monkeypatch.setattr(net, "_candidate_ips", lambda: [LAN_IP, TS_IP])
    monkeypatch.setattr(net, "_tailscale_cli_ips", lambda: [])
    monkeypatch.setattr(net, "lan_ips", lambda: [LAN_IP])
    return None


def test_tailscale_address_is_found_even_though_it_is_not_private(fake_host):
    assert net.tailscale_ips() == [TS_IP]


def test_no_tailscale_means_no_tailscale_addresses(monkeypatch):
    monkeypatch.setattr(net, "_candidate_ips", lambda: [LAN_IP, "127.0.0.1"])
    monkeypatch.setattr(net, "_tailscale_cli_ips", lambda: [])
    assert net.tailscale_ips() == []


def test_cli_result_is_merged_without_duplicates(monkeypatch):
    """The CLI is authoritative when interface enumeration misses the adapter."""
    monkeypatch.setattr(net, "_candidate_ips", lambda: [LAN_IP])
    monkeypatch.setattr(net, "_tailscale_cli_ips", lambda: [TS_IP, TS_IP_2])
    assert net.tailscale_ips() == [TS_IP, TS_IP_2]


def test_cli_failure_is_not_an_error(monkeypatch):
    """No Tailscale installed, or a slow CLI, must not break the app."""
    monkeypatch.setattr(net, "_candidate_ips", lambda: [LAN_IP])
    monkeypatch.setattr(net, "_tailscale_cli_ips", lambda: [])
    assert net.tailscale_ips() == []


# ---- ordering -------------------------------------------------------------

def test_remote_address_is_preferred_over_the_lan_address(fake_host):
    """The phone stores one address, so it must be the one that keeps working."""
    urls = net.pairing_urls(8765)
    assert urls[0] == f"http://{TS_IP}:8765"
    assert f"http://{LAN_IP}:8765" in urls
    assert urls.index(f"http://{TS_IP}:8765") < urls.index(f"http://{LAN_IP}:8765")


def test_lan_address_is_still_offered_as_a_fallback(fake_host):
    """A phone that hasn't joined the tailnet yet still has to be able to pair."""
    assert f"http://{LAN_IP}:8765" in net.pairing_urls(8765)


def test_no_tailscale_leaves_the_url_list_unchanged(monkeypatch):
    """Byte-identical behaviour for everyone who never installs Tailscale."""
    monkeypatch.setattr(net, "_candidate_ips", lambda: [LAN_IP])
    monkeypatch.setattr(net, "_tailscale_cli_ips", lambda: [])
    monkeypatch.setattr(net, "lan_ips", lambda: [LAN_IP])
    assert net.pairing_urls(8765) == [f"http://{LAN_IP}:8765"]


def test_scheme_and_port_are_respected(fake_host):
    assert net.pairing_urls(8443, scheme="https")[0] == f"https://{TS_IP}:8443"


# ---- the certificate must cover it ---------------------------------------

def test_cert_covers_the_remote_address(fake_host):
    """Without this, HTTPS over the tunnel fails to validate and the phone's
    camera scanner breaks exactly when the user is away from home."""
    ips = net.cert_ips()
    assert TS_IP in ips
    assert LAN_IP in ips


def test_cert_sans_include_the_remote_address(fake_host, monkeypatch):
    from pcrituals.cert import san_names

    names = san_names()
    assert TS_IP in names
    assert "127.0.0.1" in names and "localhost" in names


def test_installing_tailscale_reissues_the_certificate(monkeypatch, tmp_path):
    """A certificate made before Tailscale existed must not be reused.

    Otherwise HTTPS over the tunnel fails to validate and the phone's camera
    scanner breaks precisely when the user is away from home. The existing
    "SANs no longer cover today's addresses" rule already handles it — this
    proves the new addresses actually reach that check.
    """
    pytest.importorskip("cryptography")
    from pcrituals import cert as cert_mod
    from pcrituals.config import Config

    cfg = Config(data_dir=tmp_path)

    # 1. Before Tailscale: covers the LAN address only.
    monkeypatch.setattr(net, "_candidate_ips", lambda: [LAN_IP])
    monkeypatch.setattr(net, "_tailscale_cli_ips", lambda: [])
    monkeypatch.setattr(net, "lan_ips", lambda: [LAN_IP])
    cert_file, key_file = cert_mod.ensure_certificate(cfg)
    assert cert_mod.is_usable(cert_file, key_file, cert_mod.san_names()) is True

    # 2. Tailscale appears -> the old certificate is stale.
    monkeypatch.setattr(net, "_candidate_ips", lambda: [LAN_IP, TS_IP])
    assert cert_mod.is_usable(cert_file, key_file, cert_mod.san_names()) is False

    cert_mod.ensure_certificate(cfg)
    from cryptography import x509

    cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    ips = [str(i) for i in san.get_values_for_type(x509.IPAddress)]
    assert TS_IP in ips
    assert LAN_IP in ips


# ---- the pairing URL the QR encodes --------------------------------------

def test_pairing_base_url_prefers_the_remote_address(fake_host, monkeypatch, tmp_path):
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PCRITUALS_TLS", raising=False)
    from pcrituals.app import App
    from pcrituals.config import load_config

    app = App(load_config())
    assert app.pairing_base_url() == f"http://{TS_IP}:8765"


def test_pairing_base_url_uses_https_port_when_tls_is_on(fake_host, monkeypatch, tmp_path):
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PCRITUALS_TLS", "1")
    from pcrituals.app import App
    from pcrituals.config import load_config

    app = App(load_config())
    assert app.pairing_base_url() == f"https://{TS_IP}:8443"


def test_running_tunnel_wins_over_every_other_address(fake_host, monkeypatch, tmp_path):
    """The QR must advertise the tunnel while it is up.

    It is the only address that works from anywhere and needs nothing installed
    on the phone, so it outranks the LAN address and the Tailscale one.
    """
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PCRITUALS_TLS", raising=False)
    from pcrituals import tunnel as tunnel_mod
    from pcrituals.app import App
    from pcrituals.config import load_config

    monkeypatch.setattr(tunnel_mod.tunnel, "status",
                        lambda: {"running": True, "url": "https://abc-def.trycloudflare.com",
                                 "installed": True, "warning": "", "installed_hint": "", "uptime": 1})
    assert App(load_config()).pairing_base_url() == "https://abc-def.trycloudflare.com"


def test_stopped_tunnel_does_not_change_the_address(fake_host, monkeypatch, tmp_path):
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PCRITUALS_TLS", raising=False)
    from pcrituals import tunnel as tunnel_mod
    from pcrituals.app import App
    from pcrituals.config import load_config

    monkeypatch.setattr(tunnel_mod.tunnel, "status",
                        lambda: {"running": False, "url": "", "installed": True,
                                 "warning": "", "installed_hint": "", "uptime": 0})
    assert App(load_config()).pairing_base_url() == f"http://{TS_IP}:8765"


def test_pairing_base_url_unchanged_without_tailscale(monkeypatch, tmp_path):
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PCRITUALS_TLS", raising=False)
    monkeypatch.setattr(net, "_candidate_ips", lambda: [LAN_IP])
    monkeypatch.setattr(net, "_tailscale_cli_ips", lambda: [])
    monkeypatch.setattr(net, "lan_ips", lambda: [LAN_IP])
    from pcrituals.app import App
    from pcrituals.config import load_config

    app = App(load_config())
    assert app.pairing_base_url() == f"http://{LAN_IP}:8765"
