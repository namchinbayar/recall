import json
from datetime import datetime, timedelta, timezone

import pytest

from recall import chunk, openitems
from recall.openitems import Item, Session

NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)


def note(done="- Built the thing.", still=("Nothing is pushed.", "The token needs renewing.")):
    body = f"Claude Code session in ~/Projects/x.\n\n## Done\n{done}\n\n## Decisions\n- Kept it simple, because it works."
    if still:
        body += "\n\n## Still open\n" + "\n".join(f"- {s}" for s in still)
    return body + "\n\n## Where\n- ~/Projects/x"


def rows(source, day, text, cwd="/srv/x", tags=("session-note", "claude"), title=None):
    return [{"source": source, "title": title or f"Session: {source}", "chunk_index": i, "text": t, "tags": list(tags),
             "created_at": NOW - timedelta(days=20 - day), "session": {"cwd": cwd}}
            for i, t in enumerate(chunk.split(text))]


class FakeStore:
    def __init__(self):
        self.closures, self.marks = {}, {}

    def closed(self, ids):
        return set(ids) & set(self.closures)

    def close(self, item, how, why, by, now, quote=""):
        self.closures[item.id] = {"text": item.text, "how": how, "why": why, "by": by}

    def looked_at(self, source):
        return self.marks.get(source)

    def mark(self, source, digest, closed, now):
        self.marks[source] = digest


def answer(*closed):
    """A first-call reply; each closure is (id, how, why) or (id, how, why, quote)."""
    return json.dumps({"closed": [dict(zip(("id", "how", "why", "quote"), (*c, "Pushed everything.")[:4])) for c in closed]})


def agree(*ids):
    return json.dumps({"verdicts": [{"id": i, "settled": True} for i in ids]})


def asker(first, second=None):
    """The two calls of decide(): the proposal, then the review (which agrees with everything by default)."""
    def ask(system, text, schema):
        if schema is openitems.REVIEW_SCHEMA:
            return second if second is not None else agree(*[c["id"] for c in json.loads(first)["closed"]])
        return first
    return ask


def test_an_item_keeps_its_id_through_spacing_and_case_but_not_across_notes():
    a = openitems.item_id("session:a", "Nothing is pushed.")
    assert a == openitems.item_id("session:a", "  nothing  is PUSHED") and a.startswith("open:") and len(a) == 17
    assert a != openitems.item_id("session:b", "Nothing is pushed.")
    assert a != openitems.item_id("session:a", "Nothing is merged.")


def test_sections_returns_the_named_parts_with_their_headings():
    text = note()
    assert openitems.sections(text, ("done", "decisions")) == "## Done\n- Built the thing.\n\n## Decisions\n- Kept it simple, because it works."
    assert openitems.sections(text, ("where",)) == "## Where\n- ~/Projects/x"
    assert openitems.sections("no headings here", ("done",)) == ""


def test_read_notes_puts_chunks_back_together_newest_first():
    long = note(done="\n".join(f"- Step {i}: " + "words " * 50 for i in range(12)), still=("One thing.", "One thing.", "Another."))
    data = rows("session:old", 1, long) + rows("session:hermes:new", 5, note(still=()), tags=("session-note", "hermes"))
    new, old = openitems.read_notes(data)
    assert (new.source, new.kind, new.items) == ("session:hermes:new", "hermes", [])
    assert [i.text for i in old.items] == ["One thing.", "Another."]           # the same bullet twice is one item
    assert old.items[0] == Item(openitems.item_id("session:old", "One thing."), "One thing.", "session:old", "Session: session:old", old.when)
    assert old.folder == "/srv/x" and old.text.count("Step 3:") == 1 and len(old.digest) == 16


def sessions():
    return openitems.read_notes(
        rows("session:far", 2, note(still=("Far away.",)), cwd="/srv/other")
        + rows("session:near", 3, note(still=("Related elsewhere.",)), cwd="/srv/other")
        + rows("session:same", 4, note(still=("First.", "Second.")))
        + rows("session:new", 10, note(done="- Pushed everything.", still=("A new thing to do.",)))
        + rows("session:later", 12, note(still=("From the future.",)))
    )


