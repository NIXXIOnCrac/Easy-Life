"""Application configuration, loaded from environment variables / config file."""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Tables that identify *who may control this PC* rather than ritual data.
# A migration must never move these into a fresh install (see
# `_strip_credentials`): adopting another install's account is exactly what an
# attacker who plants a database in a shared directory wants.
CREDENTIAL_TABLES = ("users", "sessions", "devices", "pairing_codes")

# Key files that are credential material for the same reason.
CREDENTIAL_FILES = ("server_secret", "auth_token")


def _log(message: str) -> None:
    """Best-effort diagnostic line for migration decisions.

    Never raises: the packaged (windowed) build has no console and both
    `sys.stdout` and `sys.stderr` can be None there.
    """
    try:
        stream = sys.stderr if sys.stderr is not None else sys.stdout
        if stream is not None:
            print(f"[pcrituals] {message}", file=stream)
    except Exception:
        pass


def default_data_dir() -> Path:
    """The one stable place the app keeps its data, shared by BOTH the source
    install and the packaged .exe so they see the same rituals.

    Windows : %LOCALAPPDATA%\\Easy Life\\data
    other   : ~/.pcrituals/data
    Portable override: set PCRITUALS_DATA_DIR, or put a file named
    `portable.txt` next to the executable.
    """
    env = os.environ.get("PCRITUALS_DATA_DIR")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "Easy Life" / "data"
    return Path.home() / ".pcrituals" / "data"


def _temp_roots() -> list[Path]:
    """The system temp area: shared, disposable ground that anyone can write to.

    On Linux `/tmp` is world-writable (mode 1777), so a `pcrituals.db` planted
    there by *any* other user must never be treated as this install's data.
    """
    roots = [Path(tempfile.gettempdir())]
    if os.name == "posix":
        roots += [Path("/tmp"), Path("/var/tmp"), Path("/dev/shm")]
    return roots


def _norm(path: Path) -> Path:
    try:
        return Path(os.path.normcase(str(path.resolve())))
    except OSError:
        return Path(os.path.normcase(str(path)))


def _is_in_temp_dir(path: Path) -> bool:
    """True when `path` lives under a system temp directory."""
    target = _norm(path)
    for root in _temp_roots():
        r = _norm(root)
        if target == r or r in target.parents:
            return True
    return False


def _legacy_data_dirs() -> list[Path]:
    """Old locations data may live in (for one-time migration).

    The system temp directory is deliberately NOT in this list. On Linux it is
    world-writable, so a brand-new install once adopted `/tmp/pcrituals/
    pcrituals.db` — including its `users` and `sessions` tables — which means
    anyone who could write to `/tmp` could plant the account the app then
    adopted. Migration is only ever from a directory this user owns; see
    `legacy_dir_rejection`.
    """
    dirs: list[Path] = []
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent / "data")
    else:
        try:
            dirs.append(Path(__file__).resolve().parent.parent / "data")
        except Exception:
            pass
    # The app was previously called "PC Rituals" and kept its data under
    # %LOCALAPPDATA%\PC Rituals\data. Renaming the product moved that folder, so
    # the old one is a migration source — otherwise an existing install would
    # come up looking empty and the user's plays would appear to be gone.
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        dirs.append(Path(base) / "PC Rituals" / "data")
    else:
        dirs.append(Path.home() / ".pcrituals" / "data")
    return dirs


def legacy_dir_rejection(legacy: Path) -> str | None:
    """Why `legacy` must not be adopted, or None when it is safe.

    A legacy directory is adopted only when this process can *verify* it is
    ours: it must not be in the system temp area, must not be a symlink, and on
    POSIX must be owned by the current user and not group- or world-writable
    (a directory other accounts can write to is not ours to trust).
    """
    # Decide on the path alone first. A temp path is never an acceptable source
    # whether or not it currently exists, and checking existence first meant a
    # missing /tmp/pcrituals reported "cannot be inspected" instead — the rule
    # looked like it depended on filesystem state when it does not.
    if _is_in_temp_dir(legacy):
        return "lives in the system temp directory"
    try:
        if legacy.is_symlink():
            return "is a symlink"
        st = legacy.stat()
    except OSError as exc:
        return f"cannot be inspected ({exc})"
    if not stat.S_ISDIR(st.st_mode):
        return "is not a directory"
    if os.name == "posix":
        getuid = getattr(os, "getuid", None)
        uid = getattr(st, "st_uid", None)
        if getuid is not None and uid is not None and uid != getuid():
            return f"is owned by another user (uid {uid})"
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return "is group- or world-writable"
    return None


