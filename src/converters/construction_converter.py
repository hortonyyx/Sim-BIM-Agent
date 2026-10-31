from typing import Any

from eppy.modeleditor import IDF

from src.converters.base_converter import BaseConverter
from src.utils.logging import get_logger
from src.validator.data_model import ConstructionSchema


class ConstructionConverter(BaseConverter):
    def __init__(self, idf: IDF):
        super().__init__(idf)
        self.logger = get_logger(__name__)

    def convert(self, data: dict[str, Any]) -> None:
        self.logger.info("Converting Construction data...")
        construction_list = data.get("Construction", [])

        for construction_data in construction_list:
            try:
                validated_construction = self.validate(construction_data)
                self._add_to_idf(validated_construction)
            except Exception:
                self.state["failed"] += 1
                self.logger.exception(
                    "Failed to convert Construction '{}'",
                    construction_data.get("Name", "N/A"),
                )
                continue

    def _add_to_idf(self, val_data: ConstructionSchema) -> None:
        if self.idf.getobject("CONSTRUCTION", val_data.name):
            self.logger.warning(
                "Construction with name '{}' already exists. Skipping addition.",
                val_data.name,
            )
            self.state["skipped"] += 1
            return

        try:
            self.logger.debug("Adding Construction '{}' to IDF.", val_data.name)

            for layer_name in val_data.layers:
                if not (
                    self.idf.getobject("MATERIAL", layer_name)
                    or self.idf.getobject("MATERIAL:NOMASS", layer_name)
                    or self.idf.getobject("MATERIAL:AIRGAP", layer_name)
                    or self.idf.getobject(
                        "WINDOWMATERIAL:SIMPLEGLAZINGSYSTEM", layer_name
                    )
                ):
                    raise ValueError(
                        f"Material '{layer_name}' referenced in Construction '{val_data.name}' "
                        f"does not exist in IDF. Please add the material first."
                    )

            construction_obj = self.idf.newidfobject("CONSTRUCTION", Name=val_data.name)

            for i, layer_name in enumerate(val_data.layers):
                field_name = "Outside_Layer" if i == 0 else f"Layer_{i + 1}"

                setattr(construction_obj, field_name, layer_name)
                self.logger.debug(
                    "  - Set {} to '{}' for '{}'.",
                    field_name,
                    layer_name,
                    val_data.name,
                )
            self.state["success"] += 1
            self.logger.success(
                "Construction '{}' with {} layers added successfully.",
                val_data.name,
                len(val_data.layers),
            )

        except ValueError:
            self.state["failed"] += 1
            self.logger.exception("Failed to add Construction '{}'", val_data.name)

        except AttributeError:
            self.state["failed"] += 1
            self.logger.exception(
                "Error adding Construction '{}'. A specified field name was not found.",
                val_data.name,
            )
        except Exception:
            self.state["failed"] += 1
            self.logger.exception(
                "An unexpected error occurred while adding Construction '{}'",
                val_data.name,
            )

    def validate(self, data: dict) -> ConstructionSchema:
        return ConstructionSchema.model_validate(data)
