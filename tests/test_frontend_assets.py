"""Static checks on the frontend assets (regression guards for known bugs)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "pcrituals" / "web"


def test_no_color_mix_in_css():
    """color-mix() silently drops declarations on older WebView2 — the deck
    tint broke because of it. Keep the CSS free of color-mix()."""
    import re
    css = (WEB / "styles.css").read_text()
    # Strip /* ... */ comments, then look for real usage.
    without_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    assert "color-mix(" not in without_comments, \
        "color-mix() is not safe on all WebView2 versions"


def test_no_white_blob_default_icon():
    """The default deck icon used to be the white 🔘 emoji, which rendered as a
    stark white blob on buttons. It must not come back."""
    js = (WEB / "app.js").read_text()
    assert "🔘" not in js, "default deck icon must not be the white circle emoji"
    assert "deck-icon-empty" in js, "empty deck buttons should use the neutral glyph"


def test_web_assets_all_present():
    """Every asset the UI needs must exist (they're bundled into the exe)."""
    for name in ("index.html", "app.js", "styles.css", "manifest.json", "sw.js",
                 "icons/icon-192.png", "icons/icon-512.png", "icons/pcrituals.ico"):
        assert (WEB / name).exists(), f"missing web asset: {name}"


def test_visualizer_has_defined_bar_heights():
    """The 'now playing' waveform must have real bar heights (it previously
    rendered as indistinct squares)."""
    css = (WEB / "styles.css").read_text()
    assert ".am-visualizer.on span" in css
    assert "amWave" in css


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))