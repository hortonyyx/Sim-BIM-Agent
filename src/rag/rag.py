import asyncio
import sqlite3
import time
from collections.abc import Generator
from dataclasses import dataclass, field
from itertools import batched

from tqdm import tqdm
from tqdm.asyncio import tqdm_asyncio

from src.rag.chunk import Chunk, SQLiteProcessor, compute_chunk_id
from src.rag.embedding import GeminiEmbeddingModel, GeminiTaskType
from src.rag.vector import AsyncQdrantVectorStore, QdrantData, QdrantVectorStore
from src.utils.logging import get_logger


class AsyncRateLimiter:
    """Token-bucket style rate limiter for async contexts."""

    def __init__(self, max_per_second: float):
        self._interval = 1.0 / max_per_second
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = asyncio.get_event_loop().time()
            wait = self._last + self._interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = asyncio.get_event_loop().time()


@dataclass
class RowRecord:
    table_name: str
    record_id: int
    data_datetime: int

    def __hash__(self) -> int:
        return hash((self.table_name, self.record_id, self.data_datetime))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RowRecord):
            return False
        return (self.table_name, self.record_id, self.data_datetime) == (
            other.table_name,
            other.record_id,
            other.data_datetime,
        )


@dataclass
class VectorizedResult:
    success_count: int = 0
    failed_count: int = 0
    errors: list[Exception] = field(default_factory=list)
    deleted_ids: int = 0
    deleted_failed: bool = False
    delete_error: Exception | None = None