def test_a_note_is_shown_earlier_open_items_from_its_folder_or_close_in_meaning():
    all_ = sessions()
    new = next(s for s in all_ if s.source == "session:new")
    shown = openitems.shown_to(new, all_, set(), {"session:near"})
    assert [i.text for i in shown] == ["First.", "Second.", "Related elsewhere."]      # newest note first; not its own, not later ones
    closed = {openitems.item_id("session:same", "First.")}
    assert [i.text for i in openitems.shown_to(new, all_, closed, set())] == ["Second."]
    old = Session("session:ancient", "t", new.when - timedelta(days=openitems.LOOKBACK_DAYS + 1), "/srv/x", "claude", "",
                  [Item("open:x", "Too old.", "session:ancient", "t", None)])
    assert "Too old." not in [i.text for i in openitems.shown_to(new, all_ + [old], set(), set())]
    assert openitems.shown_to(Session("s", "t", None, "/srv/x", "claude", ""), all_, set(), set()) == []


def test_the_prompt_carries_the_note_and_each_item_with_its_id():
    all_ = sessions()
    new = next(s for s in all_ if s.source == "session:new")
    shown = openitems.shown_to(new, all_, set(), set())
    text = openitems.prompt(new, shown)
    assert "## Done\n- Pushed everything." in text and "## Still open\n- A new thing to do." in text and "## Where" not in text
    assert f"[{shown[0].id}] 2026-11-04, from \"Session: session:same\": First." in text
    assert '<session date="2026-11-10" folder="/srv/x">' in text


def test_code_accepts_only_closures_of_shown_items_with_a_reason_and_a_real_quote():
    all_ = sessions()
    new = next(s for s in all_ if s.source == "session:new")
    shown = openitems.shown_to(new, all_, set(), {"session:near"})
    first, second, related = (i.id for i in shown)
    reply = answer(
        (first, "done", "The session pushed everything to GitHub."),
        (first, "done", "Said twice: only the first one counts."),
        ("open:000000000000", "done", "An id that was never shown."),
        (second, "finished", "A way of closing that does not exist."),
        (related, "dropped", "ok"),                                                 # no real reason
        (related, "done", "The note is said to show this.", "Deployed it to production."),      # words the note does not have
        (second, "carried", "The note lists a thing again as still open.", "- A new thing to do."),
    )
    seen = []

    def ask(system, text, schema):
        seen.append((system, text, schema))
        return reply if schema is openitems.SCHEMA else agree(first, second)

    out = openitems.decide(new, shown, ask)
    assert [(c.item.text, c.how, c.quote) for c in out] == [("First.", "done", "Pushed everything."), ("Second.", "carried", "- A new thing to do.")]
    (system, text, schema), (review_system, review_text, _) = seen
    assert schema is openitems.SCHEMA and first in text and "unfinished work" in system
    assert "Be strict" in review_system and f"[{first}] (done) First." in review_text and related not in review_text

    # an id written back without its prefix, or in brackets, is still the item that was shown
    loose = answer((first[5:], "done", "The session pushed everything to GitHub."))
    assert [c.item.id for c in openitems.decide(new, shown, asker(loose, agree(f"[{first[5:]}]")))] == [first]

    # the second look can take a closure back, and is not asked when nothing was proposed
    assert [c.item.text for c in openitems.decide(new, shown, asker(reply, agree(second)))] == ["Second."]
    calls = []
    assert openitems.decide(new, shown, lambda system, text, schema: calls.append(schema) or answer()) == []
    assert calls == [openitems.SCHEMA]

    leak = answer((first, "done", "Done with the token ghp_" + "a1B2" * 9))
    assert openitems.decide(new, shown, asker(leak)) == []
    bare = Session("session:bare", "t", new.when, "/srv/x", "claude", note(still=()))      # lists nothing itself
    assert openitems.decide(bare, shown, asker(answer((first, "carried", "Listed again in this note.", "Built the thing.")))) == []
    many = [Item(f"open:{n:012d}", f"Item {n}.", "session:same", "t", None) for n in range(12)]
    full = answer(*[(i.id, "done", "The session finished this one too.") for i in many])
    assert len(openitems.decide(new, many, asker(full))) == openitems.MAX_CLOSE
    for broken in ("not json", '{"other": []}', "[]"):
        with pytest.raises(openitems.Declined):
            openitems.decide(new, shown, lambda *a, b=broken: b)
    with pytest.raises(openitems.Declined):
        openitems.decide(new, shown, asker(reply, "not json"))