def _dir_has_files(path: Path) -> bool:
    """True when `path` holds anything a user would miss.

    Empty directories don't count: `ensure_dirs()` runs before migration and
    creates the data dir plus an empty `rituals/` subdirectory, so a *brand
    new* target must still read as empty here.
    """
    if not path.exists():
        return False
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_file(follow_symlinks=False):
                        return True
                    if entry.is_dir(follow_symlinks=False) and _dir_has_files(Path(entry.path)):
                        return True
                except OSError:
                    return True
    except OSError:
        return True  # unreadable target: leave it alone
    return False


def _db_tables(path: Path) -> set[str]:
    """Table names in a SQLite file (read-only; raises sqlite3.Error if it is
    not a database at all)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        conn.close()
    return {str(r[0]) for r in rows}


def _strip_credentials(db: Path) -> list[str]:
    """Drop identity tables from an adopted copy of a legacy database.

    Adopting another install's *account* is never what the user wants, so a
    migrated database keeps rituals/history/deck and loses users, sessions,
    devices and pairing codes. Returns the tables that were dropped.
    """
    dropped: list[str] = []
    conn = sqlite3.connect(str(db))
    try:
        names = {str(r[0]) for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in CREDENTIAL_TABLES:
            if table in names:
                # `table` comes from CREDENTIAL_TABLES, never from input.
                conn.execute("DROP TABLE IF EXISTS " + table)
                dropped.append(table)
        conn.commit()
    finally:
        conn.close()
    return dropped


def _adopt_legacy_dir(cfg: "Config", legacy: Path) -> bool:
    """Copy a vetted legacy directory into the (still empty) data dir."""
    import shutil

    cfg.ensure_dirs()
    for item in legacy.iterdir():
        if not item.is_file():
            continue
        if item.name in CREDENTIAL_FILES:
            _log(f"not adopting {item.name} from {legacy}: it is a credential, "
                 "not ritual data")
            continue
        dest = cfg.data_dir / item.name
        if dest.exists():
            continue
        shutil.copy2(item, dest)

    dropped: list[str] = []
    if cfg.db_file.exists():
        dropped = _strip_credentials(cfg.db_file)
    if dropped:
        _log(f"adopted ritual data from {legacy}; dropped credential tables: "
             f"{', '.join(dropped)} (a fresh install starts with no account)")
    else:
        _log(f"adopted data from {legacy}")
    return True


@dataclass
class Config:
    """Runtime configuration for Easy Life."""

    host: str = "0.0.0.0"
    port: int = 8765

    # Opt-in HTTPS listener, used by the phone. The desktop window keeps talking
    # to plain http://127.0.0.1:<port>, so this is a *second* port rather than a
    # replacement — an existing install that never turns it on is untouched.
    # iOS Safari only exposes the camera in a secure context, which is what the
    # in-app QR scanner needs (see pcrituals/cert.py).
    tls_enabled: bool = False
    tls_port: int = 8443
    # Bind address for the HTTPS listener; empty means "same as host".
    tls_host: str = ""

    # OBS remote rescue (see pcrituals/obs.py). The password is OBS's OWN
    # WebSocket password, set once in Tools -> WebSocket Server Settings.
    # Stored here because the phone needs it to work: without it the app cannot
    # talk to a crashed OBS, which is the whole point of the feature.
    obs_host: str = "127.0.0.1"
    obs_port: int = 4455
    obs_password: str = ""
    obs_exe: str = ""
    # Adds an OBS tab to the app with scene + stream controls. Off by default:
    # most people never touch OBS, and an empty tab is just noise to them.
    streamer_mode: bool = False

    # Remote access uses a single provider: Cloudflare quick tunnel. The
    # address changes each start (a known trade-off), but it needs nothing on
    # the phone and is the simplest to get working.
    tunnel_provider: str = "cloudflare"
    # Start the tunnel automatically when the app launches, so remote access
    # survives a reboot without the user remembering to turn it on.
    tunnel_autostart: bool = False
    # Send a push to the phone when OBS's stream drops. Off by default: push
    # permission is a real ask, so the user opts in from Settings.
    notify_enabled: bool = False

    # Data directory (rituals, history, keys, pairing codes).
    data_dir: Path = field(default_factory=default_data_dir)

    # Time in seconds a pairing code stays valid.
    pairing_code_ttl: int = 300

    # Max time (seconds) an individual action is allowed to run before it's killed.
    action_timeout_default: float = 30.0

    # Bind to all interfaces so the iPhone on the same Wi-Fi can connect.
    # All sensitive endpoints require a device token, so LAN exposure is safe.
    bind_loopback_only: bool = False

    # Wake-on-LAN broadcast target.
    wol_broadcast: str = "255.255.255.255"
    wol_port: int = 9

    # Update manifest URL (set to your hosted update.json / GitHub release).
    update_url: str = ""

    @property
    def settings_file(self) -> Path:
        return self.data_dir / "settings.json"

    # Auth: when no pairing token is active, remote (non-loopback) requests are denied.
    @property
    def auth_token_file(self) -> Path:
        return self.data_dir / "auth_token"

    @property
    def secret_file(self) -> Path:
        return self.data_dir / "server_secret"

    @property
    def rituals_dir(self) -> Path:
        return self.data_dir / "rituals"

    @property
    def history_file(self) -> Path:
        return self.data_dir / "history.jsonl"

    @property
    def db_file(self) -> Path:
        return self.data_dir / "pcrituals.db"

    # The self-signed certificate and its key live with the rest of the app
    # data, never in the install directory: the install dir may be read-only
    # (Program Files) and is replaced on every update.
    @property
    def tls_cert_file(self) -> Path:
        return self.data_dir / "tls_cert.pem"

    @property
    def tls_key_file(self) -> Path:
        return self.data_dir / "tls_key.pem"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.rituals_dir.mkdir(parents=True, exist_ok=True)


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _as_bool(value: object, default: bool = False) -> bool:
    """Coerce a JSON/env value to a bool.

    `bool("false")` is True, and settings.json is hand-editable, so a user
    writing `"tls_enabled": "false"` must not switch HTTPS on.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            return True
        if v in {"0", "false", "no", "off", ""}:
            return False
        return default
    return bool(value)


