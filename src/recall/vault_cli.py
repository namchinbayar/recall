"""`recall vault ...` subcommands."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from . import config, vault

app = typer.Typer(help="Encrypted store for documents and secrets. The assistant cannot read it.", no_args_is_help=True)

Tags = Annotated[list[str] | None, typer.Option("--tag", "-t")]


@app.command()
def add(
    target: Annotated[str, typer.Argument(help="File path, or '-' for stdin.")],
    title: Annotated[str, typer.Option("--title", help="How you will search for it.")],
    note: Annotated[str | None, typer.Option("--note", help="Extra searchable words. Not secret.")] = None,
    tags: Tags = None,
    embed_content: Annotated[bool, typer.Option("--embed-content", help="Also embed the document text so its contents are searchable. Sends the text to the embedding API once.")] = False,
) -> None:
    """Encrypt and store a file. Only title and note are searchable by default."""
    settings = config.load()
    source = vault.add(settings, target, title, note=note, tags=tags, embed_content=embed_content)
    typer.echo(f"stored {source}  ({title})")


@app.command()
def get(
    query: Annotated[str, typer.Argument(help="Title words, or an exact vault:xxxx source id.")],
    copy: Annotated[bool, typer.Option("--copy", "-c", help="Copy the text to the clipboard instead of printing.")] = False,
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Write the original file here.")] = None,
    pick: Annotated[int, typer.Option("--pick", help="Which match to open, 1-based.")] = 1,
) -> None:
    """Decrypt and show the best match."""
    settings = config.load()
    if query.startswith("vault:"):
        entry = vault.get_by_source(settings, query)
        if not entry:
            raise SystemExit("no such vault entry")
    else:
        hits = vault.find(settings, query, limit=max(pick, 3))
        if not hits:
            raise SystemExit("vault is empty or nothing matches")
        if len(hits) > 1:
            for i, h in enumerate(hits, 1):
                mark = "*" if i == pick else " "
                typer.secho(f"{mark} {i}. {h['title']}  ({h['score']:.2f})  {h['source']}", fg="bright_black")
        entry = hits[pick - 1]

    data, text = vault.decrypt(entry)
    v = entry["vault"]
    typer.secho(f"{entry['title']}  [{v.get('filename') or 'stdin'}, {v['size']} bytes, {v['content_type']}]", bold=True)

    if out:
        out.write_bytes(data)
        typer.echo(f"wrote {out}")
        return
    if not text:
        raise SystemExit("binary content; use --out FILE to save it")
    if copy:
        if not shutil.which("wl-copy"):
            raise SystemExit("wl-copy not found")
        subprocess.run(["wl-copy"], input=text.encode(), check=True)
        typer.echo("copied to clipboard")
        return
    typer.echo(text)


@app.command(name="list")
def list_cmd() -> None:
    """List vault entries (titles only)."""
    rows = vault.list_entries(config.load())
    if not rows:
        typer.echo("vault is empty")
        raise typer.Exit()
    for r in rows:
        extra = [t for t in r["tags"] if t != vault.TAG]
        tag_str = f"  [{', '.join(extra)}]" if extra else ""
        typer.echo(f"{r['created_at'].astimezone():%Y-%m-%d}  {r['title']}{tag_str}  ({r['vault']['size']} B)")
        typer.secho(f"            {r['source']}", fg="bright_black")


@app.command()
def rm(source: Annotated[str, typer.Argument(help="vault:xxxx id from `recall vault list`.")]) -> None:
    """Delete a vault entry."""
    n = vault.remove(config.load(), source)
    typer.echo(f"removed {n} entry")
