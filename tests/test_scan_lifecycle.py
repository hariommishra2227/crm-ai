import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import main
from app.database import Base, ScanRun


def test_startup_recovers_only_unfinished_running_scans(tmp_path, monkeypatch, caplog):
    engine = create_engine(f"sqlite:///{tmp_path / 'scan-runs.db'}")
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, expire_on_commit=False)
    started_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    completed_at = datetime.now(timezone.utc) - timedelta(minutes=5)

    with test_session() as db:
        db.add_all(
            [
                ScanRun(
                    id=1,
                    rule="account_without_contact",
                    status="running",
                    started_at=started_at,
                ),
                ScanRun(
                    id=2,
                    rule="stale_account",
                    status="completed",
                    started_at=started_at,
                    finished_at=completed_at,
                    duration_seconds=300,
                ),
                ScanRun(
                    id=3,
                    rule="stale_deal",
                    status="failed",
                    started_at=started_at,
                    finished_at=completed_at,
                    failure_message="Existing failure",
                ),
                ScanRun(
                    id=4,
                    rule="account_without_deal",
                    status="interrupted",
                    started_at=started_at,
                    finished_at=completed_at,
                    failure_message="Previously interrupted",
                ),
            ]
        )
        db.commit()

    monkeypatch.setattr(main, "SessionLocal", test_session)
    monkeypatch.setattr(main.settings, "schedule_enabled", False)

    async def run_lifespan():
        async with main.lifespan(main.app):
            pass

    with caplog.at_level(logging.INFO):
        asyncio.run(run_lifespan())

    with test_session() as db:
        rows = {row.id: row for row in db.scalars(select(ScanRun)).all()}

        assert rows[1].status == "interrupted"
        assert rows[1].finished_at is not None
        assert rows[1].duration_seconds >= 600
        assert rows[1].failure_message == (
            "Scan interrupted by application restart or deployment."
        )

        assert rows[2].status == "completed"
        assert rows[2].finished_at == completed_at.replace(tzinfo=None)
        assert rows[2].duration_seconds == 300

        assert rows[3].status == "failed"
        assert rows[3].failure_message == "Existing failure"

        assert rows[4].status == "interrupted"
        assert rows[4].failure_message == "Previously interrupted"

    assert "recovered 1 interrupted scan runs" in caplog.text
    engine.dispose()


def test_duplicate_single_rule_scan_returns_conflict():
    rule_lock = main._rule_scan_locks["account_without_contact"]
    assert rule_lock.acquire(blocking=False)
    try:
        with pytest.raises(HTTPException) as exc_info:
            main.execute_scan("account_without_contact")
    finally:
        rule_lock.release()

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Scan already running for this rule"
