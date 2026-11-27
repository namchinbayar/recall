from datetime import datetime, timezone
from pathlib import Path

import pytest

from recall import evals
from recall.evals import Case
from recall.search import Hit


def hit(source, score=0.9, title=None):
    return Hit(source=source, title=title or source, chunk_index=0, text="", score=score, tags=[])


def write(tmp_path, text):
    path = tmp_path / "eval.toml"
    path.write_text(text)
    return path


def test_load_reads_one_source_or_a_list_and_expands_the_home_folder(tmp_path):
    cases = evals.load(write(tmp_path, '''
        [[case]]
        q = " where are the secrets "
        expect = "file:~/notes/secrets.md"
        avoid = ["file:~/notes/old.md"]
        tags = ["memory-note"]
        events = true

        [[case]]
        q = "sourdough"
        expect = []
    '''))
    home = Path.home()
    assert cases[0] == Case("where are the secrets", (f"file:{home}/notes/secrets.md",), (f"file:{home}/notes/old.md",),
                            ("memory-note",), True)
    assert cases[1] == Case("sourdough", ())


@pytest.mark.parametrize("text, said", [
    ('[[case]]\nq = "x"', "needs `q` and `expect`"),
    ('[[case]]\nq = "x"\nexpect = "a"\nexpects = "b"', "unknown keys: ['expects']"),
    ('[[case]]\nq = "x"\nexpect = "a"\n[[case]]\nq = "x"\nexpect = "b"', "repeats the question"),
    ('[[case]]\nq = "x"\nexpect = 3', "must be a source id"),
    ('title = "empty"', "no [[case]] entries"),
    ('[[case', "eval.toml"),
])
def test_load_says_what_is_wrong_with_a_question_set(tmp_path, text, said):
    with pytest.raises(SystemExit) as exc:
        evals.load(write(tmp_path, text))
    assert said in str(exc.value)


def test_a_missing_question_set_points_at_the_example(tmp_path):
    with pytest.raises(SystemExit) as exc:
        evals.load(tmp_path / "none.toml")
    assert "contrib/eval.example.toml" in str(exc.value)


def test_judge_finds_the_place_of_the_first_expected_source():
    case = Case("q", ("session:abc*", "file:/n/a.md"))
    assert evals.judge(case, [hit("x"), hit("session:abcdef"), hit("file:/n/a.md")]).rank == 2
    missed = evals.judge(case, [hit("x"), hit("session:ab")])
    assert (missed.kind, missed.rank) == ("missed", None)
    assert evals.judge(case, []).kind == "missed"
    assert evals.judge(Case("q", ("file:/n/[a].md",)), [hit("file:/n/a.md")]).rank == 1      # fnmatch classes work


def test_a_question_whose_answer_is_not_stored_is_not_a_miss():
    case = Case("q", ("file:/gone.md",))
    assert evals.judge(case, [hit("x")], stored={"x", "y"}).kind == "absent"
    assert evals.judge(case, [hit("x")], stored={"x", "file:/gone.md"}).kind == "missed"
    assert evals.judge(case, [hit("x")]).kind == "missed"            # without the list of sources: a miss


def test_a_replaced_note_above_the_answer_is_flagged():
    case = Case("q", ("new",), avoid=("old",))
    assert evals.judge(case, [hit("old"), hit("new")]).stale is True
    assert evals.judge(case, [hit("new"), hit("old")]).stale is False
    assert evals.judge(case, [hit("old"), hit("x")]).stale is True      # and the answer is missing


def test_a_question_with_no_answer_must_stay_under_the_threshold():
    case = Case("sourdough", ())
    assert evals.judge(case, [hit("x", 0.30)]).kind == "quiet"
    assert evals.judge(case, []).kind == "quiet"
    assert evals.judge(case, [hit("x", 0.70)]).kind == "noisy"
    assert evals.judge(case, [hit("x", 0.30)], quiet_below=0.2).kind == "noisy"


def outcomes():
    cases = [Case("first", ("a",)), Case("third", ("b",)), Case("miss", ("c",)), Case("gone", ("d",)),
             Case("silence", ()), Case("chatter", ()), Case("stale", ("new",), avoid=("old",))]
    results = {
        "first": [hit("a")], "third": [hit("x"), hit("y"), hit("b")], "miss": [hit("x")], "gone": [hit("x")],
        "silence": [hit("x", 0.2)], "chatter": [hit("x", 0.8)], "stale": [hit("old"), hit("new")],
    }
    seen = []
    out = evals.run(cases, lambda c: results[c.q], stored={"a", "b", "c", "new", "old", "x", "y"}, progress=seen.append)
    assert seen == [1, 2, 3, 4, 5, 6, 7]
    return out


def test_summary_counts_only_questions_that_can_be_answered():
    s = evals.summarize(outcomes())
    assert (s.asked, s.first, s.found, s.stale) == (4, 1, 3, 1)       # first, third, miss, stale
    assert s.mrr == round((1 + 1 / 3 + 0 + 1 / 2) / 4, 3)
    assert (s.quiet, s.silent, s.absent) == (1, 2, 1)
    assert evals.summarize([]).mrr == 0.0


def test_report_lists_what_went_wrong_and_how_the_baseline_moved(tmp_path):
    before = outcomes()
    path = tmp_path / "state" / "eval.json"
    assert evals.load_baseline(path) is None
    evals.save_baseline(evals.snapshot(before, {"limit": 5}, datetime(2026, 11, 26, tzinfo=timezone.utc)), path)
    baseline = evals.load_baseline(path)
    assert baseline["summary"]["first"] == 1 and "gone" not in baseline["cases"]

    text = "\n".join(evals.report(before, 5, baseline))
    for part in ("answer first          1 of 4  (25%)", "answer in the top 5   3 of 4  (75%)", "quiet when it should  1 of 2",
                 "replaced note on top  1", "missed (1)", "answer not first (2)", "not quiet (1)",
                 "expected source is not stored (1)", "against the baseline of 2026-11-26", "no question moved",
                 "      got    1. x (0.90)  x", "             2. new (0.90)  new"):
        assert part in text, part

    after = [evals.judge(o.case, hits, {"a", "b", "c", "new", "old"}) for o, hits in zip(before, [
        [hit("x"), hit("a")],           # first: was #1, now #2
        [hit("b")],                     # third: now #1
        [hit("c")],                     # miss: now found
        [hit("x")],                     # gone: still not stored
        [hit("x", 0.9)],                # silence: now noisy
        [hit("x", 0.8)],                # chatter: unchanged
        [hit("old"), hit("new")],       # stale: unchanged
    ])]
    better, worse = evals.compare(after, baseline)
    assert [o.case.q for o in better] == ["third", "miss"] and [o.case.q for o in worse] == ["first", "silence"]
    text = "\n".join(evals.report(after, 5, baseline, verbose=True))
    for part in ("better #3 → #1  third", "better missed → #1  miss", "worse  #1 → #2  first", "worse  quiet → noisy  silence",
                 "every question (7)", "first 1 → 2"):
        assert part in text, part
    assert "(the baseline counted the top 5)" not in text
    assert "(the baseline counted the top 5)" in "\n".join(evals.report(after, 8, baseline))
