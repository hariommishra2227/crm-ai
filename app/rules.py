from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from .config import Settings
from .zoho import ZohoClient

logger = logging.getLogger(__name__)


def _zoho_datetime_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def alert_severity(category: str, inactive_days: int = 0) -> str:
    if category == "Stale Deal":
        if inactive_days > 60:
            return "Critical"
        if inactive_days > 30:
            return "High"
    if category in {"Deal Without Quote", "Deal No Quote"}:
        return "High"
    return "Medium"


def alert_category_value(category: str) -> str:
    return {
        "Account Without Contact": "No Contact",
        "Account Without Deal": "No Deal",
        "Account Without Quote": "No Quote",
        "Incomplete Account Profile": "Incomplete Profile",
        "Incomplete Contact": "Incomplete Contact",
        "Stale Account": "Stale Account",
        "Stale Deal": "Stale Deal",
        "Deal Without Quote": "Deal No Quote",
    }.get(category, category)


def _days_open(alert: dict, detected_on_field: str = "Detected_On") -> int:
    detected = _parse_zoho_datetime(alert.get(detected_on_field))
    if not detected:
        return 0
    return max(0, (datetime.now(timezone.utc) - detected.astimezone(timezone.utc)).days)


@dataclass
class ScanResult:
    accounts_checked: int = 0
    alerts_created: int = 0
    alerts_resolved: int = 0
    alerts_already_open: int = 0
    alerts_updated: int = 0
    errors: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class AccountWithoutContactRule:
    category = "Account Without Contact"

    def __init__(self, client: ZohoClient, settings: Settings):
        self.client = client
        self.s = settings

    def run(self) -> ScanResult:
        result = ScanResult()
        accounts = self.client.get_all_records(
            self.s.zoho_accounts_module,
            fields=["id", "Account_Name", "Owner", "Created_Time"],
        )
        alerts = self.client.get_all_records(
            self.s.zoho_alerts_module,
            fields=[
                "id",
                self.s.alert_unique_key_field,
                self.s.alert_status_field,
                self.s.alert_generated_on_field,
            ],
        )
        by_key = {
            alert.get(self.s.alert_unique_key_field): alert
            for alert in alerts
            if alert.get(self.s.alert_unique_key_field)
        }

        for account in accounts:
            result.accounts_checked += 1
            account_id = str(account["id"])
            unique_key = f"ACCOUNT-{account_id}-WITHOUT-CONTACT"
            existing = by_key.get(unique_key)
            try:
                created = _parse_zoho_datetime(account.get("Created_Time"))
                if not created:
                    logger.warning(
                        "Account Without Contact could not parse Created_Time "
                        "account_id=%s",
                        account_id,
                    )
                account_age_days = (
                    max(
                        0,
                        (
                            datetime.now(timezone.utc)
                            - created.astimezone(timezone.utc)
                        ).days,
                    )
                    if created
                    else 0
                )
                has_contacts = self.client.has_related_records(
                    self.s.zoho_accounts_module, account_id, "Contacts"
                )
                if not has_contacts:
                    if existing and existing.get(self.s.alert_status_field) != "Resolved":
                        result.alerts_already_open += 1
                        self._update_open_alert(
                            existing, account, account_age_days
                        )
                        result.alerts_updated += 1
                    elif existing:
                        self._reopen_alert(
                            account, str(existing["id"]), account_age_days
                        )
                        result.alerts_updated += 1
                    else:
                        self._create_alert(account, unique_key, account_age_days)
                        result.alerts_created += 1
                elif existing and existing.get(self.s.alert_status_field) != "Resolved":
                    self._resolve_alert(str(existing["id"]))
                    result.alerts_resolved += 1
            except Exception as exc:
                logger.warning(
                    "Account Without Contact failed account_id=%s error=%s: %s",
                    account_id,
                    type(exc).__name__,
                    exc,
                )
                result.errors += 1
        return result

    def _create_alert(
        self, account: dict, unique_key: str, inactive_days: int
    ) -> None:
        now = _zoho_datetime_now()
        owner = account.get("Owner") or {}
        account_name = account.get("Account_Name", "Unnamed Account")
        record = {
            self.s.alert_name_field: account_name,
            self.s.alert_category_field: alert_category_value(self.category),
            self.s.alert_account_field: {"id": str(account["id"])},
            self.s.alert_inactive_days_field: inactive_days,
            self.s.alert_severity_field: alert_severity(
                self.category, inactive_days
            ),
            self.s.alert_status_field: "Open",
            self.s.alert_recommended_action_field: (
                "Add and verify at least one decision-maker or business contact."
            ),
            self.s.alert_generated_on_field: now,
            self.s.alert_unique_key_field: unique_key,
        }
        if self.s.alert_description_field:
            record[self.s.alert_description_field] = (
                f"{account_name} has no related Contact in Zoho CRM."
            )
        if owner.get("id"):
            # Setting record Owner makes private-module visibility owner-specific.
            record[self.s.alert_responsible_owner_field] = {"id": str(owner["id"])}
        self.client.create_record(self.s.zoho_alerts_module, record)

    def _reopen_alert(
        self, account: dict, alert_id: str, inactive_days: int
    ) -> None:
        account_name = account.get("Account_Name", "Unnamed Account")
        changes = {
            self.s.alert_status_field: "Open",
            self.s.alert_name_field: account_name,
            self.s.alert_category_field: alert_category_value(self.category),
            self.s.alert_severity_field: alert_severity(
                self.category, inactive_days
            ),
            self.s.alert_generated_on_field: _zoho_datetime_now(),
            self.s.alert_inactive_days_field: inactive_days,
            self.s.alert_recommended_action_field: (
                "Add and verify at least one decision-maker or business contact."
            ),
        }
        if getattr(self.s, "alert_days_open_field", None):
            changes[self.s.alert_days_open_field] = 0
        owner = account.get("Owner") or {}
        if owner.get("id"):
            changes[self.s.alert_responsible_owner_field] = {"id": str(owner["id"])}
        self.client.update_record(self.s.zoho_alerts_module, alert_id, changes)

    def _update_open_alert(
        self, alert: dict, account: dict, inactive_days: int
    ) -> None:
        account_name = account.get("Account_Name", "Unnamed Account")
        changes = {
            self.s.alert_name_field: account_name,
            self.s.alert_category_field: alert_category_value(self.category),
            self.s.alert_inactive_days_field: inactive_days,
            self.s.alert_severity_field: alert_severity(
                self.category, inactive_days
            ),
        }
        owner = account.get("Owner") or {}
        if owner.get("id"):
            changes[self.s.alert_responsible_owner_field] = {
                "id": str(owner["id"])
            }
        field = getattr(self.s, "alert_days_open_field", None)
        if field:
            changes[field] = _days_open(alert, self.s.alert_generated_on_field)
        self.client.update_record(
            self.s.zoho_alerts_module,
            str(alert["id"]),
            changes,
        )

    def _resolve_alert(self, alert_id: str) -> None:
        changes = {
            self.s.alert_status_field: "Resolved",
        }
        if getattr(self.s, "alert_resolved_on_field", None):
            changes[self.s.alert_resolved_on_field] = _zoho_datetime_now()
        self.client.update_record(
            self.s.zoho_alerts_module,
            alert_id,
            changes,
        )


