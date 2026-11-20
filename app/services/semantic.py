"""Meaning-based search for the menu and the FAQ: FastEmbed (bge-small-en-v1.5) vectors in an
embedded Qdrant index on disk. It sits next to the keyword search; app.services.search merges
the two.

The index only maps text to ids. Prices, availability and approval always come from SQL, so a
stale index can rank a result oddly but can never show a wrong price or a removed item. It
keeps itself current: each search compares a fingerprint of every document's text with what
was indexed and re-embeds only what changed (a menu edit in the portal, a new FAQ answer).

The model (about 70 MB) downloads once into data/models: when the worker starts (in the
background), or ahead of time with `python -m app.download_model`. Every tool on the machine
(the bot, the eval runner, the search check, the tests) shares that one copy. Until it's ready,
or if it can't load, search is keyword-only.
"""

import hashlib
import logging
import threading
import uuid
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

# The repo's data/models, wherever the command runs from, so every tool shares one download.
MODELS_DIR = Path(__file__).resolve().parents[2] / "data" / "models"
_NS = uuid.UUID("6f1d3c52-6c0e-4c8a-9a51-3b7f4f9b2d10")
# BGE English models retrieve better when a short query carries this instruction.
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class Embedder(Protocol):
    dim: int

    def passages(self, texts: list[str]) -> list[list[float]]: ...

    def query(self, text: str) -> list[float]: ...


class FastEmbedder:
    def __init__(self, model_name: str, cache_dir: Path):
        from fastembed import TextEmbedding

        cache_dir.mkdir(parents=True, exist_ok=True)
        self._model = TextEmbedding(model_name=model_name, cache_dir=str(cache_dir))
        low = model_name.lower()
        self._prefix = BGE_QUERY_PREFIX if "bge-" in low and "-en" in low else ""
        self.dim = len(self.query("donut"))

    def passages(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._model.passage_embed(texts)]

    def query(self, text: str) -> list[float]:
        return next(iter(self._model.query_embed(self._prefix + text))).tolist()


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class SemanticIndex:
    """One Qdrant collection per kind ("menu", "faq"), cosine distance, point id = uuid5(kind:id)."""

    def __init__(self, embedder: Embedder, client):
        self.embedder = embedder
        self.client = client
        self._lock = threading.Lock()
        self._indexed: dict[str, dict[str, str]] = {}  # kind -> {doc_id: fingerprint}

    def _collection(self, kind: str) -> str:
        return f"sprinkle_{kind}"

    def _ensure(self, kind: str) -> dict[str, str]:
        if kind in self._indexed:
            return self._indexed[kind]
        from qdrant_client import models

        name = self._collection(kind)
        known: dict[str, str] = {}
        if self.client.collection_exists(name):
            info = self.client.get_collection(name)
            size = info.config.params.vectors.size
            if size != self.embedder.dim:  # model changed: start over
                self.client.delete_collection(name)
        if not self.client.collection_exists(name):
            self.client.create_collection(
                name, vectors_config=models.VectorParams(size=self.embedder.dim, distance=models.Distance.COSINE))
        else:
            offset = None
            while True:
                points, offset = self.client.scroll(name, limit=256, offset=offset, with_payload=True, with_vectors=False)
                for p in points:
                    known[str(p.payload.get("doc_id"))] = str(p.payload.get("fp"))
                if offset is None:
                    break
        self._indexed[kind] = known
        return known

    def sync(self, kind: str, docs: dict[str, str]) -> int:
        """Make the collection hold exactly `docs` ({doc_id: text}). Returns how many were (re)embedded."""
        from qdrant_client import models

        with self._lock:
            known = self._ensure(kind)
            want = {doc_id: fingerprint(text) for doc_id, text in docs.items()}
            changed = [d for d, fp in want.items() if known.get(d) != fp]
            gone = [d for d in known if d not in want]
            name = self._collection(kind)
            if changed:
                vectors = self.embedder.passages([docs[d] for d in changed])
                self.client.upsert(name, points=[
                    models.PointStruct(id=str(uuid.uuid5(_NS, f"{kind}:{d}")), vector=v, payload={"doc_id": d, "fp": want[d]})
                    for d, v in zip(changed, vectors, strict=True)
                ])
            if gone:
                self.client.delete(name, points_selector=models.PointIdsList(
                    points=[str(uuid.uuid5(_NS, f"{kind}:{d}")) for d in gone]))
            for d in changed:
                known[d] = want[d]
            for d in gone:
                known.pop(d, None)
            if changed or gone:
                log.info("search index %s: %d updated, %d removed", kind, len(changed), len(gone))
            return len(changed)

    def search(self, kind: str, query: str, limit: int) -> list[tuple[str, float]]:
        """[(doc_id, cosine score)] best first."""
        with self._lock:
            if kind not in self._indexed:
                return []
            vector = self.embedder.query(query)
            hits = self.client.query_points(self._collection(kind), query=vector, limit=limit, with_payload=True).points
        return [(str(h.payload["doc_id"]), float(h.score)) for h in hits]


