from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.primary_reconciliation import PrimaryAlertReconciler


def settings():
    return Settings(
        zoho_client_id="x", zoho_client_secret="x", zoho_refresh_token="x",
        required_account_profile_fields=["Account_Name", "Phone"],
        stale_account_days=30, stale_deal_days=21,
    )


def stamp(days=0):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class FakeZoho:
    def __init__(self, accounts, *, contacts=None, account_deals=True, account_quotes=True,
                 deals=None, deal_quotes=True, alerts=None):
        self.accounts, self.contacts = accounts, contacts or {}
        self.account_deals, self.account_quotes = account_deals, account_quotes
        self.deals, self.deal_quotes, self.alerts = deals or [], deal_quotes, alerts or []
        self.created, self.updated, self.read_calls = [], [], []

    def get_all_records(self, module, fields=None):
        self.read_calls.append((module, fields))
        return {"Accounts": self.accounts, "Deals": self.deals, "CRM_Alert": self.alerts}[module]

    def get_related_records(self, module, record_id, related_list, fields=None):
        self.read_calls.append((module, record_id, related_list))
        return self.contacts.get(record_id, [])

    def has_related_records(self, module, record_id, related_list):
        self.read_calls.append((module, record_id, related_list))
        if module == "Deals":
            return self.deal_quotes
        return self.account_deals if related_list == "Deals" else self.account_quotes

    def create_record(self, module, record):
        assert module == "CRM_Alert"
        self.created.append(record)
        return "new"

    def update_record(self, module, record_id, changes):
        assert module == "CRM_Alert"
        self.updated.append((record_id, changes))


def account(**changes):
    value = {"id": "A1", "Account_Name": "Acme", "Phone": "1", "Owner": {"id": "O1"},
             "Created_Time": stamp(50), "Modified_Time": stamp(1)}
    value.update(changes)
    return value


def test_no_contact_has_priority_over_incomplete_profile():
    client = FakeZoho([account(Phone=None)])
    result = PrimaryAlertReconciler(client, settings()).run()
    assert result["primary_no_contact"] == 1
    assert result["primary_incomplete_profile"] == 0


def test_contact_steps_and_at_least_one_valid_contact():
    no_contacts = FakeZoho([account()])
    assert PrimaryAlertReconciler(no_contacts, settings()).run()["primary_no_contact"] == 1
    incomplete = FakeZoho([account()], contacts={"A1": [{"Email": "a@b"}]})
    assert PrimaryAlertReconciler(incomplete, settings()).run()["primary_incomplete_contact"] == 1
    valid = FakeZoho([account()], contacts={"A1": [{"Phone": "1"}, {"Email": "a@b", "Phone": "2"}]},
                     account_deals=False)
    assert PrimaryAlertReconciler(valid, settings()).run()["primary_no_deal"] == 1


def test_incomplete_contact_has_priority_over_incomplete_profile():
    client = FakeZoho([account(Phone=None)], contacts={"A1": [{"Email": "a@b"}]})
    result = PrimaryAlertReconciler(client, settings()).run()
    assert result["primary_incomplete_contact"] == 1
    assert result["primary_incomplete_profile"] == 0


def test_incomplete_profile_has_priority_over_no_deal():
    contacts = {"A1": [{"Email": "a@b", "Phone": "2"}]}
    client = FakeZoho([account(Phone=None)], contacts=contacts, account_deals=False)
    result = PrimaryAlertReconciler(client, settings()).run()
    assert result["primary_incomplete_profile"] == 1
    assert result["primary_no_deal"] == 0
    assert not any(call == ("Accounts", "A1", "Deals") for call in client.read_calls)


def test_no_deal_has_priority_over_no_quote():
    contacts = {"A1": [{"Email": "a@b", "Phone": "2"}]}
    client = FakeZoho([account()], contacts=contacts, account_deals=False, account_quotes=False)
    result = PrimaryAlertReconciler(client, settings()).run()
    assert result["primary_no_deal"] == 1
    assert result["primary_no_quote"] == 0
    assert not any(call == ("Accounts", "A1", "Quotes") for call in client.read_calls)


def test_no_quote_has_priority_over_stale_account():
    contacts = {"A1": [{"Email": "a@b", "Phone": "2"}]}
    client = FakeZoho(
        [account(Modified_Time=stamp(100))], contacts=contacts, account_quotes=False
    )
    result = PrimaryAlertReconciler(client, settings()).run()
    assert result["primary_no_quote"] == 1
    assert result["primary_stale_account"] == 0


def test_no_quote_precedes_stale_and_healthy_has_no_create():
    contacts = {"A1": [{"Email": "a@b", "Phone": "2"}]}
    no_quote = FakeZoho([account(Modified_Time=stamp(100))], contacts=contacts, account_quotes=False)
    assert PrimaryAlertReconciler(no_quote, settings()).run()["primary_no_quote"] == 1
    healthy = FakeZoho([account()], contacts=contacts)
    result = PrimaryAlertReconciler(healthy, settings()).run()
    assert result["healthy_accounts"] == 1 and result["would_create"] == 0


def test_existing_canonical_updated_and_other_legacy_open_resolved():
    alerts = [
        {"id": "1", "Account": {"id": "A1"}, "Category": "No Contact", "Status": "Open",
         "Unique_Key": "ACCOUNT-A1-WITHOUT-CONTACT"},
        {"id": "2", "Account": {"id": "A1"}, "Category": "No Deal", "Status": "Open",
         "Unique_Key": "ACCOUNT-A1-WITHOUT-DEAL"},
    ]
    client = FakeZoho([account()], alerts=alerts)
    result = PrimaryAlertReconciler(client, settings(), dry_run=False).run()
    assert result["would_create"] == 0
    assert client.updated[0][1]["Unique_Key"] == "PRIMARY-ACCOUNT-A1"
    assert client.updated[1] == ("2", {"Status": "Resolved"})


def test_dry_run_never_writes_and_deal_key_is_separate():
    deal = {"id": "D1", "Deal_Name": "Big Deal", "Stage": "Open", "Created_Time": stamp(10),
            "Modified_Time": stamp(1), "Account_Name": {"id": "A1"}}
    client = FakeZoho([], deals=[deal], deal_quotes=False)
    result = PrimaryAlertReconciler(client, settings(), dry_run=True).run()
    assert result["would_create"] == 1
    assert result["sample_actions"][0]["unique_key"] == "PRIMARY-DEAL-D1"
    assert client.created == [] and client.updated == []
