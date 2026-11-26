"""RECALL_OWNER: the name stored text uses for the person this recall belongs to."""

import os
import subprocess
import sys

from recall import config, learn, mcp_server, sessions
from recall.agent import tools

PROMPTS = (learn.SYSTEM, learn.REVIEW_SYSTEM, learn.PROFILE_SYSTEM, sessions.SYSTEM)


def test_without_a_name_the_text_says_the_user(monkeypatch):
    monkeypatch.delenv("RECALL_OWNER", raising=False)
    assert config.owner() == "the user"
    assert config.personal("{Owner} said so. Ask {owner}; it is {owner}'s.") == "The user said so. Ask the user; it is the user's."


def test_a_name_is_used_as_written_on_one_line(monkeypatch):
    monkeypatch.setenv("RECALL_OWNER", "  Ada \n Lovelace ")
    assert config.owner() == "Ada Lovelace"
    assert config.personal("{Owner} said so. Ask {owner}.") == "Ada Lovelace said so. Ask Ada Lovelace."
    monkeypatch.setenv("RECALL_OWNER", "   ")
    assert config.owner() == "the user"


def test_no_prompt_is_sent_with_a_mark_left_in_it():
    for prompt in PROMPTS:
        assert "{owner}" not in prompt and "{Owner}" not in prompt
    assert 'third person ("The user ...")' in learn.SYSTEM


def test_with_a_name_the_prompts_that_write_stored_text_use_it():
    # The prompts are filled in when recall starts, so this needs a recall started with the name.
    script = (
        "from recall import learn, sessions, mcp_server\n"
        "from recall.agent import tools\n"
        "assert 'third person (\"Ada ...\")' in learn.SYSTEM\n"
        "assert learn.REVIEW_SYSTEM.startswith(\"You check entries proposed for Ada's long-term memory\")\n"
        "assert 'the standing profile of Ada that' in learn.PROFILE_SYSTEM\n"
        "assert 'working sessions between Ada and an AI coding assistant' in sessions.SYSTEM\n"
        "assert \"'Ada runs niri\" in tools.TOOLS[1]['input_schema']['properties']['fact']['description']\n"
        "assert 'the user' not in learn.SYSTEM.lower()\n"
        # what tells an assistant how to behave still says "the user": it is talking to them
        "assert \"recall is the user's personal knowledge base\" in mcp_server.INSTRUCTIONS\n"
    )
    env = os.environ | {"RECALL_OWNER": "Ada"}      # RECALL_ENV is the empty file from conftest.py
    done = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]


def test_the_remember_example_reads_as_before_without_a_name():
    assert "'The user runs niri on CachyOS and prefers Chrome Beta.'" in tools.TOOLS[1]["input_schema"]["properties"]["fact"]["description"]
    assert mcp_server.remember.__doc__.startswith("Store one durable fact")
