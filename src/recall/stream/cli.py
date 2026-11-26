"""`recall stream ...` subcommands."""

from __future__ import annotations

import getpass
import json
import os
import time
import uuid
from pathlib import Path
from typing import Annotated

import typer

from .. import config
from . import kafka

app = typer.Typer(help="Event stream through Confluent Kafka into Atlas.", no_args_is_help=True)

ENV_FILE = Path(os.environ.get("RECALL_ENV") or Path.home() / ".config" / "recall" / "env")
PUBLIC_DEFAULTS = {
    "KAFKA_BOOTSTRAP": "<cluster>.<region>.aws.confluent.cloud:9092",
    "SCHEMA_REGISTRY_URL": "https://<registry>.<region>.aws.confluent.cloud",
}


@app.command()
def setup() -> None:
    """Save the Kafka and Schema Registry API keys to the env file (input is hidden)."""
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    keep = [l for l in lines if l.split("=", 1)[0] not in (*kafka.SECRET_KEYS, *PUBLIC_DEFAULTS)]
    values = dict(PUBLIC_DEFAULTS)
    for name in kafka.SECRET_KEYS:
        value = getpass.getpass(f"{name}: ").strip()
        if not value:
            raise SystemExit(f"{name} is empty, nothing saved.")
        values[name] = value
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text("\n".join(keep + [f"{k}={v}" for k, v in values.items()]) + "\n")
    ENV_FILE.chmod(0o600)
    typer.echo(f"saved {len(values)} settings to {ENV_FILE}")


@app.command()
def send(
    body: Annotated[str, typer.Argument(help="Event text.")],
    source: Annotated[str, typer.Option("--source", "-s")] = "manual",
    kind: Annotated[str | None, typer.Option("--kind", "-k")] = None,
) -> None:
    """Produce one event by hand (for testing the pipeline)."""
    producer = kafka.EventProducer(kafka.load())
    producer.send({
        "event_id": uuid.uuid4().hex,
        "source": source,
        "kind": kind,
        "actor": os.environ.get("USER"),
        "occurred_at": int(time.time() * 1000),
        "body": body,
        "meta": json.dumps({}),
    })
    left = producer.flush()
    typer.echo("sent" if left == 0 else f"{left} message(s) not delivered")


@app.command()
def notify(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print events instead of producing them.")] = False,
) -> None:
    """Stream desktop notifications into Kafka (runs until stopped)."""
    from . import notify as notifications

    producer = None if dry_run else kafka.EventProducer(kafka.load())
    for event in notifications.watch():
        if producer is None:
            typer.echo(json.dumps(event))
        else:
            producer.send(event)


@app.command()
def spool(
    once: Annotated[bool, typer.Option("--once", help="Deliver what is waiting, then exit.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print events instead of producing them.")] = False,
) -> None:
    """Produce events that local programs (the Hermes hook) append to the spool file."""
    from . import spool as spool_mod

    if dry_run:
        spool_mod.run(lambda e: typer.echo(json.dumps(e)), lambda: 0, once=True, commit=False)
        return
    producer = kafka.EventProducer(kafka.load())
    spool_mod.run(producer.send, producer.flush, once=once)


@app.command()
def sink(
    once: Annotated[bool, typer.Option("--once", help="Store what is waiting, then exit.")] = False,
    batch: Annotated[int, typer.Option("--batch", help="Events per embedding call.")] = 64,
) -> None:
    """Store events from Kafka in Atlas with embeddings (runs until stopped)."""
    from . import sink as sink_mod

    sink_mod.run(config.load(), batch_size=batch, once=once)


@app.command()
def purge(
    yes: Annotated[bool, typer.Option("--yes", help="Delete; without it, only report what would go.")] = False,
    retention_days: Annotated[int | None, typer.Option(
        "--retention-days", help="Also expire notifications this many days after they arrive (0 removes the expiry).",
    )] = None,
) -> None:
    """Apply the noise rules (noise.toml) to desktop notifications already stored."""
    from . import noise, purge as purge_mod

    settings = config.load()
    p = purge_mod.plan(settings, noise.load())
    total = p.kept + len(p.delete)
    typer.echo(f"{total} stored notification(s): keep {p.kept}, drop {len(p.delete)}")
    for reason, n in p.reasons.most_common():
        typer.echo(f"  {n:>6}  {reason}")
    if yes:
        typer.echo(f"deleted {purge_mod.apply(settings, p)}")
        if retention_days is not None:
            typer.echo(purge_mod.set_retention(settings, retention_days))
    else:
        typer.echo("dry run; pass --yes to delete")
