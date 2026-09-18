"""Icon resolution for Stream Deck buttons.

Auto-picks an icon for a given target:
  - Websites  -> the site's favicon (via a public favicon service)
  - Known apps/brands -> that brand's logo
  - Games     -> the store/launcher logo, else a game emoji
  - Files/folders -> a file/folder emoji
  - Commands  -> a terminal emoji

Returns a descriptor the frontend renders directly:
    {"type": "url",   "value": "https://..."}   # <img src>
    {"type": "emoji", "value": "🎮"}            # text glyph
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

# Public favicon service (no key needed). Returns a PNG we can <img> directly.
FAVICON = "https://www.google.com/s2/favicons?sz=128&domain={domain}"

# Known brands: keyword (matched against the target/label) -> domain for a logo.
KNOWN_BRANDS = {
    "discord": "discord.com",
    "spotify": "spotify.com",
    "steam": "store.steampowered.com",
    "epic": "epicgames.com",
    "battle.net": "battle.net",
    "blizzard": "blizzard.com",
    "riot": "riotgames.com",
    "league of legends": "leagueoflegends.com",
    "valorant": "playvalorant.com",
    "minecraft": "minecraft.net",
    "roblox": "roblox.com",
    "fortnite": "fortnite.com",
    "overwatch": "overwatch.blizzard.com",
    "warzone": "callofduty.com",
    "call of duty": "callofduty.com",
    "obs": "obsproject.com",
    "twitch": "twitch.tv",
    "youtube": "youtube.com",
    "netflix": "netflix.com",
    "chrome": "google.com",
    "google": "google.com",
    "firefox": "mozilla.org",
    "edge": "microsoft.com",
    "vs code": "code.visualstudio.com",
    "visual studio": "visualstudio.microsoft.com",
    "notepad": "microsoft.com",
    "explorer": "microsoft.com",
    "github": "github.com",
    "gmail": "mail.google.com",
    "reddit": "reddit.com",
    "instagram": "instagram.com",
    "twitter": "x.com",
    "x.com": "x.com",
    "tiktok": "tiktok.com",
    "signalrgb": "signalrgb.com",
    "openrgb": "openrgb.org",
    "wallpaper engine": "wallpaperengine.io",
}

KIND_FALLBACK_EMOJI = {
    "app": "🪟",
    "game": "🎮",
    "website": "🌐",
    "file": "📁",
    "command": ">_",
    "ritual": "⚡",
    "media": "🎵",
    "power": "⏻",
}


def _favicon_url(domain: str) -> str:
    return FAVICON.format(domain=domain.strip())


def _domain_from_url(url: str) -> str:
    try:
        net = urlparse(url if "://" in url else "http://" + url)
        return net.netloc or net.path
    except Exception:
        return url


def _match_brand(text: str) -> str | None:
    """Match a known brand using word boundaries so short keys like 'obs'
    don't match inside words like 'obscure'."""
    t = (text or "").lower()
    if not t:
        return None
    # Normalise separators to spaces so 'vs-code' / 'vs_code' work.
    t = re.sub(r"[\\/_.\-]+", " ", t)
    for key, domain in KNOWN_BRANDS.items():
        k = re.sub(r"[\\/_.\-]+", " ", key.lower())
        # Word-boundary match; multi-word keys match as a phrase.
        if re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", t):
            return domain
    return None


def suggest_kind(target: str) -> str:
    """Guess the deck-button kind from a target string."""
    t = (target or "").strip()
    low = t.lower()
    if low.startswith(("http://", "https://", "www.")):
        return "website"
    if "://" in low:  # steam://, riot://, etc.
        return "game"
    if re.search(r"\.(exe|app|lnk|bat|cmd)$", low):
        return "app"
    if low in ("play_pause", "next", "previous", "prev", "stop"):
        return "media"
    if low in ("lock", "sleep", "restart", "shutdown"):
        return "power"
    if any(b in low for b in ("steam", "riot", "battle.net", "epic", "valorant", "league")):
        return "game"
    return "app"


def resolve_icon(kind: str, target: str, label: str = "") -> dict:
    """Resolve an icon descriptor for a deck button."""
    kind = (kind or "app").lower()
    target = target or ""
    low = target.lower()

    # Explicit website -> favicon.
    if kind == "website":
        domain = _domain_from_url(target)
        if domain:
            return {"type": "url", "value": _favicon_url(domain)}

    # Known brand (apps & games) -> brand logo via favicon service.
    brand = _match_brand(target) or _match_brand(label)
    if brand and kind in ("app", "game", "website", "ritual", "file"):
        return {"type": "url", "value": _favicon_url(brand)}

    # Known brand embedded in a store URL (steam://rungameid/... etc).
    if kind == "game":
        if "steam" in low:
            return {"type": "url", "value": _favicon_url("store.steampowered.com")}
        if "riot" in low or "league" in low or "valorant" in low:
            return {"type": "url", "value": _favicon_url("riotgames.com")}
        if "battle.net" in low or "blizzard" in low:
            return {"type": "url", "value": _favicon_url("battle.net")}
        if "epic" in low:
            return {"type": "url", "value": _favicon_url("epicgames.com")}

    # Fallback to an emoji for the kind.
    return {"type": "emoji", "value": KIND_FALLBACK_EMOJI.get(kind, "🔘")}