"""Tests for storage and engine."""
import os
import sys
import time
import tempfile
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pcrituals.config import Config
from pcrituals.storage import Store
from pcrituals.models import Ritual, Action
from pcrituals.engine import RitualRunner, RunState
from pcrituals.platform import Platform, PlatformError


class FakePlatform:
    """Deterministic fake for engine tests. Sync methods (matches Platform interface)."""
    name = "fake"

    def __init__(self, failures: set | None = None):
        self.calls = []
        self.failures = failures or set()

    def launch(self, *a, **k): self.calls.append(("launch", a, k))
    def open_website(self, u):
        self.calls.append(("website", u))
        if "fails" in u: raise RuntimeError("network down")
    def open_path(self, *a, **k): self.calls.append(("path", a))
    def run_command(self, command, timeout, cwd=None, cancel_event=None):
        self.calls.append(("command", command))
        if "flaky" in str(command): raise RuntimeError("transient")
        return "ok"
    def close_application(self, n): self.calls.append(("close", n)); return True
    def power(self, a): self.calls.append(("power", a))
    def cpu_load(self): return 0
    def memory_usage(self): return {"percent": 0, "used": 0, "total": 0}
    def list_running_processes(self, hint): return []


def make_config(tmp):
    c = Config(data_dir=Path(tmp))
    c.ensure_dirs()
    return c


class TestStore:
    def test_crud_and_reorder(self, tmp_path):
        store = Store(make_config(tmp_path))
        r1 = Ritual(name="A", actions=[Action.website("https://x.com")])
        r2 = Ritual(name="B")
        store.save_ritual(r1)
        store.save_ritual(r2)
        assert store.count_rituals() == 2
        # reorder
        store.reorder_rituals([r2.id, r1.id])
        ids = [r.id for r in store.list_rituals()]
        assert ids == [r2.id, r1.id]
        # edit preserves position
        r1.name = "A2"
        store.save_ritual(r1)
        assert [r.id for r in store.list_rituals()] == [r2.id, r1.id]
        # delete
        assert store.delete_ritual(r1.id)
        assert store.count_rituals() == 1

    def test_duplicate_action_ordering(self, tmp_path):
        store = Store(make_config(tmp_path))
        r = Ritual(name="G", actions=[Action.delay(0.1), Action.website("https://a.b")])
        a = [x.id for x in r.actions]
        r.reorder([a[1], a[0]])
        assert r.actions[0].type.value == "website"
        assert r.actions[1].type.value == "delay"

    def test_history(self, tmp_path):
        store = Store(make_config(tmp_path))
        from pcrituals.models import HistoryEntry, RitualStatus
        e = HistoryEntry(ritual_id="1", ritual_name="R", started_at=now_dt(),
                         status=RitualStatus.COMPLETED)
        store.append_history(e)
        entries = store.list_history()
        assert len(entries) == 1
        assert entries[0].ritual_name == "R"


def now_dt():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


