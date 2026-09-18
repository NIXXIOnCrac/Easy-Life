"""Seed the store with example rituals for development / demo."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pcrituals.config import load_config
from pcrituals.storage import Store
from pcrituals.models import Ritual, Action


def main():
    config = load_config()
    store = Store(config)
    if store.count_rituals() > 0:
        print(f"Already has {store.count_rituals()} rituals; not re-seeding.")
        return

    gaming = Ritual(
        name="Gaming",
        description="Open your comms and launch into a game.",
        tags=["games"],
        actions=[
            Action.app("discord", "Open Discord"),
            Action.app("spotify", "Open Spotify"),
            Action.game("steam://rungameid/570", "Launch Steam game"),
            Action.delay(3, "Let the game start"),
            Action.close("overwolf", "Close overlays"),
        ],
    )
    work = Ritual(
        name="Work",
        description="Set up your dev environment.",
        tags=["work"],
        actions=[
            Action.app("code", "Open VS Code"),
            Action.website("https://github.com", "Open GitHub"),
            Action.website("https://mail.google.com", "Open Mail"),
            Action.delay(2, "Wait for apps"),
        ],
    )
    streaming = Ritual(
        name="Streaming",
        description="Start your broadcast stack.",
        tags=["stream"],
        actions=[
            Action.app("obs64", "Open OBS Studio"),
            Action.website("https://dashboard.twitch.tv", "Open Twitch dashboard"),
            Action.app("spotify", "Open music"),
            Action.command("start lighting-profile-day.cmd", "Apply lighting profile"),
        ],
    )
    chill = Ritual(
        name="Chill Mode",
        description="Relax: music + lo-fi to wind down.",
        tags=["music"],
        actions=[
            Action.app("spotify", "Open Spotify"),
            Action.website("https://www.youtube.com/watch?v=lTRiuFIWV54", "Open lo-fi playlist"),
        ],
    )

    for r in [gaming, work, streaming, chill]:
        store.save_ritual(r)
    print(f"Seeded {store.count_rituals()} rituals.")


if __name__ == "__main__":
    main()