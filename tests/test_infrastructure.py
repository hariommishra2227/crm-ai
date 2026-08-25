from sqlalchemy import inspect
from sqlalchemy.pool import QueuePool

from app.config import Settings
from app.database import Base, create_database_engine


def test_sqlite_database_initialization(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    assert "scan_runs" in inspect(engine).get_table_names()
    columns = {column["name"] for column in inspect(engine).get_columns("scan_runs")}
    assert {"alerts_updated", "duration_seconds", "failure_message"} <= columns
    engine.dispose()


def test_postgresql_engine_uses_normal_pooling_without_connecting():
    engine = create_database_engine(
        "postgresql+psycopg://user:password@localhost:5432/crm_intelligence"
    )
    assert isinstance(engine.pool, QueuePool)
    assert engine.pool.size() == 5
    assert engine.dialect.name == "postgresql"
    engine.dispose()


def test_scheduler_is_disabled_by_default():
    settings = Settings(
        zoho_client_id="test",
        zoho_client_secret="test",
        zoho_refresh_token="test",
        _env_file=None,
    )
    assert settings.schedule_enabled is False
