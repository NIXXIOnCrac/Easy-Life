"""A tunnel must not be able to impersonate the desktop UI.

Easy Life trusts loopback implicitly: the desktop window talks to 127.0.0.1 and
would otherwise have to log in like everyone else. That is fine while loopback
means "the machine itself".

A tunnel breaks the assumption. cloudflared (and any other reverse proxy) runs
ON the PC, so a request that arrived from the internet reaches the app with a
loopback socket address. Before this was fixed, such a request was treated as
the trusted desktop UI and handed the ENTIRE API — running Plays, power
control, shutdown — with no credential at all. Publishing the app through a
tunnel would have been a remote-shutdown backdoor for anyone who knew the URL.

The rule now: any forwarding header disqualifies a request from counting as
local. It fails closed in the only direction that matters — a caller cannot
spoof their way IN, because adding a header can only make a request look more
remote.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from pcrituals import net
from pcrituals.api import create_app

TUNNEL_HEADERS = [
    {"X-Forwarded-For": "203.0.113.9"},
    {"CF-Connecting-IP": "203.0.113.9"},
    {"X-Real-IP": "203.0.113.9"},
    {"Forwarded": "for=203.0.113.9"},
    {"CF-Ray": "8a1b2c3d4e5f-LHR"},
]


@pytest.fixture
def app_and_token(tmp_path, monkeypatch):
    # Own data dir: once ANY account exists, auth_required stops trusting
    # loopback and demands a login. Sharing the suite's default dir made these
    # tests pass alone and fail in the full run.
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    app = create_app()
    token = app.state.pcrituals.security.register_local()
    return app, token


# ---- unit level -----------------------------------------------------------

class _Req:
    """Just enough request shape for the trust helpers."""

    def __init__(self, host, headers=None):
        self.client = type("C", (), {"host": host})()
        self.headers = headers or {}


def test_plain_loopback_is_local():
    assert net.client_is_local(_Req("127.0.0.1")) is True
    assert net.client_is_local(_Req("::1")) is True


@pytest.mark.parametrize("headers", TUNNEL_HEADERS)
def test_loopback_with_forwarding_headers_is_not_local(headers):
    """The whole fix, in one assertion."""
    assert net.client_is_local(_Req("127.0.0.1", headers)) is False


def test_lan_callers_were_never_local():
    assert net.client_is_local(_Req("192.168.1.50")) is False


def test_forwarding_headers_cannot_promote_a_remote_caller():
    """Fails closed: adding a header can only make a request look more remote."""
    assert net.client_is_local(_Req("192.168.1.50", {"X-Forwarded-For": "127.0.0.1"})) is False


def test_limit_key_prefers_the_forwarded_address_over_loopback():
    req = _Req("127.0.0.1", {"CF-Connecting-IP": "203.0.113.9"})
    assert net.client_ip_for_limits(req) == "203.0.113.9"


def test_limit_key_ignores_a_spoofed_header_on_a_direct_connection():
    """A LAN caller rotating X-Forwarded-For must not dodge the rate limit."""
    req = _Req("192.168.1.50", {"X-Forwarded-For": "1.2.3.4"})
    assert net.client_ip_for_limits(req) == "192.168.1.50"


def test_limit_key_takes_the_first_hop_of_a_chain():
    req = _Req("127.0.0.1", {"X-Forwarded-For": "203.0.113.9, 70.41.3.18"})
    assert net.client_ip_for_limits(req) == "203.0.113.9"


# ---- through the real API -------------------------------------------------

def test_tunnelled_request_gets_401_instead_of_the_whole_api(app_and_token):
    """The regression that matters: this used to return 200 with no credential."""
    app, _ = app_and_token
    # Same app, but the request now carries forwarding headers — i.e. it came
    # through a tunnel sitting on this machine.
    tunneled = TestClient(app, client=("127.0.0.1", 51234))
    tunneled.headers.update({"X-Forwarded-For": "203.0.113.9"})

    assert tunneled.get("/api/rituals").status_code == 401
    assert tunneled.get("/api/status").status_code == 401


@pytest.mark.parametrize("headers", TUNNEL_HEADERS)
def test_every_forwarding_header_blocks_the_local_bypass(app_and_token, headers):
    app, _ = app_and_token
    c = TestClient(app, client=("127.0.0.1", 51234))
    c.headers.update(headers)
    assert c.get("/api/rituals").status_code == 401, headers


def test_loopback_without_headers_still_works(app_and_token):
    """The desktop UI must keep working unchanged — this is why the app is usable."""
    app, _ = app_and_token
    local = TestClient(app, client=("127.0.0.1", 51234))
    assert local.get("/api/rituals").status_code == 200


def test_tunnelled_request_with_a_valid_token_still_works(app_and_token):
    """A paired phone through the tunnel is still a paired phone."""
    app, token = app_and_token
    c = TestClient(app, client=("127.0.0.1", 51234))
    c.headers.update({"X-Forwarded-For": "203.0.113.9"})
    r = c.get("/api/rituals", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_first_run_setup_cannot_be_claimed_through_a_tunnel(app_and_token):
    """Whoever creates the first account owns the PC.

    With the tunnel up before setup, an open /auth/setup would let anyone on the
    internet claim the machine. Setup is a physical act and stays local.
    """
    app, _ = app_and_token
    tunneled = TestClient(app, client=("127.0.0.1", 51234))
    tunneled.headers.update({"CF-Connecting-IP": "203.0.113.9"})
    r = tunneled.post("/api/auth/setup", json={"username": "attacker", "password": "hunter22"})
    assert r.status_code == 403
    # ...and no account was created.
    assert app.state.pcrituals.auth.has_users() is False


def test_setup_still_works_from_the_pc_itself(app_and_token):
    app, _ = app_and_token
    local = TestClient(app, client=("127.0.0.1", 51234))
    r = local.post("/api/auth/setup", json={"username": "owner", "password": "a-good-password"})
    assert r.status_code == 200
    assert app.state.pcrituals.auth.has_users() is True


def test_quit_is_refused_through_a_tunnel(app_and_token):
    """A tunnelled caller must not be able to shut the PC's app down."""
    app, token = app_and_token
    app.state.pcrituals_request_quit = lambda: True
    c = TestClient(app, client=("127.0.0.1", 51234))
    c.headers.update({"X-Forwarded-For": "203.0.113.9"})
    r = c.post("/api/quit", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
