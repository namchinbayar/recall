import io
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from recall.agent import chat, tools


def text(value):
    return SimpleNamespace(type="text", text=value)


def call(ident, name="search_knowledge", **args):
    return SimpleNamespace(type="tool_use", id=ident, name=name, input=args)


class Stream:
    def __init__(self, reply):
        self.reply = reply

    def __enter__(self):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(())

    def get_final_message(self):
        return self.reply


class Client:
    """Stands in for the Anthropic client: hands out the replies it was given, in order, and
    keeps the history each call was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.sent = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self.stream))

    def stream(self, **request):
        self.sent.append(list(request["messages"]))
        return Stream(self.replies.pop(0))


def reply(*blocks):
    stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
    return SimpleNamespace(content=list(blocks), stop_reason=stop)


def session(*replies):
    return chat.Session(settings=None, client=Client(*replies), model="m", effort="low", show_tools=False)


def unanswered(messages):
    """Ids of tool calls in the history with no result in the message after them."""
    missing = []
    for at, m in enumerate(messages):
        if m["role"] != "assistant":
            continue
        asked = [b.id for b in m["content"] if b.type == "tool_use"]
        after = messages[at + 1]["content"] if at + 1 < len(messages) else []
        got = {r["tool_use_id"] for r in after if isinstance(r, dict) and r.get("type") == "tool_result"}
        missing += [i for i in asked if i not in got]
    return missing


def test_a_tool_turn_runs_to_the_final_answer(monkeypatch):
    monkeypatch.setattr(tools, "run", lambda settings, name, args: ("three hits", False))
    s = session(reply(call("t1", query="niri")), reply(text("It reloads by itself.")))
    assert s.turn("how does niri reload?", out=io.StringIO()) == "It reloads by itself."
    assert [m["role"] for m in s.messages] == ["user", "assistant", "user", "assistant"]
    assert s.messages[2]["content"] == [{"type": "tool_result", "tool_use_id": "t1", "content": "three hits"}]


def test_ctrl_c_during_a_tool_leaves_a_history_the_next_turn_can_use(monkeypatch):
    def run(settings, name, args):
        if args["query"] == "slow":
            raise KeyboardInterrupt
        return "found", False

    monkeypatch.setattr(tools, "run", run)
    s = session(reply(call("t1", query="quick"), call("t2", query="slow"), call("t3", query="never")),
                reply(text("ok")))
    with pytest.raises(KeyboardInterrupt):
        s.turn("look three things up", out=io.StringIO())
    assert unanswered(s.messages) == []
    results = s.messages[-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2", "t3"]
    assert [bool(r.get("is_error")) for r in results] == [False, True, True]
    assert s.turn("never mind", out=io.StringIO()) == "ok"
    assert unanswered(s.client.sent[-1]) == []


def test_a_failed_model_call_takes_the_unanswered_question_back():
    down = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.example/v1/messages"))
    s = session(reply(text("first answer")), down, reply(text("second answer")))
    s.turn("one", out=io.StringIO())
    with pytest.raises(chat.CALL_ERRORS):
        s.turn("two", out=io.StringIO())
    assert len(s.messages) == 2            # the first exchange, and nothing of the failed one
    assert s.turn("two", out=io.StringIO()) == "second answer"
    assert [m["role"] for m in s.client.sent[-1]] == ["user", "assistant", "user"]


def test_a_connection_dropped_mid_reply_counts_as_a_failed_call():
    dropped = next(kind for kind in chat.CALL_ERRORS if kind is not anthropic.APIError)("peer closed the connection")
    s = session(dropped, reply(text("here now")))
    with pytest.raises(chat.CALL_ERRORS):
        s.turn("hello?", out=io.StringIO())
    assert s.messages == []
    assert s.turn("hello?", out=io.StringIO()) == "here now"


def test_a_failed_call_after_tools_ran_keeps_their_results(monkeypatch):
    monkeypatch.setattr(tools, "run", lambda settings, name, args: ("found", False))
    down = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.example/v1/messages"))
    s = session(reply(call("t1", query="x")), down)
    with pytest.raises(chat.CALL_ERRORS):
        s.turn("look it up", out=io.StringIO())
    assert [m["role"] for m in s.messages] == ["user", "assistant", "user"]
    assert unanswered(s.messages) == []


def test_a_tool_that_raises_becomes_an_error_result(monkeypatch):
    from recall import ingest

    def refuse(*args, **kwargs):
        raise httpx.ConnectError("name resolution failed\nmore detail")

    monkeypatch.setattr(ingest, "ingest", refuse)
    content, is_error = tools.run(None, "save_source", {"target": "https://nope.test/"})
    assert is_error
    assert content == "save_source failed: ConnectError: name resolution failed"
