from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.rules import (
    AccountWithoutDealRule,
    IncompleteAccountProfileRule,
    StaleDealRule,
)


def settings():
    return SimpleNamespace(
        zoho_accounts_module="Accounts", zoho_deals_module="Deals",
        zoho_alerts_module="CRM_Alerts", zoho_account_deals_related_list="Deals",
        stale_deal_days=21,
        required_account_profile_fields=["Account_Name", "Phone"],
        alert_name_field="Name", alert_category_field="Category",
        alert_account_field="Account", alert_deal_field="Deal",
        alert_responsible_owner_field="Owner",
        alert_inactive_days_field="Inactive_Days", alert_severity_field="Severity",
        alert_status_field="Status", alert_description_field=None,
        alert_recommended_action_field="Recommended_Action",
        alert_generated_on_field="Generated_On", alert_unique_key_field="Unique_Key",
    )


class FakeZoho:
    def __init__(self, records, alerts=None, related=None):
        self.records = records
        self.alerts = alerts or []
        self.related = related or []
        self.created = []
        self.updated = []

    def get_all_records(self, module, fields=None):
        return self.alerts if module == "CRM_Alerts" else self.records

    def get_related_records(self, module, record_id, related_list):
        return self.related

    def has_related_records(self, module, record_id, related_list):
        return bool(self.related)

    def create_record(self, module, record):
        self.created.append(record)
        return "900"

    def update_record(self, module, record_id, changes):
        self.updated.append((record_id, changes))


def test_account_without_deal_uses_configured_related_list_and_deduplicates():
    alert = {"id": "900", "Unique_Key": "ACCOUNT-1-WITHOUT-DEAL", "Status": "New"}
    client = FakeZoho([{"id": "1", "Account_Name": "ABC"}], alerts=[alert])
    result = AccountWithoutDealRule(client, settings()).run()
    assert result.alerts_already_open == 1
    assert not client.created


def test_stale_deal_updates_existing_alert_and_ignores_closed_deal():
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    records = [
        {"id": "1", "Deal_Name": "Open", "Stage": "Negotiation", "Modified_Time": old},
        {"id": "2", "Deal_Name": "Won", "Stage": "Closed Won", "Modified_Time": old},
    ]
    alert = {"id": "900", "Unique_Key": "DEAL-1-STALE", "Status": "New"}
    client = FakeZoho(records, alerts=[alert])
    result = StaleDealRule(client, settings()).run()
    assert result.alerts_updated == 1
    assert len(client.updated) == 1
    assert not client.created


def test_incomplete_profile_creates_one_alert_listing_missing_fields():
    created = (datetime.now(timezone.utc) - timedelta(days=12, seconds=5)).isoformat()
    client = FakeZoho(
        [
            {
                "id": "1",
                "Account_Name": "ABC",
                "Phone": "",
                "Owner": {"id": "7"},
                "Created_Time": created,
            }
        ]
    )
    result = IncompleteAccountProfileRule(client, settings()).run()
    assert result.alerts_created == 1
    assert "Phone" not in client.created[0]
    assert None not in client.created[0]
    assert client.created[0]["Name"] == "ABC"
    assert client.created[0]["Category"] == "Incomplete Profile"
    assert client.created[0]["Inactive_Days"] == 12
    assert client.created[0]["Owner"] == {"id": "7"}
