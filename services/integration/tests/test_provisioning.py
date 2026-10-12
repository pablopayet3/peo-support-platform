import asyncio

from integration import provisioning
from integration.config import get_settings
from integration.main import app


def test_sidebar_app_gets_its_key_url_and_field_ids(client, fake_zd, monkeypatch):
    monkeypatch.setattr(get_settings(), "zendesk_app_installation_id", "360001")
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "northwind-integration.up.railway.app")
    result = asyncio.run(provisioning.sync_sidebar_app(app.state.zendesk))
    assert result == "updated"
    sent = fake_zd.app_settings_updates[-1]["settings"]
    assert sent["integration_api_key"] == "sidebar-key"          # the sidebar's key, never the agent's
    assert sent["integration_base_url"] == "https://northwind-integration.up.railway.app"
    assert sent["field_enrollment_id"] == 105


def test_nothing_is_sent_without_an_installation_id(client, fake_zd):
    assert asyncio.run(provisioning.sync_sidebar_app(app.state.zendesk)).startswith("skipped")
    assert fake_zd.app_settings_updates == []
