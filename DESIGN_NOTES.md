# Easy Life — "Liquid Glass" design notes

How the Apple-grade surface layer works, what changed conceptually, how to wire it in, and what can't work in an
older WebView2 / iOS Safari.

---

## 1. Wire it in (lead developer)

Add the two new stylesheets to `pcrituals/web/index.html` **after** the existing `styles.css` link, so they win the
cascade at equal specificity:

```html
<link rel="stylesheet" href="/styles.css">
<link rel="stylesheet" href="/tokens.css">        <!-- NEW: design tokens -->
<link rel="stylesheet" href="/liquid-glass.css">  <!-- NEW: surface upgrade -->
```

- `tokens.css` only declares custom properties on `:root` — nothing else, so it is inert on its own.
- `liquid-glass.css` is the only file with styling rules. It targets the exact selectors the app already uses
  (`.card`, `.ritual-card`, `.sidebar`, `.btn`, `.modal`, `.deck-btn`, `.am-player`, …), so no HTML/JS changes are
  needed. If a future view adds new surfaces, give them the same treatment or the base `styles.css` look applies.
- Nothing in the app's `styles.css`, `app.js`, or `index.html` was edited. These two files are additive drop-ins.

## 2. What changed conceptually (vs. the existing design)

The app already had a credible glassmorphism base. This layer pushes it to the Apple liquid-glass register:

| Dimension | Was (styles.css) | Now (liquid-glass.css) |
|-----------|------------------|------------------------|
| Surface fill | single `rgba` tint per class | **layered** fill: a 60%-white → transparent chroma gradient at the top edge sitting over the base tint, giving that "light catches the top" glass refraction |
| Backdrop | `blur(34–42px)` only, mixed values | consistent `blur(36px) saturate(180%)` on large surfaces and `blur(20px) saturate(180%)` on compact ones — saturation makes the neon blobs bleed through like liquid |
| Specular edge | `inset 0 1px 0 rgba(255,255,255,.07–.25)` ad-hoc | unified `--lg-shadow-top` = `inset 0 1px 0 rgba(255,255,255,0.28)` **plus** a softened 1px light sweep across the top of every major panel |
| Shadow | one heavy `0 12px 40px` | Apple's **two-layer** soft shadows (rest vs. raised vs. modal), re-gauged for the dark canvas |
| Radii | 13/16/22/24 ad-hoc | Apple's corner set 12/16/20/pill, applied consistently |
| Hover on touch | `:hover` always fires (sticky on iPhone) | hover gated behind `@media (hover:hover)`; on touch the hover state is neutralized so nothing sticks after a tap |
| Motion | per-class timings | tokens: 120/200/350/450 ms with the app's existing ease curves |
| No-blur safety | translucent fill only (washed out) | explicit opaque near-black fill so surfaces stay intentional and legible if `backdrop-filter` is absent |

## 3. Compatibility & honest limits

**Safe on the platforms that matter** — the stylesheets deliberately avoid everything that broke or could break on the
older WebView2 / iOS Safari 15:
- **No `color-mix()`** (this is what caused the previous white/blank artifacts), **no `:has()`, no `@container`, no CSS
  nesting**. Only plain selectors, `@media`, `@supports`, media queries, and `@keyframes`-free transitions.
- `-webkit-backdrop-filter` is declared before standard `backdrop-filter` for iOS 15.
- Every blurred surface has an **intentional no-blur fallback** via `@supports not ((backdrop-filter) or (-webkit-backdrop-filter))`
  — a gradient over `rgba(22,24,34,0.92)`, not a white gap.

**Caveats / what can't be perfect:**
1. **True corner continuity (squircle) is not reproducible in pure CSS** without `mask` tricks that are flaky in old
   WebView2. We approximate it with Apple's `border-radius` set (12/16/20/pill). On iOS 18+ `border-radius: 20px` is
   actually visually close to the system squircle; on Windows it renders as a plain rounded corner. Acceptable, safe.
2. **Backdrop blur in old WebView2** (pre ~Chromium 76, Evergreen enabled) simply won't render `backdrop-filter`; those
   devices get the opaque fallback from §6. Update WebView2 to Evergreen for the full liquid look.
3. **`@media (hover: hover)`** is broadly supported (Chromium 41+, iOS Safari 9+), so the touch gating is safe.
4. **Saturation/`saturate()` in `backdrop-filter`** is fine in both targets; it just softens to no-op in the fallback.
5. **`-webkit-tap-highlight-color: transparent`** is used deliberately — focus-visible outlines (already in styles.css)
   still provide keyboard accessibility, so we're not removing focus indication, only the grey tap flash.

## 4. What we did NOT verify (hand this to QA)
- Actual pixel rendering in a real Apple device (iPhone Safari) and a real Windows WebView2 build — no hardware here.
- Whether `theme-color #07070c` on the existing radial-gradient background reads "liquid enough" behind the saturate
  boost; if it feels flat, bump the radial blob opacities in `styles.css` (touched indirectly, not by us).
- Exact `blur(36px)` vs `blur(20px)` split — chosen to match the app's existing 34–42px range; tweak the two
  `--lg-blur*` tokens rather than individual rules if it looks too heavy on low-end GPUs.