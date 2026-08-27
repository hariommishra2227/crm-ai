import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.rules import (
    AccountWithoutContactRule,
    AccountWithoutDealRule,
    DealWithoutQuoteRule,
    StaleDealRule,
)


def settings():
    return SimpleNamespace(
        zoho_accounts_module="Accounts",
        zoho_deals_module="Deals",
        zoho_alerts_module="CRM_Alerts",
        zoho_account_deals_related_list="Deals",
        zoho_deal_quotes_related_list="Quotes",
        stale_deal_days=21,
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
    def __init__(self, records, alerts=None, has_related=False):
        self.records = records
        self.alerts = alerts or []
        self.has_related = has_related
        self.created = []
        self.updated = []
        self.requested_fields = []

    def get_all_records(self, module, fields=None):
        self.requested_fields.append((module, fields))
        return self.alerts if module == "CRM_Alerts" else self.records

    def has_related_records(self, module, record_id, related_list):
        return self.has_related

    def create_record(self, module, record):
        self.created.append(record)
        return "900"

    def update_record(self, module, record_id, changes):
        self.updated.append((record_id, changes))


def age(days):
    return (datetime.now(timezone.utc) - timedelta(days=days, seconds=5)).isoformat()


def test_account_without_contact_uses_account_created_time():
    client = FakeZoho(
        [
            {
                "id": "1",
                "Account_Name": "ABC",
                "Owner": {"id": "71"},
                "Created_Time": age(45),
            }
        ]
    )

    AccountWithoutContactRule(client, settings()).run()

    assert client.created[0]["Inactive_Days"] == 45
    assert client.created[0]["Name"] == "ABC"
    assert client.created[0]["Category"] == "No Contact"
    assert client.created[0]["Owner"] == {"id": "71"}
    assert "Created_Time" in client.requested_fields[0][1]


def test_deal_without_quote_uses_deal_created_time():
    client = FakeZoho(
        [
            {
                "id": "2",
                "Deal_Name": "Renewal",
                "Owner": {"id": "72"},
                "Created_Time": age(25),
            }
        ]
    )

    DealWithoutQuoteRule(client, settings()).run()

    assert client.created[0]["Inactive_Days"] == 25
    assert client.created[0]["Category"] == "Deal No Quote"
    assert client.created[0]["Owner"] == {"id": "72"}


def test_stale_deal_continues_to_use_modified_time():
    client = FakeZoho(
        [
            {
                "id": "3",
                "Deal_Name": "Expansion",
                "Stage": "Negotiation",
                "Created_Time": age(200),
                "Modified_Time": age(40),
            }
        ]
    )

    StaleDealRule(client, settings()).run()

    assert client.created[0]["Inactive_Days"] == 40


def test_invalid_created_time_uses_zero_and_logs_record_id(caplog):
    client = FakeZoho(
        [{"id": "4", "Account_Name": "Invalid", "Created_Time": "bad-date"}]
    )

    with caplog.at_level(logging.WARNING):
        AccountWithoutDealRule(client, settings()).run()

    assert client.created[0]["Inactive_Days"] == 0
    assert "record_id=4" in caplog.text


def test_existing_open_missing_relation_alert_gets_latest_inactive_days():
    alert = {
        "id": "900",
        "Unique_Key": "ACCOUNT-5-WITHOUT-DEAL",
        "Status": "Open",
    }
    client = FakeZoho(
        [
            {
                "id": "5",
                "Account_Name": "Existing",
                "Owner": {"id": "75"},
                "Created_Time": age(45),
            }
        ],
        alerts=[alert],
    )

    result = AccountWithoutDealRule(client, settings()).run()

    assert result.alerts_already_open == 1
    assert client.updated[0][1]["Inactive_Days"] == 45
    assert client.updated[0][1]["Severity"] == "Medium"
    assert client.updated[0][1]["Name"] == "Existing"
    assert client.updated[0][1]["Category"] == "No Deal"
    assert client.updated[0][1]["Owner"] == {"id": "75"}


def test_existing_stale_deal_severity_is_refreshed_as_days_increase():
    alert = {
        "id": "901",
        "Unique_Key": "DEAL-6-STALE",
        "Status": "Open",
        "Severity": "Medium",
    }
    client = FakeZoho(
        [
            {
                "id": "6",
                "Deal_Name": "Old Deal",
                "Stage": "Negotiation",
                "Modified_Time": age(40),
            }
        ],
        alerts=[alert],
    )

    StaleDealRule(client, settings()).run()

    assert client.updated[0][1]["Inactive_Days"] == 40
    assert client.updated[0][1]["Severity"] == "High"
