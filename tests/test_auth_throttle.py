"""Regression tests for the login throttle.

Two real problems this guards against:
  1. Lockout was keyed on the username alone, so anyone on the LAN could lock
     the actual user out by failing their logins on purpose.
  2. The failure dict grew without bound — a caller sending a stream of random
     usernames leaked memory for the lifetime of the process.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from pcrituals.auth import (LOCKOUT_THRESHOLD, MAX_TRACKED_FAILURES, AuthError,
                            ThrottleError)
from pcrituals.config import load_config


def _auth(tmp_path, monkeypatch):
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    config = load_config()
    from pcrituals.auth import AuthManager
    return AuthManager(config)


def test_lockout_is_keyed_per_client(tmp_path, monkeypatch):
    """A remote attacker must not be able to lock out the local user."""
    a = _auth(tmp_path, monkeypatch)
    a.create_user("alex", "secret123")

    # Attacker from another address fails many times.
    for _ in range(LOCKOUT_THRESHOLD + 2):
        with pytest.raises(AuthError):
            a.login("alex", "wrong", client="10.0.0.9")

    # The real user, from their own address, can still sign in.
    tok = a.login("alex", "secret123", client="127.0.0.1")
    assert tok["token"]


def test_throttle_raises_429_style_error(tmp_path, monkeypatch):
    a = _auth(tmp_path, monkeypatch)
    a.create_user("alex", "secret123")
    for _ in range(LOCKOUT_THRESHOLD):
        with pytest.raises(AuthError):
            a.login("alex", "wrong", client="127.0.0.1")
    with pytest.raises(ThrottleError) as err:
        a.login("alex", "secret123", client="127.0.0.1")
    assert err.value.status == 429


def test_failure_dict_is_bounded(tmp_path, monkeypatch):
    """Many distinct usernames must not grow the failure map without limit."""
    a = _auth(tmp_path, monkeypatch)
    a.create_user("alex", "secret123")
    for i in range(MAX_TRACKED_FAILURES + 400):
        try:
            a.login(f"probe{i}", "x", client="10.0.0.9")
        except AuthError:
            pass
    assert len(a._failures) <= MAX_TRACKED_FAILURES + 10


def test_successful_login_clears_that_identity(tmp_path, monkeypatch):
    a = _auth(tmp_path, monkeypatch)
    a.create_user("alex", "secret123")
    with pytest.raises(AuthError):
        a.login("alex", "wrong", client="127.0.0.1")
    a.login("alex", "secret123", client="127.0.0.1")
    assert "alex|127.0.0.1" not in a._failures