class AlertRule:
    """Small shared alert helper used by new rules; the original rule is unchanged."""

    category = ""

    def __init__(self, client: ZohoClient, settings: Settings):
        self.client = client
        self.s = settings

    def _alerts_by_key(self) -> dict[str, dict]:
        alerts = self.client.get_all_records(
            self.s.zoho_alerts_module,
            fields=[
                "id",
                self.s.alert_unique_key_field,
                self.s.alert_status_field,
                self.s.alert_generated_on_field,
            ],
        )
        return {
            str(a[self.s.alert_unique_key_field]): a
            for a in alerts
            if a.get(self.s.alert_unique_key_field)
        }

    def _create_alert(
        self,
        source: dict,
        unique_key: str,
        title: str,
        description: str,
        action: str,
        *,
        inactive_days: int = 0,
        account_id: str | None = None,
        deal_id: str | None = None,
    ) -> None:
        record = {
            self.s.alert_name_field: title,
            self.s.alert_category_field: alert_category_value(self.category),
            self.s.alert_inactive_days_field: inactive_days,
            self.s.alert_severity_field: alert_severity(self.category, inactive_days),
            self.s.alert_status_field: "Open",
            self.s.alert_recommended_action_field: action,
            self.s.alert_generated_on_field: _zoho_datetime_now(),
            self.s.alert_unique_key_field: unique_key,
        }
        if account_id:
            record[self.s.alert_account_field] = {"id": account_id}
        if deal_id:
            record[self.s.alert_deal_field] = {"id": deal_id}
        if self.s.alert_description_field:
            record[self.s.alert_description_field] = description
        owner = source.get("Owner") or {}
        if owner.get("id"):
            record[self.s.alert_responsible_owner_field] = {"id": str(owner["id"])}
        self.client.create_record(self.s.zoho_alerts_module, record)

    def _resolve_alert(self, alert_id: str) -> None:
        changes = {self.s.alert_status_field: "Resolved"}
        if getattr(self.s, "alert_resolved_on_field", None):
            changes[self.s.alert_resolved_on_field] = _zoho_datetime_now()
        self.client.update_record(self.s.zoho_alerts_module, alert_id, changes)

    def _create_if_missing(self, result: ScanResult, existing: dict | None, **kwargs) -> None:
        if existing:
            if existing.get(self.s.alert_status_field) != "Resolved":
                result.alerts_already_open += 1
                inactive_days = kwargs.get("inactive_days", 0)
                changes = {
                    self.s.alert_name_field: kwargs["title"],
                    self.s.alert_category_field: alert_category_value(
                        self.category
                    ),
                    self.s.alert_inactive_days_field: inactive_days,
                    self.s.alert_severity_field: alert_severity(
                        self.category, inactive_days
                    ),
                }
                field = getattr(self.s, "alert_days_open_field", None)
                if field:
                    changes[field] = _days_open(
                        existing, self.s.alert_generated_on_field
                    )
                owner = kwargs["source"].get("Owner") or {}
                if owner.get("id"):
                    changes[self.s.alert_responsible_owner_field] = {
                        "id": str(owner["id"])
                    }
                self.client.update_record(
                    self.s.zoho_alerts_module,
                    str(existing["id"]),
                    changes,
                )
                result.alerts_updated += 1
                return
            changes = {
                self.s.alert_status_field: "Open",
                self.s.alert_name_field: kwargs["title"],
                self.s.alert_category_field: alert_category_value(self.category),
                self.s.alert_recommended_action_field: kwargs["action"],
                self.s.alert_generated_on_field: _zoho_datetime_now(),
                self.s.alert_severity_field: alert_severity(
                    self.category, kwargs.get("inactive_days", 0)
                ),
                self.s.alert_inactive_days_field: kwargs.get("inactive_days", 0),
            }
            if getattr(self.s, "alert_days_open_field", None):
                changes[self.s.alert_days_open_field] = 0
            if self.s.alert_description_field:
                changes[self.s.alert_description_field] = kwargs["description"]
            owner = kwargs["source"].get("Owner") or {}
            if owner.get("id"):
                changes[self.s.alert_responsible_owner_field] = {
                    "id": str(owner["id"])
                }
            self.client.update_record(
                self.s.zoho_alerts_module, str(existing["id"]), changes
            )
            result.alerts_updated += 1
            return
        self._create_alert(**kwargs)
        result.alerts_created += 1


