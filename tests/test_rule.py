from types import SimpleNamespace

from app.rules import AccountWithoutContactRule


def settings():
    return SimpleNamespace(
        zoho_accounts_module="Accounts",
        zoho_alerts_module="CRM_Alerts",
        alert_name_field="Name",
        alert_category_field="Category",
        alert_account_field="Account",
        alert_responsible_owner_field="Owner",
        alert_inactive_days_field="Inactive_Days",
        alert_severity_field="Severity",
        alert_status_field="Status",
        alert_description_field=None,
        alert_recommended_action_field="Recommended_Action",
        alert_generated_on_field="Generated_On",
        alert_resolved_on_field=None,
        alert_unique_key_field="Unique_Key",
    )


class FakeZoho:
    def __init__(self, has_contact=False, alert=None):
        self.has_contact = has_contact
        self.alert = alert
        self.created = []
        self.updated = []

    def get_all_records(self, module, fields=None):
        if module == "Accounts":
            return [{"id": "101", "Account_Name": "ABC Ltd", "Owner": {"id": "7"}}]
        return [self.alert] if self.alert else []

    def get_related_records(self, module, record_id, related_list):
        return [{"id": "501"}] if self.has_contact else []

    def has_related_records(self, module, record_id, related_list):
        return self.has_contact

    def create_record(self, module, record):
        self.created.append(record)
        return "900"

    def update_record(self, module, record_id, changes):
        self.updated.append((record_id, changes))


def test_creates_alert_when_account_has_no_contact():
    client = FakeZoho(has_contact=False)
    result = AccountWithoutContactRule(client, settings()).run()
    assert result.alerts_created == 1
    assert client.created[0]["Unique_Key"] == "ACCOUNT-101-WITHOUT-CONTACT"
    assert client.created[0]["Owner"] == {"id": "7"}


def test_does_not_duplicate_open_alert():
    alert = {"id": "900", "Unique_Key": "ACCOUNT-101-WITHOUT-CONTACT", "Status": "New"}
    client = FakeZoho(has_contact=False, alert=alert)
    result = AccountWithoutContactRule(client, settings()).run()
    assert result.alerts_already_open == 1
    assert not client.created


def test_resolves_alert_after_contact_is_added():
    alert = {"id": "900", "Unique_Key": "ACCOUNT-101-WITHOUT-CONTACT", "Status": "New"}
    client = FakeZoho(has_contact=True, alert=alert)
    result = AccountWithoutContactRule(client, settings()).run()
    assert result.alerts_resolved == 1
    assert client.updated[0][0] == "900"
    assert client.updated[0][1]["Status"] == "Resolved"
