from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    zoho_accounts_url: str = "https://accounts.zoho.in"
    zoho_api_domain: str = "https://www.zohoapis.in"

    zoho_client_id: str
    zoho_client_secret: str
    zoho_refresh_token: str

    # Zoho Modules
    zoho_accounts_module: str = "Accounts"
    zoho_deals_module: str = "Deals"
    zoho_alerts_module: str = "CRM_Alert"
    # Related Lists
    zoho_account_deals_related_list: str = "Deals"
    zoho_account_quotes_related_list: str = "Quotes"
    zoho_deal_quotes_related_list: str = "Quotes"
    # Rule Settings
    stale_account_days: int = 30
    stale_deal_days: int = 21
    required_account_profile_fields: list[str] = [
        "Account_Name",
        "Phone",
        "Website",
        "Industry",
        "Billing_City",
        "Billing_State",
        "Billing_Country",
    ]
    # CRM Alerts Field API Names
    alert_name_field: str = "Name"
    alert_category_field: str = "Category"
    alert_account_field: str = "Account"
    alert_deal_field: str = "Deal"
    alert_responsible_owner_field: str = "Owner"

    alert_severity_field: str = "Severity"
    alert_status_field: str = "Status"

    alert_inactive_days_field: str = "Inactive_Days"
    alert_recommended_action_field: str = "Recommended_Action"
    alert_generated_on_field: str = "Generated_On"
    alert_unique_key_field: str = "Unique_Key"
    alert_days_open_field: str | None = None

    # Keep optional fields disabled until created in Zoho
    alert_description_field: str | None = None
    alert_resolved_on_field: str | None = None

    # Database
    database_url: str = "sqlite:///./crm_intelligence.db"

    # Scheduler
    schedule_enabled: bool = False
    schedule_hour_ist: int = 8


@lru_cache
def get_settings() -> Settings:
    return Settings()
