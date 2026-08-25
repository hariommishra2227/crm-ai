import httpx
import pytest

from app.config import Settings
from app.zoho import ZohoAPIError, ZohoClient


def _settings() -> Settings:
    return Settings(
        zoho_client_id="test-client",
        zoho_client_secret="test-secret",
        zoho_refresh_token="test-refresh",
    )


def _client(handler) -> ZohoClient:
    client = ZohoClient(_settings(), transport=httpx.MockTransport(handler))
    client._access_token = "test-access-token"
    return client


def _response(request, records, more=False, token=None, status=200):
    return httpx.Response(
        status,
        request=request,
        json={
            "data": records,
            "info": {"more_records": more, "next_page_token": token},
        },
    )


@pytest.mark.parametrize("total", [0, 200, 600, 2000])
def test_numeric_pagination_sizes(total):
    calls = []

    def handler(request):
        calls.append(request)
        page = int(request.url.params["page"])
        start = (page - 1) * 200
        count = max(0, min(200, total - start))
        records = [{"id": str(i)} for i in range(start, start + count)]
        return _response(request, records, start + count < total)

    client = _client(handler)
    records = client.get_all_records("Accounts", fields=["id"])
    client.close()

    assert len(records) == total
    assert len(calls) == max(1, (total + 199) // 200)
    assert all("page_token" not in call.url.params for call in calls)


@pytest.mark.parametrize("total", [2200, 10_000])
def test_page_tokens_fetch_beyond_2000(total):
    calls = []
    delivered = 0

    def handler(request):
        nonlocal delivered
        calls.append(request)
        count = min(200, total - delivered)
        records = [{"id": str(i)} for i in range(delivered, delivered + count)]
        delivered += count
        more = delivered < total
        token = f"token-{delivered}" if more and delivered >= 2000 else None
        return _response(request, records, more, token)

    client = _client(handler)
    records = client.get_all_records("Accounts", fields=["id", "Account_Name"])
    client.close()

    assert len(records) == total
    assert len(calls) == total // 200
    assert calls[9].url.params["page"] == "10"
    assert "page_token" not in calls[9].url.params
    assert calls[10].url.params["page_token"] == "token-2000"
    assert "page" not in calls[10].url.params
    assert all(call.url.params["fields"] == "id,Account_Name" for call in calls)


def test_repeated_page_token_raises_clear_error():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        token = "repeated-token" if calls >= 10 else None
        return _response(request, [{"id": str(calls)}], True, token)

    client = _client(handler)
    with pytest.raises(ZohoAPIError, match="repeated page token"):
        client.get_all_records("Accounts", fields=["id"])
    client.close()


def test_related_records_preserve_fields_id_and_support_tokens():
    calls = []

    def handler(request):
        calls.append(request)
        call_number = len(calls)
        token = "related-token" if call_number == 10 else None
        return _response(request, [{"id": str(call_number)}], call_number <= 10, token)

    client = _client(handler)
    records = client.get_related_records("Accounts", "123", "Contacts")
    client.close()

    assert len(records) == 11
    assert all(call.url.params["fields"] == "id" for call in calls)
    assert calls[10].url.params["page_token"] == "related-token"
    assert "page" not in calls[10].url.params


def test_related_existence_check_fetches_only_one_id():
    calls = []

    def handler(request):
        calls.append(request)
        return _response(request, [{"id": "1"}])

    client = _client(handler)
    assert client.has_related_records("Accounts", "123", "Contacts") is True
    client.close()
    assert len(calls) == 1
    assert calls[0].url.params["fields"] == "id"
    assert calls[0].url.params["per_page"] == "1"


def test_429_is_retried(monkeypatch):
    calls = 0
    delays = []

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, request=request, headers={"Retry-After": "0"})
        return _response(request, [{"id": "1"}])

    monkeypatch.setattr("app.zoho.time.sleep", delays.append)
    client = _client(handler)
    assert client.get_all_records("Accounts", fields=["id"]) == [{"id": "1"}]
    client.close()
    assert calls == 2
    assert delays == [0.0]


def test_permanent_400_is_not_retried(monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            400,
            request=request,
            json={"code": "INVALID_DATA", "message": "invalid data"},
        )

    monkeypatch.setattr("app.zoho.time.sleep", lambda _: pytest.fail("unexpected retry"))
    client = _client(handler)
    with pytest.raises(ZohoAPIError, match="INVALID_DATA"):
        client.get_all_records("Accounts", fields=["id"])
    client.close()
    assert calls == 1


def test_temporary_server_error_stops_at_retry_limit(monkeypatch):
    calls = 0
    delays = []

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503, request=request, text="temporarily unavailable")

    monkeypatch.setattr("app.zoho.time.sleep", delays.append)
    client = _client(handler)
    with pytest.raises(ZohoAPIError, match="503"):
        client.get_all_records("Accounts", fields=["id"])
    client.close()
    assert calls == 3
    assert delays == [1, 2]
