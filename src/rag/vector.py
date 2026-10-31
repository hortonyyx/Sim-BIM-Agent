from abc import ABC, abstractmethod
from typing import Any
from uuid import UUID

from attr import dataclass

from src.rag.chunk import Chunk
from src.utils.logging import get_logger

_PAYLOAD_CORE_KEYS = {
    "description",
    "table_name",
    "record_id",
    "data_dict",
    "datetime",
}


@dataclass
class QdrantData:
    id: str | int | UUID | None
    description: str
    table_name: str
    record_id: int
    datetime: int
    full_data: dict
    metadata: dict
    score: float | None


def _extract_payload(
    payload: dict[str, Any],
    *,
    point_id: Any = None,
    score: float | None = None,
) -> QdrantData:
    return QdrantData(
        id=point_id,
        description=payload.get("description", ""),
        table_name=payload.get("table_name", ""),
        record_id=payload.get("record_id", ""),
        datetime=payload.get("datetime", 0),
        full_data=payload.get("data_dict", {}),
        metadata={k: v for k, v in payload.items() if k not in _PAYLOAD_CORE_KEYS},
        score=score,
    )


class IVectorStore(ABC):
    @abstractmethod
    def add(
        self, chunks: list[Chunk], embeddings: list[list[float]], batch_size: int
    ) -> None:
        pass

    @abstractmethod
    def delete(self, ids: list[str]) -> None:
        pass

    @abstractmethod
    def search(
        self,
        query: list[float],
        top_k: int,
        table_name: str | None = None,
        score_threshold: float | None = None,
    ) -> list[QdrantData]:
        pass

    @abstractmethod
    def get_all_points(self) -> list[QdrantData]:
        pass


class IAsyncVectorStore(ABC):
    @abstractmethod
    async def add(
        self,
        chunks: tuple[Chunk, ...] | list[Chunk],
        embeddings: tuple[list[float], ...] | list[list[float]],
        batch_size: int,
    ) -> None:
        pass

    @abstractmethod
    async def delete(self, ids: list[str]) -> None:
        pass

    @abstractmethod
    async def search(
        self,
        query: list[float],
        top_k: int,
        table_name: str | None = None,
        score_threshold: float | None = None,
    ) -> list[QdrantData]:
        pass

    @abstractmethod
    async def get_all_points(self) -> list[QdrantData]:
        pass


class AsyncQdrantVectorStore(IAsyncVectorStore):
    def __init__(
        self,
        url: str,
        api_key: str,
        collection_name: str,
        dimension: int,
        prefer_grpc: bool = True,
    ):
        import asyncio

        from qdrant_client import AsyncQdrantClient

        self.client = AsyncQdrantClient(
            url=url,
            api_key=api_key,
            prefer_grpc=prefer_grpc,
        )
        self.collection_name = collection_name
        self.dimension = dimension
        self._collection_ready = False
        self._init_lock = asyncio.Lock()
        self.logger = get_logger(__name__)

    async def _ensure_collection(self) -> None:
        if self._collection_ready:
            return
        async with self._init_lock:
            if self._collection_ready:
                return
            await self._create_collection()
            self._collection_ready = True

    async def _create_collection(self) -> None:
        from qdrant_client.models import Distance, VectorParams

        if not await self.client.collection_exists(self.collection_name):
            await self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(
                    size=self.dimension,
                    distance=Distance.COSINE,
                ),
            )
        else:
            info = await self.client.get_collection(self.collection_name)
            existing_size = info.config.params.vectors.size  # type: ignore
            if existing_size != self.dimension:
                raise ValueError(
                    f"Collection '{self.collection_name}' exists with dimension {existing_size}, "
                    f"but expected {self.dimension}."
                )
            self.logger.info(
                "Collection {} already exists (dimension={})",
                self.collection_name,
                existing_size,
            )

    async def add(
        self,
        chunks: tuple[Chunk, ...] | list[Chunk],
        embeddings: tuple[list[float], ...] | list[list[float]],
        batch_size: int,
    ) -> None:
        from qdrant_client.models import PointStruct

        await self._ensure_collection()

        points = [
            PointStruct(
                id=chunk.chunk_id,
                vector=embedding,
                payload=chunk.to_qdrant_payload(),
            )
            for chunk, embedding in zip(chunks, embeddings, strict=True)
        ]

        for i in range(0, len(points), batch_size):
            await self.client.upsert(
                collection_name=self.collection_name,
                points=points[i : i + batch_size],
            )

        self.logger.info("Added {} chunks to {}", len(chunks), self.collection_name)

    async def delete(self, ids: list[str]) -> None:
        from qdrant_client.models import PointIdsList

        await self._ensure_collection()

        await self.client.delete(
            collection_name=self.collection_name,
            points_selector=PointIdsList(points=ids),  # type: ignore
        )
        self.logger.info("Deleted {} points from {}", len(ids), self.collection_name)

    async def search(
        self,
        query: list[float],
        top_k: int,
        table_name: str | None = None,
        score_threshold: float | None = None,
    ) -> list[QdrantData]:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        await self._ensure_collection()

        search_filter = None
        if table_name:
            search_filter = Filter(
                must=[
                    FieldCondition(
                        key="table_name",
                        match=MatchValue(value=table_name),
                    )
                ]
            )

        query_results = (
            await self.client.query_points(
                collection_name=self.collection_name,
                query=query,
                query_filter=search_filter,
                limit=top_k,
                score_threshold=score_threshold,
                with_payload=True,
            )
        ).points

        return [
            _extract_payload(point.payload or {}, point_id=point.id, score=point.score)
            for point in query_results
        ]

    async def get_all_points(self) -> list[QdrantData]:
        await self._ensure_collection()

        all_results: list[QdrantData] = []
        offset = None

        while True:
            scroll_result, next_offset = await self.client.scroll(
                collection_name=self.collection_name,
                with_payload=True,
                with_vectors=False,
                offset=offset,
            )

            for record in scroll_result:
                all_results.append(
                    _extract_payload(record.payload or {}, point_id=record.id)
                )

            offset = next_offset
            if offset is None:
                break

        self.logger.info(
            "Retrieved total {} points from {}",
            len(all_results),
            self.collection_name,
        )
        return all_results

    async def get_zero_vector_points(self) -> list[dict]:
        await self._ensure_collection()

        zero_points: list[dict] = []
        offset = None

        self.logger.info("Scanning for zero vectors in {}...", self.collection_name)

        while True:
            scroll_result, next_offset = await self.client.scroll(
                collection_name=self.collection_name,
                with_payload=True,
                with_vectors=True,
                offset=offset,
                limit=100,
            )

            for record in scroll_result:
                if record.vector is not None and all(v == 0.0 for v in record.vector):
                    payload = record.payload or {}
                    zero_points.append(
                        {
                            "id": record.id,
                            "table_name": payload.get("table_name", ""),
                            "record_id": payload.get("record_id", ""),
                            "datetime": payload.get("datetime", ""),
                        }
                    )

            offset = next_offset
            if offset is None:
                break

        self.logger.info("Scan complete. Found {} zero vector(s).", len(zero_points))
        return zero_points


