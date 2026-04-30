"""Vertex AI / Gemini embedding helper used by the long-term memory layer.

Produces unit-norm 768-dim float32 vectors and stores them as raw little-endian
bytes (3 KB per vector). At query time we fetch all of a user's memories,
unpack the bytes with NumPy, and compute cosine similarity in-process —
fast enough for tens of thousands of vectors per user without needing
``pgvector``. (We keep the storage layout pgvector-compatible so the upgrade
path is just a column type change + an index.)
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Iterable, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Multilingual model — sized for APAC reach (Hindi/Mandarin/Japanese/etc).
DEFAULT_MODEL = "text-multilingual-embedding-002"
DEFAULT_DIM = 768


def _model_name() -> str:
    return os.environ.get("SIDEKICK_EMBED_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def _output_dim() -> int:
    raw = os.environ.get("SIDEKICK_EMBED_DIM", "").strip()
    if not raw:
        return DEFAULT_DIM
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_DIM


@lru_cache(maxsize=1)
def _client():
    """Cached ``google.genai`` client — uses Vertex AI when env vars are set."""
    try:
        from google import genai  # type: ignore
    except Exception:
        logger.warning("google.genai SDK not available; memory embeddings disabled.")
        return None
    try:
        return genai.Client()
    except Exception:
        logger.exception("Failed to construct genai.Client for embeddings")
        return None


def _embed(text: str, task_type: str) -> Optional[bytes]:
    """Compute a single embedding and return raw float32 bytes, or None on failure.

    Args:
        text (str): Up to ~2 000 tokens; longer inputs are best summarised first.
        task_type (str): ``RETRIEVAL_DOCUMENT`` for stored memories, ``RETRIEVAL_QUERY`` for lookups.

    Returns:
        Optional[bytes]: Little-endian float32 bytes (length = dim × 4), or None.
    """
    s = (text or "").strip()
    if not s:
        return None
    client = _client()
    if client is None:
        return None
    try:
        from google.genai import types as gt  # type: ignore

        cfg = gt.EmbedContentConfig(task_type=task_type)
        resp = client.models.embed_content(
            model=_model_name(), contents=s, config=cfg
        )
    except Exception:
        logger.exception("embed_content failed")
        return None
    embs = getattr(resp, "embeddings", None) or []
    if not embs or not getattr(embs[0], "values", None):
        return None
    arr = np.asarray(embs[0].values, dtype=np.float32)
    # L2-normalise so `cosine = dot product` and we don't need division at query time.
    n = float(np.linalg.norm(arr))
    if n > 0:
        arr = arr / n
    return arr.tobytes()


def embed_for_storage(text: str) -> Optional[bytes]:
    """Embed ``text`` as a memory (use this when persisting a fact)."""
    return _embed(text, "RETRIEVAL_DOCUMENT")


def embed_for_query(text: str) -> Optional[bytes]:
    """Embed ``text`` as a query (use this when searching)."""
    return _embed(text, "RETRIEVAL_QUERY")


def decode_vec(blob: bytes) -> np.ndarray:
    """Materialise a stored embedding back into a NumPy array (already unit-norm)."""
    return np.frombuffer(blob, dtype=np.float32)


def cosine_top_k(
    query_blob: bytes,
    candidates: Iterable[tuple[bytes, dict]],
    *,
    k: int = 5,
    min_score: float = 0.5,
) -> list[tuple[float, dict]]:
    """Score ``candidates`` against ``query_blob`` and return the top ``k``.

    Both query and stored embeddings are L2-normalised (see :func:`_embed`),
    so cosine similarity reduces to a dot product.

    Args:
        query_blob (bytes): Output of :func:`embed_for_query`.
        candidates (Iterable[tuple[bytes, dict]]): Pairs of ``(embedding_bytes,
            row_dict)`` — the dict is what gets returned to callers.
        k (int): Maximum number of hits.
        min_score (float): Drop hits with cosine below this floor (well-calibrated
            multilingual models put noise around 0.2–0.3).

    Returns:
        list[tuple[float, dict]]: ``[(score, row), ...]`` sorted high → low.
    """
    if not query_blob:
        return []
    q = decode_vec(query_blob)
    scored: list[tuple[float, dict]] = []
    for blob, row in candidates:
        if not blob:
            continue
        v = decode_vec(blob)
        if v.shape != q.shape:
            continue
        s = float(np.dot(q, v))
        if s < min_score:
            continue
        scored.append((s, row))
    scored.sort(key=lambda x: -x[0])
    return scored[:k]