def migrate_legacy_data(cfg: "Config") -> bool:
    """One-time: if the new data dir has no database but an older location does,
    copy the old ritual data over so the user doesn't lose their rituals.
    Returns True if a migration happened.

    The data dir is where the *accounts* live, so adoption is guarded (F19):

      * never over an existing database, and never over a data dir that already
        holds files (the user's own data always wins);
      * only from a directory this process can verify is ours — not the system
        temp directory, not a symlink, not group- or world-writable, and on
        POSIX owned by the current user (`legacy_dir_rejection`);
      * never an account: credential tables (users/sessions/devices/
        pairing_codes) and credential files are left behind, so an upgraded
        install keeps its rituals and starts with no account.
    """
    if cfg.db_file.exists():
        return False  # already has data
    if _dir_has_files(cfg.data_dir):
        _log("not migrating legacy data: the data directory already has files in it")
        return False
    for legacy in _legacy_data_dirs():
        try:
            if legacy.resolve() == cfg.data_dir.resolve():
                continue
            src_db = legacy / "pcrituals.db"
            if not src_db.exists():
                continue
            reason = legacy_dir_rejection(legacy)
            if reason:
                _log(f"ignoring legacy data directory {legacy}: it {reason}")
                continue
            # Prove it is a usable rituals database *before* writing anything.
            try:
                tables = _db_tables(src_db)
            except sqlite3.Error as exc:
                _log(f"ignoring legacy data directory {legacy}: its database is "
                     f"not readable ({exc})")
                continue
            if "rituals" not in tables:
                _log(f"ignoring legacy data directory {legacy}: its database has "
                     "no rituals table")
                continue
            return _adopt_legacy_dir(cfg, legacy)
        except Exception as exc:
            _log(f"legacy data migration from {legacy} failed: {exc}")
            continue
    return False


