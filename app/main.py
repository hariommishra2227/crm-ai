from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
from threading import Lock

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from zoneinfo import ZoneInfo

from .config import get_settings
from .database import ScanRun, SessionLocal
from .rules import (
    AccountWithoutContactRule,
    AccountWithoutDealRule,
    AccountWithoutQuoteRule,
    DealWithoutQuoteRule,
    IncompleteAccountProfileRule,
    StaleAccountRule,
    StaleDealRule,
)
from .zoho import ZohoAPIError, ZohoClient
from .primary_reconciliation import PrimaryAlertReconciler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)
settings = get_settings()
scheduler = BackgroundScheduler(timezone=ZoneInfo("Asia/Kolkata"))
_full_scan_lock = Lock()
_account_health_lock = Lock()


RULES = {
    "account_without_contact": AccountWithoutContactRule,
    "account_without_deal": AccountWithoutDealRule,
    "account_without_quote": AccountWithoutQuoteRule,
    "deal_without_quote": DealWithoutQuoteRule,
    "stale_account": StaleAccountRule,
    "stale_deal": StaleDealRule,
    "incomplete_account_profile": IncompleteAccountProfileRule,
}
_rule_scan_locks = {name: Lock() for name in RULES}


def recover_interrupted_scan_runs() -> int:
    finished_at = datetime.now(timezone.utc)
    with SessionLocal() as db:
        runs = db.scalars(
            select(ScanRun).where(
                ScanRun.status == "running",
                ScanRun.finished_at.is_(None),
            )
        ).all()
        for run in runs:
            run.status = "interrupted"
            run.finished_at = finished_at
            run.failure_message = (
                "Scan interrupted by application restart or deployment."
            )
            if run.started_at:
                started_at = run.started_at
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=timezone.utc)
                run.duration_seconds = max(
                    0.0, (finished_at - started_at).total_seconds()
                )
        db.commit()

    logger.info("recovered %s interrupted scan runs", len(runs))
    return len(runs)


def execute_scan(rule_name: str = "account_without_contact") -> dict:
    if not _account_health_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Primary reconciliation or scan already running")
    rule_lock = _rule_scan_locks[rule_name]
    if not rule_lock.acquire(blocking=False):
        _account_health_lock.release()
        raise HTTPException(
            status_code=409,
            detail="Scan already running for this rule",
        )
    try:
        return _execute_scan_unlocked(rule_name)
    finally:
        rule_lock.release()
        _account_health_lock.release()


def _execute_scan_unlocked(rule_name: str) -> dict:
    started_at = datetime.now(timezone.utc)
    run = ScanRun(rule=rule_name, status="running", started_at=started_at)
    client: ZohoClient | None = None

    with SessionLocal() as db:
        try:
            db.add(run)
            db.commit()
            db.refresh(run)
            logger.info("scan started rule=%s scan_run_id=%s", rule_name, run.id)

            client = ZohoClient(settings)
            result = RULES[rule_name](client, settings).run()
            run.status = "completed" if result.errors == 0 else "completed_with_errors"
            run.accounts_checked = result.accounts_checked
            run.alerts_created = result.alerts_created
            run.alerts_resolved = result.alerts_resolved
            run.alerts_already_open = result.alerts_already_open
            run.alerts_updated = result.alerts_updated
            run.errors = result.errors
            response = {"scan_run_id": run.id, **result.to_dict()}
        except Exception as exc:
            db.rollback()
            run.status = "failed"
            run.errors = max(run.errors or 0, 1)
            run.failure_message = str(exc)[:2000]
            db.add(run)
            logger.exception("scan failed rule=%s", rule_name)
            response = None
            failure = exc
        finally:
            if client:
                client.close()
            finished_at = datetime.now(timezone.utc)
            run.finished_at = finished_at
            run.duration_seconds = max(
                0.0, (finished_at - started_at).total_seconds()
            )
            try:
                db.add(run)
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("failed to persist scan history rule=%s", rule_name)
                raise

    if response is None:
        if isinstance(failure, ZohoAPIError):
            raise HTTPException(status_code=502, detail=str(failure)) from failure
        raise HTTPException(status_code=500, detail="Scan failed") from failure

    logger.info(
        "scan completed rule=%s checked=%s created=%s resolved=%s updated=%s errors=%s",
        rule_name,
        response["accounts_checked"],
        response["alerts_created"],
        response["alerts_resolved"],
        response["alerts_updated"],
        response["errors"],
    )
    return response


