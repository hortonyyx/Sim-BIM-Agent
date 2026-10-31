import sqlite3
from datetime import datetime

from src.database.datatools._share import TIMESTAMP, UNSET, _UnsetType
from src.database.datatools.datadescription import update_description_schedule_compact

MAX_FIELDS = 200


def _validate_compact_values(compact_values: list[str]) -> list[str | None]:
    """Validate and pad compact_values to MAX_FIELDS. Raises if exceeds limit."""
    if len(compact_values) > MAX_FIELDS:
        raise ValueError(
            f"compact_values has {len(compact_values)} items, exceeding the maximum of {MAX_FIELDS}. "
            "Please reduce the number of schedule tokens."
        )
    return compact_values + [None] * (MAX_FIELDS - len(compact_values))


def create_schedule_compact(
    db_path: str,
    name: str,
    latitude: float,
    longitude: float,
    architecture_type: str,
    schedule_type_limit_name: str,
    compact_values: list[str],
) -> None:
    full_compact_values = _validate_compact_values(compact_values)

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()

        field_cols = ", ".join([f"field_{i}" for i in range(1, MAX_FIELDS + 1)])
        placeholders = ", ".join(["?"] * MAX_FIELDS)
        sql = f"""
            INSERT INTO schedule_compact (
                name, latitude, longitude, architecture_type,
                schedule_type_limit_name, {field_cols}, datetime
            ) VALUES (?, ?, ?, ?, ?, {placeholders}, ?)
        """
        des_data = [
            name,
            latitude,
            longitude,
            architecture_type,
            schedule_type_limit_name,
            *full_compact_values,
        ]
        timestamp_int = int(datetime.now().strftime(TIMESTAMP))

        cursor.execute(sql, [*des_data, timestamp_int])
        new_id = cursor.lastrowid
        des_data.insert(0, new_id)
        update_description_schedule_compact(des_data, cur=cursor)
        conn.commit()


def update_schedule_compact(
    db_path: str,
    schedule_compact_id: int,
    name: str | _UnsetType = UNSET,
    latitude: float | _UnsetType = UNSET,
    longitude: float | _UnsetType = UNSET,
    architecture_type: str | _UnsetType = UNSET,
    schedule_type_limit_name: str | _UnsetType = UNSET,
    compact_values: list[str] | _UnsetType = UNSET,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM schedule_compact WHERE id = ?", (schedule_compact_id,)
        )
        row = cursor.fetchone()
        if row is None:
            raise ValueError(
                f"Schedule Compact with ID {schedule_compact_id} not found."
            )

        sql = (
            """
            UPDATE schedule_compact
            SET name = ?, latitude = ?, longitude = ?, architecture_type = ?,
                schedule_type_limit_name = ?,
                """
            + ", ".join([f"field_{i} = ?" for i in range(1, MAX_FIELDS + 1)])
            + """,
                datetime = ?
            WHERE id = ?
        """
        )
        values = [
            name if name is not UNSET else row["name"],
            latitude if latitude is not UNSET else row["latitude"],
            longitude if longitude is not UNSET else row["longitude"],
            architecture_type
            if architecture_type is not UNSET
            else row["architecture_type"],
            schedule_type_limit_name
            if schedule_type_limit_name is not UNSET
            else row["schedule_type_limit_name"],
        ]
        if not isinstance(compact_values, _UnsetType):
            values.extend(_validate_compact_values(compact_values))
        else:
            values.extend([row[f"field_{i}"] for i in range(1, MAX_FIELDS + 1)])

        timestamp_int = int(datetime.now().strftime(TIMESTAMP))
        values.append(timestamp_int)
        values.append(schedule_compact_id)

        cursor.execute(sql, values)
        update_description_schedule_compact(
            [schedule_compact_id, *values[:-2]], cur=cursor
        )
        conn.commit()


def delete_schedulecompact(db_path: str, schedule_compact_id: int) -> None:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM schedule_compact WHERE id = ?", (schedule_compact_id,)
        )
        conn.commit()


def list_schedule_compact(db_path: str) -> list[tuple]:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM schedule_compact")
        return cursor.fetchall()