def load_config() -> Config:
    """Load configuration from environment variables."""
    c = Config()

    c.host = os.environ.get("PCRITUALS_HOST", c.host)
    c.port = int(os.environ.get("PCRITUALS_PORT", str(c.port)))
    c.pairing_code_ttl = int(os.environ.get("PCRITUALS_PAIRING_TTL", str(c.pairing_code_ttl)))
    c.bind_loopback_only = _env_bool("PCRITUALS_LOOPBACK", c.bind_loopback_only)
    c.update_url = os.environ.get("PCRITUALS_UPDATE_URL", c.update_url)
    # HTTPS is opt-in: PCRITUALS_TLS=1 turns it on, PCRITUALS_TLS_PORT/_HOST
    # tune the second listener. `python -m pcrituals.cert --enable` does the
    # same thing persistently through settings.json (below).
    c.tls_enabled = _env_bool("PCRITUALS_TLS", c.tls_enabled)
    c.tls_port = int(os.environ.get("PCRITUALS_TLS_PORT", str(c.tls_port)))
    c.tls_host = os.environ.get("PCRITUALS_TLS_HOST", c.tls_host)
    c.obs_host = os.environ.get("PCRITUALS_OBS_HOST", c.obs_host)
    c.obs_port = int(os.environ.get("PCRITUALS_OBS_PORT", str(c.obs_port)))
    c.obs_password = os.environ.get("PCRITUALS_OBS_PASSWORD", c.obs_password)
    c.obs_exe = os.environ.get("PCRITUALS_OBS_EXE", c.obs_exe)
    c.streamer_mode = _env_bool("PCRITUALS_STREAMER_MODE", c.streamer_mode)
    c.tunnel_provider = os.environ.get("PCRITUALS_TUNNEL_PROVIDER", c.tunnel_provider)
    c.tunnel_autostart = _env_bool("PCRITUALS_TUNNEL_AUTOSTART", c.tunnel_autostart)
    c.notify_enabled = _env_bool("PCRITUALS_NOTIFY_ENABLED", c.notify_enabled)

    data_dir = os.environ.get("PCRITUALS_DATA_DIR")
    if data_dir:
        c.data_dir = Path(data_dir)

    c.ensure_dirs()
    # Bring over data from an older location (different exe vs source paths).
    try:
        migrate_legacy_data(c)
    except Exception:
        pass

    # Load user-editable settings (e.g. update URL) persisted at runtime.
    try:
        import json
        s = json.loads(c.settings_file.read_text())
        c.update_url = s.get("update_url", c.update_url)
        # A saved flag is the persistent way to enable HTTPS; it wins over the
        # built-in default but not over the env var, which a one-off run sets.
        if "tls_enabled" in s:
            c.tls_enabled = _as_bool(s.get("tls_enabled"), c.tls_enabled)
        if "tls_port" in s:
            c.tls_port = int(s.get("tls_port") or c.tls_port)
        if "tls_host" in s:
            c.tls_host = str(s.get("tls_host") or c.tls_host)
        for key in ("obs_host", "obs_password", "obs_exe"):
            if key in s:
                setattr(c, key, str(s.get(key) or ""))
        if "obs_port" in s:
            c.obs_port = int(s.get("obs_port") or c.obs_port)
        if "streamer_mode" in s:
            c.streamer_mode = _as_bool(s.get("streamer_mode"), c.streamer_mode)
        if "tunnel_provider" in s:
            c.tunnel_provider = str(s.get("tunnel_provider") or "cloudflare")
        if "tunnel_autostart" in s:
            c.tunnel_autostart = _as_bool(s.get("tunnel_autostart"), c.tunnel_autostart)
        if "notify_enabled" in s:
            c.notify_enabled = _as_bool(s.get("notify_enabled"), c.notify_enabled)
    except Exception:
        pass

    c.ensure_dirs()
    return c