"""A semantic index over the collected corpus (FR-261, FR-282).

Keyword search cannot answer "which postings are like this one": FTS5 matches
terms, and the dream-fit comparison spends a model call per record.  With an
embeddings model configured, this stores one vector per entity and answers
nearest-neighbour queries in pure Python - no numerical dependency, and no
behaviour on an installation that has not chosen a model.

The vectors are an *index*, not a source of truth: rebuilding is always safe,
and search degrades to an empty list rather than an error when the index or the
model is absent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from typing import Any

from dreamjob.db.connection import insert_row, new_id, query_all, update_row, utcnow
from dreamjob.llm.client import LLMClient

log = logging.getLogger(__name__)

#: How much of a description to embed.  An embedding of the whole body buys
#: little over the head of it and costs the same per token.
MAX_TEXT_CHARS = 4_000


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity; zero when either vector is empty."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _join(*parts: Any) -> str:
    return " ".join(str(p) for p in parts if p).strip()[:MAX_TEXT_CHARS]


def vacancy_text(row: dict) -> str:
    return _join(
        row.get("title"),
        row.get("function_family"),
        row.get("seniority"),
        row.get("location"),
        row.get("description"),
    )


def company_text(row: dict) -> str:
    return _join(
        row.get("name"),
        row.get("business_summary"),
        row.get("sector_codes"),
        row.get("size_band"),
        row.get("stage"),
    )


_TEXT_BUILDERS = {"vacancy": vacancy_text, "company": company_text}


def _stored_vectors(entity_type: str, model: str) -> list[tuple[str, list[float]]]:
    rows = query_all(
        "SELECT entity_id, vector FROM embedding WHERE entity_type = ? AND model = ?",
        (entity_type, model),
    )
    out: list[tuple[str, list[float]]] = []
    for row in rows:
        try:
            vector = json.loads(row["vector"])
        except (TypeError, ValueError):
            continue
        if isinstance(vector, list) and vector:
            out.append((row["entity_id"], vector))
    return out


def _upsert(entity_type: str, entity_id: str, model: str, vector: list[float], digest: str) -> None:
    existing = query_all(
        "SELECT id FROM embedding WHERE entity_type = ? AND entity_id = ? AND model = ?",
        (entity_type, entity_id, model),
    )
    values = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "model": model,
        "dim": len(vector),
        "vector": json.dumps(vector),
        "content_hash": digest,
        "created_at": utcnow(),
    }
    if existing:
        update_row("embedding", existing[0]["id"], values)
    else:
        insert_row("embedding", {"id": new_id(), **values})


def backfill(
    entity_type: str = "vacancy",
    *,
    llm: LLMClient | None = None,
    limit: int | None = None,
    batch: int = 64,
) -> dict[str, Any]:
    """Embed the entities that have no vector, or whose text has changed."""
    builder = _TEXT_BUILDERS.get(entity_type)
    if builder is None:
        raise ValueError(f"No text builder for entity_type {entity_type!r}")
    client = llm or LLMClient()
    model = (client.settings.embeddings_model or "").strip()
    if not model:
        return {"indexed": 0, "skipped": "no embeddings model configured", "entity_type": entity_type}

    sql = f"SELECT * FROM {entity_type}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = query_all(sql)
    known = {
        row["entity_id"]: row["content_hash"]
        for row in query_all(
            "SELECT entity_id, content_hash FROM embedding WHERE entity_type = ? AND model = ?",
            (entity_type, model),
        )
    }

    pending: list[tuple[str, str, str]] = []
    for row in rows:
        text = builder(row)
        if not text:
            continue
        digest = _content_hash(text)
        if known.get(row["id"]) == digest:
            continue
        pending.append((row["id"], text, digest))

    indexed = 0
    for start in range(0, len(pending), batch):
        chunk = pending[start : start + batch]
        vectors = client.embed([text for _, text, _ in chunk])
        for (entity_id, _text, digest), vector in zip(chunk, vectors, strict=False):
            _upsert(entity_type, entity_id, model, vector, digest)
            indexed += 1
    return {
        "indexed": indexed,
        "considered": len(rows),
        "model": model,
        "entity_type": entity_type,
    }


def search(
    query: str,
    *,
    llm: LLMClient | None = None,
    entity_type: str = "vacancy",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """The ``limit`` entities most similar to ``query``, best first."""
    if not query.strip():
        return []
    client = llm or LLMClient()
    model = (client.settings.embeddings_model or "").strip()
    if not model:
        return []
    try:
        query_vector = client.embed([query])[0]
    except Exception:  # noqa: BLE001 - search is an enhancement, never a failure
        log.warning("Semantic search unavailable", exc_info=True)
        return []
    scored = [
        (cosine(query_vector, vector), entity_id)
        for entity_id, vector in _stored_vectors(entity_type, model)
    ]
    scored.sort(reverse=True)
    top = scored[: max(1, int(limit))]
    if not top:
        return []
    ids = [entity_id for _, entity_id in top]
    marks = ", ".join("?" for _ in ids)
    rows = query_all(f"SELECT * FROM {entity_type} WHERE id IN ({marks})", tuple(ids))
    by_id = {row["id"]: row for row in rows}
    return [
        {**by_id.get(entity_id, {}), "similarity": round(score, 4)}
        for score, entity_id in top
        if entity_id in by_id
    ]


def available(llm: LLMClient | None = None) -> bool:
    client = llm or LLMClient()
    return bool((client.settings.embeddings_model or "").strip())
