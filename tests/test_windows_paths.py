"""Off-Windows regression tests for the Windows-specific code paths.

The app only ever runs on a real Windows machine, so nothing in
`pcrituals/platform.py`, `wol.py`, `desktop.py` or `launcher.py` gets executed
on the development host. Every OS call in those modules therefore goes through
an injectable seam (subprocess.run/Popen, os.startfile) and the *logic* - argv
construction, path/env expansion, packet bytes, stream guarding - is verified
here with fakes.

Each test in this file is a regression test for a bug that shipped: the
docstring says what the code did before and why that was wrong.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pcrituals import platform as platform_mod
from pcrituals.actions import dispatch_action
from pcrituals.models import Action
from pcrituals.platform import (
    PROCESS_TOOL_TIMEOUT_S,
    GenericPlatform,
    PlatformError,
    WindowsPlatform,
    _text_kwargs,
)
from pcrituals.wol import WakeOnLan


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------
class _Result:
    """CompletedProcess look-alike returned by RecordingRun."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class RecordingRun:
    """Stand-in for subprocess.run: records argv/kwargs, returns canned results."""

    def __init__(self, *results: _Result, default: _Result | None = None) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self._results = list(results)
        self._default = default if default is not None else _Result()

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs))
        if self._results:
            return self._results.pop(0)
        return self._default

    @property
    def argv(self) -> list[str]:
        return self.calls[0][0] if self.calls else []

    @property
    def kwargs(self) -> dict:
        return self.calls[0][1] if self.calls else {}


