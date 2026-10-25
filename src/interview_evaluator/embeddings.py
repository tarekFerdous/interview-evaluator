"""Text embeddings for rubric suggestion and calibration retrieval (Voyage AI)."""

import os
from functools import lru_cache
from typing import Literal, Protocol

import voyageai
from fastapi import HTTPException

EMBEDDING_MODEL = "voyage-3.5"
EMBEDDING_DIM = 1024  # voyage-3.5 default; matches vector(1024) in the migrations

InputType = Literal["document", "query"]


class Embedder(Protocol):
    def embed(self, texts: list[str], input_type: InputType = "document") -> list[list[float]]: ...


class VoyageEmbedder:
    def __init__(self, api_key: str, model: str = EMBEDDING_MODEL):
        self.client = voyageai.Client(api_key=api_key, max_retries=2, timeout=30)
        self.model = model

    def embed(self, texts: list[str], input_type: InputType = "document") -> list[list[float]]:
        result = self.client.embed(
            texts, model=self.model, input_type=input_type, output_dimension=EMBEDDING_DIM
        )
        return result.embeddings


def get_voyage_api_key() -> str | None:
    return os.environ.get("VOYAGE_API_KEY") or None


@lru_cache
def _voyage(api_key: str) -> VoyageEmbedder:
    return VoyageEmbedder(api_key)


def get_optional_embedder() -> Embedder | None:
    """For paths that work without embeddings (storing and evaluating); None when unconfigured."""
    key = get_voyage_api_key()
    return _voyage(key) if key else None


def get_embedder() -> Embedder:
    key = get_voyage_api_key()
    if key is None:
        raise HTTPException(503, "Embeddings are not configured (VOYAGE_API_KEY is unset)")
    return _voyage(key)
