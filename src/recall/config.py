"""Settings loaded from the environment (and a .env file if present)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _load_env() -> None:
    """Load the first env file found, so `recall` works from any directory.

    Order: $RECALL_ENV, ./.env, ~/.config/recall/env, <project root>/.env.
    """
    candidates = [
        os.environ.get("RECALL_ENV"),
        ".env",
        Path.home() / ".config" / "recall" / "env",
        Path(__file__).resolve().parents[2] / ".env",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            load_dotenv(c)
            return


_load_env()


def owner() -> str:
    """Who this recall belongs to, as stored text should name them: RECALL_OWNER, else "the user".
    It goes into the prompts that write facts, session notes and the profile, so a fact reads
    "Ada runs niri" where it would read "The user runs niri"."""
    return " ".join(os.environ.get("RECALL_OWNER", "").split())[:60] or "the user"


def personal(text: str) -> str:
    """A prompt with its {owner} and {Owner} marks filled in ({Owner} starts a sentence)."""
    name = owner()
    return text.replace("{Owner}", name[:1].upper() + name[1:]).replace("{owner}", name)


@dataclass(frozen=True)
class Settings:
    mongodb_uri: str
    voyage_api_key: str
    db_name: str = "recall"
    collection: str = "chunks"
    index_name: str = "vector_index"
    voyage_model: str = "voyage-4"
    dimensions: int = 1024
    voyage_base_url: str | None = None
    text_index_name: str = "text_index"
    rerank_model: str | None = "rerank-3"
    # Other databases on the same cluster that search also reads. Nothing is ever written to them.
    read_dbs: tuple[str, ...] = ()
    # Database user given apart from the URI (MONGODB_USERNAME / MONGODB_PASSWORD). They replace any
    # credentials in the URI, so an instance can share the cluster address without the main user's password.
    mongodb_username: str | None = None
    mongodb_password: str | None = None


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


def _read_dbs(own: str) -> tuple[str, ...]:
    """RECALL_READ_DBS: comma-separated database names, searched but never written."""
    names = [n.strip() for n in os.environ.get("RECALL_READ_DBS", "").split(",")]
    return tuple(dict.fromkeys(n for n in names if n and n != own))


def load() -> Settings:
    db_name = os.environ.get("RECALL_DB", "recall")
    return Settings(
        mongodb_uri=_require("MONGODB_URI"),
        voyage_api_key=_require("VOYAGE_API_KEY"),
        db_name=db_name,
        collection=os.environ.get("RECALL_COLLECTION", "chunks"),
        index_name=os.environ.get("RECALL_INDEX", "vector_index"),
        voyage_model=os.environ.get("VOYAGE_MODEL", "voyage-4"),
        dimensions=int(os.environ.get("VOYAGE_DIMENSIONS", "1024")),
        voyage_base_url=os.environ.get("VOYAGE_BASE_URL") or None,
        text_index_name=os.environ.get("RECALL_TEXT_INDEX", "text_index"),
        # Empty RECALL_RERANK_MODEL turns reranking off.
        rerank_model=os.environ.get("RECALL_RERANK_MODEL", "rerank-3") or None,
        read_dbs=_read_dbs(db_name),
        mongodb_username=os.environ.get("MONGODB_USERNAME") or None,
        mongodb_password=os.environ.get("MONGODB_PASSWORD") or None,
    )
