"""The ``knowledge`` option section."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_SIZE_LIMIT = 1_000_000


@dataclass(frozen=True)
class KnowledgeConfig:
    """Options of the knowledge area.

    The embedding keys are the defaults. The user can select another model
    with ``PUT /api/knowledge/settings``. An empty ``embedding_model`` turns
    vector search off, so search uses keywords only.
    ``max_vector_distance`` drops vector matches that are too far from the
    query. ``index_entries`` is the number of index entries of each store in
    the turn context.
    """

    embedding_provider: str = "openai"
    embedding_model: str = ""
    embedding_dimensions: int | None = None
    default_size_limit: int = DEFAULT_SIZE_LIMIT
    max_upload_bytes: int = 25_000_000
    index_entries: int = 50
    embed_batch_size: int = 64
    embed_retry_seconds: float = 60.0
    load_sqlite_vec: bool = True
    # Cosine distance (0 to 2) above which a vector match is not a result.
    # The right value depends on the embedding model.
    max_vector_distance: float = 0.75
