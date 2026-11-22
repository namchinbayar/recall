"""Conversation loop: streams text, runs tools, keeps history, saves sessions.

A manual loop rather than the SDK tool runner so that text streams to the
terminal token by token while tools run, and so history stays in our hands
for persistence. Assistant content (including thinking blocks) is passed
back exactly as received.
"""

from __future__ import annotations

import os
import socket
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

import anthropic

from .. import db
from ..config import Settings
from . import tools

try:    # the HTTP library the SDK streams over; older SDKs use httpx
    from httpx2 import HTTPError as _TransportError
except ImportError:
    from httpx import HTTPError as _TransportError

# What a failed model call raises: the SDK's own errors, and a connection that drops in the
# middle of a streamed reply, which the SDK passes on as the HTTP library raised it.
CALL_ERRORS = (anthropic.APIError, _TransportError)

SYSTEM = """You are recall, a personal assistant with a memory. You help one person: the user who runs you. You have tools over their private knowledge base (saved articles, notes, files, and memories from past conversations) and over metrics from their machines and manual logs.

How to work:
- When a question touches the user's life, setup, decisions, saved material, or earlier conversations, call search_knowledge before answering. Do not guess at personal facts.
- Search as often as the question needs; separate searches for separate parts of a question work better than one blurred query. Results are reranked, so a score near 1 is a strong match and below about 0.3 is weak.
- Stream events (desktop notifications, Hermes conversations) are left out of search unless you pass include_events. Turn it on for questions about what happened, what was said, or what the user was working on recently.
- If a call reports a rate limit, answer what you can from results already in hand and say what you could not check.
- When the user tells you something durable (a preference, a fact about their setup, a decision, a correction), call remember with one standalone sentence per fact. Skip transient details.
- When they state a measurement ("slept 7 hours", "weigh 72 kg"), call record_metric.
- For questions about their machine's load, temperature, or memory, call metrics_summary.
- Say when the knowledge base has nothing relevant rather than inventing an answer.
- Cite which source a fact came from when it matters.
- The user also keeps an encrypted vault of documents and secrets that you cannot read or search. If they ask for something like a password, document number, or file from the vault, tell them to run `recall vault get "<title words>"` in a terminal.

Style: plain, direct, short. No preamble. No bullet lists unless listing several parallel things. The user is a developer; technical detail is welcome, filler is not."""


@dataclass
class Session:
    settings: Settings
    client: anthropic.Anthropic
    model: str
    effort: str
    messages: list = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    show_tools: bool = True
    _id: object = None

    @classmethod
    def start(cls, settings: Settings, show_tools: bool = True) -> "Session":
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise SystemExit(
                "ANTHROPIC_API_KEY is not set. Add it to ~/.config/recall/env (or the project .env)."
            )
        return cls(
            settings=settings,
            client=anthropic.Anthropic(),
            model=os.environ.get("RECALL_MODEL", "claude-opus-5-5"),
            effort=os.environ.get("RECALL_EFFORT", "medium"),
            show_tools=show_tools,
        )

    # ----- one user turn -------------------------------------------------

    def turn(self, user_text: str, out=sys.stdout) -> str:
        """Run one user turn to completion. Returns the final assistant text."""
        now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
        asked = len(self.messages)
        self.messages.append({"role": "user", "content": f"[{now}, host {socket.gethostname()}]\n{user_text}"})

        final_text = ""
        while True:
            try:
                with self.client.beta.messages.stream(
                    model=self.model,
                    max_tokens=16000,
                    system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
                    tools=tools.TOOLS,
                    messages=self.messages,
                    output_config={"effort": self.effort},
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                ) as stream:
                    for event in stream:
                        if event.type == "content_block_delta" and event.delta.type == "text_delta":
                            out.write(event.delta.text)
                            out.flush()
                    response = stream.get_final_message()
            except CALL_ERRORS:
                if len(self.messages) == asked + 1:   # nothing came back, so the question can be asked again
                    del self.messages[asked:]
                raise

            self.messages.append({"role": "assistant", "content": response.content})
            final_text = "".join(b.text for b in response.content if b.type == "text")

            if response.stop_reason == "refusal":
                out.write("\n[declined by safety classifiers]\n")
                break
            if response.stop_reason == "max_tokens":
                out.write("\n[cut off at max_tokens]\n")
                break
            if response.stop_reason != "tool_use":
                break

            calls = [block for block in response.content if block.type == "tool_use"]
            results = []
            try:
                for block in calls:
                    if self.show_tools:
                        out.write(f"\n  \033[2m> {tools.describe_call(block.name, block.input)}\033[0m\n")
                        out.flush()
                    content, is_error = tools.run(self.settings, block.name, block.input)
                    item = {"type": "tool_result", "tool_use_id": block.id, "content": content}
                    if is_error:
                        item["is_error"] = True
                    results.append(item)
            finally:
                # Every call needs an answer in the history, or the API refuses the next turn.
                # Ctrl-C in the middle of a tool leaves some without one.
                results += [
                    {"type": "tool_result", "tool_use_id": block.id, "content": "interrupted before it finished", "is_error": True}
                    for block in calls[len(results):]
                ]
                self.messages.append({"role": "user", "content": results})

        out.write("\n")
        return final_text

    # ----- persistence ----------------------------------------------------

    def save(self, summary: str | None = None) -> None:
        if not self.messages:
            return
        doc = {
            "started_at": self.started_at,
            "ended_at": datetime.now(timezone.utc),
            "host": socket.gethostname(),
            "model": self.model,
            "messages": [_serialize(m) for m in self.messages],
        }
        if summary:
            doc["summary"] = summary
        coll = db.client(self.settings)[self.settings.db_name]["sessions"]
        if self._id is None:
            self._id = coll.insert_one(doc).inserted_id
        else:
            coll.replace_one({"_id": self._id}, doc)

    def wrap_up(self, out=sys.stdout) -> None:
        """Ask the model to store durable facts from this session, then save."""
        if len(self.messages) < 2:
            return
        self.turn(
            "This session is ending. If anything said here is worth keeping for future "
            "conversations and is not already stored, call remember for each such fact. "
            "Then reply with one line summarizing the session, or 'nothing to keep'.",
            out=out,
        )
        last = self.messages[-1]
        summary = "".join(b.text for b in last["content"] if getattr(b, "type", "") == "text") if last["role"] == "assistant" else None
        self.save(summary)


def _serialize(message: dict) -> dict:
    content = message["content"]
    if isinstance(content, str):
        return message
    out = []
    for block in content:
        out.append(block.model_dump() if hasattr(block, "model_dump") else block)
    return {"role": message["role"], "content": out}
