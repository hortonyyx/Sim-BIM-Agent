import sqlite3
import uuid

from pydantic import BaseModel, ConfigDict, Field

from src.utils.logging import get_logger


def compute_chunk_id(table_name: str, record_id: int) -> str:
    """Deterministic point ID for a given table + record pair."""
    content_str = f"energyplus_database_{table_name}_{record_id}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, content_str))


class Chunk(BaseModel):
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        validate_assignment=True,
        extra="forbid",
    )
    table_name: str = Field(
        description="The name of the table the chunk is vectored from"
    )
    record_id: int = Field(description="The ID of the record in the table")
    description: str = Field(description="The description content of the chunk")
    data_dict: dict = Field(description="The full data record as a dictionary")
    datetime: int = Field(description="The datetime when the chunk was created")
    metadata: dict = Field(
        default_factory=dict,
        description="The metadata of the chunk",
    )

    @property
    def chunk_id(self) -> str:
        return compute_chunk_id(self.table_name, self.record_id)

    _RESERVED_PAYLOAD_KEYS = frozenset(
        {
            "description",
            "table_name",
            "record_id",
            "data_dict",
            "datetime",
        }
    )

    def to_qdrant_payload(self) -> dict:
        payload = {
            "description": self.description,
            "table_name": self.table_name,
            "record_id": self.record_id,
            "data_dict": self.data_dict,
            "datetime": self.datetime,
        }
        for k, v in self.metadata.items():
            if k not in self._RESERVED_PAYLOAD_KEYS:
                payload[k] = v
        return payload


class SQLiteProcessor:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.logger = get_logger(__name__)

    def process_data(
        self, table_name: str, record_id: int, content_column: str = "description"
    ) -> Chunk | None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    f"SELECT * FROM [{table_name}] WHERE id = ?", (record_id,)
                )
                result = cursor.fetchone()

                if result is None:
                    return None

                full_dict = dict(result)
                if content_column not in full_dict:
                    self.logger.error(
                        "Column '{}' not found in table {}",
                        content_column,
                        table_name,
                    )
                    return None
                for required_col in ("id", "datetime"):
                    if required_col not in full_dict:
                        self.logger.error(
                            "Required column '{}' not found in table {}",
                            required_col,
                            table_name,
                        )
                        return None
                exclude_keys = {content_column, "id", "datetime"}
                clean_data = {
                    k: v for k, v in full_dict.items() if k not in exclude_keys
                }

                return Chunk(
                    table_name=table_name,
                    record_id=record_id,
                    description=full_dict[content_column],
                    data_dict=clean_data,
                    datetime=full_dict["datetime"],
                )
        except Exception as e:
            self.logger.error("Error processing {} ID {}: {}", table_name, record_id, e)
            raise