class RelatedRecordMissingRule(AlertRule):
    source_module_setting = ""
    related_list_setting = ""
    record_name_field = ""
    unique_key_suffix = ""
    title_prefix = ""
    description_noun = ""
    action = ""

    def run(self) -> ScanResult:
        result = ScanResult()
        module = getattr(self.s, self.source_module_setting)
        records = self.client.get_all_records(
            module,
            fields=[
                "id",
                self.record_name_field,
                "Owner",
                "Account_Name",
                "Created_Time",
            ],
        )
        alerts = self._alerts_by_key()
        for record in records:
            result.accounts_checked += 1
            record_id = str(record["id"])
            key = f"{self.unique_key_suffix.split('-', 1)[0]}-{record_id}-{self.unique_key_suffix.split('-', 1)[1]}"
            try:
                created = _parse_zoho_datetime(record.get("Created_Time"))
                if not created:
                    logger.warning(
                        "%s could not parse Created_Time record_id=%s",
                        self.category,
                        record_id,
                    )
                inactive_days = (
                    max(
                        0,
                        (
                            datetime.now(timezone.utc)
                            - created.astimezone(timezone.utc)
                        ).days,
                    )
                    if created
                    else 0
                )
                has_related = self.client.has_related_records(
                    module, record_id, getattr(self.s, self.related_list_setting)
                )
                if has_related:
                    existing = alerts.get(key)
                    if existing and existing.get(self.s.alert_status_field) != "Resolved":
                        self._resolve_alert(str(existing["id"]))
                        result.alerts_resolved += 1
                    continue
                name = record.get(self.record_name_field) or "Unnamed Record"
                account = record.get("Account_Name") or {}
                account_id = record_id if module == self.s.zoho_accounts_module else (
                    str(account["id"]) if isinstance(account, dict) and account.get("id") else None
                )
                alert_name = name
                self._create_if_missing(
                    result,
                    alerts.get(key),
                    source=record,
                    unique_key=key,
                    title=alert_name,
                    description=f"{name} has no related {self.description_noun} in Zoho CRM.",
                    action=self.action,
                    inactive_days=inactive_days,
                    account_id=account_id,
                    deal_id=record_id if module == self.s.zoho_deals_module else None,
                )
            except Exception as exc:
                logger.warning(
                    "%s failed module=%s record_id=%s related_list=%s error=%s: %s",
                    self.category,
                    module,
                    record_id,
                    getattr(self.s, self.related_list_setting),
                    type(exc).__name__,
                    exc,
                )
                result.errors += 1
        return result


