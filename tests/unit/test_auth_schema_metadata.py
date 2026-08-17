from app.infrastructure.database.models import AuthSessionORM, RefreshTokenORM


class TestAuthSchemaMetadata:
    def test_temporal_constraints_and_cleanup_indexes_are_declared(
        self,
    ) -> None:
        """
        Проверяем: ORM metadata защищает временные инварианты auth state.
        Успех: check constraints и partial cleanup indexes имеют стабильные имена.
        Нежелательное поведение: защита существует только в Pydantic или migration.
        """
        session_constraint_names = {
            constraint.name for constraint in AuthSessionORM.__table__.constraints
        }
        refresh_constraint_names = {
            constraint.name for constraint in RefreshTokenORM.__table__.constraints
        }
        session_indexes = {
            index.name: str(index.dialect_options["postgresql"]["where"])
            for index in AuthSessionORM.__table__.indexes
        }

        assert {
            "ck_auth_sessions_idle_expires_after_created",
            "ck_auth_sessions_revoked_after_created",
        } <= session_constraint_names
        assert "ck_refresh_tokens_used_after_created" in refresh_constraint_names
        assert (
            session_indexes["ix_auth_sessions_idle_expires_at_active"]
            == "revoked_at IS NULL"
        )
        assert (
            session_indexes["ix_auth_sessions_revoked_at_not_null"]
            == "revoked_at IS NOT NULL"
        )
