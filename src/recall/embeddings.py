"""Local embeddings via fastembed (ONNX, no network at runtime after model download)."""

from __future__ import annotations

from functools import lru_cache
from typing import Iterable

from fastembed import TextEmbedding

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"  # 384 dims, ~30MB


@lru_cache(maxsize=1)
def _model(name: str = DEFAULT_MODEL) -> TextEmbedding:
    return TextEmbedding(model_name=name)


def embed(texts: Iterable[str], model: str = DEFAULT_MODEL) -> list[list[float]]:
    m = _model(model)
    return [list(map(float, v)) for v in m.embed(list(texts))]


def embed_one(text: str, model: str = DEFAULT_MODEL) -> list[float]:
    return embed([text], model)[0]