class AccountWithoutDealRule(RelatedRecordMissingRule):
    category = "Account Without Deal"
    source_module_setting = "zoho_accounts_module"
    related_list_setting = "zoho_account_deals_related_list"
    record_name_field = "Account_Name"
    unique_key_suffix = "ACCOUNT-WITHOUT-DEAL"
    title_prefix = "No deal"
    description_noun = "Deal"
    action = "Review the account and create an opportunity if there is sales potential."


class AccountWithoutQuoteRule(RelatedRecordMissingRule):
    category = "Account Without Quote"
    source_module_setting = "zoho_accounts_module"
    related_list_setting = "zoho_account_quotes_related_list"
    record_name_field = "Account_Name"
    unique_key_suffix = "ACCOUNT-WITHOUT-QUOTE"
    title_prefix = "No quote"
    description_noun = "Quote"
    action = "Review whether a quotation should be prepared for this account."


class DealWithoutQuoteRule(RelatedRecordMissingRule):
    category = "Deal Without Quote"
    source_module_setting = "zoho_deals_module"
    related_list_setting = "zoho_deal_quotes_related_list"
    record_name_field = "Deal_Name"
    unique_key_suffix = "DEAL-WITHOUT-QUOTE"
    title_prefix = "No quote"
    description_noun = "Quote"
    action = "Prepare or link a quotation for this deal."


