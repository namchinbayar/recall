from pathlib import Path

import pytest

from recall import sync


def write(root: Path, rel: str, text: str = "some words\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def watch(root: Path, **kw) -> sync.Watch:
    return sync.Watch(path=root.resolve(), **kw)


def have_after(todo: sync.Plan) -> dict:
    """What recall would hold once the plan's saves are stored."""
    return {i.source: {"title": i.title, "tags": i.tags, "sync": {"hash": i.digest, "root": i.root}} for i in todo.save}


def test_candidates_follow_include_exclude_and_skip_hidden(tmp_path):
    for rel in ("a.md", "b.txt", "c.py", "MEMORY.md", ".hidden.md", "sub/d.md", "drafts/e.md",
                ".git/f.md", "sub/.cache/g.md", "node_modules/h.md", "keys/server.pem", "sub/.env"):
        write(tmp_path, rel)
    (tmp_path / "link.md").symlink_to(tmp_path / "a.md")
    w = watch(tmp_path, exclude=("MEMORY.md", "drafts/"))
    assert [p.relative_to(tmp_path).as_posix() for p in sync.candidates(w)] == ["a.md", "b.txt", "sub/d.md"]
    flat = watch(tmp_path, recursive=False, include=("*.md", "*.pem"))
    assert [p.name for p in sync.candidates(flat)] == ["MEMORY.md", "a.md"]


def test_single_file_watch_allows_a_dotfile_but_not_a_key(tmp_path):
    assert sync.candidates(watch(write(tmp_path, ".zshrc"))) == [tmp_path / ".zshrc"]
    assert sync.candidates(watch(write(tmp_path, "id_ed25519"))) == []
    assert sync.candidates(watch(write(tmp_path, ".env.local"))) == []


@pytest.mark.parametrize("text, kind", [
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nabc", "private key"),
    ("key is sk-ant-api03-abcdefghijklmnopqrstuv", "API key"),
    ("x\ny\nGH=ghp_abcdefghijklmnopqrstuvwxyz0123456789", "GitHub token, line 3"),
    ("AKIAIOSFODNN7EXAMPLE", "AWS key"),
    ("mongodb+srv://user:hunter2pass@cluster.mongodb.net/db", "password in a URL"),
    ('API_KEY="aB3dE5fG7hJ9kL1mN3pQ5rS7tU9v"', "secret value"),
    ("secret = 0123456789abcdef0123456789abcdef", "secret value"),
    ("SSN 123-45-6789", "SSN"),
    ("EIN: 12-3456789", "EIN"),
    ("token pst_abcdefghijklmnop::QWERTYuiop1234567890xyz", "Proton Pass token"),
])
def test_find_secret_catches(text, kind):
    assert kind in sync.find_secret(text)


@pytest.mark.parametrize("text", [
    "token: /home/you/Projects/recall/contrib/bin/recall-mcp-thing",
    "auth: mcp__8dd4b6f9-fa3e-412b-a669-7b8460f41e9a",
    "password = op://vault/recall/MONGODB_URI",
    "MONGODB_URI=pass://vault/MONGODB_URI/credential",
    "the secret: use-the-documented-setup-command-first",
    "on 2026-11-08 session 55448dc4-649d-487d-a04e-f04d274fd681 call 415-555-0123",
    "see https://github.com/YaLTeR/niri/wiki and http://127.0.0.1:4317/",
    "{{resolve:secretsmanager:secret-id:SecretString:json-key}}",
])
def test_find_secret_leaves_ordinary_text(text):
    assert sync.find_secret(text) is None


def test_plan_saves_new_then_only_what_changed(tmp_path):
    a, b = write(tmp_path, "a.md"), write(tmp_path, "sub/b.md")
    w = watch(tmp_path, title="Notes", tags=("notes",))
    first = sync.plan([w], {})
    assert [(i.title, i.tags, i.source) for i in first.save] == [
        ("Notes: a.md", ["notes"], f"file:{a}"), ("Notes: sub/b.md", ["notes"], f"file:{b}")]
    have = have_after(first)

    again = sync.plan([w], have)
    assert not again.changes and again.unchanged == 2

    b.write_text("different words\n")
    a.unlink()
    third = sync.plan([w], have)
    assert [i.path for i in third.save] == [b] and third.remove == [f"file:{a}"] and third.unchanged == 0


def test_retagging_a_watch_resaves_its_files(tmp_path):
    write(tmp_path, "a.md")
    have = have_after(sync.plan([watch(tmp_path, tags=("one",))], {}))
    todo = sync.plan([watch(tmp_path, tags=("two",))], have)
    assert [i.tags for i in todo.save] == [["two"]]


def test_secret_binary_big_and_empty_files_are_skipped_and_dropped(tmp_path):
    ok = write(tmp_path, "ok.md")
    leak = write(tmp_path, "leak.md", "fine for now\n")
    have = have_after(sync.plan([watch(tmp_path)], {}))
    leak.write_text("TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789\n")
    write(tmp_path, "blob.txt").write_bytes(b"\x00\x01\x02binary")
    write(tmp_path, "big.txt", "x" * 3000)
    write(tmp_path, "empty.md", "  \n")
    todo = sync.plan([watch(tmp_path, max_bytes=2000)], have)
    assert todo.save == [] and todo.unchanged == 1
    why = {p.name: reason for p, reason in todo.skipped}
    assert "GitHub token, line 1" in why["leak.md"] and "ghp_" not in why["leak.md"]
    assert why["blob.txt"] == "binary" and why["empty.md"] == "empty" and "larger than 2 KB" in why["big.txt"]
    assert todo.remove == [f"file:{leak}"]      # the copy saved before the secret appeared goes away
    assert f"file:{ok}" not in todo.remove


def test_takes_over_a_recall_add_source_and_keeps_its_title_and_tags(tmp_path):
    f = write(tmp_path, "config.kdl")
    have = {f"file:{f}": {"title": "niri: config.kdl", "tags": ["desktop", "niri"], "sync": None},
            "file:/elsewhere/README.md": {"title": "README.md", "tags": [], "sync": None}}
    todo = sync.plan([watch(f)], have)
    assert [(i.title, i.tags) for i in todo.save] == [("niri: config.kdl", ["desktop", "niri"])]
    assert todo.remove == [] and todo.orphans == []      # a source sync never saved is never removed
    kept = sync.plan([watch(f)], have_after(todo))
    assert not kept.changes
    f.write_text("changed\n")
    assert [(i.title, i.tags) for i in sync.plan([watch(f)], have_after(todo)).save] == [("niri: config.kdl", ["desktop", "niri"])]


def test_missing_root_removes_nothing_and_dropped_watch_is_an_orphan(tmp_path):
    a = write(tmp_path / "one", "a.md")
    b = write(tmp_path / "two", "b.md")
    one, two = watch(tmp_path / "one"), watch(tmp_path / "two")
    have = have_after(sync.plan([one, two], {}))
    a.unlink()
    (tmp_path / "one").rmdir()
    todo = sync.plan([one], have)
    assert todo.missing == [one.path] and todo.remove == [] and todo.orphans == [f"file:{b}"]


def test_apply_stores_with_the_hash_and_reports_failures(tmp_path, monkeypatch):
    write(tmp_path, "a.md"), write(tmp_path, "b.md")
    stored, removed = [], []

    def fake_store(settings, source, title, text, tags, extra):
        if source.endswith("b.md"):
            raise RuntimeError("rate limited\nmore")
        stored.append((source, title, extra))
        return source, 1

    monkeypatch.setattr(sync.ingest, "store", fake_store)
    monkeypatch.setattr(sync.ingest, "remove", lambda s, source: removed.append(source) or 3)
    todo = sync.plan([watch(tmp_path)], {})
    todo.remove, todo.orphans = ["file:/gone.md"], ["file:/orphan.md"]
    saved, gone, failed = sync.apply("S", todo)
    assert (saved, gone) == (1, 1) and removed == ["file:/gone.md"]
    assert stored[0][2] == {"sync": {"hash": todo.save[0].digest, "root": str(tmp_path.resolve())}}
    assert failed == [(tmp_path / "b.md", "RuntimeError: rate limited")]
    assert sync.apply("S", todo, prune=True)[1] == 2


def test_load_config_and_fingerprint(tmp_path):
    f = write(tmp_path / "notes", "a.md")
    cfg = tmp_path / "sync.toml"
    cfg.write_text(f'[[watch]]\npath = "{tmp_path}/notes"\ntitle = "Notes"\ntags = ["n"]\nexclude = ["x/"]\nmax_kb = 9\n')
    (w,) = sync.load(cfg)
    assert (w.path, w.title, w.tags, w.exclude, w.max_bytes, w.include) == (
        (tmp_path / "notes").resolve(), "Notes", ("n",), ("x/",), 9000, sync.DEFAULT_INCLUDE)
    before = sync.fingerprint([w])
    f.write_text("longer text than before\n")
    assert sync.fingerprint([w]) != before
    cfg.write_text('[[watch]]\npath = "/x"\ninclud = ["*.md"]\n')
    with pytest.raises(SystemExit, match="includ"):
        sync.load(cfg)
    with pytest.raises(SystemExit, match="no sync config"):
        sync.load(tmp_path / "nope.toml")


def test_report_shortens_home_paths(tmp_path):
    f = write(tmp_path, "docs/a.md")
    todo = sync.plan([watch(tmp_path / "docs", title="Docs")], {})
    todo.orphans.append(f"file:{tmp_path}/old.md")
    lines = sync.report(todo, home=tmp_path)
    assert lines[0] == "  save     ~/docs/a.md  ->  Docs: a.md" and "~/old.md" in lines[1]
