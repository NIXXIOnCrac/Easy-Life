"""Turning a sentence into a Play.

The point of this parser is that it must never quietly build a Play that does
less than the user asked. A Play that silently drops "start stream" looks fine
in the builder and fails at the worst possible moment, so anything not
understood comes back in `unmatched` and the UI shows it.

Each test below is a phrasing a real person would type.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pcrituals.nlplay import draft
from pcrituals.models import ActionType as AT


def types(text):
    return [s["type"] for s in draft(text)["steps"]]


def targets(text):
    return [s["target"] for s in draft(text)["steps"]]


# ---- the sentence this was built for --------------------------------------

def test_the_streaming_sentence_end_to_end():
    d = draft("i want a play to open obs then switch to MAIN 1 scene then start "
              "stream to twitch and i want the name to be 1234")
    assert d["name"] == "1234"
    assert [s["type"] for s in d["steps"]] == [
        AT.APP.value, AT.OBS_SCENE.value, AT.OBS_STREAM_START.value]
    assert d["steps"][1]["target"] == "MAIN 1"
    assert d["unmatched"] == []


def test_the_streaming_sentence_notes_the_destination_it_cannot_set():
    """OBS owns the stream destination. Saying nothing would imply it works."""
    d = draft("open obs then switch to MAIN 1 scene then start stream to twitch")
    assert any("twitch" in n for n in d["notes"])


def test_a_trailing_conjunction_does_not_leak_into_a_step():
    """Regression: naming the Play at the end left "...to twitch and", so the
    note read \'can\'t switch destination to twitch and\'."""
    d = draft("open obs then start stream to twitch and i want the name to be 1234")
    assert d["name"] == "1234"
    assert not any("twitch and" in n for n in d["notes"]), d["notes"]
    assert d["unmatched"] == []


# ---- naming ---------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "open obs and name it Gaming",
    "open obs, call it Gaming",
    "open obs. the name should be Gaming",
    "open obs, name: Gaming",
])
def test_the_name_is_picked_up_in_several_phrasings(text):
    assert draft(text)["name"] == "Gaming"


def test_no_name_leaves_it_blank_for_the_user_to_fill():
    d = draft("open obs then start streaming")
    assert d["name"] == ""
    assert len(d["steps"]) == 2


# ---- steps ----------------------------------------------------------------

def test_scene_phrasings():
    for text in ("switch to MAIN 1 scene", "switch scene to MAIN 1",
                 "change to MAIN 1 scene", "go to MAIN 1 scene"):
        d = draft(text)
        assert types(text) == [AT.OBS_SCENE.value], text
        assert d["steps"][0]["target"] == "MAIN 1", text


def test_stream_start_and_stop():
    assert types("start streaming") == [AT.OBS_STREAM_START.value]
    assert types("go live") == [AT.OBS_STREAM_START.value]
    assert types("stop the stream") == [AT.OBS_STREAM_STOP.value]
    assert types("end streaming") == [AT.OBS_STREAM_STOP.value]


def test_a_domain_is_a_website_not_an_app():
    """Regression: the clause splitter used to break on "." and turn this into
    "open youtube" + "com"."""
    d = draft("open youtube.com")
    assert types("open youtube.com") == [AT.WEBSITE.value]
    assert d["steps"][0]["target"] == "https://youtube.com"
    assert d["unmatched"] == []


def test_website_phrasing():
    d = draft("open website twitch.tv")
    assert types("open website twitch.tv") == [AT.WEBSITE.value]
    assert d["steps"][0]["target"] == "https://twitch.tv"


def test_known_apps_resolve_to_a_launchable_target():
    assert targets("open steam") == ["steam"]
    assert targets("open discord") == ["discord"]


def test_delays_in_several_units():
    assert draft("wait 30 seconds")["steps"][0]["params"]["seconds"] == 30
    assert draft("wait 2 minutes")["steps"][0]["params"]["seconds"] == 120
    assert draft("delay 1 hour")["steps"][0]["params"]["seconds"] == 3600


def test_close_apps():
    d = draft("close obs")
    assert types("close obs") == [AT.CLOSE.value]
    assert d["steps"][0]["target"] == "obs"


def test_power_actions():
    assert types("lock the pc") == [AT.POWER.value]
    assert types("put my pc to sleep") == [AT.POWER.value]
    assert draft("restart the computer")["steps"][0]["target"] == "restart"


def test_power_with_a_delay_becomes_two_steps():
    """"lock the pc after 10 minutes" is two steps; dropping the time would
    silently do something else."""
    d = draft("lock the pc after 10 minutes")
    assert [s["type"] for s in d["steps"]] == [AT.DELAY.value, AT.POWER.value]
    assert d["steps"][0]["params"]["seconds"] == 600


def test_run_command():
    d = draft("run command ipconfig /all")
    assert types("run command ipconfig /all") == [AT.COMMAND.value]
    assert d["steps"][0]["target"] == "ipconfig /all"


# ---- sequencing -----------------------------------------------------------

def test_ordering_is_preserved():
    d = draft("open obs, switch to BRB scene, wait 5 seconds, start streaming")
    assert [s["type"] for s in d["steps"]] == [
        AT.APP.value, AT.OBS_SCENE.value, AT.DELAY.value, AT.OBS_STREAM_START.value]


def test_and_joins_two_commands():
    assert types("open obs and start streaming") == [
        AT.APP.value, AT.OBS_STREAM_START.value]


def test_and_inside_a_name_is_not_split():
    """"rock and roll" is a name, not two commands."""
    d = draft("open obs, name it rock and roll")
    assert d["name"] == "rock and roll"
    assert types("open obs, name it rock and roll") == [AT.APP.value]


def test_third_person_verbs_after_that():
    d = draft("make a play that opens discord and starts streaming")
    assert [s["type"] for s in d["steps"]] == [AT.APP.value, AT.OBS_STREAM_START.value]


# ---- failing loudly -------------------------------------------------------

def test_nonsense_is_reported_not_silently_dropped():
    d = draft("do a barrel roll")
    assert d["steps"] == []
    assert d["unmatched"] == ["do a barrel roll"]


def test_a_partly_understood_sentence_keeps_the_rest_and_flags_the_rest():
    d = draft("open obs then invent a new colour then start streaming")
    assert [s["type"] for s in d["steps"]] == [AT.APP.value, AT.OBS_STREAM_START.value]
    assert d["unmatched"] == ["invent a new colour"]


def test_empty_input_is_handled():
    d = draft("   ")
    assert d["steps"] == []
    assert d["notes"]


def test_the_draft_never_saves_anything(tmp_path, monkeypatch):
    """It must be reviewable before it becomes a Play."""
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    draft("open obs then start streaming")
    assert list(tmp_path.rglob("*.db")) == []
