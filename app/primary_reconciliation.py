from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .rules import _parse_zoho_datetime, _zoho_datetime_now, alert_severity
from .zoho import ZohoClient

logger = logging.getLogger(__name__)

ACCOUNT_REMARKS = {
    "Incomplete Profile", "No Contact", "Incomplete Contact", "No Deal",
    "No Quote", "Stale Account",
}
ACCOUNT_LEGACY = {
    "account without contact": "No Contact", "account_without_contact": "No Contact",
    "no contact": "No Contact", "account without deal": "No Deal",
    "account_without_deal": "No Deal", "no deal": "No Deal",
    "account without quote": "No Quote", "account_without_quote": "No Quote",
    "no quote": "No Quote", "incomplete account profile": "Incomplete Profile",
    "incomplete_account_profile": "Incomplete Profile", "incomplete profile": "Incomplete Profile",
    "incomplete contact": "Incomplete Contact", "stale account": "Stale Account",
    "stale_account": "Stale Account",
}
DEAL_LEGACY = {
    "deal without quote": "Deal No Quote", "deal_without_quote": "Deal No Quote",
    "deal no quote": "Deal No Quote", "stale deal": "Stale Deal", "stale_deal": "Stale Deal",
}
ACCOUNT_NAME_PREFIXES = ("no contact: ", "no deal: ", "no quote: ")


def _lookup_id(value: Any) -> str | None:
    if isinstance(value, dict) and value.get("id") is not None:
        return str(value["id"])
    return str(value) if value not in (None, "") else None


def _days(value: object, *, label: str, record_id: str) -> int:
    parsed = _parse_zoho_datetime(value)
    if not parsed:
        logger.warning("could not parse %s record_id=%s; using 0 days", label, record_id)
        return 0
    return max(0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).days)


