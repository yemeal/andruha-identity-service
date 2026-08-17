from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelCaseBase(BaseModel):
    """Базовая схема для всех DTO. Конвертирует snake_case в camelCase."""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
    )


class CamelCaseOrmBase(CamelCaseBase):
    """Базовая схема для Response-моделей (которые читают из SQLAlchemy)"""

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        alias_generator=to_camel,
    )
