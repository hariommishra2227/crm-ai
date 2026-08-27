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


def reconcile_account(*, contact, profile_complete=True, deal=True, quote=True, stale=False):
    source = account(Modified_Time=stamp(100) if stale else stamp(1))
    if not profile_complete:
        source["Phone"] = None
    client = FakeZoho(
        [source], contacts={"A1": contact} if contact is not None else {},
        account_deals=deal, account_quotes=quote,
    )
    return client, PrimaryAlertReconciler(client, settings()).run()


def test_email_only_phone_only_and_both_are_valid_contacts():
    for contact in (
        [{"Email": "a@b", "Phone": None}],
        [{"Email": None, "Phone": "123"}],
        [{"Email": "a@b", "Phone": "123"}],
    ):
        _, result = reconcile_account(contact=contact, deal=False, quote=False)
        assert result["primary_no_deal"] == 1
        assert result["accounts_with_contact"] == 1
        assert result["accounts_with_incomplete_contact"] == 0


def test_blank_contact_is_incomplete_when_there_is_no_deal():
    _, result = reconcile_account(
        contact=[{"Email": None, "Phone": None}], deal=False, quote=False
    )
    assert result["primary_incomplete_contact"] == 1
    assert result["accounts_with_incomplete_contact"] == 1


def test_no_contact_or_incomplete_profile_with_deal_and_no_quote_is_no_quote():
    _, no_contact = reconcile_account(contact=None, deal=True, quote=False)
    _, incomplete_profile = reconcile_account(
        contact=[{"Email": "a@b"}], profile_complete=False, deal=True, quote=False
    )
    assert no_contact["primary_no_quote"] == 1
    assert incomplete_profile["primary_no_quote"] == 1


def test_incomplete_profile_with_deal_and_quote_is_healthy_when_active():
    _, result = reconcile_account(
        contact=[{"Phone": "123"}], profile_complete=False, deal=True, quote=True
    )
    assert result["healthy_accounts"] == 1
    assert result["primary_incomplete_profile"] == 0


def test_no_deal_classification_uses_contact_then_profile_facts():
    _, no_contact = reconcile_account(contact=None, deal=False, quote=False)
    _, incomplete_contact = reconcile_account(
        contact=[{"Email": None, "Phone": None}], deal=False, quote=False
    )
    _, incomplete_profile = reconcile_account(
        contact=[{"Email": "a@b"}], profile_complete=False, deal=False, quote=False
    )
    _, no_deal = reconcile_account(
        contact=[{"Phone": "123"}], deal=False, quote=False
    )
    assert no_contact["primary_no_contact"] == 1
    assert incomplete_contact["primary_incomplete_contact"] == 1
    assert incomplete_profile["primary_incomplete_profile"] == 1
    assert no_deal["primary_no_deal"] == 1


def test_deal_and_quote_use_staleness_for_final_classification():
    _, stale = reconcile_account(contact=None, deal=True, quote=True, stale=True)
    _, active = reconcile_account(contact=None, deal=True, quote=True, stale=False)
    assert stale["primary_stale_account"] == 1
    assert stale["accounts_stale"] == 1
    assert active["healthy_accounts"] == 1


def test_all_account_facts_are_collected_before_classification():
    client, result = reconcile_account(contact=None, profile_complete=False, deal=False, quote=True)
    assert result["healthy_accounts"] == 1
    assert result["accounts_without_contact"] == 1
    assert result["accounts_profile_incomplete"] == 1
    assert result["accounts_without_deal"] == 1
    assert result["accounts_with_quote"] == 1
    assert ("Accounts", "A1", "Deals") in client.read_calls
    assert ("Accounts", "A1", "Quotes") in client.read_calls


def test_existing_canonical_updated_and_other_legacy_open_resolved():
    alerts = [
        {"id": "1", "Account": {"id": "A1"}, "Category": "No Contact", "Status": "Open",
         "Unique_Key": "ACCOUNT-A1-WITHOUT-CONTACT"},
        {"id": "2", "Account": {"id": "A1"}, "Category": "No Deal", "Status": "Open",
         "Unique_Key": "ACCOUNT-A1-WITHOUT-DEAL"},
    ]
    client = FakeZoho([account()], account_deals=False, account_quotes=False, alerts=alerts)
    result = PrimaryAlertReconciler(client, settings(), dry_run=False).run()
    assert result["would_create"] == 0
    assert client.updated[0][1]["Unique_Key"] == "PRIMARY-ACCOUNT-A1"
    assert client.updated[1] == ("2", {"Status": "Resolved"})


def test_existing_primary_is_reused_on_second_reconciliation():
    primary = {
        "id": "1", "Account": {"id": "A1"}, "Category": "No Deal",
        "Status": "Open", "Unique_Key": "PRIMARY-ACCOUNT-A1",
    }
    contacts = {"A1": [{"Email": "a@b"}]}
    client = FakeZoho(
        [account()], contacts=contacts, account_deals=False, account_quotes=False,
        alerts=[primary],
    )
    first = PrimaryAlertReconciler(client, settings(), dry_run=True).run()
    second = PrimaryAlertReconciler(client, settings(), dry_run=True).run()
    assert first["would_create"] == second["would_create"] == 0
    assert first["would_update"] == second["would_update"] == 1
    assert first["would_resolve"] == second["would_resolve"] == 0


def test_account_dry_run_plans_one_primary_and_performs_zero_writes():
    client, result = reconcile_account(contact=None, deal=True, quote=False)
    assert result["would_create"] == 1
    assert result["would_update"] == result["would_resolve"] == 0
    assert result["sample_actions"][0]["unique_key"] == "PRIMARY-ACCOUNT-A1"
    assert client.created == [] and client.updated == []


def test_dry_run_never_writes_and_deal_key_is_separate():
    deal = {"id": "D1", "Deal_Name": "Big Deal", "Stage": "Open", "Created_Time": stamp(10),
            "Modified_Time": stamp(1), "Account_Name": {"id": "A1"}}
    client = FakeZoho([], deals=[deal], deal_quotes=False)
    result = PrimaryAlertReconciler(client, settings(), dry_run=True).run()
    assert result["would_create"] == 1
    assert result["sample_actions"][0]["unique_key"] == "PRIMARY-DEAL-D1"
    assert client.created == [] and client.updated == []
