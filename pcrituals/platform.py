"""Cross-platform action execution.

`Platform` is an abstraction over the host OS. The concrete `WindowsPlatform`
works on Windows (powershell for power controls, os.startfile / subprocess for
launching); on any other OS a generic implementation is used so the engine is
testable and doesn't crash outside Windows.

Testability note
----------------
The host the app is *developed* on is not the host it *runs* on, so every OS
call in `WindowsPlatform` goes through `self._run` / `self._popen` /
`self._startfile` (defaults: `subprocess.run` / `subprocess.Popen` /
`os.startfile`). Tests inject fakes and exercise the real Windows argument
construction, process-name normalisation, csv parsing, path/env expansion and
timeout handling on any platform, instead of leaving that logic unverified.
"""
from __future__ import annotations

import csv
import io
import os
import re
import signal
import subprocess
import sys
import time
import webbrowser
from typing import Any, Callable, Optional

# How long a taskkill/tasklist invocation may take before it is considered hung.
PROCESS_TOOL_TIMEOUT_S = 15.0

# Image names (lower-case, with the .exe suffix taskkill matches on) that an
# action must never be allowed to close. Killing any of these does not "close an
# app": smss/csrss/wininit/winlogon/services/lsass take the whole session down
# with a bluescreen, and svchost hosts unrelated services. Close actions can be
# fired remotely (phone / voice), so the refusal belongs here, not in the UI.
CRITICAL_PROCESS_IMAGES = frozenset({
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "svchost.exe", "system", "system.exe", "registry",
})

# `%NAME%` is how Windows users write environment variables; it is what the
# file-picker / docs produce. Kept as a module constant so path expansion can be
# exercised on a non-Windows host.
_WIN_VAR_RE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")

# PowerShell commands per power action. Module level so the exact argv sent to
# the shell (the part that decides whether the machine locks, sleeps or wipes
# its uptime) can be asserted in tests.
POWER_COMMANDS: dict[str, str] = {
    # Exit non-zero when LockWorkStation() returns false: PowerShell would
    # otherwise exit 0 for a lock that never happened and the UI would report
    # the step as executed.
    "lock": ("Add-Type '[DllImport(\"user32.dll\")]public static extern bool "
             "LockWorkStation();' -Name U -Namespace W; "
             "if (-not [W.U]::LockWorkStation()) { exit 1 }"),
    "sleep": "rundll32.exe powrprof.dll,SetSuspendState 0,1,0",
    "restart": "shutdown /r /t 0 /f",
    "shutdown": "shutdown /s /t 0 /f",
}


def _power_command(action: object) -> Optional[str]:
    """Return the PowerShell command for a power action, or None if unknown.

    `action` comes from user JSON, so it may be any type: a list/dict is
    unhashable and used to raise TypeError out of the dict lookup instead of the
    legible PlatformError the engine can report.
    """
    if not isinstance(action, str):
        return None
    return POWER_COMMANDS.get(action)


def _default_startfile(target: str) -> None:
    """Open `target` through the Windows shell (ShellExecute).

    Kept as a module-level function so it can be injected: os.startfile only
    exists on Windows, which would leave every caller of it untestable.
    """
    os.startfile(target)  # type: ignore[attr-defined]


class PlatformError(RuntimeError):
    """Raised when a platform operation fails."""


def _text_kwargs() -> dict[str, Any]:
    """Keyword args for text-mode subprocess I/O.

    Windows console programs write the OEM/ANSI code page, not UTF-8, so a
    command that prints a non-ASCII character raises UnicodeDecodeError with
    the default (strict) decoding and the whole action fails. `errors="replace"`
    degrades to a replacement character instead of aborting the step.
    """
    return {"text": True, "errors": "replace"}