class PrimaryAlertReconciler:
    """Plan and optionally apply canonical account-health and deal-risk alerts."""

    def __init__(self, client: ZohoClient, settings: Settings, *, dry_run: bool = True):
        self.client, self.s, self.dry_run = client, settings, dry_run
        self.counts = Counter()
        self.sample: list[dict] = []

    def run(self) -> dict:
        account_fields = list(dict.fromkeys([
            "id", "Account_Name", "Owner", "Created_Time", "Modified_Time",
            *self.s.required_account_profile_fields,
        ]))
        accounts = self.client.get_all_records(self.s.zoho_accounts_module, fields=account_fields)
        alerts = self.client.get_all_records(self.s.zoho_alerts_module, fields=self._alert_fields())
        account_alerts: dict[str, list[dict]] = {}
        deal_alerts: dict[str, list[dict]] = {}
        for alert in alerts:
            if self._is_account_alert(alert):
                aid = _lookup_id(alert.get(self.s.alert_account_field))
                if aid:
                    account_alerts.setdefault(aid, []).append(alert)
                else:
                    self.counts["ambiguous_skipped"] += 1
            if self._is_deal_alert(alert):
                did = _lookup_id(alert.get(self.s.alert_deal_field))
                if did:
                    deal_alerts.setdefault(did, []).append(alert)
                else:
                    self.counts["ambiguous_skipped"] += 1

        for account in accounts:
            self.counts["accounts_checked"] += 1
            try:
                remark, days = self._account_primary(account)
                if remark is None:
                    self.counts["healthy_accounts"] += 1
                else:
                    self.counts["primary_" + remark.lower().replace(" ", "_")] += 1
                self._reconcile(account, remark, days, account_alerts.get(str(account["id"]), []), deal=False)
            except Exception as exc:
                logger.warning("primary account reconciliation failed account_id=%s error=%s", account.get("id"), exc)
                self.counts["ambiguous_skipped"] += 1

        deals = self.client.get_all_records(
            self.s.zoho_deals_module,
            fields=["id", "Deal_Name", "Owner", "Account_Name", "Stage", "Created_Time", "Modified_Time"],
        )
        for deal in deals:
            try:
                remark, days = self._deal_primary(deal)
                self._reconcile(deal, remark, days, deal_alerts.get(str(deal["id"]), []), deal=True)
            except Exception as exc:
                logger.warning("primary deal reconciliation failed deal_id=%s error=%s", deal.get("id"), exc)
                self.counts["ambiguous_skipped"] += 1

        keys = [
            "accounts_checked", "accounts_with_contact", "accounts_without_contact",
            "accounts_with_incomplete_contact", "accounts_profile_complete",
            "accounts_profile_incomplete", "accounts_with_deal", "accounts_without_deal",
            "accounts_with_quote", "accounts_without_quote", "accounts_stale",
            "healthy_accounts", "primary_incomplete_profile",
            "primary_no_contact", "primary_incomplete_contact", "primary_no_deal",
            "primary_no_quote", "primary_stale_account", "would_create", "would_update",
            "would_resolve", "ambiguous_skipped",
        ]
        return {**{key: self.counts[key] for key in keys}, "dry_run": self.dry_run, "sample_actions": self.sample}

    def _alert_fields(self) -> list[str]:
        return list(dict.fromkeys(filter(None, [
            "id", self.s.alert_name_field, self.s.alert_category_field,
            self.s.alert_account_field, self.s.alert_deal_field, self.s.alert_status_field,
            self.s.alert_unique_key_field, self.s.alert_generated_on_field,
        ])))

    def _normalized(self, alert: dict, mapping: dict[str, str]) -> str | None:
        value = str(alert.get(self.s.alert_category_field) or "").strip().casefold()
        return mapping.get(value)

    def _is_account_alert(self, alert: dict) -> bool:
        key = str(alert.get(self.s.alert_unique_key_field) or "").upper()
        name = str(alert.get(self.s.alert_name_field) or "").casefold()
        return bool(self._normalized(alert, ACCOUNT_LEGACY) or key.startswith("PRIMARY-ACCOUNT-") or
                    (key.startswith("ACCOUNT-") and any(x in key for x in ("CONTACT", "DEAL", "QUOTE", "PROFILE", "STALE"))) or
                    name.startswith(ACCOUNT_NAME_PREFIXES))

    def _is_deal_alert(self, alert: dict) -> bool:
        key = str(alert.get(self.s.alert_unique_key_field) or "").upper()
        return bool(self._normalized(alert, DEAL_LEGACY) or key.startswith("PRIMARY-DEAL-") or
                    (key.startswith("DEAL-") and any(x in key for x in ("QUOTE", "STALE"))))

    def _account_primary(self, account: dict) -> tuple[str | None, int]:
        aid = str(account["id"])
        created_days = _days(account.get("Created_Time"), label="Account Created_Time", record_id=aid)
        contacts = self.client.get_related_records(
            self.s.zoho_accounts_module, aid, "Contacts", fields=["id", "Email", "Phone"]
        )
        if not contacts:
            contact_status = "No Contact"
            self.counts["accounts_without_contact"] += 1
        else:
            self.counts["accounts_with_contact"] += 1
            contact_status = (
                "Valid"
                if any(contact.get("Email") or contact.get("Phone") for contact in contacts)
                else "Incomplete Contact"
            )
            if contact_status == "Incomplete Contact":
                self.counts["accounts_with_incomplete_contact"] += 1

        profile_complete = all(
            account.get(field) for field in self.s.required_account_profile_fields
        )
        self.counts[
            "accounts_profile_complete" if profile_complete else "accounts_profile_incomplete"
        ] += 1

        has_deal = self.client.has_related_records(
            self.s.zoho_accounts_module, aid, self.s.zoho_account_deals_related_list
        )
        self.counts["accounts_with_deal" if has_deal else "accounts_without_deal"] += 1

        has_quote = self.client.has_related_records(
            self.s.zoho_accounts_module, aid, self.s.zoho_account_quotes_related_list
        )
        self.counts["accounts_with_quote" if has_quote else "accounts_without_quote"] += 1

        stale_days = _days(account.get("Modified_Time"), label="Account Modified_Time", record_id=aid)
        is_stale = stale_days > self.s.stale_account_days
        if is_stale:
            self.counts["accounts_stale"] += 1

        if has_deal and not has_quote:
            return "No Quote", created_days
        if has_quote:
            return ("Stale Account", stale_days) if is_stale else (None, 0)
        if not has_deal:
            if contact_status == "No Contact":
                return "No Contact", created_days
            if contact_status == "Incomplete Contact":
                return "Incomplete Contact", created_days
            if not profile_complete:
                return "Incomplete Profile", created_days
            return "No Deal", created_days
        logger.warning("unexpected account fact combination account_id=%s", aid)
        return None, 0

    def _deal_primary(self, deal: dict) -> tuple[str | None, int]:
        did = str(deal["id"])
        if str(deal.get("Stage") or "").strip().casefold() in {"closed won", "closed lost"}:
            return None, 0
        if not self.client.has_related_records(self.s.zoho_deals_module, did, self.s.zoho_deal_quotes_related_list):
            return "Deal No Quote", _days(deal.get("Created_Time"), label="Deal Created_Time", record_id=did)
        stale_days = _days(deal.get("Modified_Time"), label="Deal Modified_Time", record_id=did)
        return ("Stale Deal", stale_days) if stale_days > self.s.stale_deal_days else (None, 0)

    def _reconcile(self, source: dict, remark: str | None, days: int, alerts: list[dict], *, deal: bool) -> None:
        record_id = str(source["id"])
        key = f"PRIMARY-{'DEAL' if deal else 'ACCOUNT'}-{record_id}"
        open_alerts = [a for a in alerts if str(a.get(self.s.alert_status_field) or "").casefold() != "resolved"]
        canonical = None
        if remark:
            mapping = DEAL_LEGACY if deal else ACCOUNT_LEGACY
            matching = [a for a in alerts if self._normalized(a, mapping) == remark]
            canonical = next((a for a in matching if a in open_alerts), None) or (matching[0] if matching else None)
            if canonical is None:
                canonical = next((a for a in alerts if a.get(self.s.alert_unique_key_field) == key), None)
            payload = self._payload(source, remark, days, key, deal=deal)
            if canonical:
                self._action("update", str(canonical["id"]), payload, key)
            else:
                self._action("create", None, payload, key)
        for alert in open_alerts:
            if canonical is None or str(alert.get("id")) != str(canonical.get("id")):
                self._action("resolve", str(alert["id"]), {self.s.alert_status_field: "Resolved"}, key)

    def _payload(self, source: dict, remark: str, days: int, key: str, *, deal: bool) -> dict:
        name_field = "Deal_Name" if deal else "Account_Name"
        payload = {
            self.s.alert_name_field: source.get(name_field) or ("Unnamed Deal" if deal else "Unnamed Account"),
            self.s.alert_category_field: remark, self.s.alert_status_field: "Open",
            self.s.alert_inactive_days_field: days, self.s.alert_severity_field: alert_severity(remark, days),
            self.s.alert_recommended_action_field: self._action_text(remark),
            self.s.alert_unique_key_field: key, self.s.alert_generated_on_field: _zoho_datetime_now(),
        }
        if deal:
            payload[self.s.alert_deal_field] = {"id": str(source["id"])}
            aid = _lookup_id(source.get("Account_Name"))
            if aid:
                payload[self.s.alert_account_field] = {"id": aid}
        else:
            payload[self.s.alert_account_field] = {"id": str(source["id"])}
        owner = source.get("Owner") or {}
        if isinstance(owner, dict) and owner.get("id"):
            payload[self.s.alert_responsible_owner_field] = {"id": str(owner["id"])}
        return payload

    @staticmethod
    def _action_text(remark: str) -> str:
        return {
            "Incomplete Profile": "Complete and verify the missing account profile fields.",
            "No Contact": "Add and verify at least one account contact.",
            "Incomplete Contact": "Add both email and phone to at least one related contact.",
            "No Deal": "Review the account and create a deal where appropriate.",
            "No Quote": "Prepare or link a quote for the account.",
            "Stale Account": "Contact the account owner and record the next customer action.",
            "Deal No Quote": "Prepare or link a quotation for this deal.",
            "Stale Deal": "Contact the customer and update the deal status or next action.",
        }[remark]

    def _action(self, kind: str, alert_id: str | None, payload: dict, key: str) -> None:
        self.counts["would_" + kind] += 1
        if len(self.sample) < 20:
            self.sample.append({"action": kind, "alert_id": alert_id, "unique_key": key,
                                "remark": payload.get(self.s.alert_category_field)})
        if self.dry_run:
            return
        if kind == "create":
            self.client.create_record(self.s.zoho_alerts_module, payload)
        else:
            self.client.update_record(self.s.zoho_alerts_module, str(alert_id), payload)