class FakeProc:
    """Minimal Popen result object for run_command / power tests."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0,
                 pid: int = 4242, communicate_raises: BaseException | None = None) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.pid = pid
        self.killed = False
        self.communicate_calls: list[float | None] = []
        self._raises = communicate_raises
        self._raised = False

    def communicate(self, timeout=None):
        self.communicate_calls.append(timeout)
        if self._raises is not None:
            # A real Popen raises TimeoutExpired on *every* call while the child
            # is still running -- not just the first. run_command polls in short
            # slices so it can honour a cancel request, so this must keep raising
            # until something kills the process.
            raise self._raises
        return (self.stdout, self.stderr)

    def kill(self) -> None:
        self.killed = True


class RecordingPopen:
    """Stand-in for subprocess.Popen."""

    def __init__(self, proc: FakeProc | None = None,
                 raises: BaseException | None = None) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.proc = proc if proc is not None else FakeProc()
        self._raises = raises

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs))
        if self._raises is not None:
            raise self._raises
        return self.proc

    @property
    def argv(self) -> list[str]:
        return self.calls[0][0] if self.calls else []

    @property
    def kwargs(self) -> dict:
        return self.calls[0][1] if self.calls else {}


class RecordingStartfile:
    """Stand-in for os.startfile (which does not exist off Windows)."""

    def __init__(self, raises: BaseException | None = None) -> None:
        self.targets: list[str] = []
        self._raises = raises

    def __call__(self, target: str) -> None:
        self.targets.append(target)
        if self._raises is not None:
            raise self._raises


class FakePlatform:
    """Platform that records what the action layer asked for."""

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def launch(self, *a, **k): self.calls.append(("launch", a))
    def open_path(self, *a, **k): self.calls.append(("open_path", a))
    def open_website(self, *a, **k): self.calls.append(("open_website", a))
    def run_command(self, *a, **k): self.calls.append(("run_command", a)); return ""
    def launch_game(self, *a, **k): self.calls.append(("launch_game", a))
    def close_application(self, *a, **k): self.calls.append(("close_application", a)); return True
    def power(self, *a, **k): self.calls.append(("power", a))
    def cpu_load(self): return 0.0
    def memory_usage(self): return {"percent": 0.0, "used": 0, "total": 0}
    def list_running_processes(self, hint): return []


def make_windows(*, run=None, popen=None, startfile=None) -> WindowsPlatform:
    return WindowsPlatform(run=run or RecordingRun(),
                           popen=popen or RecordingPopen(),
                           startfile=startfile or RecordingStartfile())


# --------------------------------------------------------------------------
# wol.py — magic packet
# --------------------------------------------------------------------------
class TestWakeOnLanPacket:
    """Regression: 'AA BB CC DD EE FF' was rejected as "must be 6 bytes".

    Whitespace is the format Windows' own `getmac` prints, so the most common
    way to copy an address into the UI failed with a misleading length error.
    """

    CANONICAL = "aa:bb:cc:dd:ee:ff"

    @pytest.mark.parametrize("text", [
        "aa:bb:cc:dd:ee:ff",
        "AA-BB-CC-DD-EE-FF",
        "aabbccddeeff",
        "AABBCCDDEEFF",
        "aabb.ccdd.eeff",
        "aa bb cc dd ee ff",
        "AA BB CC DD EE FF",
        "aa\tbb\tcc\tdd\tee\tff",
        "  aa:bb:cc:dd:ee:ff  ",
    ])
    def test_all_common_formats_parse_to_the_same_bytes(self, text):
        assert WakeOnLan._parse_mac(text) == bytes.fromhex("aabbccddeeff")

    def test_magic_packet_is_exactly_102_bytes_and_byte_correct(self):
        mac = bytes.fromhex("aabbccddeeff")
        payload = WakeOnLan._magic_packet("AA:BB:CC:DD:EE:FF")
        assert len(payload) == 102
        # 6 x 0xFF, then the 6-byte MAC repeated exactly 16 times.
        assert payload[:6] == b"\xff" * 6
        assert payload[6:] == mac * 16
        assert payload.count(mac) == 16

    def test_space_separated_mac_builds_the_same_packet(self):
        assert (WakeOnLan._magic_packet("AA BB CC DD EE FF")
                == WakeOnLan._magic_packet(self.CANONICAL))

    def test_invalid_macs_are_rejected_not_sent(self, monkeypatch):
        """An invalid address must raise before any socket is opened."""
        opened = []

        class SocketSpy:
            def __init__(self, *a, **k): opened.append(a)

        monkeypatch.setattr("pcrituals.wol.socket.socket", SocketSpy)
        for bad in ("", "   ", "ZZ:ZZ", "aa:bb", "aa:bb:cc:dd:ee",
                    "aa:bb:cc:dd:ee:ff:00", "aabbccddeefg", "not-a-mac", None):
            with pytest.raises(ValueError):
                WakeOnLan().send(bad)
        assert opened == []

    def test_broadcast_payload_matches_the_packet_builder(self, monkeypatch):
        """The bytes on the wire are the validated 102-byte packet."""
        sent = []

        class SocketSpy:
            def __init__(self, *a, **k): pass
            def setsockopt(self, *a): pass
            def sendto(self, payload, addr): sent.append((payload, addr))
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr("pcrituals.wol.socket.socket", SocketSpy)
        WakeOnLan("192.168.1.255", 9).send("aa bb cc dd ee ff")
        payload, addr = sent[0]
        assert payload == WakeOnLan._magic_packet("aa:bb:cc:dd:ee:ff")
        assert len(payload) == 102
        assert addr == ("192.168.1.255", 9)

    def test_send_unicast_validates_the_mac_too(self):
        with pytest.raises(ValueError):
            WakeOnLan().send_unicast("bogus", "192.168.1.10")


# --------------------------------------------------------------------------
# platform.py — close_application
# --------------------------------------------------------------------------
class TestCloseApplication:
    """Regression: taskkill /IM accepts wildcards, so close_application('*')
    killed every running executable, and 'lsass'/'csrss' took the machine down.
    """

    @pytest.mark.parametrize("name", ["*", "*.*", "steam*", "?", "a?c"])
    def test_wildcards_are_refused_before_any_taskkill(self, name):
        run = RecordingRun()
        with pytest.raises(PlatformError) as err:
            make_windows(run=run).close_application(name)
        assert "wildcard" in str(err.value).lower()
        assert run.calls == []  # nothing was invoked

    @pytest.mark.parametrize("name", ["lsass", "LSASS.EXE", "csrss", "winlogon",
                                      "services", "smss", "svchost", "wininit",
                                      "System"])
    def test_system_critical_processes_are_refused(self, name):
        run = RecordingRun()
        with pytest.raises(PlatformError) as err:
            make_windows(run=run).close_application(name)
        assert "critical" in str(err.value).lower()
        assert run.calls == []

    def test_critical_list_is_normalised_to_image_names(self):
        # _image_name appends .exe and lowercases nothing, so the set must hold
        # lower-case image names with the suffix.
        for image in platform_mod.CRITICAL_PROCESS_IMAGES:
            assert image == image.lower()

    def test_graceful_then_force_close(self):
        # Graceful (no /F) failing falls back to taskkill /F; success either way.
        run = RecordingRun(_Result(returncode=128, stderr="not found"),
                           _Result(returncode=0))
        assert make_windows(run=run).close_application("firefox") is True
        assert run.calls[0][0] == ["taskkill", "/IM", "firefox.exe"]
        assert run.calls[1][0] == ["taskkill", "/F", "/IM", "firefox.exe"]

    def test_graceful_timeout_still_falls_back_to_the_forced_kill(self):
        """Regression: a hung app made the graceful taskkill time out, which
        aborted the step without ever trying /F.
        """
        calls = []

        def hang_then_ok(cmd, **kwargs):
            calls.append(list(cmd))
            if "/F" not in cmd:
                raise subprocess.TimeoutExpired(cmd, PROCESS_TOOL_TIMEOUT_S)
            return _Result(returncode=0)

        assert make_windows(run=hang_then_ok).close_application("notepad") is True
        assert calls == [
            ["taskkill", "/IM", "notepad.exe"],
            ["taskkill", "/F", "/IM", "notepad.exe"],
        ]

    def test_forced_kill_timeout_is_a_platform_error(self):
        def always_hang(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, PROCESS_TOOL_TIMEOUT_S)

        plat = make_windows(run=always_hang)
        with pytest.raises(PlatformError) as err:
            plat.close_application("notepad")
        assert "notepad.exe" in str(err.value)

    def test_not_running_returns_false_without_raising(self):
        run = RecordingRun(_Result(returncode=128), _Result(returncode=128))
        assert make_windows(run=run).close_application("firefox") is False

    def test_graceful_success_skips_the_force_kill(self):
        run = RecordingRun(_Result(returncode=0))
        assert make_windows(run=run).close_application("notepad") is True
        assert len(run.calls) == 1  # no /F second pass

    @pytest.mark.parametrize("given,expected", [
        ("notepad", "notepad.exe"),
        ("notepad.exe", "notepad.exe"),          # was 'notead.exe' with rstrip
        ("firefox.exe", "firefox.exe"),          # was 'firefo.exe'
        ("code.exe", "code.exe"),                # was 'cod.exe'
        (r"C:\Program Files\Mozilla Firefox\firefox.exe", "firefox.exe"),
        (r"C:\Program Files\App With Spaces\app", "app.exe"),
        ('"C:\\Program Files\\App\\app.exe"', "app.exe"),
        ("C:/Program Files/App/app.exe", "app.exe"),
    ])
    def test_image_name_normalisation(self, given, expected):
        assert WindowsPlatform._image_name(given) == expected

    def test_empty_process_name_is_rejected(self):
        with pytest.raises(PlatformError):
            make_windows().close_application("   ")
        with pytest.raises(PlatformError):
            make_windows().close_application(r"C:\Program Files\\")


# --------------------------------------------------------------------------
# platform.py — path handling / os.startfile
# --------------------------------------------------------------------------
class TestOpenPath:
    """Regression: a %LOCALAPPDATA% that could not be resolved stayed literal
    and failed as "file not found"; a file with no associated app raised a raw
    OSError instead of a legible error.
    """

    def test_windows_variables_are_expanded(self, monkeypatch, tmp_path):
        target = tmp_path / "Steam" / "steam.exe"
        target.parent.mkdir()
        target.write_text("x")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        startfile = RecordingStartfile()
        make_windows(startfile=startfile).open_path("%LOCALAPPDATA%/Steam/steam.exe")
        assert startfile.targets == [str(target)]

    def test_backslash_path_formula_expands(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\me\AppData\Local")
        assert (WindowsPlatform._expand_path(r"%LOCALAPPDATA%\Steam\steam.exe")
                == r"C:\Users\me\AppData\Local\Steam\steam.exe")

    def test_undefined_variable_names_the_variable(self):
        startfile = RecordingStartfile()
        with pytest.raises(PlatformError) as err:
            make_windows(startfile=startfile).open_path(
                r"%NOPE_NOT_SET%\games\thing.exe")
        assert "NOPE_NOT_SET" in str(err.value)
        assert startfile.targets == []

    def test_missing_file_is_reported_legibly(self, tmp_path):
        startfile = RecordingStartfile()
        missing = tmp_path / "gone.exe"
        with pytest.raises(PlatformError) as err:
            make_windows(startfile=startfile).open_path(str(missing))
        assert "not found" in str(err.value)
        assert str(missing) in str(err.value)
        assert startfile.targets == []

    def test_no_file_association_is_reported_legibly(self, tmp_path):
        doc = tmp_path / "notes.xyz"
        doc.write_text("x")
        boom = OSError(1, "No application is associated with the specified file")
        boom.winerror = 1155  # type: ignore[attr-defined]
        startfile = RecordingStartfile(raises=boom)
        with pytest.raises(PlatformError) as err:
            make_windows(startfile=startfile).open_path(str(doc))
        message = str(err.value)
        assert str(doc) in message
        assert "no application is associated" in message.lower()

    def test_blank_path_is_rejected(self):
        with pytest.raises(PlatformError):
            make_windows().open_path("   ")


# --------------------------------------------------------------------------
# platform.py — websites / store-scheme links
# --------------------------------------------------------------------------
class TestOpenUrl:
    """Regression: an unregistered scheme (a store link with no store
    installed) raised a raw OSError out of webbrowser/open_website.
    """

    def test_website_is_handed_to_the_default_browser(self, monkeypatch):
        opened = []
        monkeypatch.setattr(platform_mod.webbrowser, "open",
                            lambda url: opened.append(url) or True)
        make_windows().open_website("https://example.com/x")
        assert opened == ["https://example.com/x"]

    def test_unregistered_scheme_is_a_platform_error(self, monkeypatch):
        boom = OSError(1, "No application is associated with the specified file")
        boom.winerror = 1155  # type: ignore[attr-defined]

        def fake_open(url):
            raise boom

        monkeypatch.setattr(platform_mod.webbrowser, "open", fake_open)
        with pytest.raises(PlatformError) as err:
            make_windows().open_website("steam://rungameid/570")
        assert "steam://rungameid/570" in str(err.value)
        assert "no app is registered" in str(err.value)

    def test_false_return_is_not_a_failure(self, monkeypatch):
        """webbrowser returns False while a browser is still starting up."""
        monkeypatch.setattr(platform_mod.webbrowser, "open", lambda url: False)
        make_windows().open_website("https://example.com")

    def test_blank_url_is_rejected(self):
        with pytest.raises(PlatformError):
            make_windows().open_website("")

    def test_game_store_link_uses_the_scheme_handler(self, monkeypatch):
        opened = []
        monkeypatch.setattr(platform_mod.webbrowser, "open",
                            lambda url: opened.append(url) or True)
        make_windows().launch_game("steam://rungameid/570")
        assert opened == ["steam://rungameid/570"]

    def test_game_exe_uses_the_launch_path_not_a_scheme(self):
        startfile = RecordingStartfile()
        make_windows(startfile=startfile).launch_game(r"C:\Games\game.exe")
        assert startfile.targets == [r"C:\Games\game.exe"]

    def test_failing_scheme_surfaces_through_the_game_action(self, monkeypatch):
        def fake_open(url):
            raise OSError(1, "no association")

        monkeypatch.setattr(platform_mod.webbrowser, "open", fake_open)
        with pytest.raises(PlatformError):
            make_windows().launch_game("epic://launch/fortnite")


# --------------------------------------------------------------------------
# platform.py — launch
# --------------------------------------------------------------------------
class TestLaunch:
    """Regression: a bad path or a file with no handler surfaced as a raw
    FileNotFoundError / AttributeError from deep inside subprocess.
    """

    def test_shell_resolution_is_used_when_there_is_no_extension_handler(self):
        startfile = RecordingStartfile()
        popen = RecordingPopen()
        make_windows(startfile=startfile, popen=popen).launch("notepad")
        assert startfile.targets == ["notepad"]
        assert popen.calls == []  # shell handled it

    def test_unassociated_target_falls_back_to_direct_spawn(self):
        startfile = RecordingStartfile(raises=OSError(1, "no association"))
        popen = RecordingPopen()
        make_windows(startfile=startfile, popen=popen).launch(r"C:\App\app.exe")
        assert popen.argv == [r"C:\App\app.exe"]

    def test_missing_executable_is_a_platform_error(self, monkeypatch):
        startfile = RecordingStartfile(raises=OSError(1, "no association"))
        popen = RecordingPopen(raises=FileNotFoundError(2, "cannot find the file"))
        with pytest.raises(PlatformError) as err:
            make_windows(startfile=startfile, popen=popen).launch(
                r"C:\NoSuchFolder\app.exe")
        assert "app.exe" in str(err.value)

    def test_args_are_passed_without_a_shell(self):
        """A list argv with no shell= means no command injection through args."""
        popen = RecordingPopen()
        make_windows(popen=popen).launch(r"C:\App\app.exe", ["--profile", "a b"])
        assert popen.argv == [r"C:\App\app.exe", "--profile", "a b"]
        assert "shell" not in popen.kwargs
        assert popen.kwargs.get("creationflags") in (0, 0x10)

    def test_blank_target_is_rejected(self):
        with pytest.raises(PlatformError):
            make_windows().launch("")


# --------------------------------------------------------------------------
# platform.py — run_command
# --------------------------------------------------------------------------
class TestRunCommand:
    def test_stdin_is_devnull(self):
        """Regression: the child inherited the parent's stdin, so a command
        that prompts (pause, set /p, most CLIs) blocked until the timeout in a
        windowed app where no answer can ever be typed.
        """
        popen = RecordingPopen(proc=FakeProc(stdout="hello", returncode=0))
        assert make_windows(popen=popen).run_command("echo hello", 5.0) == "hello"
        assert popen.kwargs["stdin"] == subprocess.DEVNULL
        assert popen.argv == ["cmd", "/c", "echo hello"]
        assert "shell" not in popen.kwargs  # cmd.exe parses, Python does not

    def test_output_is_decoded_leniently(self):
        """Non-UTF8 console output must not raise UnicodeDecodeError."""
        assert _text_kwargs()["text"] is True
        assert _text_kwargs()["errors"] == "replace"
        popen = RecordingPopen(proc=FakeProc(stdout="ok", returncode=0))
        make_windows(popen=popen).run_command("type file", 5.0)
        assert popen.kwargs["errors"] == "replace"
        assert popen.kwargs["text"] is True

    def test_timeout_kills_the_whole_process_tree(self):
        proc = FakeProc(communicate_raises=subprocess.TimeoutExpired("cmd", 1))
        popen = RecordingPopen(proc=proc)
        run = RecordingRun()
        plat = make_windows(run=run, popen=popen)
        with pytest.raises(PlatformError) as err:
            plat.run_command("ping -t 127.0.0.1", 1.0)
        assert "timed out" in str(err.value)
        # taskkill /F /T /PID <pid> stops the children cmd.exe spawned, then the
        # handle is released so no zombie/pipe leak survives.
        assert run.argv == ["taskkill", "/F", "/T", "/PID", str(proc.pid)]
        assert proc.killed is True
        assert proc.communicate_calls[-1] == 5  # reaped

    def test_nonzero_exit_reports_stderr(self):
        popen = RecordingPopen(proc=FakeProc(stderr="denied", returncode=1))
        with pytest.raises(PlatformError) as err:
            make_windows(popen=popen).run_command("net use", 5.0)
        assert "denied" in str(err.value)

    def test_missing_shell_is_a_platform_error(self):
        popen = RecordingPopen(raises=FileNotFoundError(2, "cmd not found"))
        with pytest.raises(PlatformError):
            make_windows(popen=popen).run_command("dir", 5.0)


# --------------------------------------------------------------------------
# platform.py — power
# --------------------------------------------------------------------------
class TestPower:
    """Regression: the action value comes from user JSON; a list/dict value was
    unhashable and raised TypeError out of the command lookup. The destructive
    commands themselves must not change silently either.
    """

    def test_unknown_action_is_rejected(self):
        run, popen = RecordingRun(), RecordingPopen()
        for bad in ("format", "hibernate", "", None, ["shutdown"], {"a": 1}, 0):
            with pytest.raises(PlatformError):
                make_windows(run=run, popen=popen).power(bad)  # type: ignore[arg-type]
        assert run.calls == [] and popen.calls == []

    @pytest.mark.parametrize("action", ["restart", "shutdown"])
    def test_destructive_argv_is_exact(self, action):
        run = RecordingRun(_Result(returncode=0))
        make_windows(run=run).power(action)
        assert run.argv == ["powershell", "-NoProfile", "-Command",
                            platform_mod.POWER_COMMANDS[action]]
        assert run.argv[-1] == f"shutdown /{'r' if action == 'restart' else 's'} /t 0 /f"
        assert run.kwargs.get("timeout") == 30

    def test_lock_exits_nonzero_when_the_lock_fails(self):
        # PowerShell returns 0 even when LockWorkStation() reports failure, so
        # the command itself has to exit non-zero or the UI reports success.
        assert "exit 1" in platform_mod.POWER_COMMANDS["lock"]
        assert "LockWorkStation" in platform_mod.POWER_COMMANDS["lock"]

    def test_lock_failure_is_surfaced(self):
        run = RecordingRun(_Result(returncode=1, stderr="lock refused"))
        with pytest.raises(PlatformError) as err:
            make_windows(run=run).power("lock")
        assert "lock" in str(err.value) and "lock refused" in str(err.value)

    def test_sleep_is_fire_and_forget(self):
        run, popen = RecordingRun(), RecordingPopen()
        make_windows(run=run, popen=popen).power("sleep")
        assert popen.argv == ["powershell", "-NoProfile", "-Command",
                              platform_mod.POWER_COMMANDS["sleep"]]
        assert run.calls == []  # never waited on: the machine suspends

    def test_sleep_spawn_failure_is_a_platform_error(self):
        popen = RecordingPopen(raises=FileNotFoundError(2, "powershell not found"))
        with pytest.raises(PlatformError):
            make_windows(popen=popen).power("sleep")

    def test_blocked_power_command_timeout_is_a_platform_error(self):
        def always_hang(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 30)

        with pytest.raises(PlatformError) as err:
            make_windows(run=always_hang).power("shutdown")
        assert "shutdown" in str(err.value) and "timed out" in str(err.value)


# --------------------------------------------------------------------------
# platform.py — list_running_processes
# --------------------------------------------------------------------------
class TestListRunningProcesses:
    TASKLIST_CSV = (
        '"Image Name","PID","Session Name","Session#","Mem Usage"\n'
        '"chrome.exe","1234","Console","1","123,456 K"\n'
        '"chrome_helper.exe","4321","Console","1","1,234 K"\n'
        '"firefox.exe","999","Console","1","9,999 K"\n'
    )

    def test_csv_is_parsed_so_the_memory_column_cannot_match(self):
        run = RecordingRun(_Result(stdout=self.TASKLIST_CSV))
        found = make_windows(run=run).list_running_processes("chrome")
        assert found == ["chrome.exe (1234)", "chrome_helper.exe (4321)"]
        assert run.argv == ["tasklist", "/fo", "csv", "/nh"]

    def test_hint_filtering_is_case_insensitive_and_optional(self):
        run = RecordingRun(default=_Result(stdout=self.TASKLIST_CSV))
        plat = make_windows(run=run)
        assert plat.list_running_processes("FIREFOX") == ["firefox.exe (999)"]
        assert plat.list_running_processes("") == [
            "chrome.exe (1234)", "chrome_helper.exe (4321)", "firefox.exe (999)"]

    def test_runner_without_stdout_returns_empty(self):
        """Regression: stdout was read outside the try, so a degraded runner
        (or a killed tasklist) raised AttributeError out of the action.
        """
        class NoStdout:
            returncode = 1

        plat = make_windows(run=lambda cmd, **kw: NoStdout())
        assert plat.list_running_processes("chrome") == []

    def test_tasklist_failure_returns_empty(self):
        def boom(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 15)

        assert make_windows(run=boom).list_running_processes("chrome") == []


# --------------------------------------------------------------------------
# platform.py — GenericPlatform (POSIX dev/test host)
# --------------------------------------------------------------------------
class TestGenericClose:
    """Regression: `pkill -f <empty>` matches every process, and a name
    starting with '-' was parsed as a pkill option.
    """

    def test_empty_pattern_is_refused(self, monkeypatch):
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
        with pytest.raises(PlatformError):
            GenericPlatform().close_application("   ")
        assert calls == []

    def test_double_dash_stops_option_parsing(self, monkeypatch):
        calls = []

        class R:
            returncode = 0

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return R()

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert GenericPlatform().close_application("-9") is True
        assert calls == [["pkill", "-f", "--", "-9"]]


# --------------------------------------------------------------------------
# actions.py — values that come from user JSON
# --------------------------------------------------------------------------
class TestActionInputs:
    """Regression: params['action'] from JSON could be a list/dict, which is
    unhashable and raised TypeError instead of rejecting the step.
    """

    @pytest.mark.parametrize("value", [["shutdown"], {"a": 1}, None, "explode", 5])
    async def test_invalid_power_params_never_reach_the_platform(self, value):
        plat = FakePlatform()
        action = Action(type="power", params={"action": value})
        with pytest.raises(PlatformError) as err:
            await dispatch_action(plat, action)
        assert "unsupported power action" in str(err.value)
        assert plat.calls == []  # nothing destructive ran

    @pytest.mark.parametrize("value", ["lock", "sleep", "restart", "shutdown"])
    async def test_valid_power_params_dispatch(self, value):
        plat = FakePlatform()
        await dispatch_action(plat, Action(type="power", params={"action": value}))
        assert plat.calls == [("power", (value,))]

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    async def test_non_finite_delay_is_rejected(self, value):
        plat = FakePlatform()
        with pytest.raises(PlatformError) as err:
            # asyncio.timeout is a safety net: asyncio.sleep(inf) never returns,
            # so without the guard this test would hang instead of failing.
            async with asyncio.timeout(1):
                await dispatch_action(
                    plat, Action(type="delay", params={"seconds": value}))
        assert "finite" in str(err.value)

    async def test_delay_accepts_string_and_missing_seconds(self):
        plat = FakePlatform()
        await dispatch_action(plat, Action(type="delay", params={"seconds": "0.01"}))
        await dispatch_action(plat, Action(type="delay", params={}))
        await dispatch_action(plat, Action(type="delay", target="0.01"))

    async def test_junk_delay_is_reported_legibly(self):
        plat = FakePlatform()
        with pytest.raises(PlatformError) as err:
            await dispatch_action(
                plat, Action(type="delay", params={"seconds": "soon"}))
        assert "must be a number" in str(err.value)


# --------------------------------------------------------------------------
# desktop.py / launcher.py — frozen-build startup
# --------------------------------------------------------------------------
class TestFrozenStartup:
    def test_data_dir_is_prepared_before_the_stream_guard(self, monkeypatch, tmp_path):
        """Regression: _ensure_streams() ran first, so in portable mode the log
        file (the only diagnostics a windowed build has) was written to the
        process working directory instead of next to the data.
        """
        import desktop

        root = tmp_path / "Easy Life"
        root.mkdir()
        exe = root / "Easy Life.exe"
        exe.write_text("")
        (root / "portable.txt").write_text("")  # opt in to portable mode

        saved_env = os.environ.copy()
        try:
            monkeypatch.setenv("PCRITUALS_PORTABLE", "1")
            monkeypatch.delenv("PCRITUALS_DATA_DIR", raising=False)
            monkeypatch.setattr(sys, "frozen", True, raising=False)
            monkeypatch.setattr(sys, "executable", str(exe))
            monkeypatch.setattr(sys, "stdout", None)
            monkeypatch.setattr(sys, "stderr", None)
            monkeypatch.chdir(tmp_path)

            def stop_after_setup():
                raise RuntimeError("stop before the real server starts")

            monkeypatch.setattr(desktop, "_start_server", stop_after_setup)
            with pytest.raises(RuntimeError):
                desktop.main()
        finally:
            os.environ.clear()
            os.environ.update(saved_env)

        assert (root / "data" / "pcrituals.log").exists()

    def test_launcher_guards_streams_before_starting_the_server(self, monkeypatch, tmp_path):
        """Regression: launcher.py called api.run() with the default uvicorn
        log config, which builds a formatter from sys.stdout and died with
        "Unable to configure formatter 'default'" in a console-less build.
        """
        import launcher
        import pcrituals.api as api

        seen = {}

        def fake_run():
            seen["stdout_is_none"] = sys.stdout is None
            seen["stderr_is_none"] = sys.stderr is None

        monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)
        monkeypatch.setattr(api, "run", fake_run)
        monkeypatch.delattr(sys, "frozen", raising=False)
        launcher.main()
        assert seen == {"stdout_is_none": False, "stderr_is_none": False}

    def test_uvicorn_default_logging_is_why_the_guard_exists(self, monkeypatch):
        """Documents the failure the entry points guard against: uvicorn's
        default log config cannot be built without a stdout/stderr stream.
        """
        import uvicorn

        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)
        app = lambda scope, receive, send: None  # noqa: E731
        with pytest.raises(Exception) as err:
            uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="info")
        assert "formatter" in str(err.value).lower() or "isatty" in str(err.value).lower()
        # The same config with log_config=None (what desktop.py passes) is fine,
        # so the guard + log_config=None together cover both entry points.
        uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="info",
                       log_config=None)

    def test_desktop_server_uses_no_log_config(self):
        import inspect
        import desktop

        source = inspect.getsource(desktop._start_server)
        assert "log_config=None" in source
        assert "daemon=True" in source  # never blocks interpreter shutdown

    def test_webview_fallback_keeps_the_server_referenced(self):
        """A browser fallback must not let the server thread be collected.

        The server thread is a daemon, so returning from main() kills it. Both
        the native-window path and the browser fallback therefore block on
        _stay_alive() rather than falling off the end of main().
        """
        import inspect
        import desktop

        source = inspect.getsource(desktop.main)
        assert "webbrowser.open(url)" in source
        assert source.count("_stay_alive(server)") >= 2, source
        # ...and the block ends when something asks the app to stop.
        assert "server.should_exit" in inspect.getsource(desktop._stay_alive)

    def test_background_flag_serves_without_opening_a_window(self):
        """--background is what the Windows login entry passes: serve only.

        The user's requirement is that starting the app shows nothing at all —
        no window, no console, no script.
        """
        import inspect
        import desktop

        source = inspect.getsource(desktop.main)
        assert '"--background" in sys.argv' in source, source
        assert "if background:" in source, source

        import pcrituals.startup as startup
        assert startup.BACKGROUND_FLAG == "--background"
