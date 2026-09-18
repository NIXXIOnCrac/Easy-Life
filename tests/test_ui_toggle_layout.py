"""The on/off switch must stay a working switch.

The delayed-action card had an inline `style="width:34px;height:19px"` on the
toggle, overriding the stylesheet's 44x25 track. The knob is 21-22px positioned
at `left:21px` when checked, so on a 34px track it hung ~8px off the end — the
switch visibly broke, and the numbers lived in two places that could drift.

These tests assert the layout INVARIANT rather than a pixel value, so resizing
the switch stays allowed as long as the geometry keeps holding:

    knob fits inside the track, and sits flush when checked.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "pcrituals/web/styles.css").read_text(encoding="utf-8")
APP_JS = (ROOT / "pcrituals/web/app.js").read_text(encoding="utf-8")


def _block(css: str, selector: str) -> str:
    """Return the declaration block for a selector (first match)."""
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, f"no rule found for {selector}"
    return m.group(1)


def _px(block: str, prop: str) -> float | None:
    m = re.search(rf"\b{re.escape(prop)}\s*:\s*(-?\d+(?:\.\d+)?)px", block)
    return float(m.group(1)) if m else None


def test_toggle_geometry_is_consistent():
    """The knob must fit in the track and sit flush when switched on."""
    track = _block(CSS, ".toggle")
    knob = _block(CSS, ".toggle::after")
    checked = _block(CSS, ".toggle:checked::after")

    w = _px(track, "width")
    h = _px(track, "height")
    kw = _px(knob, "width")
    kh = _px(knob, "height")
    inset = _px(knob, "left")          # the gap at the "off" end
    on_left = _px(checked, "left")

    assert None not in (w, h, kw, kh, inset, on_left), "toggle geometry is incomplete"

    # 1. The knob must not stick out of the track in either direction.
    assert kw + 2 * inset <= w, f"knob ({kw}) + insets does not fit in the track ({w})"
    assert kh + 2 * inset <= h, f"knob ({kh}) + insets does not fit in the track height ({h})"

    # 2. When switched on, the knob must land flush against the far end.
    assert on_left + kw + inset == w, (
        f"the checked knob (left {on_left} + width {kw}) does not reach the end of "
        f"a {w}px track — the switch will look broken"
    )


@pytest.mark.parametrize("prop", ["width", "height"])
def test_no_toggle_is_resized_inline(prop):
    """An inline size on the toggle is what broke it: it silently beat the
    stylesheet, leaving the knob geometry describing a different-sized track."""
    offenders = [
        line.strip()[:120]
        for line in APP_JS.splitlines()
        if 'class="toggle' in line and re.search(rf'style="[^"]*{prop}\s*:', line)
    ]
    assert not offenders, (
        f"a toggle sets {prop} inline, which overrides the stylesheet and can "
        f"detach the knob: {offenders}"
    )


def test_action_tools_are_a_single_row():
    """The row's controls (toggle, duplicate, delete) belong side by side."""
    block = _block(CSS, ".action-tools")
    assert "display: flex" in block.replace("flex;", "flex;"), block
    assert "flex-direction: column" not in block, (
        "the action tools must sit in a row, not stacked in a column"
    )
    # And the markup must actually use that class.
    assert 'class="action-tools"' in APP_JS, "the action row does not use .action-tools"


def test_action_tools_are_larger_and_react_to_hover():
    """They were small and inert; the user asked for bigger and a hover pop."""
    lg = _block(CSS, ".icon-btn.lg")
    assert _px(lg, "padding") and _px(lg, "padding") >= 8, "the buttons are still small"
    assert "transition" in lg, "the buttons have no transition to animate a hover"

    hover = _block(CSS, ".icon-btn.lg:hover")
    assert "transform" in hover and "scale" in hover, (
        "hover should lift/scale the button so it 'pops'"
    )
    # Touch devices cannot hover, so the lift must be neutralised there.
    assert "@media (hover: none)" in CSS, "hover effects are not neutralised on touch"