def test_a_quote_must_be_in_the_note_whatever_the_markup_around_it():
    n = Session("s", "t", None, None, "claude", "## Done\n- Added `Apple TV` Oct 14 payment ($14.99)\n  to Airtable.\n- Pushed.")
    assert openitems.quoted(n, "Added Apple TV Oct 14 payment ($14.99) to Airtable.")
    assert openitems.quoted(n, "- added **apple tv** oct 14 payment")
    assert not openitems.quoted(n, "Verified the Apple TV renewal on Nov 14")
    assert not openitems.quoted(n, "Pushed.")                                          # too short to show anything


def test_settle_pending_goes_oldest_first_and_looks_at_each_note_once():
    data = (rows("session:a", 4, note(still=("Push it.", "Renew the token.")))
            + rows("session:b", 10, note(done="- Pushed it to GitHub.", still=("Renew the token.",)))
            + rows("session:c", 12, note(done="- Renewed the token.", still=())))
    store, asked, lines = FakeStore(), [], []
    push, renew_a = openitems.item_id("session:a", "Push it."), openitems.item_id("session:a", "Renew the token.")
    renew_b = openitems.item_id("session:b", "Renew the token.")

    def ask(system, text, schema):
        if schema is openitems.REVIEW_SCHEMA:
            return agree(push, renew_a, renew_b)
        asked.append(text)
        if "Pushed it to GitHub." in text:
            return answer((push, "done", "The session pushed it to GitHub.", "Pushed it to GitHub."),
                          (renew_a, "carried", "Listed again as still open here.", "- Renew the token."))
        return answer((renew_b, "done", "The session renewed the token.", "Renewed the token."))

    since = NOW - timedelta(days=30)
    kw = dict(now=NOW, load=lambda s, d: data, near=lambda s, n: set())
    counts = openitems.settle_pending(None, store, ask, since, lines.append, **kw)
    assert counts == {"asked": 2, "closed": 3, "nothing": 1, "failed": 0, "chars": counts["chars"]} and counts["chars"] > 500
    assert store.closures[push]["by"] == "session:b" and store.closures[renew_a]["how"] == "carried"
    assert store.closures[renew_b] == {"text": "Renew the token.", "how": "done", "why": "The session renewed the token.", "by": "session:c"}
    assert renew_a not in asked[1] and renew_b in asked[1]           # c is not shown what b already closed
    assert set(store.marks) == {"session:a", "session:b", "session:c"}
    assert lines == ["  closed   2 open item(s): session:b", "  closed   1 open item(s): session:c"]

    again = openitems.settle_pending(None, store, ask, since, **kw)
    assert (again["asked"], again["nothing"]) == (0, 0) and len(asked) == 2

    # a note rewritten when its session went on is looked at again
    data[:] = [r for r in data if r["source"] != "session:c"] + rows("session:c", 12, note(done="- Renewed the token and more.", still=()))
    assert openitems.settle_pending(None, store, lambda *a: answer(), since, **kw)["nothing"] == 1   # nothing left open before it


def test_a_dry_run_and_a_failed_call_write_nothing():
    data = rows("session:a", 4, note(still=("Push it.",))) + rows("session:b", 10, note(done="- Pushed it.", still=()))
    since = NOW - timedelta(days=30)
    kw = dict(now=NOW, load=lambda s, d: data, near=lambda s, n: set())
    store = FakeStore()
    counts = openitems.settle_pending(None, store, None, since, **kw)
    assert (counts["asked"], counts["nothing"], counts["closed"]) == (1, 1, 0) and counts["chars"] > 0
    assert store.marks == {} and store.closures == {}

    lines = []
    counts = openitems.settle_pending(None, store, lambda *a: "sorry", since, lines.append, **kw)
    assert counts["failed"] == 1 and "session:b" not in store.marks and "not settled" in lines[0]
    fresh = FakeStore()
    assert openitems.settle_pending(None, fresh, lambda *a: answer(), since, limit=1, **kw)["asked"] == 0      # only session:a
    assert set(fresh.marks) == {"session:a"}