class TestEngine:
    def _runner(self, ritual, tmp_path=None, platform=None):
        events = []
        r = RitualRunner(ritual, platform=platform or FakePlatform(),
                         on_event=events.append)
        return r, events

    async def test_successful_run(self):
        ritual = Ritual(name="R", actions=[
            Action.website("https://ok.com"),
            Action.delay(0.01),
        ])
        r, events = self._runner(ritual)
        state = await r.run()
        assert state == RunState.COMPLETED
        assert r.actions_completed == 2
        types = [e["type"] for e in events]
        assert types[0] == "started"
        assert types[-1] == "finished"
        assert any(t == "step_done" for t in types)
        # history recorded
        assert r.history.status.value == "completed"
        assert r.history.actions_total == 2

    async def test_stop_on_error_default(self):
        ritual = Ritual(name="R", stop_on_error=True, actions=[
            Action.website("fails-here"),
            Action.website("https://never.com"),
        ])
        r, events = self._runner(ritual)
        state = await r.run()
        assert state == RunState.FAILED
        # second action never ran
        assert r.actions_completed == 0
        assert [e for e in events if e["type"] == "step_done"] == []

    async def test_continue_on_error(self):
        ritual = Ritual(name="R", stop_on_error=False, actions=[
            Action.website("fails-here"),
            Action.website("https://ok.com"),
        ])
        r, events = self._runner(ritual)
        state = await r.run()
        assert state == RunState.COMPLETED  # one failed but continued
        assert r.actions_completed == 1
        assert r.actions_failed == 1
        finish = [e for e in events if e["type"] == "finished"][-1]
        assert finish["status"] == "completed"

    async def test_cancellation_between_steps(self):
        ritual = Ritual(name="R", actions=[Action.delay(2), Action.delay(2)])
        r, events = self._runner(ritual)

        async def canceller():
            await asyncio.sleep(0.1)
            r.cancel()

        task = asyncio.create_task(r.run())
        await canceller()
        await task
        assert r.state == RunState.CANCELLED
        # did not run full sequence
        assert r.actions_completed < 2

    async def test_timeout(self):
        ritual = Ritual(name="R", actions=[
            Action(target="https://slow", type="website", timeout=0.1)
        ])
        # force a slow website to trigger timeout
        events = []
        class SlowPlat(FakePlatform):
            def open_website(self, u):
                time.sleep(1)
        r = RitualRunner(ritual, platform=SlowPlat(), on_event=events.append)
        state = await r.run()
        assert state == RunState.FAILED
        errs = [e for e in events if e["type"] == "step_failed"]
        assert len(errs) == 1
        assert "timed out" in errs[0]["error"].lower()

    async def test_retry(self):
        ritual = Ritual(name="R", actions=[
            Action(type="command", target="flaky", params={"retries": 2}, name="cmd")
        ])
        calls = []
        class FlakyPlat(FakePlatform):
            def run_command(self, command, timeout, cwd=None, cancel_event=None):
                calls.append(command)
                if len(calls) == 1:
                    raise RuntimeError("transient error")
                return "ok"
        r, events = self._runner(ritual, platform=FlakyPlat())
        state = await r.run()
        assert state == RunState.COMPLETED
        assert len(calls) == 2  # original + 1 retry
        assert any(e["type"] == "step_retry" for e in events)


class TestEngineWindowsActionInputs:
    """End-to-end checks that the Windows input guards (see
    tests/test_windows_paths.py) surface as legible step failures instead of
    escaping the run.

    The action values here are the ones a user can actually save: `params` is
    free-form JSON straight from the deck/ritual editor, so an unhashable
    "action" or a NaN delay reached the platform layer before.
    """

    def _run(self, ritual, platform):
        events = []
        runner = RitualRunner(ritual, platform=platform, on_event=events.append)
        return runner, events

    async def test_malformed_power_action_fails_the_step_legibly(self):
        plat = FakePlatform()
        ritual = Ritual(name="P", actions=[
            Action(type="power", params={"action": ["shutdown"]}),
        ])
        runner, events = self._run(ritual, plat)
        assert await runner.run() == RunState.FAILED
        failures = [e for e in events if e["type"] == "step_failed"]
        assert len(failures) == 1
        assert "unsupported power action" in failures[0]["error"]
        # The destructive call was never reached.
        assert ("power", ("shutdown",)) not in plat.calls
        assert not [c for c in plat.calls if c[0] == "power"]

    async def test_nan_delay_is_reported_as_a_delay_problem(self):
        ritual = Ritual(name="D", actions=[
            Action(type="delay", params={"seconds": float("nan")}),
        ])
        runner, events = self._run(ritual, FakePlatform())
        assert await runner.run() == RunState.FAILED
        error = [e for e in events if e["type"] == "step_failed"][0]["error"]
        assert "delay" in error and "finite" in error

    async def test_close_of_a_critical_process_is_refused_end_to_end(self):
        """WindowsPlatform refuses before taskkill; the engine reports why and
        the process is left alone (this runs the real Windows argument path).
        """
        taskkill_calls = []

        def fake_run(cmd, **kwargs):
            taskkill_calls.append(list(cmd))
            raise AssertionError("taskkill must not be invoked")

        from pcrituals.platform import WindowsPlatform
        plat = WindowsPlatform(run=fake_run)
        ritual = Ritual(name="C", actions=[Action(type="close", target="lsass")])
        runner = RitualRunner(ritual, platform=plat)
        assert await runner.run() == RunState.FAILED
        assert taskkill_calls == []
        assert "critical" in (runner.history.steps[0].detail or "").lower()


def run_tests():
    import pytest
    import tempfile as _t
    with _t.TemporaryDirectory() as tmp:
        sys.exit(pytest.main([__file__, "-q", "-x", "--no-header"]))


if __name__ == "__main__":
    import asyncio
    run_tests()