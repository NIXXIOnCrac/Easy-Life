# PC Rituals — Frontend Audit (vanilla-JS SPA + PWA)

**Scope:** `pcrituals/web/{app.js, styles.css, index.html, sw.js, manifest.json}`.
**Method:** full read of all five files, static cross-referencing (onclick targets vs
declarations, DOM ids vs references, dead CSS classes), plus live jsdom probes against a
mock/real API (scripts listed in *Live reproduction*). Read-only audit — no app file was
modified.
**Not re-reported** (already fixed by the lead dev): render() error isolation; the
PC-login vs paired-phone token split on the Phone Remote page; the Integrations
empty-cache; api() signing out on 401. Remaining instances of those are flagged in the
table and summarised at the end.

---

## Findings (most severe first)

| ID | Severity | File | Line(s) | What is wrong | Why it matters | Suggested fix |
|----|----------|------|---------|----------------|----------------|---------------|
| **F-01** | **High** | app.js | 169–177 (`fn();`), 982 (`async function renderRemote`), 1011, 1069 (`async function renderRemoteBody`), 1226 (`async function renderIntegrations`) | The page dispatcher renders the current page with a **non-awaited** call: `const fn = pages[currentRoute]; … try { fn(); } catch (e) {…}` (174–177). Two page renderers are `async` (`renderRemote`, `renderIntegrations`), so the `try/catch` only wraps the *synchronous* part. Any throw **after** an `await` becomes an unhandled promise rejection and the "This page hit a problem" error card is **never shown**. | This is the exact failure mode the render-isolation fix was meant to remove, and it still exists on the Phone Remote and Integrations pages — the two pages involved in the user's complaints. A phone sitting on the Remote page with one bad payload emits an unhandled rejection **on every 2.5 s poll tick** and shows a blank page forever (verified, F-01 in Live reproduction). | Either `await fn()` inside the `try`, or make `fn()` return a promise caught by the dispatcher: `const r = fn(); if (r && typeof r.catch === "function") r.catch(e => renderPageError(currentRoute, e));`, and apply the same isolation inside the async renderers so they share the error-card path. |
| **F-02** | **High** | app.js | 194–217 (refresh*), consumed at 378/381/407/428/781/1094/1196/1296 | Every `refresh*` stores the raw decoded response with **no type check**: `state.rituals = await apiGet("/rituals")` (198), same for `history` (201), `devices` (204), `deck` (210). All consumers then assume an array (`state.rituals.map(...)` 428/1094, `.length` 378, `state.history.filter` 1196, `state.devices.map` 1296, `state.deck.map` 781). A **200 response whose body is not an array** (API shape change, a proxy/error page returned as JSON, a renamed field) puts a wrong-typed value into module state and *every* subsequent render throws. | On sync pages this degrades to the error card (recoverable); on the Remote page it is fatal because of F-01 (blank page, repeating rejection). One malformed 200 bricks the page until a manual reload. | Validate at the boundary, e.g. `const d = await apiGet("/rituals"); state.rituals = Array.isArray(d) ? d : [];` — the pattern the dev already used for integrations (`Array.isArray(d) ? d : []`, line 1232). Apply to rituals, history, devices, deck, backups. |
| **F-03** | **High** | app.js | 1224–1238 (catch at 1233–1234), 1265–1283 | **Remaining instance of the "Integrations cached empty forever" bug.** The sentinel is documented as `null = never loaded (retry)` (1224) and the fetch only runs when `integrationData === null` (1228), but the failure path sets `integrationData = []` instead of leaving it `null`: `} catch (_) { integrationData = []; }` (1233–1234). After one failed fetch the sentinel is gone, so **no render ever retries** — the page shows "No integrations detected. Click **Re-scan**." permanently. Only the explicit Re-scan button (`refreshIntegrationData`, 1276–1282, which re-nulls) recovers. | Verified: two visits to the Integrations page after a single 500 produced **one** total `/integrations` request (none on the revisit). The lead dev's fix is defeated on the very failure path it targeted. | In the catch, leave the sentinel so the next render retries: set a separate `integrationError` flag / `integrationData = null`, and show "Couldn't load — retrying" instead of the empty-state. Keep `[]` only for a *successful* empty result. |
| **F-04** | Medium | app.js | 993 (button), 1141–1160, **1155** (`setToken(res.token)`); token model 20–25, 56 | **Remaining instance of the PC-token / phone-token conflation.** On the PC (`isLocal`) the Remote page offers "Enter Code Manually" (993) → `manualPairModal()` → on success it runs `setToken(res.token)` (1155), writing a **device token over the logged-in PC session token** in the same `token` variable (`localStorage["pcrituals.token"]`). | Verified reachable: the modal opens on the PC (`modal opened: true`). After it, the PC browser is silently re-identified as a paired device (served `username:"phone"` by `/auth/status`), a bogus "PCself" row appears in Paired Devices, and the original PC session token is gone from storage — so the PC can only get its account back by logging in again. A user on the PC can also type the same on-screen code here and trigger this by accident. | On a loopback/PC origin, do **not** write a device token over the session token. Easiest: drop the "Enter Code Manually" affordance from the PC branch entirely (a PC never needs to redeem its own code), or store the device token under a distinct key and never overwrite `pcrituals.token` when the origin is loopback. |
| **F-05** | Medium | app.js | 284 (`setInterval(tick, 2500)`), 301–304, 158–161, 1537 | The live-update timer polls `/run/current` **every 2.5 s, forever, on every route** (`tick()` → `refreshCurrentRun()`; `refresh()` also fans out **10 GETs** on each call). `refreshCurrentRun()` (257–263) has **no in-flight guard and no request sequencing**, so if a response takes longer than 2.5 s (or an SSE event at 305–317 races the tick) two requests overlap and the **older response can land last and overwrite the newer state** (run progress can jump backwards / a just-finished run can reappear as running). | Real stale-data class, and a steady background load even when no ritual is running and the user is on Settings. | Add a single-flight guard (`if (this._inFlight) return;`) and/or a monotonically increasing request id so only the newest response is applied. Consider polling `/run/current` only while a run is active, and widen the idle interval. |
| **F-06** | Medium | sw.js | 15–26 (esp. 20–25), 3 | Two cache defects: (a) the network-first branch caches **every** response with `caches.open(CACHE).then(c => c.put(e.request, clone))` — there is **no `res.ok` check**, so a 404/500 (or an error page) is written into the offline cache and later served offline; (b) the catch falls back to `caches.match(e.request).then(m => m || caches.match('/'))` for **all** requests, so a failed request for any sub-resource that isn't already cached (e.g. a newly added icon/asset) returns the cached **HTML document** as the response. | (a) can pin a broken response into the cache; (b) hands an HTML body to a CSS/JS/image request (`SyntaxError: Unexpected token '<'` if it ever hits app.js) instead of letting it fail. The API/SSE path is correctly excluded (`/api/` guard, line 18) — that part is fine. | Only cache when `res.ok` (`if (res.ok) …`); restrict the `/` fallback to navigation requests (`if (e.request.mode === 'navigate') return caches.match('/')`) and otherwise `return Response.error()`/rethrow. |
| **F-07** | Medium | app.js + index.html | 69–78 (`openModal`), 80–92 (`confirmModal`), 1629–1647 (`renderTour`); icon-only buttons at 272, 451–453, 620–621, 811, 1080, 1427; index.html 5 | Accessibility gaps: **no modal is dismissible with Escape and there is no focus trap** — `openModal` only moves focus in (76) and the only keydown handlers in the file are the Enter key on the auth form (1596) and on the pairing code (1062). Tab can move behind the backdrop, and none of the **8 `class="icon-btn"` controls have an `aria-label`** (they rely on `title`, e.g. the 🗑/⧉/⏻ buttons), and `aria-label` appears **0** times in app.js. Separately, `index.html` sets `maximum-scale=1` (line 5), which blocks pinch-zoom. | Keyboard/screen-reader users cannot reliably dismiss dialogs or identify icon buttons; blocking zoom is a WCAG 1.4.4 failure. | Add a document-level `keydown` for `Escape` → `closeModal()`, implement a simple focus trap (keep Tab within `.modal`), and add `aria-label` to every icon-only button. Replace `maximum-scale=1` with nothing (or `maximum-scale=5`). |
| **F-08** | Low | app.js | 1526–1531 | A freshly opened **already-paired phone lands on the Dashboard, not Phone Remote**: `initApp` only forces `#/remote` when `!token` (`if (onPhone && !token && !location.hash)`), so a paired phone with no hash renders `dashboard`. | Verified (`active page: page-dashboard`). The phone's primary job is the remote control surface; landing on the dashboard is a confusing first view and hides the PC controls behind a tab tap. | Force `#/remote` for any non-loopback origin without a hash, regardless of `token` (`if (onPhone && !location.hash) location.hash = "#/remote"`). |
| **F-09** | Low | app.js | 1426–1427 | HTML-escaping is used inside a JS string literal in an inline handler: `onclick="restoreBackup('${esc(b.name)}')"` / `deleteBackup('${esc(b.name)}')`. `esc()` turns `'` into `&#39;`, which the HTML parser decodes **back to `'`** before the JS runs, so a backup name containing an apostrophe produces `restoreBackup('Dad's setup')` → SyntaxError → the button silently does nothing (and a crafted name can break out of the string). | UI-only impact today (the frontend always sends `label:"manual"`, and server names are `manual-<timestamp>`, so quotes don't occur in practice — **UNVERIFIED as user-reachable through the UI**), but the escaping is wrong for the context and is a latent injection/broken-button bug. | Don't embed data in inline handlers; attach listeners in JS and pass the value from a dataset, or JS-escape (`JSON.stringify`) instead of HTML-escaping. |
| **F-10** | Low | app.js | 1009, 1401; styles.css 356 | Dead/placebo UI: `#remote-status-area` (app.js 1009) and `#update-status-line` (app.js 1401) are created but **never referenced again** — so "Check Now" (renderSettings 1399) can never show the inline update status the empty div implies, and `.pair-banner` (styles.css 356) is defined but never emitted. Also `["addAction", …].forEach(f => { window[f] = window[f]; })` (app.js 759) is a no-op. | Users see a "Check Now" button with no visible result (only a toast on failure); dead markup/CSS misleads future maintenance. | Either populate `#update-status-line` from `checkForUpdate()`, or remove the div; delete `.pair-banner` and the no-op `forEach`. |
| **F-11** | Low | app.js | 282–300, 290 | The phone opens its SSE stream **before pairing**, with an empty token: `startSSE`/`connectSSE` run at `initApp` (1533) with `token === ""`, so the URL is `api/events` with no `token=` (verified: `esUrls = ["api/events"]`). The unauthenticated stream 401s and `es.onerror` (298) retries every 3 s. It does eventually pick up a token after pairing (because the retry re-reads the module `token`), so this **self-heals within ~3 s** — but until then the phone gets no push events and hammers `/events`. | Wasteful retry loop / brief gap in live updates on the phone; a permanently invalid token would loop forever. | Build the SSE URL lazily on each connect (already effectively true) and, right after `setToken` in the pairing flows, call `connectSSE()` so the authenticated stream opens immediately; consider exponential backoff instead of a fixed 3 s. |
| **F-12** | Low | styles.css | 341 (`.toast bottom:28px`), 518–536 (`.am-bar`, `.am-bar-thumb`), 604–631 (mobile bar) | Cosmetic/affordance mismatches: the mobile tab bar is `position:fixed; bottom:0` (607) while `.toast` sits at `bottom:28px` (341), so on a phone toasts overlap the tab bar; the media progress bar renders a draggable-looking `.am-bar-thumb` with `cursor:default` (518–536) but has **no seek handler**, implying scrubbing that doesn't work. | Minor polish; the seek bar actively misleads ("tap to seek" does nothing). | Raise `.toast` above the mobile bar (`calc(var(--safe-bottom) + 64px)`) or lower its z-index/position; either implement seeking on `.am-bar` or remove the thumb and make it a plain progress bar. |

---

## Live reproduction

All scripts are throwaway probes outside the repo, in `/opt/data/scratch/`
(`/tmp` was not writable in this environment). They use the same jsdom pattern as
`tests/deep_pages_test.js` (jsdom + `runScripts:'outside-only'`, injected `fetch`,
stubbed `EventSource`, dispatched `DOMContentLoaded`). They mock the API so they do not
depend on the running server or on login.

**F-01 — async page renderer escapes the error isolation** — `scratch/probeE.js`
```bash
cd /opt/data/pc-rituals && node /opt/data/scratch/probeE.js
```
Observed:
```
remote page active: page-remote
remote page text (blank=?): ""
error card shown: false
unhandled rejections in 7s: 5  (~1 per 2.5s tick = ~2-3 expected)
```
Same condition via `scratch/probeC.js` (RITUALS_SHAPE=object) shows the contrast: the
**sync** page shows the card while the **async** page does not:
```
-- A) PHONE remote page --   >>> NODE UNHANDLED REJECTION: state.rituals.map is not a function
                             page-remote text: (blank)   has error card: false
-- B) sync rituals page --   rituals page text: This page hit a problem. state.rituals.map is not a function  Reload
                             has error card: true
```
(Stack shown in jsdom points at `renderRituals … app.js:428` → `render … app.js:177`.)

**F-04 — "Enter Code Manually" overwrites the PC session token** — `scratch/probeC.js`, section C:
```
Enter Code Manually handler is window.manualPairModal: function
modal opened: true
```
(Static confirmation of the overwrite: `manualPairModal` runs `setToken(res.token)` at
app.js:1155; `setToken` writes `localStorage["pcrituals.token"]`, app.js:56.)

**F-03 — Integrations never auto-retry after one failure** — `scratch/probeD.js`:
```
first visit calls: 1   text: Integrations Applications you can control inside Rituals. …
second visit calls: 1  (retry if >1)   text: Integrations … 
>> auto-retried after failure: false (expected true if the null-sentinel worked)
```

**F-08 — freshly opened paired phone lands on Dashboard** — `scratch/probeD.js`:
```
active page: page-dashboard
>> lands on remote? false
```

**F-11 — phone SSE opened without a token** — `scratch/probeC.js` / original probe:
`SSE urls: ["api/events"]` (no `token=`), later confirmed with a boot token present that
the URL includes the token then.

---

## Remaining instances of the four already-fixed bugs

| Already-fixed bug | Remaining instance? | Where |
|---|---|---|
| **1. render() had no error isolation** | **YES** — the isolation only covers the *synchronous* page renderers. The async renderers `renderRemote`, `renderRemoteBody`, `renderIntegrations` are invoked without `await`, so a throw after their `await` is an unhandled rejection and no error card is shown. | **F-01** (app.js 174–177 vs 982/1011/1069/1226) |
| **2. One token variable for PC user vs paired phone** | **YES** — `manualPairModal` (offered on the PC) writes a *device* token over the logged-in PC *session* token via `setToken`. | **F-04** (app.js 993, 1141–1160, 1155) |
| **3. Integrations cached empty forever** | **YES** — the failure path still stores `[]` instead of keeping the `null` retry sentinel, so no render retries after a failed fetch. | **F-03** (app.js 1233–1234) |
| **4. api() signing the user out on any 401** | **No remaining instance found.** The `/auth/` and `/pair/` exclusions (app.js 32) cover the session-sensitive endpoints; `/auth/status` even reports a device token as authenticated (api.py:139), so the exclusion set is adequate. | — |

---

## Explicitly marked UNVERIFIED / suspected-but-not-confirmed

* **F-09**: whether a backup name containing an apostrophe is reachable through the UI —
  I could not produce one via the frontend (it always sends `label:"manual"`); the
  encoding bug is real but the user-reachable trigger is unproven.
* The reported **"glitches / breaks when I revoke access of the phones"** complaint: I
  found **no frontend bug** in `revokeDevice`/`revokeAll`/`unpair` beyond the token trap
  in F-04. `auth_required` (api.py:59–62) accepts device tokens everywhere and
  `revoke_all` only touches the `devices` table (`UPDATE devices SET revoked=1`,
  security.py:217) while the PC session lives in `sessions`, so revoke-all cannot lock
  the PC out at the API level. If the revoke breakage is real it is most likely
  backend-side or the F-04 token trap — flagged for the lead dev rather than invented as
  a frontend finding.
