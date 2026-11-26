"""Voyage AI embeddings."""

from __future__ import annotations

import voyageai

from .config import Settings

_client: voyageai.Client | None = None


def _vo(settings: Settings) -> voyageai.Client:
    global _client
    if _client is None:
        kwargs = {"api_key": settings.voyage_api_key}
        if settings.voyage_base_url:
            kwargs["base_url"] = settings.voyage_base_url
        _client = voyageai.Client(**kwargs)
    return _client


def embed_documents(settings: Settings, texts: list[str]) -> list[list[float]]:
    """Embed chunks for storage. Batched; Voyage caps at 128 texts per call."""
    out: list[list[float]] = []
    for i in range(0, len(texts), 128):
        batch = texts[i : i + 128]
        result = _vo(settings).embed(
            batch,
            model=settings.voyage_model,
            input_type="document",
            output_dimension=settings.dimensions,
        )
        out.extend(result.embeddings)
    return out


def embed_query(settings: Settings, text: str) -> list[float]:
    """Embed a search query. Uses input_type='query', which matters for Voyage."""
    result = _vo(settings).embed(
        [text],
        model=settings.voyage_model,
        input_type="query",
        output_dimension=settings.dimensions,
    )
    return result.embeddings[0]


def rerank(settings: Settings, query: str, documents: list[str]) -> list[float]:
    """Relevance score per document, in input order."""
    result = _vo(settings).rerank(query, documents, model=settings.rerank_model, truncation=True)
    scores = [0.0] * len(documents)
    for r in result.results:
        scores[r.index] = r.relevance_score
    return scores
