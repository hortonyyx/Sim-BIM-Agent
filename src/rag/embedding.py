from abc import ABC, abstractmethod
from enum import Enum

from src.configs.config import EmbeddingConfig
from src.utils.logging import get_logger


class IEmbeddingModel(ABC):
    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        pass


class GeminiTaskType(Enum):
    SEMANTIC_SIMILARITY = "SEMANTIC_SIMILARITY"
    CLASSIFICATION = "CLASSIFICATION"
    CLUSTERING = "CLUSTERING"
    RETRIEVAL_DOCUMENT = "RETRIEVAL_DOCUMENT"
    RETRIEVAL_QUERY = "RETRIEVAL_QUERY"
    CODE_RETRIEVAL_QUERY = "CODE_RETRIEVAL_QUERY"
    QUESTION_ANSWERING = "QUESTION_ANSWERING"
    FACT_VERIFICATION = "FACT_VERIFICATION"


class GeminiEmbeddingModel(IEmbeddingModel):
    def __init__(
        self,
        api_key: str,
        config: EmbeddingConfig | None = None,
    ):
        from google.genai import Client

        self.client: Client = Client(api_key=api_key)
        self.config = config or EmbeddingConfig()
        self.logger = get_logger(__name__)

    def embed_batch(
        self,
        texts: list[str],
        model_name: str | None = None,
        dimension: int | None = None,
        task_type: GeminiTaskType | str | None = None,
    ) -> list[list[float]]:
        from google.genai import types

        model_name = model_name or self.config.model_name
        dimension = dimension or self.config.dimension
        task_type = task_type or self.config.task_type
        task_type_str: str = (
            task_type.value if isinstance(task_type, Enum) else str(task_type)
        )

        try:
            result = self.client.models.embed_content(
                model=model_name,
                contents=texts,
                config=types.EmbedContentConfig(
                    task_type=task_type_str,
                    output_dimensionality=dimension,
                ),
            )

        except Exception as e:
            self.logger.exception("Embedding API error: {}", e)
            raise ValueError(f"Embedding API call failed: {e}") from e

        if not result.embeddings:
            raise ValueError("Embedding API returned no embeddings")

        if len(result.embeddings) != len(texts):
            raise ValueError(
                f"Embedding API returned {len(result.embeddings)} embeddings for {len(texts)} texts"
            )

        vectors: list[list[float]] = []
        for idx, emb in enumerate(result.embeddings):
            if emb.values is None:
                raise ValueError(
                    f"Embedding API returned an empty embedding at index {idx}"
                )
            vectors.append(emb.values)
        return vectors

    async def embed_batch_async(
        self,
        texts: list[str],
        model_name: str | None = None,
        dimension: int | None = None,
        task_type: GeminiTaskType | str | None = None,
    ) -> list[list[float]]:
        from google.genai import types

        model_name = model_name or self.config.model_name
        dimension = dimension or self.config.dimension
        task_type = task_type or self.config.task_type
        task_type_str: str = (
            task_type.value if isinstance(task_type, Enum) else str(task_type)
        )

        try:
            result = await self.client.aio.models.embed_content(
                model=model_name,
                contents=texts,
                config=types.EmbedContentConfig(
                    task_type=task_type_str,
                    output_dimensionality=dimension,
                ),
            )

        except Exception as e:
            raise ValueError(f"Embedding API call failed: {e}") from e

        if not result.embeddings:
            raise ValueError("Embedding API returned no embeddings")

        if len(result.embeddings) != len(texts):
            raise ValueError(
                f"Embedding API returned {len(result.embeddings)} embeddings for {len(texts)} texts"
            )

        vectors: list[list[float]] = []
        for idx, emb in enumerate(result.embeddings):
            if emb.values is None:
                raise ValueError(
                    f"Embedding API returned an empty embedding at index {idx}"
                )
            vectors.append(emb.values)
        return vectors
