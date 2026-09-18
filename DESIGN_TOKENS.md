# Easy Life — Apple "Liquid Glass" Design Tokens

Extracted from the MIT-licensed **`apple-design-skill`** repo
(`https://github.com/chaos-xxl/apple-design-skill`, `main` branch, LICENSE = MIT, © 2025 Apple Design Skill
Contributors). The skill's authoritative material is shipped as `CLAUDE.md` (a self-contained superset) plus
per-domain modules under `prompts/` — the repo has **no** root `SKILL.md`; `CLAUDE.md` is the canonical token file.
Where the repo ships no value (motion easing/duration, backdrop blur), values are patched from Apple's public Human
Interface Guidelines and the app's existing motion in `styles.css`; every such gap is flagged below.

How files map onto the app:
- `pcrituals/web/tokens.css` → the token values below as CSS custom properties (root block).
- `pcrituals/web/liquid-glass.css` → the surface rules that consume those tokens.
- Design rationale and wiring instructions → `pcrituals/web/../DESIGN_NOTES.md` (repo root).

The app is **dark** (`theme-color #07070c`) and already ships a neon glass look in `styles.css`. The token set below
layers Apple-derived values on top and re-uses the app's existing variable names (`--glass*`, `--text`, `--green`,
`--radius*`, `--shadow`, …) wherever possible so nothing fights the current dark palette.

---

