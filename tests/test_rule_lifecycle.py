from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.rules import (
    AccountWithoutDealRule,
    IncompleteAccountProfileRule,
    StaleDealRule,
    _zoho_datetime_now,
    alert_category_value,
    alert_severity,
)


def settings():
    return SimpleNamespace(
        zoho_accounts_module="Accounts",
        zoho_deals_module="Deals",
        zoho_alerts_module="CRM_Alerts",
        zoho_account_deals_related_list="Deals",
        stale_deal_days=21,
        required_account_profile_fields=["Account_Name", "Phone"],
        alert_name_field="Name",
        alert_category_field="Category",
        alert_account_field="Account",
        alert_deal_field="Deal",
        alert_responsible_owner_field="Owner",
        alert_inactive_days_field="Inactive_Days",
        alert_severity_field="Severity",
        alert_status_field="Status",
        alert_description_field=None,
        alert_recommended_action_field="Recommended_Action",
        alert_generated_on_field="Generated_On",
        alert_resolved_on_field=None,
        alert_days_open_field=None,
        alert_unique_key_field="Unique_Key",
    )


class FakeZoho:
    def __init__(self, records, alerts, related=None):
        self.records = records
        self.alerts = alerts
        self.related = related or []
        self.created = []
        self.updated = []

    def get_all_records(self, module, fields=None):
        return self.alerts if module == "CRM_Alerts" else self.records

    def get_related_records(self, module, record_id, related_list, fields=None):
        return self.related

    def has_related_records(self, module, record_id, related_list):
        return bool(self.related)

    def create_record(self, module, record):
        self.created.append(record)
        return "new"

    def update_record(self, module, record_id, changes):
        self.updated.append((record_id, changes))


def test_related_record_rule_resolves_when_relation_exists():
    alert = {"id": "9", "Unique_Key": "ACCOUNT-1-WITHOUT-DEAL", "Status": "Open"}
    client = FakeZoho([{"id": "1", "Account_Name": "ABC"}], [alert], [{"id": "2"}])
    result = AccountWithoutDealRule(client, settings()).run()
    assert result.alerts_resolved == 1
    assert client.updated == [("9", {"Status": "Resolved"})]


def test_resolved_related_record_alert_is_reopened_without_duplicate():
    alert = {"id": "9", "Unique_Key": "ACCOUNT-1-WITHOUT-DEAL", "Status": "Resolved"}
    client = FakeZoho([{"id": "1", "Account_Name": "ABC"}], [alert])
    result = AccountWithoutDealRule(client, settings()).run()
    assert result.alerts_updated == 1
    assert client.updated[0][1]["Status"] == "Open"
    assert not client.created


def test_incomplete_profile_alert_resolves_when_profile_is_complete():
    alert = {"id": "9", "Unique_Key": "ACCOUNT-1-INCOMPLETE-PROFILE", "Status": "Open"}
    client = FakeZoho([{"id": "1", "Account_Name": "ABC", "Phone": "123"}], [alert])
    result = IncompleteAccountProfileRule(client, settings()).run()
    assert result.alerts_resolved == 1
    assert client.updated == [("9", {"Status": "Resolved"})]


def test_stale_alert_resolves_after_recent_modification():
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    alert = {"id": "9", "Unique_Key": "DEAL-1-STALE", "Status": "Open"}
    record = {"id": "1", "Deal_Name": "Deal", "Stage": "Open", "Modified_Time": recent}
    client = FakeZoho([record], [alert])
    result = StaleDealRule(client, settings()).run()
    assert result.alerts_resolved == 1
    assert client.updated == [("9", {"Status": "Resolved"})]


def test_datetime_format_and_deterministic_severity():
    value = _zoho_datetime_now()
    assert "." not in value.split("+")[0]
    assert alert_severity("Stale Deal", 61) == "Critical"
    assert alert_severity("Stale Deal", 31) == "High"
    assert alert_severity("Stale Deal", 30) == "Medium"
    assert alert_severity("Deal Without Quote") == "High"
    assert alert_severity("Account Without Contact") == "Medium"


def test_short_readable_alert_category_values():
    assert alert_category_value("Account Without Contact") == "No Contact"
    assert alert_category_value("Account Without Deal") == "No Deal"
    assert alert_category_value("Account Without Quote") == "No Quote"
    assert alert_category_value("Incomplete Account Profile") == "Incomplete Profile"
    assert alert_category_value("Stale Account") == "Stale Account"
    assert alert_category_value("Stale Deal") == "Stale Deal"
    assert alert_category_value("Deal Without Quote") == "Deal No Quote"
