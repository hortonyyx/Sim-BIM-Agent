import sqlite3
from datetime import datetime

from src.database.datatools._share import TIMESTAMP, UNSET, _UnsetType
from src.database.datatools.datadescription import update_description_construction

MAX_LAYERS = 20


def _fetch_ids_batch(
    cursor: sqlite3.Cursor, layer_names: list[str | None]
) -> list[int | None]:
    """Batch fetch material IDs for a list of layer names (single query)."""
    non_null = {name for name in layer_names if name is not None}
    if not non_null:
        return [None] * len(layer_names)

    placeholders = ", ".join(["?"] * len(non_null))
    cursor.execute(
        f"SELECT name, id FROM all_materials WHERE name IN ({placeholders})",
        list(non_null),
    )
    rows = cursor.fetchall()

    # Detect duplicate material names
    seen: dict[str, int] = {}
    for row in rows:
        if row["name"] in seen:
            raise ValueError(
                f"Ambiguous material name '{row['name']}': multiple entries in all_materials "
                f"(ids: {seen[row['name']]}, {row['id']}). Use a unique name or specify by id."
            )
        seen[row["name"]] = row["id"]
    name_to_id = seen

    result: list[int | None] = []
    for name in layer_names:
        if name is None:
            result.append(None)
        elif name in name_to_id:
            result.append(name_to_id[name])
        else:
            raise ValueError(f"Material '{name}' not found in all_materials.")
    return result


def create_construction(
    db_path: str,
    name: str,
    latitude: float,
    longitude: float,
    architecture_type: str,
    layers: list[str],
) -> None:
    if not layers or len(layers) > MAX_LAYERS:
        raise ValueError(
            f"layers must contain 1-{MAX_LAYERS} items, got {len(layers) if layers else 0}"
        )

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        layer_ids = _fetch_ids_batch(
            cursor, layers + [None] * (MAX_LAYERS - len(layers))
        )
        timestamp_int = int(datetime.now().strftime(TIMESTAMP))

        layer_cols = ", ".join(f"layer_{i}" for i in range(1, MAX_LAYERS + 1))
        placeholders = ", ".join(["?"] * (5 + MAX_LAYERS))
        sql = f"INSERT INTO constructions (name, latitude, longitude, architecture_type, {layer_cols}, datetime) VALUES ({placeholders})"

        des_data = [
            name,
            latitude,
            longitude,
            architecture_type,
            *layer_ids,
            timestamp_int,
        ]
        cursor.execute(sql, des_data)
        new_id = cursor.lastrowid
        des_data_for_desc = [
            new_id,
            name,
            latitude,
            longitude,
            architecture_type,
            *layer_ids,
        ]
        update_description_construction(des_data_for_desc, cur=cursor)
        conn.commit()


def update_construction(
    db_path: str,
    construction_id: int,
    name: str | _UnsetType = UNSET,
    latitude: float | _UnsetType = UNSET,
    longitude: float | _UnsetType = UNSET,
    architecture_type: str | _UnsetType = UNSET,
    layers: list[str] | _UnsetType = UNSET,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM constructions WHERE id=?", (construction_id,))
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"No construction found with id {construction_id}")

        updated_name = name if name is not UNSET else row["name"]
        updated_lat = latitude if latitude is not UNSET else row["latitude"]
        updated_lon = longitude if longitude is not UNSET else row["longitude"]
        updated_arch = (
            architecture_type
            if architecture_type is not UNSET
            else row["architecture_type"]
        )

        if not isinstance(layers, _UnsetType):
            if not layers or len(layers) > MAX_LAYERS:
                raise ValueError(
                    f"layers must contain 1-{MAX_LAYERS} items, got {len(layers)}"
                )
            padded = layers + [None] * (MAX_LAYERS - len(layers))
            updated_layer_ids = _fetch_ids_batch(cursor, padded)
        else:
            updated_layer_ids = [row[f"layer_{i}"] for i in range(1, MAX_LAYERS + 1)]

        timestamp_int = int(datetime.now().strftime(TIMESTAMP))
        layer_set = ", ".join(f"layer_{i} = ?" for i in range(1, MAX_LAYERS + 1))
        sql = f"""
            UPDATE constructions
            SET name = ?, latitude = ?, longitude = ?, architecture_type = ?,
                {layer_set}, datetime = ?
            WHERE id = ?
        """
        values = [
            updated_name,
            updated_lat,
            updated_lon,
            updated_arch,
            *updated_layer_ids,
            timestamp_int,
            construction_id,
        ]
        cursor.execute(sql, values)

        des_data = [
            construction_id,
            updated_name,
            updated_lat,
            updated_lon,
            updated_arch,
            *updated_layer_ids,
        ]
        update_description_construction(des_data, cur=cursor)
        conn.commit()


def delete_construction(db_path: str, construction_id: int) -> None:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM constructions WHERE id = ?", (construction_id,))
        conn.commit()


def list_constructions(db_path: str) -> list[tuple]:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM constructions")
        return cursor.fetchall()