def execute_all_scans() -> dict:
    if not _full_scan_lock.acquire(blocking=False):
        logger.warning("full scan skipped because another full scan is running")
        return {"status": "skipped_already_running", "rules": {}}

    logger.info("full scan started")
    results: dict[str, dict] = {}
    has_errors = False
    try:
        for name in RULES:
            try:
                results[name] = execute_scan(name)
                if results[name].get("errors", 0):
                    has_errors = True
            except HTTPException as exc:
                has_errors = True
                results[name] = {"status": "failed", "detail": exc.detail}
            except Exception:
                has_errors = True
                logger.exception("unexpected full-scan rule failure rule=%s", name)
                results[name] = {"status": "failed", "detail": "Unexpected error"}
        status = "completed_with_errors" if has_errors else "completed"
        return {"status": status, "rules": results}
    finally:
        logger.info("full scan finished errors=%s", has_errors)
        _full_scan_lock.release()


def run_scheduled_scan() -> None:
    result = execute_all_scans()
    logger.info("scheduled scan finished status=%s", result["status"])


@asynccontextmanager
async def lifespan(_: FastAPI):
    recover_interrupted_scan_runs()
    if settings.schedule_enabled:
        scheduler.add_job(
            run_scheduled_scan,
            "cron",
            hour=settings.schedule_hour_ist,
            minute=0,
            id="daily_crm_intelligence",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        logger.info("scheduler enabled daily_hour_ist=%s", settings.schedule_hour_ist)
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="CRM Intelligence MVP", version="0.2.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> JSONResponse:
    database_status = "ok"
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except Exception:
        database_status = "unavailable"
        logger.exception("readiness database check failed")

    zoho_configured = all(
        (
            settings.zoho_client_id,
            settings.zoho_client_secret,
            settings.zoho_refresh_token,
            settings.zoho_api_domain,
        )
    )
    payload = {
        "status": "ready" if database_status == "ok" and zoho_configured else "not_ready",
        "database": database_status,
        "zoho": "configured" if zoho_configured else "configuration_missing",
    }
    return JSONResponse(payload, status_code=200 if payload["status"] == "ready" else 503)


@app.get("/scan-runs")
def scan_runs(limit: int = Query(default=50, ge=1, le=200)) -> list[dict]:
    with SessionLocal() as db:
        runs = db.scalars(
            select(ScanRun).order_by(ScanRun.started_at.desc()).limit(limit)
        ).all()
        return [
            {
                "id": run.id,
                "rule": run.rule,
                "status": run.status,
                "records_checked": run.accounts_checked,
                "alerts_created": run.alerts_created,
                "alerts_resolved": run.alerts_resolved,
                "alerts_already_open": run.alerts_already_open,
                "alerts_updated": run.alerts_updated,
                "errors": run.errors,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "duration_seconds": run.duration_seconds,
                "failure_message": run.failure_message,
            }
            for run in runs
        ]


@app.post("/scans/account-without-contact")
def scan_account_without_contact() -> dict:
    return execute_scan()


@app.post("/scans/account-without-deal")
def scan_account_without_deal() -> dict:
    return execute_scan("account_without_deal")


@app.post("/scans/account-without-quote")
def scan_account_without_quote() -> dict:
    return execute_scan("account_without_quote")


@app.post("/scans/deal-without-quote")
def scan_deal_without_quote() -> dict:
    return execute_scan("deal_without_quote")


@app.post("/scans/stale-accounts")
def scan_stale_accounts() -> dict:
    return execute_scan("stale_account")


@app.post("/scans/stale-deals")
def scan_stale_deals() -> dict:
    return execute_scan("stale_deal")


@app.post("/scans/incomplete-account-profile")
def scan_incomplete_account_profile() -> dict:
    return execute_scan("incomplete_account_profile")


@app.post("/scans/all")
def scan_all() -> dict:
    return execute_all_scans()


@app.post("/alerts/reconcile-primary")
def reconcile_primary(
    dry_run: bool = Query(default=True),
    confirm: str | None = Query(default=None),
) -> dict:
    if not dry_run and confirm != "RECONCILE_PRIMARY_ALERTS":
        raise HTTPException(status_code=400, detail="Exact confirmation is required for real reconciliation")
    if not _account_health_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Account-health reconciliation or scan already running")
    client: ZohoClient | None = None
    try:
        client = ZohoClient(settings)
        return PrimaryAlertReconciler(client, settings, dry_run=dry_run).run()
    except ZohoAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        if client:
            client.close()
        _account_health_lock.release()
