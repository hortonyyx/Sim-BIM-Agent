import sqlite3
from datetime import datetime

from src.database.datatools._share import TIMESTAMP, UNSET, _UnsetType
from src.database.datatools.datadescription import (
    update_description_schedule_type_limits,
)


def _validate_limits_and_coords(
    latitude: float,
    longitude: float,
    lower_limit_value: float,
    upper_limit_value: float,
) -> None:
    if not -90 <= latitude <= 90:
        raise ValueError(f"latitude must be between -90 and 90, got {latitude}")
    if not -180 <= longitude <= 180:
        raise ValueError(f"longitude must be between -180 and 180, got {longitude}")
    if lower_limit_value > upper_limit_value:
        raise ValueError(
            f"lower_limit_value ({lower_limit_value}) must be "
            f"<= upper_limit_value ({upper_limit_value})"
        )


def create_schedule_type_limits(
    db_path: str,
    name: str,
    latitude: float,
    longitude: float,
    architecture_type: str,
    lower_limit_value: float,
    upper_limit_value: float,
    numeric_type: str,
    unit_type: str,
) -> None:
    _validate_limits_and_coords(
        latitude, longitude, lower_limit_value, upper_limit_value
    )
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        sql = "INSERT INTO schedule_type_limits (name, latitude, longitude, architecture_type, lower_limit_value, upper_limit_value, numeric_type, unit_type, datetime) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        des_data = [
            name,
            latitude,
            longitude,
            architecture_type,
            lower_limit_value,
            upper_limit_value,
            numeric_type,
            unit_type,
        ]
        timestamp_int = int(datetime.now().strftime(TIMESTAMP))

        cursor.execute(sql, [*des_data, timestamp_int])
        new_id = cursor.lastrowid
        if new_id is None:
            raise RuntimeError("Failed to insert schedule type limits record")
        des_data.insert(0, new_id)
        update_description_schedule_type_limits(des_data, cur=cursor)
        conn.commit()


def update_schedule_type_limits(
    db_path: str,
    schedule_type_limits_id: int,
    name: str | _UnsetType = UNSET,
    latitude: float | _UnsetType = UNSET,
    longitude: float | _UnsetType = UNSET,
    architecture_type: str | _UnsetType = UNSET,
    lower_limit_value: float | _UnsetType = UNSET,
    upper_limit_value: float | _UnsetType = UNSET,
    numeric_type: str | _UnsetType = UNSET,
    unit_type: str | _UnsetType = UNSET,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM schedule_type_limits WHERE id = ?",
            (schedule_type_limits_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise ValueError(
                f"Schedule Type Limits with id {schedule_type_limits_id} does not exist."
            )

        updated_name = name if name is not UNSET else row["name"]
        if isinstance(latitude, _UnsetType):
            updated_latitude = float(row["latitude"])
        else:
            updated_latitude = float(latitude)
        if isinstance(longitude, _UnsetType):
            updated_longitude = float(row["longitude"])
        else:
            updated_longitude = float(longitude)
        updated_architecture_type = (
            architecture_type
            if architecture_type is not UNSET
            else row["architecture_type"]
        )
        if isinstance(lower_limit_value, _UnsetType):
            updated_lower_limit_value = float(row["lower_limit_value"])
        else:
            updated_lower_limit_value = float(lower_limit_value)
        if isinstance(upper_limit_value, _UnsetType):
            updated_upper_limit_value = float(row["upper_limit_value"])
        else:
            updated_upper_limit_value = float(upper_limit_value)
        updated_numeric_type = (
            numeric_type if numeric_type is not UNSET else row["numeric_type"]
        )
        updated_unit_type = unit_type if unit_type is not UNSET else row["unit_type"]

        _validate_limits_and_coords(
            updated_latitude,
            updated_longitude,
            updated_lower_limit_value,
            updated_upper_limit_value,
        )

        sql = """
            UPDATE schedule_type_limits
            SET name = ?, latitude = ?, longitude = ?, architecture_type = ?,
                lower_limit_value = ?, upper_limit_value = ?, numeric_type = ?, unit_type = ?, datetime = ?
            WHERE id = ?
        """
        timestamp_int = int(datetime.now().strftime(TIMESTAMP))
        values = [
            updated_name,
            updated_latitude,
            updated_longitude,
            updated_architecture_type,
            updated_lower_limit_value,
            updated_upper_limit_value,
            updated_numeric_type,
            updated_unit_type,
            timestamp_int,
            schedule_type_limits_id,
        ]
        cursor.execute(sql, values)

        des_data = [
            schedule_type_limits_id,
            updated_name,
            updated_latitude,
            updated_longitude,
            updated_architecture_type,
            updated_lower_limit_value,
            updated_upper_limit_value,
            updated_numeric_type,
            updated_unit_type,
        ]
        update_description_schedule_type_limits(des_data, cur=cursor)
        conn.commit()


def delete_scheduletypelimits(db_path: str, scheduletypelimits_id: int) -> None:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM schedule_type_limits WHERE id = ?", (scheduletypelimits_id,)
        )
        conn.commit()


def list_schedule_type_limits(db_path: str) -> list[tuple]:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM schedule_type_limits ORDER BY id")
        return cursor.fetchall()