## 1. Colors
Source: `apple-design-skill` CLAUDE.md "Design Tokens → Colors" (values are Apple's apple.com palette).

| Token (our file)      | Value | Apple source token | Applies to |
|-----------------------|-------|--------------------|------------|
| `--lg-text-primary`   | `#F5F5F7` | `--apple-text-on-dark` / `--apple-bg-light-gray` | Body & headline text on dark canvas |
| `--lg-text-secondary` | `rgba(245,245,247,0.72)` | `--apple-text-secondary` `#6E6E73` (mapped onto dark) | Subtitles, card labels |
| `--lg-text-tertiary`  | `rgba(245,245,247,0.5)` | `--apple-text-tertiary` `#86868B` | Muted meta, hints |
| `--lg-accent`         | `#2997FF` | `--apple-gradient-text-blue` start `#2997FF` | Interactive accents, focus |
| near-black canvas     | `#07070c` (unchanged) | `--apple-bg-dark` `#1D1D1F` → `#000` gradient | Page background (kept as the app's existing, slightly deeper black) |
| `--lg-glass-fill`     | `rgba(255,255,255,0.06)` | — from app `--glass` `rgba(255,255,255,0.055)` | Base glass tint |
| `--lg-glass-fill-2`   | `rgba(255,255,255,0.03)` | — from app `--glass` gradient | Lower tint in layered fill |
| `--lg-glass-highlight`| `rgba(255,255,255,0.6)` | — | Top specular sheen (see §4) |
| `--lg-glass-border`   | `rgba(255,255,255,0.14)` | — from app `--glass-border` `0.12` | Hairline surface borders |
| `--lg-glass-border-strong` | `rgba(255,255,255,0.24)` | — from app `--glass-border-strong` `0.22` | Hover/emphasized borders |

Neon accents (`--green --cyan --blue --purple`, from `styles.css`) are retained unchanged — they are the app's
identity; Apple's restrained palette exists for apple.com marketing pages, not for a product tool.

## 2. Typography
Source: `apple-design-skill` CLAUDE.md "Typography" + "Font Size Scale" + "Line Height" + "Letter Spacing".

| Token            | Value | Apple source | Applies to |
|------------------|-------|--------------|------------|
| display font     | `SF Pro Display` … | `--apple-font-display` (en) | `h1.page-title`, `.am-title` |
| text font        | `SF Pro Text` … | `--apple-font-text` (en) | Body, controls, buttons |
| `--apple-weight-title` | 600 | — | Section headings |
| `--apple-weight-title-bold` | 700 | — | `h1.page-title` (app already uses 700 ✓) |
| body weight      | 400 / 500 | `--apple-weight-body` / `-medium` | Body, medium emphasis |
| hero size        | 28–32px (page title) | `--apple-font-size-subtitle` 28 / `-max` 32 | `h1.page-title` (app uses 32 ✓) |
| body size        | 15–17px | `--apple-font-size-body` 17 | Body, `.page-sub` |
| caption size     | 12–14px | `--apple-font-size-caption` 12/14 | `.stat-pill`, `.deck-btn-label` |
| line-height title| `1.1` | `--apple-leading-title` | `h1.page-title` |
| line-height body | `1.5` | `--apple-leading-body-tight` 1.5 / `-loose` 1.58 | Body, `.page-sub` |
| letter-spacing title | `-0.015em` | `--apple-tracking-en-title` | `h1.page-title` (app uses -0.025em; Apple recommends ≥ -0.02em) |
| letter-spacing title-tight | `-0.02em` | `--apple-tracking-en-title-tight` | `.card h3`, `.ritual-card h3` |

App already sits in this band; only minor tightening to Apple's exact values. Font stack in `tokens.css` reuses the
app's `--font` (already SF Pro-first).

## 3. Spacing & layout
Source: `apple-design-skill` CLAUDE.md "Design Tokens → Spacing" + "Layout Patterns".

| Token | Value | Apple source | Applies to |
|-------|-------|--------------|------------|
| section gap | `80–120px` (`100` default) | `--apple-section-gap` 100 / `-sm` 80 / `-lg` 120 | Page/panel breathing room |
| component gap sm | `16px` | `--apple-component-gap-sm` | `.grid`, card internals |
| component gap md | `32px` | `--apple-component-gap-md` | Between blocks |
| component gap lg | `48px` | `--apple-component-gap-lg` | Above section titles |
| card padding | `24–40px` | `--apple-card-padding` 32 / `-sm` 24 / `-lg` 40 | `.card`, `.modal`, `.auth-card` |
| content max-width | `980px` | `--apple-content-max-width` | `.main` column |
| content max-width lg | `1200px` | `--apple-content-max-width-lg` | `.main` on large desktop |
| breakpoints | `734 / 1068 / 1440` | `--apple-breakpoint-*` | Not used directly; app's `820`/`400` media queries retained |

The app's `.main` already caps at **1180px** — inside Apple's 980–1200 content band ✓.

## 4. Radius, shadows, glass
Source: `apple-design-skill` CLAUDE.md "Design Tokens → Border Radius / Shadows". Motion/blur patched (flagged).

| Token | Value | Apple source | Applies to |
|-------|-------|--------------|------------|
| `--lg-radius-sm`   | `12px` | `--apple-radius-card-sm` 12 | `.nav-item`, inputs |
| `--lg-radius-md`   | `16px` | (app `--radius` 16) | `.action-row`, `.history-item` |
| `--lg-radius-lg`   | `20px` | `--apple-radius-card-lg` 20 | `.card`, `.ritual-card`, `.runner-panel` |
| `--lg-radius-pill` | `999px` | `--apple-radius-button` (pill) | `.btn`, `.am-pill`, `.toggle` |
| `--lg-shadow-rest` | `0 2px 8px rgba(0,0,0,0.30), 0 8px 24px rgba(0,0,0,0.34)` | `--apple-shadow-card` `0 2px 8px …0.04, 0 8px 24px …0.08` (light-theme, re-gauged for dark canvas) | resting glass surfaces |
| `--lg-shadow-raised` | `0 4px 12px rgba(0,0,0,0.34), 0 14px 34px rgba(0,0,0,0.46)` | `--apple-shadow-hover` (re-gauged) | hover / lifted cards |
| `--lg-shadow-modal` | `0 8px 20px rgba(0,0,0,0.40), 0 24px 70px rgba(0,0,0,0.60)` | `--apple-shadow-modal` (re-gauged) | `.modal`, `.auth-card` |
| `--lg-shadow-top`   | `inset 0 1px 0 rgba(255,255,255,0.28)` | ✓ specular top light edge (from app glass + Apple highlight idioms) | every glass panel |
| `--lg-blur`        | `20px` | *(patch — repo ships no blur)* macOS/iOS glass standard ≈ 20px | compact glass, buttons |
| `--lg-blur-lg`     | `36px` | *(patch)* | large surfaces (cards, sidebar, modal) |
| `--lg-saturate`    | `180%` | *(patch)* "liquid" color-bleed typical of Apple glass | all blurred surfaces |

**Specular top highlight** (the defining Apple glass cue) = the `inset 0 1px 0` in `--lg-shadow-top` plus a layers
`::before` light sweep across the top edge (existing in `styles.css` on `.card`/`.ritual-card`/`.runner-panel`,
extended in `liquid-glass.css`).

## 5. Motion
Source: **patch** — `apple-design-skill` ships no easing/duration numbers. Values below are the app's existing
curves (already Apple-like) plus HIG guidance (motion ≤ 1/3 s reads as "immediate"; honor `prefers-reduced-motion`).

| Token | Value | Source / rationale | Applies to |
|-------|-------|--------------------|------------|
| `--lg-ease-standard` | `cubic-bezier(.2,.8,.2,1)` | app `styles.css` (used throughout); HIG-style ease-out | panels, cards |
| `--lg-ease-spring`   | `cubic-bezier(.3,.9,.4,1)` | app `styles.css` iOS `.toggle` | switches, knobs |
| `--lg-duration-instant` | `0.12s` | press feedback | `:active` scale |
| `--lg-duration-fast` | `0.2s` | HIG "immediate" | hover, borders |
| `--lg-duration-basic` | `0.35s` | content transitions | panel/modal entrance |
| `--lg-duration-flow`  | `0.45s` | pronounced choreography (sparingly) | welcome/full-surface |
| reduced motion | disable all | HIG + already in `styles.css` | `@media (prefers-reduced-motion: reduce)` |

---

## Source checklist & honest gaps
- **Extracted verbatim from the repo:** colors, typography scale/weights/line-heights/letter-spacing, spacing scale,
  radii (12/18/20/pill), the near-black `#1D1D1F` principle, multi-layer shadow *structure*, content width 980–1200.
- **Patched (repo has none):** backdrop `blur` px, `saturate` %, and easing/duration curves → taken from the app's own
  `styles.css` motion + Apple HIG qualitative guidance. Apple does not publish numeric blur/easing tables in HIG; the
  blur range (20–36px, saturate 180%) is the widely-used macOS/iOS glass recipe consistent with the app's existing
  34–42px values.
- **Deliberately overridden vs. Apple light theme:** shadow alpha (re-gauged much darker) and accent hue (kept neon) —
  because the target canvas is the app's dark `#07070c`, not apple.com's white `#F5F5F7`.