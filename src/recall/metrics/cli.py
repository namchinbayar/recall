"""`recall metrics ...` subcommands."""

from __future__ import annotations

import socket
import sys
import time
from datetime import datetime, timezone
from typing import Annotated

import typer

from .. import config
from . import sample, store

app = typer.Typer(help="System and manual metrics in a time series collection.", no_args_is_help=True)


@app.command()
def init() -> None:
    """Create the metrics time series collection."""
    typer.echo(store.ensure_collection(config.load()))


@app.command()
def collect(
    loop: Annotated[bool, typer.Option("--loop", help="Keep sampling forever.")] = False,
    interval: Annotated[int, typer.Option("--interval", "-i", help="Seconds between samples.")] = 60,
    host: Annotated[str | None, typer.Option("--host", help="Override the host label.")] = None,
    quiet: Annotated[bool, typer.Option("--quiet", "-q")] = False,
) -> None:
    """Take one system sample and store it (or loop with --loop)."""
    settings = config.load()
    while True:
        doc = sample.take(host)
        try:
            store.insert(settings, doc)
        except Exception as exc:  # a network blip must not end a --loop collector
            if not loop:
                raise
            print(f"sample not stored: {exc}", file=sys.stderr, flush=True)
            time.sleep(interval)
            continue
        if not quiet:
            typer.echo(
                f"{doc['ts']:%H:%M:%S} {doc['meta']['host']}  cpu {doc['cpu_pct']:.0f}%  "
                f"load {doc['load1']}  mem {doc['mem_pct']:.0f}%  "
                f"temp {doc['cpu_temp'] if doc['cpu_temp'] is not None else '-'}"
            )
        if not loop:
            break
        time.sleep(interval)


@app.command()
def add(
    name: Annotated[str, typer.Argument(help="Metric name, e.g. sleep_hours, weight_kg.")],
    value: Annotated[float, typer.Argument()],
    at: Annotated[str | None, typer.Option("--at", help="ISO timestamp; default now.")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
) -> None:
    """Record a manual metric, like sleep or weight."""
    settings = config.load()
    ts = datetime.fromisoformat(at) if at else datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.astimezone()  # interpret naive input as local time
    doc = {
        "ts": ts.astimezone(timezone.utc),
        "meta": {"host": socket.gethostname(), "kind": "manual", "name": name},
        "value": value,
    }
    if note:
        doc["note"] = note
    store.insert(settings, doc)
    typer.echo(f"recorded {name}={value} at {doc['ts']:%Y-%m-%d %H:%M} UTC")


@app.command()
def show(
    since: Annotated[str, typer.Option("--since", help="Window like 90m, 24h, 7d.")] = "24h",
    bucket: Annotated[str, typer.Option("--bucket", help="minute, hour, or day.")] = "hour",
    host: Annotated[str | None, typer.Option("--host")] = None,
    name: Annotated[str | None, typer.Option("--name", help="Show a manual metric instead.")] = None,
) -> None:
    """Averages per bucket for system samples, or a manual metric's values."""
    settings = config.load()
    if name:
        rows = store.manual(settings, since, name)
        if not rows:
            typer.echo("no entries")
            raise typer.Exit()
        for r in rows:
            note = f"  {r['note']}" if r.get("note") else ""
            typer.echo(f"{r['ts'].astimezone():%Y-%m-%d %H:%M}  {r['value']}{note}")
        return

    rows = store.summary(settings, since, host, bucket)
    if not rows:
        typer.echo("no samples")
        raise typer.Exit()
    fmt = "%Y-%m-%d %H:%M" if bucket != "day" else "%Y-%m-%d"
    typer.echo(f"{'bucket':<17}{'cpu%':>6}{'load1':>7}{'mem%':>6}{'temp':>6}{'n':>5}")
    for r in rows:
        temp = f"{r['cpu_temp']:.0f}" if r.get("cpu_temp") is not None else "-"
        typer.echo(
            f"{r['_id'].astimezone().strftime(fmt):<17}{r['cpu_pct']:>6.0f}{r['load1']:>7.2f}"
            f"{r['mem_pct']:>6.0f}{temp:>6}{r['samples']:>5}"
        )