def _parse_zoho_datetime(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


class StaleRule(AlertRule):
    module_setting = ""
    name_field = ""
    threshold_setting = ""
    key_prefix = ""
    title_prefix = "Inactive"
    action = ""

    def run(self) -> ScanResult:
        result = ScanResult()
        module = getattr(self.s, self.module_setting)
        fields = ["id", self.name_field, "Owner", "Modified_Time"]
        if self.key_prefix == "DEAL":
            fields += ["Stage", "Account_Name"]
        records = self.client.get_all_records(module, fields=fields)
        alerts = self._alerts_by_key()
        now = datetime.now(timezone.utc)
        for record in records:
            result.accounts_checked += 1
            try:
                stage = str(record.get("Stage") or "")
                if self.key_prefix == "DEAL" and stage.casefold() in {"closed won", "closed lost"}:
                    key = f"{self.key_prefix}-{record['id']}-STALE"
                    existing = alerts.get(key)
                    if existing and existing.get(self.s.alert_status_field) != "Resolved":
                        self._resolve_alert(str(existing["id"]))
                        result.alerts_resolved += 1
                    continue
                modified = _parse_zoho_datetime(record.get("Modified_Time"))
                if not modified:
                    logger.warning(
                        "%s could not parse Modified_Time record_id=%s",
                        self.category,
                        record.get("id"),
                    )
                days = (
                    max(0, (now - modified.astimezone(timezone.utc)).days)
                    if modified
                    else 0
                )
                record_id = str(record["id"])
                key = f"{self.key_prefix}-{record_id}-STALE"
                if days <= getattr(self.s, self.threshold_setting):
                    existing = alerts.get(key)
                    if existing and existing.get(self.s.alert_status_field) != "Resolved":
                        self._resolve_alert(str(existing["id"]))
                        result.alerts_resolved += 1
                    continue
                name = record.get(self.name_field) or "Unnamed Record"
                account = record.get("Account_Name") or {}
                account_name = account.get("name") if isinstance(account, dict) else None
                description = f"{name} has had no activity or modification for {days} days."
                if self.key_prefix == "DEAL":
                    description = (
                        f"Deal: {name}; Account: {account_name or 'Not available'}; "
                        f"Stage: {stage or 'Not available'}; inactive days: {days}."
                    )
                existing = alerts.get(key)
                alert_name = name
                if existing and existing.get(self.s.alert_status_field) != "Resolved":
                    result.alerts_already_open += 1
                    changes = {
                        self.s.alert_name_field: alert_name,
                        self.s.alert_category_field: alert_category_value(
                            self.category
                        ),
                        self.s.alert_inactive_days_field: days,
                        self.s.alert_severity_field: alert_severity(self.category, days),
                    }
                    owner = record.get("Owner") or {}
                    if owner.get("id"):
                        changes[self.s.alert_responsible_owner_field] = {
                            "id": str(owner["id"])
                        }
                    if getattr(self.s, "alert_days_open_field", None):
                        changes[self.s.alert_days_open_field] = _days_open(
                            existing, self.s.alert_generated_on_field
                        )
                    if self.s.alert_description_field:
                        changes[self.s.alert_description_field] = description
                    self.client.update_record(
                        self.s.zoho_alerts_module,
                        str(existing["id"]),
                        changes,
                    )
                    result.alerts_updated += 1
                    continue
                account_id = record_id if self.key_prefix == "ACCOUNT" else (
                    str(account["id"]) if isinstance(account, dict) and account.get("id") else None
                )
                self._create_if_missing(
                    result,
                    existing,
                    source=record,
                    unique_key=key,
                    title=alert_name,
                    description=description,
                    action=self.action,
                    inactive_days=days,
                    account_id=account_id,
                    deal_id=record_id if self.key_prefix == "DEAL" else None,
                )
            except Exception as exc:
                logger.warning(
                    "%s failed record_id=%s error=%s: %s",
                    self.category,
                    record.get("id"),
                    type(exc).__name__,
                    exc,
                )
                result.errors += 1
        return result


class StaleAccountRule(StaleRule):
    category = "Stale Account"
    module_setting = "zoho_accounts_module"
    name_field = "Account_Name"
    threshold_setting = "stale_account_days"
    key_prefix = "ACCOUNT"
    action = "Contact the account owner and record the next customer action."


class StaleDealRule(StaleRule):
    category = "Stale Deal"
    module_setting = "zoho_deals_module"
    name_field = "Deal_Name"
    threshold_setting = "stale_deal_days"
    key_prefix = "DEAL"
    title_prefix = "Deal inactive"
    action = "Contact the customer and update the deal status or next action."


class IncompleteAccountProfileRule(AlertRule):
    category = "Incomplete Account Profile"

    def run(self) -> ScanResult:
        result = ScanResult()
        fields = list(
            dict.fromkeys(
                [
                    "id",
                    "Account_Name",
                    "Owner",
                    "Created_Time",
                    *self.s.required_account_profile_fields,
                ]
            )
        )
        accounts = self.client.get_all_records(self.s.zoho_accounts_module, fields=fields)
        alerts = self._alerts_by_key()
        for account in accounts:
            result.accounts_checked += 1
            try:
                account_id = str(account["id"])
                created = _parse_zoho_datetime(account.get("Created_Time"))
                if not created:
                    logger.warning(
                        "Incomplete Account Profile could not parse Created_Time "
                        "account_id=%s",
                        account_id,
                    )
                inactive_days = (
                    max(
                        0,
                        (
                            datetime.now(timezone.utc)
                            - created.astimezone(timezone.utc)
                        ).days,
                    )
                    if created
                    else 0
                )
                missing = [field for field in self.s.required_account_profile_fields if not account.get(field)]
                if not missing:
                    key = f"ACCOUNT-{account['id']}-INCOMPLETE-PROFILE"
                    existing = alerts.get(key)
                    if existing and existing.get(self.s.alert_status_field) != "Resolved":
                        self._resolve_alert(str(existing["id"]))
                        result.alerts_resolved += 1
                    continue
                name = account.get("Account_Name") or "Unnamed Account"
                key = f"ACCOUNT-{account_id}-INCOMPLETE-PROFILE"
                labels = ", ".join(field.replace("_", " ") for field in missing)
                self._create_if_missing(
                    result, alerts.get(key), source=account, unique_key=key,
                    title=name,
                    description=f"{name} is missing: {labels}",
                    action="Complete and verify the missing account profiling information.",
                    inactive_days=inactive_days,
                    account_id=account_id,
                )
            except Exception as exc:
                logger.warning(
                    "Incomplete Account Profile failed account_id=%s error=%s: %s",
                    account.get("id"),
                    type(exc).__name__,
                    exc,
                )
                result.errors += 1
        return result
