"""База знаний комплаенса и RAG-поиск (SR-07, SR-11, SR-12; ADR-0004).

Реализует retrieval-first подход: применимые правила извлекаются семантическим
поиском исключительно из утверждённой базы знаний. Генерация правил из
параметрических знаний модели запрещена (BRULE-02) — агент оперирует только
извлечёнными чанками.

Архитектура (ADR-0005, on-premise):
  - векторная БД: локальная ChromaDB (PersistentClient);
  - embedding: мультиязычная модель paraphrase-multilingual-MiniLM-L12-v2
    (sentence-transformers, локально). Обеспечивает корректную семантику
    русскоязычных регламентов. Требует: pip install sentence-transformers.

Формат чанка соответствует Data Model, раздел 4.3.

ВАЖНО: при смене embedding-модели необходимо удалить data/chroma и
переиндексировать базу — векторные пространства разных моделей несовместимы.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set

import chromadb
from chromadb.utils import embedding_functions

from src.config import get_settings
from src.schemas import Rule

#: Мультиязычная embedding-модель для корректной работы с русскоязычными
#: регламентами (on-premise, NFR-10). Требует: pip install sentence-transformers.
_RU_EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"


def _build_embedding_function():
    """Возвращает мультиязычную embedding-функцию.

    Явно сообщает, какая модель используется, и падает с понятной ошибкой,
    если sentence-transformers не установлен или модель недоступна
    (вместо тихого отката на англоязычную модель).
    """
    try:
        fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=_RU_EMBEDDING_MODEL
        )
        print(f"[knowledge_base] embedding: {_RU_EMBEDDING_MODEL} (multilingual)")
        return fn
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Не удалось загрузить мультиязычную модель '{_RU_EMBEDDING_MODEL}'. "
            "Установите зависимости: pip install sentence-transformers. "
            f"Причина: {exc}"
        ) from exc


@dataclass(frozen=True)
class RuleChunk:
    """Чанк базы знаний (Data Model, раздел 4.3).

    В PoC одно правило = один чанк (правила атомарны по статьям/пунктам).
    """
    chunk_id: str
    rule_id: str
    regulation_ref: str
    section: str
    category: str
    text: str
    effective_from: str
    version: str

    @classmethod
    def from_rule(cls, rule: Rule) -> "RuleChunk":
        """Строит чанк из правила (чанкинг по статье/пункту, Data Model раздел 4.2)."""
        return cls(
            chunk_id=f"{rule.rule_id}-chunk-001",
            rule_id=rule.rule_id,
            regulation_ref=rule.regulation_ref,
            section=rule.title,
            category=rule.category.value,
            text=rule.text,
            effective_from=rule.effective_from.isoformat(),
            version=rule.version,
        )

    def to_document(self) -> str:
        """Текст для векторизации (заголовок + раздел + тело правила)."""
        return f"{self.section}. {self.text}"

    def to_metadata(self) -> Dict[str, str]:
        """Метаданные для ChromaDB (скалярные значения для фильтрации)."""
        return {
            "rule_id": self.rule_id,
            "regulation_ref": self.regulation_ref,
            "section": self.section,
            "category": self.category,
            "effective_from": self.effective_from,
            "version": self.version,
        }


@dataclass(frozen=True)
class RetrievalResult:
    """Результат семантического поиска правила.

    distance — дистанция схожести ChromaDB (чем меньше, тем релевантнее).
    """
    rule_id: str
    regulation_ref: str
    section: str
    text: str
    distance: float


class KnowledgeBase:
    """База знаний комплаенса с RAG-поиском (SR-07).

    Инкапсулирует работу с ChromaDB: индексацию чанков, семантический поиск
    и валидацию source_ref (SR-12).
    """

    def __init__(
        self,
        rules: List[Rule],
        *,
        collection_name: Optional[str] = None,
        persist_dir: Optional[str] = None,
    ) -> None:
        settings = get_settings()
        self._collection_name = collection_name or settings.vector_store.collection_name
        self._persist_dir = persist_dir or settings.vector_store.persist_dir
        self._chunks: List[RuleChunk] = [RuleChunk.from_rule(rule) for rule in rules]
        self._known_regulation_refs: Set[str] = {rule.regulation_ref for rule in rules}
        self._known_rule_ids: Set[str] = {rule.rule_id for rule in rules}
        self._collection = None

    def _get_collection(self):
        """Создаёт/открывает коллекцию ChromaDB с мультиязычной embedding-функцией."""
        if self._collection is None:
            client = chromadb.PersistentClient(path=self._persist_dir)
            self._collection = client.get_or_create_collection(
                name=self._collection_name,
                embedding_function=_build_embedding_function(),
            )
        return self._collection

    def index(self) -> int:
        """Индексирует чанки правил в векторной БД (SR-07).

        Returns:
            int: число проиндексированных чанков.
        """
        collection = self._get_collection()
        collection.add(
            ids=[chunk.chunk_id for chunk in self._chunks],
            documents=[chunk.to_document() for chunk in self._chunks],
            metadatas=[chunk.to_metadata() for chunk in self._chunks],
        )
        return len(self._chunks)

    def search(self, query: str, k: int = 3) -> List[RetrievalResult]:
        """Семантический поиск правил по запросу (SR-07).

        Args:
            query: текстовый запрос (контекст инцидента / описание паттерна).
            k: число возвращаемых наиболее релевантных правил.

        Returns:
            Список RetrievalResult, отсортированный по релевантности
            (наименьшая дистанция — наиболее релевантное правило).
        """
        collection = self._get_collection()
        result = collection.query(query_texts=[query], n_results=k)

        retrieved: List[RetrievalResult] = []
        ids = result.get("ids", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]
        documents = result.get("documents", [[]])[0]
        distances = result.get("distances", [[]])[0]

        for idx, _id in enumerate(ids):
            meta = metadatas[idx]
            retrieved.append(
                RetrievalResult(
                    rule_id=meta["rule_id"],
                    regulation_ref=meta["regulation_ref"],
                    section=meta["section"],
                    text=documents[idx],
                    distance=float(distances[idx]),
                )
            )
        return retrieved

    def get_known_regulation_refs(self) -> Set[str]:
        """Возвращает множество существующих regulation_ref (для SR-12)."""
        return set(self._known_regulation_refs)

    def validate_source_ref(self, source_ref: str) -> bool:
        """Проверяет существование ссылки на регламент (SR-12, BRULE-02).

        Ссылка валидна, если она точно соответствует известному regulation_ref
        либо начинается с него (формат «regulation_ref / section»).

        Args:
            source_ref: ссылка из черновика отчёта.

        Returns:
            bool: True, если ссылка существует в базе знаний.
        """
        if source_ref in self._known_regulation_refs:
            return True
        return any(
            source_ref.startswith(known)
            for known in self._known_regulation_refs
        )
    
    def validate_rule_id(self, rule_id: str) -> bool:
        """Проверяет существование rule_id в базе знаний (SR-12, BRULE-02)."""
        return rule_id in self._known_rule_ids

    def get_rule_id_by_regulation_ref(self, regulation_ref: str) -> Optional[str]:
        """Возвращает rule_id правила по regulation_ref (для нормализации)."""
        for chunk in self._chunks:
            if chunk.regulation_ref == regulation_ref:
                return chunk.rule_id
        return None

    def extract_regulation_ref(self, source_ref: str) -> Optional[str]:
        """Извлекает известный regulation_ref из строки (если он в ней как подстрока).

        Защита от загрязнения формата: если LLM сгенерировала
        'R-115-002 | [115-ФЗ, ст.6, п.2', метод вернёт '115-ФЗ, ст.6, п.2'.
        """
        if not source_ref:
            return None
        for known in self._known_regulation_refs:
            if known in source_ref:
                return known
        return None