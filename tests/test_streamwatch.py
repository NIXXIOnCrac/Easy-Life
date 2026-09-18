"""The stream watcher: notify the phone when OBS's stream drops.

The important property is that it only notifies on a REAL drop, once, and
never on startup (we don't know whether the stream was already down).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pcrituals.streamwatch import StreamWatcher


class _Cfg:
    def __init__(self, streamer_mode=True, notify_enabled=True):
        self.streamer_mode = streamer_mode
        self.notify_enabled = notify_enabled


class _FakeOBS:
    def __init__(self, streaming):
        self._streaming = streaming

    def status(self):
        return {"streaming": self._streaming, "reachable": True}


class _FakeNotifier:
    def __init__(self):
        self.sent = []

    def send_to_all(self, payload):
        self.sent.append(payload)
        return [{"ok": True}]


def _make(streaming, cfg=None, notifier=None):
    obs = _FakeOBS(streaming)
    n = notifier or _FakeNotifier()
    w = StreamWatcher(cfg or _Cfg(), n, obs_factory=lambda c: obs)
    return w, n, obs


def test_first_check_sets_baseline_and_does_not_notify():
    w, n, _ = _make(streaming=True)
    w.check_once()
    assert n.sent == []


def test_drop_notifies_once():
    w, n, obs = _make(streaming=True)
    w.check_once()          # baseline: streaming
    obs._streaming = False  # the stream dies
    w.check_once()
    assert len(n.sent) == 1
    assert "Stream went down" in n.sent[0]


def test_stays_down_does_not_re_notify():
    w, n, obs = _make(streaming=True)
    w.check_once()
    obs._streaming = False
    w.check_once()          # notify
    w.check_once()          # still down
    w.check_once()          # still down
    assert len(n.sent) == 1


def test_recovers_then_drops_again_notifies_again():
    w, n, obs = _make(streaming=True)
    w.check_once()
    obs._streaming = False
    w.check_once()          # 1st drop
    obs._streaming = True
    w.check_once()          # back up
    obs._streaming = False
    w.check_once()          # 2nd drop
    assert len(n.sent) == 2


def test_starting_down_does_not_notify():
    """If the stream is already down when the watcher starts, don't spam."""
    w, n, obs = _make(streaming=False)
    w.check_once()          # baseline: down
    w.check_once()          # still down
    assert n.sent == []


def test_notify_disabled_never_notifies():
    w, n, obs = _make(streaming=True, cfg=_Cfg(notify_enabled=False))
    w.check_once()
    obs._streaming = False
    w.check_once()
    assert n.sent == []


def test_streamer_mode_off_resets_baseline():
    """Turning the feature off resets the baseline so a later drop while off
    is not reported as if it happened while on."""
    w, n, obs = _make(streaming=True)
    w.check_once()          # baseline streaming
    w.config.streamer_mode = False
    w.check_once()          # off -> baseline reset
    obs._streaming = False
    w.config.streamer_mode = True
    w.check_once()          # on again, baseline re-set to down
    w.check_once()          # still down -> no notify
    assert n.sent == []


def test_obs_unreachable_counts_as_down():
    class _Dead:
        def status(self):
            raise ConnectionRefusedError("no OBS")

    n = _FakeNotifier()
    w = StreamWatcher(_Cfg(), n, obs_factory=lambda c: _Dead())
    w.check_once()          # baseline: down
    w.check_once()          # still down
    assert n.sent == []


def test_a_failed_push_does_not_kill_the_watcher():
    class _Boom:
        def send_to_all(self, payload):
            raise RuntimeError("push service down")

    w, _, obs = _make(streaming=True, notifier=_Boom())
    w.check_once()
    obs._streaming = False
    w.check_once()          # must not raise
    assert True


def test_start_watcher_is_one_per_process():
    """create_app runs many times (tests, factory launches). A watcher each
    time left a pile of sleeping threads."""
    import threading
    from pcrituals.streamwatch import start_watcher, stop_watcher

    stop_watcher()
    n = _FakeNotifier()
    cfg = _Cfg()
    for _ in range(4):
        start_watcher(cfg, n, interval=60)
    count = [t.name for t in threading.enumerate()].count("stream-watch")
    stop_watcher()
    assert count == 1, f"expected 1 watcher thread, found {count}"


def test_start_stop_lifecycle():
    w, n, obs = _make(streaming=True)
    w.start()
    w.stop()
    assert w._thread is not None
    # starting twice is a no-op
    w.start()
    w.start()
    w.stop()