class Platform:
    """Interface for executing OS-level actions."""

    name = "generic"

    def launch(self, target: str, args: list[str] | None = None) -> None:
        raise NotImplementedError

    def open_path(self, path: str) -> None:
        raise NotImplementedError

    def open_website(self, url: str) -> None:
        raise NotImplementedError

    def run_command(self, command: str, timeout: float, cwd: str | None = None,
                    cancel_event: Any = None) -> str:
        """Run a command and return captured stdout.

        `cancel_event` (a threading.Event) lets a caller stop a running command;
        implementations must kill the process tree when it is set.
        """
        raise NotImplementedError

    def launch_game(self, target: str, args: list[str] | None = None) -> None:
        raise NotImplementedError

    def close_application(self, process_name: str) -> bool:
        """Attempt a graceful close. Return True if a process was signalled."""
        raise NotImplementedError

    def power(self, action: str) -> None:
        raise NotImplementedError

    def cpu_load(self) -> float:
        raise NotImplementedError

    def memory_usage(self) -> dict:
        raise NotImplementedError

    def list_running_processes(self, hint: str) -> list[str]:
        raise NotImplementedError

    # Injectable so tests can drive OS interaction without spawning anything,
    # and so both platforms share one kill path.
    _popen = staticmethod(subprocess.Popen)

    def _kill_tree(self, proc: Any) -> None:
        """Force-kill a process and its children, then release its pipes."""
        try:
            if hasattr(os, "killpg") and hasattr(os, "getpgid"):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass


def _wait_with_cancel(proc: Any, timeout: float, cancel_event: Any, kill: Any,
                      label: str = "command") -> tuple:
    """Wait for a process, honouring a cancel request.

    Nothing can interrupt `subprocess.run(timeout=...)`, which is why pressing
    Stop used to return success while the command kept running to completion —
    dangerous for a "run command" step. Waiting in short slices lets a cancel
    request kill the process tree instead.

    Raises PlatformError('cancelled by user') if the event is set, and
    PlatformError('<label> timed out ...') on the deadline.
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            kill(proc)
            raise PlatformError(f"{label} timed out after {timeout:g}s")
        try:
            return proc.communicate(timeout=min(0.25, remaining))
        except subprocess.TimeoutExpired:
            if cancel_event is not None and cancel_event.is_set():
                kill(proc)
                raise PlatformError("cancelled by user")


class GenericPlatform(Platform):
    """Works on POSIX/any host. Used for tests and non-Windows dev."""

    name = "generic"

    def launch(self, target: str, args: list[str] | None = None) -> None:
        cmd = [target] + (args or [])
        subprocess.Popen(cmd)  # noqa: S603 - list form, no shell

    def open_path(self, path: str) -> None:
        expanded = os.path.expanduser(path)
        if os.path.isdir(expanded):
            subprocess.Popen(["xdg-open", expanded])  # noqa: S603
        else:
            webbrowser.open("file://" + expanded)

    def open_website(self, url: str) -> None:
        webbrowser.open(url)

    def run_command(self, command: str, timeout: float, cwd: str | None = None,
                    cancel_event: Any = None) -> str:
        proc = self._popen(command, shell=True, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           cwd=cwd, start_new_session=True, **_text_kwargs())
        out, err = _wait_with_cancel(proc, timeout, cancel_event, self._kill_tree)
        if proc.returncode != 0:
            raise PlatformError(f"command exited {proc.returncode}: {(err or '').strip()}")
        return (out or "").strip()

    def launch_game(self, target: str, args: list[str] | None = None) -> None:
        self.launch(target, args)

    def close_application(self, process_name: str) -> bool:
        # `pkill -f <pattern>`: an empty pattern matches EVERY process, so a
        # blank name would kill the developer's whole session; a name starting
        # with "-" would be parsed by pkill as an option. `--` ends option
        # parsing, and a blank name is refused.
        pattern = (process_name or "").strip()
        if not pattern:
            raise PlatformError("no process name given")
        try:
            proc = subprocess.run(["pkill", "-f", "--", pattern],
                                  capture_output=True, timeout=PROCESS_TOOL_TIMEOUT_S)
            return proc.returncode == 0
        except Exception:
            return False

    def power(self, action: str) -> None:
        # Deliberately a no-op that claims success so tests can exercise the engine.
        pass

    def cpu_load(self) -> float:
        try:
            import psutil
            return psutil.cpu_percent(interval=0.05)
        except Exception:
            return 0.0

    def memory_usage(self) -> dict:
        try:
            import psutil
            m = psutil.virtual_memory()
            return {"percent": m.percent, "used": m.used, "total": m.total}
        except Exception:
            return {"percent": 0.0, "used": 0, "total": 0}

    def list_running_processes(self, hint: str) -> list[str]:
        try:
            out = subprocess.run(["pgrep", "-fl", hint], capture_output=True,
                                 timeout=PROCESS_TOOL_TIMEOUT_S, **_text_kwargs())
            return [l.strip() for l in (out.stdout or "").splitlines() if l.strip()]
        except Exception:
            return []


class WindowsPlatform(Platform):
    """Windows-specific implementation.

    - Apps / files / folders:   os.startfile for shell resolution.
    - Websites:                 Windows opens the default browser.
    - Games:                    store:// URLs (e.g. steam://rungameid/...) or exe.
    - Commands:                 cmd /c, with a timeout that kills the child tree.
    - Close app:                taskkill /IM <name> (graceful first, /F fallback).
    - Power controls:           PowerShell commands.
    - Metrics:                  psutil when available.

    Safety guards, because these actions can be triggered from a phone or by
    voice and nothing here is reversible: `power` only accepts the whitelisted
    action strings, and `close_application` refuses wildcard names (taskkill
    would expand `*` to every executable) and the system-critical images.
    """

    name = "windows"

    def __init__(self, *, run: Optional[Callable[..., Any]] = None,
                 popen: Optional[Callable[..., Any]] = None,
                 startfile: Optional[Callable[[str], None]] = None) -> None:
        # Seams: real OS calls by default, injectable fakes in tests.
        self._run = run or subprocess.run
        self._popen = popen or subprocess.Popen
        self._startfile = startfile or _default_startfile
        try:
            import psutil  # noqa: F401
            self._psutil = True
        except Exception:
            self._psutil = False

    def _start(self, cmd: list[str]) -> None:
        creationflags = 0
        if sys.platform == "win32":
            # CREATE_NEW_CONSOLE so GUI apps detach cleanly.
            creationflags = 0x00000010
        try:
            self._popen(cmd, creationflags=creationflags)
        except OSError as e:
            # FileNotFoundError for a bad path, PermissionError for a blocked
            # exe. The engine shows str(e) to the user, so it must not be a bare
            # "[WinError 2] The system cannot find the file specified".
            raise PlatformError(
                f"could not launch {cmd[0]!r}: {getattr(e, 'strerror', None) or e}") from e

    def launch(self, target: str, args: list[str] | None = None) -> None:
        if not target:
            raise PlatformError("no application target given")
        if args:
            cmd = [target] + list(args)
            self._start(cmd)
        else:
            # os.startfile lets Windows resolve the app by name / open default handler.
            try:
                self._startfile(target)
            except OSError:
                # No shell association (or the path is not an executable): fall
                # back to spawning it directly, which reports the real failure.
                self._start([target])
            except AttributeError:
                # os.startfile does not exist on this host (non-Windows dev).
                self._start([target])

    @classmethod
    def _expand_path(cls, path: str) -> str:
        """Expand `%VAR%` / `~` in a user-supplied path, refusing unknowns.

        `os.path.expandvars` leaves a `%LOCALAPPDATA%` it cannot resolve in the
        string, so a typo'd variable silently became a literal folder name and
        the failure surfaced as \"cannot find the file\". Naming the variable is
        the only thing that tells the user what to fix.
        """
        raw = (path or "").strip().strip('"')
        if not raw:
            raise PlatformError("no path given")
        # Expand %VAR% ourselves instead of relying on os.path.expandvars: that
        # function is host-specific (POSIX expands $VAR, Windows %VAR%), which
        # would make this branch untestable off Windows.
        raw = _WIN_VAR_RE.sub(
            lambda m: os.environ.get(m.group(1), m.group(0)), raw)
        raw = os.path.expandvars(os.path.expanduser(raw))
        missing = sorted({m.group(1) for m in _WIN_VAR_RE.finditer(raw)})
        if missing:
            raise PlatformError(
                f"path {path!r} uses undefined environment variable(s): "
                f"{', '.join(missing)}")
        return raw

    def open_path(self, path: str) -> None:
        resolved = self._expand_path(path)
        if not os.path.exists(resolved):
            raise PlatformError(f"file or folder not found: {resolved}")
        try:
            self._startfile(resolved)
        except OSError as e:
            detail = getattr(e, "strerror", None) or str(e)
            if getattr(e, "winerror", None) == 1155 or "associat" in detail.lower():
                detail += (" (no application is associated with this file type - "
                           "set a default program for it)")
            raise PlatformError(f"could not open {resolved}: {detail}") from e

    def open_website(self, url: str) -> None:
        if not url:
            raise PlatformError("no url given")
        self._open_url(url)

    def _open_url(self, url: str) -> None:
        """Hand a URL / custom scheme to the shell.

        An unregistered scheme (a game store link on a PC without that store)
        makes os.startfile raise OSError, which is the shell's way of saying
        "nothing is installed to handle this" - the step must report that, not
        a bare WinError. A False *return* is not treated as failure:
        webbrowser.open also returns False when it has successfully handed the
        URL to a browser that is still starting up.
        """
        try:
            webbrowser.open(url)
        except OSError as e:
            detail = getattr(e, "strerror", None) or str(e)
            if getattr(e, "winerror", None) == 1155 or "associat" in detail.lower():
                detail += " (no app is registered to handle this link)"
            raise PlatformError(f"could not open {url!r}: {detail}") from e

    def run_command(self, command: str, timeout: float, cwd: str | None = None,
                    cancel_event: Any = None) -> str:
        """Run `cmd /c <command>` and return its stdout.

        The shell is used on purpose: a "run command" step is meant to accept
        normal command-line syntax (pipes, redirection) exactly as typed by the
        user on their own machine. The command is passed as a single argv
        element with shell=False, so it is cmd.exe - not Python - that parses
        it, and there is no additional quoting layer to escape.
        """
        try:
            proc = self._popen(["cmd", "/c", command],
                               stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, cwd=cwd, **_text_kwargs())
        except OSError as e:
            raise PlatformError(
                f"could not start the command shell: {getattr(e, 'strerror', None) or e}") from e
        # stdin is DEVNULL on purpose: with the default (inherit) a command that
        # asks for input - `pause`, `set /p`, most interactive CLIs - blocks
        # until the timeout in a windowed app, where the inherited handle is
        # invalid and the prompt can never be answered.
        # Killing cmd.exe alone leaves the command it spawned running forever,
        # so the whole process tree is terminated (taskkill /T); the same path
        # runs when the user cancels.
        out, err = _wait_with_cancel(proc, timeout, cancel_event, self._kill_tree)
        if proc.returncode != 0:
            raise PlatformError(
                f"command exited {proc.returncode}: {(err or '').strip()}")
        return (out or "").strip()

    def _kill_tree(self, proc: Any) -> None:
        """Force-kill a process and its children, then release its pipes."""
        pid = getattr(proc, "pid", None)
        if pid:
            try:
                self._run(["taskkill", "/F", "/T", "/PID", str(pid)],
                          capture_output=True, timeout=PROCESS_TOOL_TIMEOUT_S,
                          **_text_kwargs())
            except Exception:
                pass
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass

    def launch_game(self, target: str, args: list[str] | None = None) -> None:
        # Store scheme URLs (steam://, epic://, etc) are opened by the OS handler.
        if "://" in target:
            self._open_url(target)
        else:
            self.launch(target, args)

    @staticmethod
    def _image_name(process_name: str) -> str:
        """Normalise a user-supplied name to the image name taskkill matches.

        taskkill /IM matches the executable's image name, so a full path must be
        reduced to its file name and ".exe" appended when it is missing. This
        used to be `process_name.rstrip(".exe") + ".exe"`, which strips a *set
        of characters* rather than a suffix: "firefox.exe" became "firefo.exe"
        and "code.exe" became "cod.exe", so closing those apps silently failed.
        """
        name = (process_name or "").strip().strip('"')
        # Split on both separators explicitly: os.path.basename is host-specific
        # and would not split a Windows path when running on another OS.
        name = name.replace("/", "\\").rsplit("\\", 1)[-1].strip()
        if not name:
            raise PlatformError("no process name given")
        if "." not in name:
            name += ".exe"
        return name

    @staticmethod
    def _validate_kill_target(image: str) -> None:
        """Refuse names that would close far more than "an app".

        taskkill /IM accepts `*`/`?` wildcards, so `*` (or the bare `steam*` a
        user might type hoping for a substring match) expands to every running
        executable - or to unrelated helper processes. A close action can be
        triggered from a phone or by voice, and there is no undo.
        """
        if any(ch in image for ch in "*?"):
            raise PlatformError(
                f"refusing wildcard process name {image!r}: it would close every "
                "matching program. Use the exact image name (e.g. steam.exe).")
        if image.lower() in CRITICAL_PROCESS_IMAGES:
            raise PlatformError(
                f"refusing to close system-critical process {image!r}: "
                "Windows does not survive it.")

    def close_application(self, process_name: str) -> bool:
        image = self._image_name(process_name)
        self._validate_kill_target(image)
        # Graceful close first (posts WM_CLOSE so the app can save).
        try:
            graceful = self._run(["taskkill", "/IM", image], capture_output=True,
                                 timeout=PROCESS_TOOL_TIMEOUT_S, **_text_kwargs())
            if graceful.returncode == 0:
                return True
        except subprocess.TimeoutExpired:
            # A hung app can leave even the graceful taskkill waiting. Fall
            # through to the forced kill rather than failing the step (which
            # would report "could not close" while the app keeps running).
            pass
        # Force close fallback.
        try:
            force = self._run(["taskkill", "/F", "/IM", image], capture_output=True,
                              timeout=PROCESS_TOOL_TIMEOUT_S, **_text_kwargs())
        except subprocess.TimeoutExpired as e:
            raise PlatformError(f"closing {image} timed out") from e
        return force.returncode == 0

    def power(self, action: str) -> None:
        cmd = _power_command(action)
        if not cmd:
            raise PlatformError(f"unsupported power action: {action!r}")
        argv = ["powershell", "-NoProfile", "-Command", cmd]
        if action == "sleep":
            # Fire and forget: the machine suspends during this call, so waiting
            # on it (or applying a timeout) would either block the ritual until
            # resume or fail it with a spurious timeout afterwards.
            try:
                self._popen(argv)
            except OSError as e:
                raise PlatformError(
                    f"power action 'sleep' could not start: "
                    f"{getattr(e, 'strerror', None) or e}") from e
            return
        try:
            proc = self._run(argv, capture_output=True, timeout=30, **_text_kwargs())
        except subprocess.TimeoutExpired as e:
            # A shutdown that blocks (a stuck service, a waiting dialog) must
            # not surface as a raw subprocess error.
            raise PlatformError(f"power action '{action}' timed out") from e
        if proc.returncode != 0:
            err = (proc.stderr or "").strip() or f"exit code {proc.returncode}"
            # Surface the failure: the UI used to report "executed" even when
            # e.g. the lock call was rejected.
            raise PlatformError(f"power action '{action}' failed: {err}")

    def cpu_load(self) -> float:
        if self._psutil:
            import psutil
            return psutil.cpu_percent(interval=0.05)
        return 0.0

    def memory_usage(self) -> dict:
        if self._psutil:
            import psutil
            m = psutil.virtual_memory()
            return {"percent": m.percent, "used": m.used, "total": m.total}
        return {"percent": 0.0, "used": 0, "total": 0}

    def list_running_processes(self, hint: str) -> list[str]:
        """Return "image.exe (pid)" lines whose image name contains `hint`."""
        try:
            result = self._run(["tasklist", "/fo", "csv", "/nh"], capture_output=True,
                               timeout=PROCESS_TOOL_TIMEOUT_S, **_text_kwargs())
        except Exception:
            return []
        # capture_output always sets stdout, but a degraded/injected runner may
        # not; reading it unguarded raised AttributeError out of the action.
        text = getattr(result, "stdout", None) or ""
        needle = (hint or "").strip().lower()
        found: list[str] = []
        # Parse the CSV properly: the memory column itself contains commas, so
        # a substring match over the raw line can match the wrong process.
        for row in csv.reader(io.StringIO(text)):
            if not row:
                continue
            name = row[0].strip()
            if name.lower() == "image name":
                # tasklist /nh currently omits the header; skip it anyway so a
                # header can never be offered to the user as a process name.
                continue
            if needle and needle not in name.lower():
                continue
            pid = row[1].strip() if len(row) > 1 else ""
            found.append(f"{name} ({pid})" if pid else name)
        return found


def get_platform() -> Platform:
    """Return the platform implementation for the current OS."""
    if sys.platform == "win32":
        return WindowsPlatform()
    return GenericPlatform()
