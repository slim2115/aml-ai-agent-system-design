"""Immutable, local retrieval snapshots. Ranked chunks are candidate evidence only.

The small demonstration corpus is indexed in memory, avoiding a mutable external
collection and stale records. A factory publishes an index only after all corpus,
chunk and vector checks succeed. Source dates use UTC midnight explicitly; this
is a demonstration convention, not an approved legal applicability policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Annotated, Any, Callable, Literal

from pydantic import Field, TypeAdapter, model_validator

from src.config import Settings, get_settings, resolve_project_path
from src.contracts.v1 import ApplicableRule, Identifier, RuleCitation, StrictModel, Timestamp, instant
from src.retrieval.embedding import build_embedding_function


_QUERY_TEXT = {
    "structuring": "Признаки разбиения суммы на несколько операций",
    "threshold": "Порог суммы операции обязательного контроля",
    "threshold_breach": "Порог суммы операции обязательного контроля",
    "kyc": "Идентификация клиента и обновление сведений",
    "kyc_mismatch": "Идентификация клиента и обновление сведений",
    "high_risk": "Юрисдикции с повышенным уровнем риска",
    "high_risk_country": "Юрисдикции с повышенным уровнем риска",
    "reporting": "Порядок представления сведений об операциях",
    "velocity": "Частота операций клиента за период",
}
_MAX_CHUNKS = 10000
_MAX_CORPUS_BYTES = 5 * 1024 * 1024
_TIMESTAMP = TypeAdapter(Timestamp)


class RetrievalFailure(RuntimeError):
    """A safe stable code; no source text, path or provider error is exposed."""

    def __init__(self, code: str) -> None:
        allowed = {"INVALID_CORPUS", "AMBIGUOUS_RULE", "INVALID_QUERY", "EMBEDDING_NOT_READY", "INVALID_AS_OF"}
        self.code = code if code in allowed else "INVALID_CORPUS"
        super().__init__(self.code)


class _RuleEdition(StrictModel):
    rule_id: Identifier
    title: Annotated[str, Field(min_length=1, max_length=4000, pattern=r"\S")]
    regulation_ref: Annotated[str, Field(min_length=1, max_length=4000, pattern=r"\S")]
    category: Literal["structuring", "threshold", "kyc", "high_risk", "reporting"]
    text: Annotated[str, Field(min_length=1, max_length=100000, pattern=r"\S")]
    effective_from: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", min_length=10, max_length=10)]
    effective_until: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", min_length=10, max_length=10)] | None = None
    version: Identifier

    @model_validator(mode="after")
    def dates(self):
        start = date.fromisoformat(self.effective_from)
        if self.effective_until is not None and date.fromisoformat(self.effective_until) <= start:
            raise ValueError("invalid edition interval")
        return self


@dataclass(frozen=True)
class IndexedChunk:
    citation: RuleCitation
    text: str
    category: str
    char_start: int
    char_end: int
    token_count: int
    source_text_sha256: str
    effective_from: str
    effective_until: str | None


@dataclass(frozen=True)
class RetrievedChunk:
    citation: RuleCitation
    text: str
    distance: float
    category: str
    char_start: int
    char_end: int
    token_count: int

    def as_applicable(self) -> ApplicableRule:
        """Convert wire shape only; the caller must separately approve applicability."""
        return ApplicableRule(citation=self.citation, rule_text=self.text)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _as_of(value: str) -> datetime:
    try:
        return instant(_TIMESTAMP.validate_python(value)).astimezone(timezone.utc)
    except Exception:
        raise RetrievalFailure("INVALID_AS_OF") from None


def _active(chunk: IndexedChunk, as_of: datetime) -> bool:
    # Explicit UTC calendar date, independent of the workstation timezone.
    calendar = as_of.date().isoformat()
    return chunk.effective_from <= calendar and (chunk.effective_until is None or calendar < chunk.effective_until)


def _validate_editions(records: list[dict]) -> tuple[_RuleEdition, ...]:
    if type(records) is not list or not records or len(records) > 1000:
        raise RetrievalFailure("INVALID_CORPUS")
    try:
        editions = tuple(sorted((_RuleEdition.model_validate(record) for record in records),
                                key=lambda item: (item.rule_id, item.effective_from, item.version)))
    except Exception:
        raise RetrievalFailure("INVALID_CORPUS") from None
    keys = [(edition.rule_id, edition.version) for edition in editions]
    if len(keys) != len(set(keys)):
        raise RetrievalFailure("INVALID_CORPUS")
    previous = None
    for edition in editions:
        if previous and previous.rule_id == edition.rule_id:
            if previous.effective_until is None or previous.effective_until > edition.effective_from:
                raise RetrievalFailure("AMBIGUOUS_RULE")
        previous = edition
    return editions


def _token_ids(tokenizer: Any, text: str, *, offsets: bool = False) -> dict:
    encoded = tokenizer(text, add_special_tokens=False, truncation=False, return_offsets_mapping=offsets)
    ids = encoded["input_ids"]
    if not isinstance(ids, list) or not ids or any(type(item) is not int for item in ids):
        raise ValueError("unsupported tokenizer result")
    return encoded


def _token_count(tokenizer: Any, text: str) -> int:
    encoded = tokenizer(text, add_special_tokens=True, truncation=False)
    ids = encoded["input_ids"]
    if not isinstance(ids, list) or any(type(item) is not int for item in ids):
        raise ValueError("unsupported tokenizer result")
    return len(ids)


def _chunks(edition: _RuleEdition, tokenizer: Any, max_seq: int, overlap: int) -> tuple[IndexedChunk, ...]:
    encoded = _token_ids(tokenizer, edition.text, offsets=True)
    offsets = encoded.get("offset_mapping")
    if not isinstance(offsets, list) or len(offsets) != len(encoded["input_ids"]):
        raise ValueError("fast tokenizer offsets required")
    previous_start = -1
    for pair in offsets:
        if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                or any(type(value) is not int for value in pair)
                or not 0 <= pair[0] < pair[1] <= len(edition.text) or pair[0] < previous_start):
            raise ValueError("invalid tokenizer offsets")
        previous_start = pair[0]
    special = tokenizer.num_special_tokens_to_add(pair=False)
    if type(special) is not int or not 0 <= special < max_seq:
        raise ValueError("invalid special token count")
    budget = max_seq - special
    chunks = []
    start = 0
    while start < len(offsets):
        end = min(len(offsets), start + budget)
        char_start = 0 if start == 0 else offsets[start][0]
        while end > start:
            char_end = len(edition.text) if end == len(offsets) else offsets[end][0]
            text = edition.text[char_start:char_end]
            count = _token_count(tokenizer, text)
            if text.strip() and len(text) <= 4000 and count <= max_seq:
                break
            end -= 1
        if end <= start:
            raise ValueError("cannot represent token-safe chunk")
        identity = {
            "rule_id": edition.rule_id, "version": edition.version,
            "source_ref": edition.regulation_ref, "char_start": char_start,
            "char_end": char_end, "text_sha256": _digest(text.encode("utf-8")),
        }
        chunks.append(IndexedChunk(
            citation=RuleCitation(rule_id=edition.rule_id, rule_version=edition.version,
                                  source_ref=edition.regulation_ref,
                                  chunk_id="chunk-" + _digest(_canonical(identity)),
                                  content_sha256=identity["text_sha256"]),
            text=text, category=edition.category, char_start=char_start, char_end=char_end,
            token_count=count, source_text_sha256=_digest(edition.text.encode("utf-8")),
            effective_from=edition.effective_from, effective_until=edition.effective_until,
        ))
        if end == len(offsets):
            break
        start = max(start + 1, end - overlap)
    return tuple(chunks)


def _vectors(value: Any, count: int, dimension: int | None = None) -> tuple[tuple[float, ...], ...]:
    if not isinstance(value, list) or len(value) != count:
        raise ValueError("invalid embedding count")
    result = []
    for row in value:
        if (not isinstance(row, list) or not row or any(type(item) not in (int, float) or not math.isfinite(item) for item in row)):
            raise ValueError("invalid embedding vector")
        if dimension is None:
            dimension = len(row)
        if len(row) != dimension:
            raise ValueError("inconsistent vector dimensions")
        norm = math.sqrt(math.fsum(float(item) * float(item) for item in row))
        if not math.isfinite(norm) or norm == 0:
            raise ValueError("invalid embedding norm")
        result.append(tuple(float(item) / norm for item in row))
    return tuple(result)


@dataclass(frozen=True)
class RuleIndex:
    fingerprint: str
    chunks: tuple[IndexedChunk, ...]
    _vectors: tuple[tuple[float, ...], ...]
    _embedding: Any
    _max_seq: int
    _provenance_json: str

    @classmethod
    def from_file(cls, path: Path | str, *, settings: Settings | None = None,
                  embedding_factory: Callable[[Settings], Any] | None = None) -> "RuleIndex":
        selected = settings or get_settings()
        try:
            source = Path(resolve_project_path(path, root=selected.project_root, key="RULE_CORPUS_PATH"))
            if not source.is_file() or source.stat().st_size > _MAX_CORPUS_BYTES:
                raise ValueError("invalid corpus file")
            raw = source.read_bytes()
            records = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_no_duplicate_keys)
            _validate_editions(records)  # Fail bad data before loading a model.
        except RetrievalFailure:
            raise
        except Exception:
            raise RetrievalFailure("INVALID_CORPUS") from None
        try:
            manifest = Path(resolve_project_path(selected.llm.embedding_manifest_path,
                            root=selected.project_root, key="EMBEDDING_MANIFEST_PATH"))
            before = manifest.read_bytes()
            embedding = (embedding_factory or build_embedding_function)(selected)
            if manifest.read_bytes() != before:
                raise ValueError("manifest changed while loading")
        except Exception:
            raise RetrievalFailure("EMBEDDING_NOT_READY") from None
        return cls.from_records(records, embedding=embedding, embedding_manifest_digest=_digest(before),
                                source_sha256=_digest(raw))

    @classmethod
    def from_records(cls, records: list[dict], *, embedding: Any, embedding_manifest_digest: str,
                     source_sha256: str | None = None) -> "RuleIndex":
        editions = _validate_editions(records)
        if (not isinstance(embedding_manifest_digest, str) or re.fullmatch(r"[0-9a-f]{64}", embedding_manifest_digest) is None
                or (source_sha256 is not None and (not isinstance(source_sha256, str)
                    or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None))):
            raise RetrievalFailure("INVALID_CORPUS")
        try:
            model = embedding._model
            tokenizer = model.tokenizer
            max_seq = model.max_seq_length
            if type(max_seq) is not int or not 4 <= max_seq <= 65536:
                raise ValueError("invalid embedding input limit")
            special = tokenizer.num_special_tokens_to_add(pair=False)
            if type(special) is not int or not 0 <= special < max_seq:
                raise ValueError("invalid special tokens")
            overlap = min(16, (max_seq - special) // 4)
            recipe = {"name": "offset-windows-v1", "max_seq_length": max_seq,
                      "special_tokens": special, "overlap_tokens": overlap, "max_characters": 4000,
                      "tokenizer_class": type(tokenizer).__name__, "edition_calendar": "UTC-midnight"}
            built = []
            for edition in editions:
                built.extend(_chunks(edition, tokenizer, max_seq, overlap))
                if len(built) > _MAX_CHUNKS:
                    raise ValueError("too many chunks")
            chunks = tuple(built)
            if len({chunk.citation.chunk_id for chunk in chunks}) != len(chunks):
                raise ValueError("duplicate chunk identity")
        except Exception:
            raise RetrievalFailure("INVALID_CORPUS") from None
        try:
            vectors = _vectors(embedding([chunk.text for chunk in chunks]), len(chunks))
        except Exception:
            raise RetrievalFailure("EMBEDDING_NOT_READY") from None
        corpus_digest = _digest(_canonical([edition.model_dump(mode="json") for edition in editions]))
        inventory_digest = _digest(_canonical([
            {"citation": chunk.citation.model_dump(mode="json"), "char_start": chunk.char_start,
             "char_end": chunk.char_end, "token_count": chunk.token_count,
             "source_text_sha256": chunk.source_text_sha256} for chunk in chunks
        ]))
        provenance = {"schema_version": 1, "corpus_sha256": corpus_digest,
                      "source_sha256": source_sha256, "embedding_manifest_sha256": embedding_manifest_digest,
                      "chunk_inventory_sha256": inventory_digest,
                      "chunk_recipe": recipe, "rule_editions": len(editions), "chunks": len(chunks),
                      "storage": "immutable-in-memory", "legal_approval": "not-established"}
        fingerprint = _digest(_canonical(provenance))
        return cls(fingerprint, chunks, vectors, embedding, max_seq,
                   json.dumps(provenance, ensure_ascii=False, sort_keys=True))

    @property
    def citation_catalog(self) -> tuple[RuleCitation, ...]:
        return tuple(chunk.citation for chunk in self.chunks)

    @property
    def provenance(self) -> dict:
        return json.loads(self._provenance_json)

    def active_chunk_count(self, as_of: str) -> int:
        selected = _as_of(as_of)
        return sum(_active(chunk, selected) for chunk in self.chunks)

    def search(self, query: str, *, as_of: str, k: int = 3) -> list[RetrievedChunk]:
        if type(query) is not str or query not in _QUERY_TEXT or type(k) is not int or not 1 <= k <= _MAX_CHUNKS:
            raise RetrievalFailure("INVALID_QUERY")
        selected = _as_of(as_of)
        active = [(chunk, vector) for chunk, vector in zip(self.chunks, self._vectors) if _active(chunk, selected)]
        if not active:
            return []
        try:
            safe_text = _QUERY_TEXT[query]
            if _token_count(self._embedding._model.tokenizer, safe_text) > self._max_seq:
                raise ValueError("query exceeds embedding input limit")
            vector = _vectors(self._embedding([safe_text]), 1, len(self._vectors[0]))[0]
            ranked = [(max(0.0, min(2.0, 1.0 - math.fsum(left * right for left, right in zip(vector, indexed)))), chunk)
                      for chunk, indexed in active]
        except Exception:
            raise RetrievalFailure("EMBEDDING_NOT_READY") from None
        ranked.sort(key=lambda pair: (pair[0], pair[1].citation.chunk_id))
        return [RetrievedChunk(citation=chunk.citation, text=chunk.text, distance=distance,
                               category=chunk.category, char_start=chunk.char_start, char_end=chunk.char_end,
                               token_count=chunk.token_count) for distance, chunk in ranked[:k]]