class RAGSystem:
    def __init__(
        self,
        qdrant_url: str,
        qdrant_api_key: str,
        qdrant_collection_name: str,
        gemini_api_key: str,
        index_db_path: str = "data/database/EP_Agent_data.db",
    ):
        self.sqlite_processor = SQLiteProcessor(db_path=index_db_path)
        self.vector_store = QdrantVectorStore(
            url=qdrant_url,
            api_key=qdrant_api_key,
            collection_name=qdrant_collection_name,
        )
        self.async_vector_store = AsyncQdrantVectorStore(
            url=qdrant_url,
            api_key=qdrant_api_key,
            collection_name=qdrant_collection_name,
            dimension=3072,
        )
        self.embedding_model = GeminiEmbeddingModel(api_key=gemini_api_key)
        self.logger = get_logger(__name__)

    def search(
        self,
        query: str,
        top_k: int = 5,
        chunk_type: str | None = None,
        score_threshold: float | None = 0.5,
    ) -> list[QdrantData]:
        embeddings = self.embedding_model.embed_batch(
            [query], task_type=GeminiTaskType.RETRIEVAL_QUERY
        )[0]
        results = self.vector_store.search(
            embeddings, top_k, chunk_type, score_threshold
        )
        return results

    def build_context(
        self,
        query: str,
        top_k: int = 5,
        chunk_type: str | None = None,
        score_threshold: float | None = 0.5,
    ) -> str:
        results = self.search(query, top_k, chunk_type, score_threshold)

        if not results:
            return ""

        context_parts = []
        for i, result in enumerate(results):
            content = result.description
            table = result.table_name
            record_id = result.record_id
            score = result.score if result.score is not None else 0.0

            context_parts.append(
                f"[Document {i + 1}] (Table: {table}, RecordID: {record_id}, Score: {score:.2f})\nDescription: {content}\n---\n"
            )

        return "\n".join(context_parts)

    def embed(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        return self.embedding_model.embed_batch(texts)

    def chunk(
        self,
        table_name: str,
        record_id: int,
    ) -> Chunk:
        result = self.sqlite_processor.process_data(
            table_name=table_name, record_id=record_id
        )
        if result is None:
            self.logger.error("Failed to chunk {}-{}.", table_name, record_id)
            raise ValueError(f"Failed to chunk {table_name}-{record_id}.")
        return result

    def _get_all_chunks_table_id(self) -> Generator[RowRecord, None, None]:
        for point in self.vector_store.get_all_points():
            yield RowRecord(
                table_name=point.table_name,
                record_id=int(point.record_id),
                data_datetime=int(point.datetime),
            )

    def _get_chunkable_tables(
        self, cursor: sqlite3.Cursor
    ) -> Generator[sqlite3.Row, None, None]:
        """Return table names that have id, datetime, and description columns."""
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
        )
        for table in tqdm(cursor.fetchall(), desc="Getting chunkable tables"):
            cursor.execute(f"PRAGMA table_info([{table['name']}])")
            columns = {row["name"] for row in cursor.fetchall()}
            if {"id", "datetime", "description"}.issubset(columns):
                yield table

    def _get_all_sql(self) -> Generator[RowRecord, None, None]:
        with sqlite3.connect(self.sqlite_processor.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            for table in self._get_chunkable_tables(cursor):
                cursor.execute(f"SELECT id, datetime FROM [{table['name']}]")
                for row in tqdm(
                    cursor.fetchall(), desc=f"Getting SQL records from {table['name']}"
                ):
                    yield RowRecord(table["name"], int(row["id"]), int(row["datetime"]))

    def check_rag_sync(self) -> tuple[set[RowRecord], set[RowRecord]]:
        """Compare SQL and Qdrant to find out-of-sync records.

        Returns:
            (unsync_data, stale_data):
                unsync_data — records in SQL but not in Qdrant (need embedding).
                stale_data  — records in Qdrant but not in SQL (need deletion).
        """
        vector_points = self._get_all_chunks_table_id()
        all_sql_records = self._get_all_sql()

        vector_set = set(vector_points)
        sql_set = set(all_sql_records)

        local_data = sql_set - vector_set
        cloud_data = vector_set - sql_set

        return local_data, cloud_data

    def _collect_chunks(self, records: set[RowRecord]) -> Generator[Chunk, None, None]:
        for record in tqdm(records, desc="Collecting chunks"):
            table = record.table_name
            record_id = record.record_id
            try:
                yield self.chunk(table, record_id)
            except ValueError:
                self.logger.error("Skipping {}-{}: chunk not found", table, record_id)
                continue

    def _embed_and_upsert(
        self, chunks: Generator[Chunk, None, None], batch_count: int
    ) -> VectorizedResult:
        if batch_count <= 0:
            raise ValueError("batch_count must be greater than 0")
        result = VectorizedResult()
        for i, batch in tqdm(
            enumerate(batched(chunks, batch_count)), desc="Embedding", unit="batch"
        ):
            try:
                descriptions = [chunk.description for chunk in batch]
                embeddings = self.embed(descriptions)
                self.vector_store.add(batch, embeddings, batch_count)
                result.success_count += 1
            except Exception as e:
                result.failed_count += 1
                result.errors.append(e)
                self.logger.warning("Failed to process batch {}", i // batch_count)
                time.sleep(1)
                continue
        return result

    async def _embed_and_upsert_async(
        self,
        chunks: Generator[Chunk, None, None],
        batch_count: int,
        max_concurrency: int = 5,
        max_requests_per_minute: int = 2700,
        max_retries: int = 3,
    ) -> VectorizedResult:
        if batch_count <= 0:
            raise ValueError("batch_count must be greater than 0")

        result = VectorizedResult()
        semaphore = asyncio.Semaphore(max_concurrency)
        limiter = AsyncRateLimiter(max_per_second=max_requests_per_minute / 60.0)

        async def process_batch(batch: tuple[Chunk, ...], i: int) -> None:
            async with semaphore:
                for attempt in range(max_retries):
                    await limiter.acquire()
                    try:
                        descriptions = [chunk.description for chunk in batch]
                        embeddings = await self.embedding_model.embed_batch_async(
                            descriptions
                        )
                        await self.async_vector_store.add(
                            batch, embeddings, batch_count
                        )
                        result.success_count += 1
                        return
                    except Exception as e:
                        is_rate_limited = "429" in str(
                            e
                        ) or "RESOURCE_EXHAUSTED" in str(e)
                        if is_rate_limited and attempt < max_retries - 1:
                            wait = 60 * (attempt + 1)
                            self.logger.warning(
                                "Rate limited on batch {}, retry {}/{} in {}s",
                                i,
                                attempt + 1,
                                max_retries,
                                wait,
                            )
                            await asyncio.sleep(wait)
                        else:
                            result.failed_count += 1
                            result.errors.append(e)
                            self.logger.warning("Failed to process batch {}: {}", i, e)
                            return

        tasks = [
            asyncio.create_task(process_batch(batch, i))
            for i, batch in enumerate(batched(chunks, batch_count))
        ]

        await tqdm_asyncio.gather(*tasks, desc="Embedding", unit="batch")

        return result

    def _delete_stale_points(
        self, cloud_data: set[RowRecord]
    ) -> tuple[int, bool, Exception | None]:
        """Delete Qdrant points whose records no longer exist in SQLite.

        Returns (deleted_count, failed, error).
        """
        if not cloud_data:
            return 0, False, None
        stale_ids = [compute_chunk_id(r.table_name, r.record_id) for r in cloud_data]
        try:
            self.vector_store.delete(stale_ids)
            self.logger.info("Deleted {} stale vectors from Qdrant.", len(stale_ids))
            return len(stale_ids), False, None
        except Exception as e:
            self.logger.error("Failed to delete stale vectors: {}", e)
            return 0, True, e

    async def _delete_stale_points_async(
        self, cloud_data: set[RowRecord]
    ) -> tuple[int, bool, Exception | None]:
        if not cloud_data:
            return 0, False, None
        stale_ids = [compute_chunk_id(r.table_name, r.record_id) for r in cloud_data]
        try:
            await self.async_vector_store.delete(stale_ids)
            self.logger.info("Deleted {} stale vectors from Qdrant.", len(stale_ids))
            return len(stale_ids), False, None
        except Exception as e:
            self.logger.error("Failed to delete stale vectors: {}", e)
            return 0, True, e

    def sync_rag(
        self,
        batch_count: int = 100,
    ) -> VectorizedResult:
        """Sync SQL records to Qdrant. Returns number of failed batches."""
        local_data, cloud_data = self.check_rag_sync()
        self.logger.info(
            "Found {} local records to vectorize, "
            "{} cloud records not in local database.",
            len(local_data),
            len(cloud_data),
        )

        deleted_ids, del_failed, del_error = self._delete_stale_points(cloud_data)

        chunks = self._collect_chunks(local_data)
        result = self._embed_and_upsert(chunks, batch_count)
        result.deleted_ids = deleted_ids
        result.deleted_failed = del_failed
        result.delete_error = del_error
        return result

    async def sync_rag_async(
        self,
        batch_count: int = 100,
        max_concurrency: int = 5,
    ) -> VectorizedResult:
        local_data, cloud_data = self.check_rag_sync()
        self.logger.info(
            "Found {} local records to vectorize, "
            "{} cloud records not in local database.",
            len(local_data),
            len(cloud_data),
        )

        deleted_ids, del_failed, del_error = await self._delete_stale_points_async(
            cloud_data
        )

        chunks = self._collect_chunks(local_data)
        result = await self._embed_and_upsert_async(
            chunks, batch_count, max_concurrency
        )
        result.deleted_ids = deleted_ids
        result.deleted_failed = del_failed
        result.delete_error = del_error
        return result