class QdrantVectorStore(IVectorStore):
    def __init__(
        self,
        url: str,
        api_key: str,
        collection_name: str,
        prefer_grpc: bool = True,
        dimension: int = 3072,
    ):
        from qdrant_client import QdrantClient

        self.client = QdrantClient(
            url=url,
            api_key=api_key,
            prefer_grpc=prefer_grpc,
        )
        self.collection_name = collection_name
        self.dimension = dimension
        self.logger = get_logger(__name__)
        self._create_collection()

    def _create_collection(self) -> None:
        from qdrant_client.models import Distance, VectorParams

        if not self.client.collection_exists(self.collection_name):
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(
                    size=self.dimension,
                    distance=Distance.COSINE,
                ),
            )
        else:
            info = self.client.get_collection(self.collection_name)
            existing_size = info.config.params.vectors.size  # type: ignore
            if existing_size != self.dimension:
                raise ValueError(
                    f"Collection '{self.collection_name}' exists with dimension {existing_size}, "
                    f"but expected {self.dimension}."
                )
            self.logger.info(
                "Collection {} already exists (dimension={})",
                self.collection_name,
                existing_size,
            )

    def add(
        self,
        chunks: list[Chunk],
        embeddings: list[list[float]],
        batch_size: int,
    ) -> None:
        from qdrant_client.models import PointStruct

        points = [
            PointStruct(
                id=chunk.chunk_id,
                vector=embedding,
                payload=chunk.to_qdrant_payload(),
            )
            for chunk, embedding in zip(chunks, embeddings, strict=True)
        ]

        for i in range(0, len(points), batch_size):
            self.client.upsert(
                collection_name=self.collection_name,
                points=points[i : i + batch_size],
            )

        self.logger.info("Added {} chunks to {}", len(chunks), self.collection_name)

    def delete(self, ids: list[str]) -> None:
        from qdrant_client.models import PointIdsList

        self.client.delete(
            collection_name=self.collection_name,
            points_selector=PointIdsList(points=ids),  # type: ignore
        )
        self.logger.info("Deleted {} points from {}", len(ids), self.collection_name)

    def search(
        self,
        query: list[float],
        top_k: int = 10,
        table_name: str | None = None,
        score_threshold: float | None = None,
    ) -> list[QdrantData]:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        search_filter = None
        if table_name:
            search_filter = Filter(
                must=[
                    FieldCondition(
                        key="table_name",
                        match=MatchValue(value=table_name),
                    )
                ]
            )

        query_results = self.client.query_points(
            collection_name=self.collection_name,
            query=query,
            query_filter=search_filter,
            limit=top_k,
            score_threshold=score_threshold,
            with_payload=True,
        ).points

        return [
            _extract_payload(point.payload or {}, point_id=point.id, score=point.score)
            for point in query_results
        ]

    def get_all_points(self) -> list[QdrantData]:
        all_results: list[QdrantData] = []
        offset = None

        while True:
            scroll_result, next_offset = self.client.scroll(
                collection_name=self.collection_name,
                with_payload=True,
                with_vectors=False,
                offset=offset,
                limit=10000,
            )

            for record in scroll_result:
                all_results.append(
                    _extract_payload(record.payload or {}, point_id=record.id)
                )

            offset = next_offset
            if offset is None:
                break

        self.logger.info(
            "Retrieved total {} points from {}",
            len(all_results),
            self.collection_name,
        )
        return all_results

    def get_zero_vector_points(self) -> list[dict]:
        zero_points: list[dict] = []
        offset = None

        self.logger.info("Scanning for zero vectors in {}...", self.collection_name)

        while True:
            scroll_result, next_offset = self.client.scroll(
                collection_name=self.collection_name,
                with_payload=True,
                with_vectors=True,
                offset=offset,
                limit=10000,
            )

            for record in scroll_result:
                if record.vector is not None and all(v == 0.0 for v in record.vector):
                    payload = record.payload or {}
                    zero_points.append(
                        {
                            "id": record.id,
                            "table_name": payload.get("table_name", ""),
                            "record_id": payload.get("record_id", ""),
                            "datetime": payload.get("datetime", ""),
                        }
                    )

            offset = next_offset
            if offset is None:
                break

        self.logger.info("Scan complete. Found {} zero vector(s).", len(zero_points))
        return zero_points