# --- the process-wide index -------------------------------------------------------------

_index: SemanticIndex | None = None
_state = {"status": "off", "error": None}
_start_lock = threading.Lock()


def get_index() -> SemanticIndex | None:
    """The index once it's ready; None while loading, when off, or after a failure."""
    return _index


def status() -> dict:
    return dict(_state)


def set_index(index: SemanticIndex | None) -> None:
    """Install an index directly (tests, or a caller that built its own)."""
    global _index
    _index = index
    _state.update(status="ready" if index else "off", error=None)


def _open_client(settings):
    from qdrant_client import QdrantClient

    if settings.qdrant_url:
        try:
            client = QdrantClient(url=settings.qdrant_url, timeout=3, check_compatibility=False)
            client.get_collections()
            return client
        except Exception as exc:  # noqa: BLE001 - any failure means "use the local index"
            log.warning("Qdrant server at %s not reachable (%s); using the local index instead",
                        settings.qdrant_url, type(exc).__name__)
    path = settings.vector_index_path or settings.database_path.parent / "vector_index"
    path.mkdir(parents=True, exist_ok=True)
    return QdrantClient(path=str(path))


def model_dir(settings) -> Path:
    return Path(settings.model_cache_path) if settings.model_cache_path else MODELS_DIR


def load_model(settings) -> FastEmbedder:
    """Load the embedding model, downloading it first if it isn't in the models folder yet."""
    return FastEmbedder(settings.embedding_model, model_dir(settings))


def build(settings, embedder: Embedder | None = None, client=None) -> SemanticIndex:
    if embedder is None:
        embedder = load_model(settings)
    return SemanticIndex(embedder, client if client is not None else _open_client(settings))


def load_now(settings, in_memory: bool = True) -> SemanticIndex:
    """Load the model and install an index right away, in this thread (the eval runner and the
    real-model tests). In memory by default, so it never touches the live bot's index."""
    client = None
    if in_memory:
        from qdrant_client import QdrantClient

        client = QdrantClient(":memory:")
    index = build(settings, client=client)
    set_index(index)
    return index


def start(settings, on_ready=None) -> threading.Thread | None:
    """Load the model and open the index in a background thread (once per process)."""
    with _start_lock:
        if not settings.semantic_search or _state["status"] in ("loading", "ready"):
            return None
        _state.update(status="loading", error=None)

    def run():
        global _index
        try:
            index = build(settings)
            if on_ready:
                on_ready(index)
            _index = index
            _state.update(status="ready", error=None)
            log.info("semantic search ready (%s)", settings.embedding_model)
        except Exception as exc:  # noqa: BLE001 - search falls back to keywords
            _state.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:300])
            log.warning("semantic search unavailable, using keyword search only: %s", _state["error"])

    t = threading.Thread(target=run, name="semantic-index", daemon=True)
    t.start()
    return t
