from __future__ import annotations

import time
from typing import Any

import httpx

from .config import Settings


class ZohoAPIError(RuntimeError):
    pass


class ZohoClient:
    _MAX_REQUEST_ATTEMPTS = 3
    _MAX_PAGINATION_REQUESTS = 10_000

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self._client = httpx.Client(timeout=30, transport=transport)
        self._access_token: str | None = None

    def close(self) -> None:
        self._client.close()

    def _refresh_access_token(self) -> str:
        response = self._client.post(
            f"{self.settings.zoho_accounts_url}/oauth/v2/token",
            data={
                "refresh_token": self.settings.zoho_refresh_token,
                "client_id": self.settings.zoho_client_id,
                "client_secret": self.settings.zoho_client_secret,
                "grant_type": "refresh_token",
            },
        )
        self._raise_for_error(response)
        payload = response.json()
        self._access_token = payload["access_token"]
        # Zoho returns the correct regional API domain; prefer it when present.
        self.settings.zoho_api_domain = payload.get(
            "api_domain", self.settings.zoho_api_domain
        )
        return self._access_token

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if not self._access_token:
            self._refresh_access_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Zoho-oauthtoken {self._access_token}"
        url = f"{self.settings.zoho_api_domain}/crm/v8/{path.lstrip('/')}"
        refreshed = False

        for attempt in range(self._MAX_REQUEST_ATTEMPTS):
            response = self._client.request(method, url, headers=headers, **kwargs)
            if response.status_code == 401 and not refreshed:
                headers["Authorization"] = (
                    f"Zoho-oauthtoken {self._refresh_access_token()}"
                )
                refreshed = True
                response = self._client.request(method, url, headers=headers, **kwargs)

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt + 1 < self._MAX_REQUEST_ATTEMPTS:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = min(float(retry_after), 30.0) if retry_after else 2**attempt
                    except ValueError:
                        delay = 2**attempt
                    time.sleep(delay)
                    continue

            self._raise_for_error(response)
            return response.json() if response.content else {}

        raise ZohoAPIError("Zoho API request retry limit exceeded")

    @staticmethod
    def _raise_for_error(response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        raise ZohoAPIError(f"Zoho API {response.status_code}: {detail}")

    def _get_paginated_records(
        self, path: str, fields: list[str] | None = None
    ) -> list[dict]:
        records: list[dict] = []
        page = 1
        page_token: str | None = None
        seen_tokens: set[str] = set()

        for _ in range(self._MAX_PAGINATION_REQUESTS):
            params: dict[str, Any] = {"per_page": 200}
            params["fields"] = ",".join(fields) if fields else "id"
            if page_token:
                params["page_token"] = page_token
            else:
                params["page"] = page

            payload = self._request("GET", path, params=params)
            records.extend(payload.get("data", []))
            info = payload.get("info", {})
            if not info.get("more_records", False):
                return records

            if page_token or page >= 10:
                next_token = info.get("next_page_token")
                if not next_token:
                    raise ZohoAPIError(
                        "Zoho pagination indicated more records but returned no "
                        "next_page_token"
                    )
                if next_token in seen_tokens:
                    raise ZohoAPIError("Zoho pagination returned a repeated page token")
                seen_tokens.add(next_token)
                page_token = next_token
            else:
                page += 1

        raise ZohoAPIError("Zoho pagination safety limit exceeded")

    def get_all_records(self, module: str, fields: list[str] | None = None) -> list[dict]:
        return self._get_paginated_records(module, fields)

    def get_related_records(
        self,
        module: str,
        record_id: str,
        related_list: str,
        fields: list[str] | None = None,
    ) -> list[dict]:
        return self._get_paginated_records(
            f"{module}/{record_id}/{related_list}", fields
        )

    def has_related_records(
        self, module: str, record_id: str, related_list: str
    ) -> bool:
        payload = self._request(
            "GET",
            f"{module}/{record_id}/{related_list}",
            params={"page": 1, "per_page": 1, "fields": "id"},
        )
        return bool(payload.get("data"))

    def create_record(self, module: str, record: dict[str, Any]) -> str:
        payload = self._request("POST", module, json={"data": [record]})
        result = payload["data"][0]
        if result.get("status") != "success":
            raise ZohoAPIError(f"Create failed: {result}")
        return result["details"]["id"]

    def update_record(self, module: str, record_id: str, changes: dict[str, Any]) -> None:
        payload = self._request(
            "PUT", module, json={"data": [{"id": record_id, **changes}]}
        )
        result = payload["data"][0]
        if result.get("status") != "success":
            raise ZohoAPIError(f"Update failed: {result}")
