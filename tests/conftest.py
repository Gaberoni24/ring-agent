import pytest

import app.tools.gcal as gcal_mod
import app.tools.ticktick as ticktick_mod
from app import db
from app.config import settings


@pytest.fixture(autouse=True)
def test_settings(tmp_path, monkeypatch):
    """Point all state at a temp dir and fill in dummy credentials so tool
    handlers don't short-circuit on 'not configured'."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "webhook_secret", "testsecret")
    monkeypatch.setattr(settings, "ticktick_api_token", "ticktick-dummy")
    monkeypatch.setattr(settings, "notion_token", "notion-dummy")
    monkeypatch.setattr(settings, "ntfy_topic", "test-topic")
    monkeypatch.setattr(settings, "anthropic_api_key", "anthropic-dummy")
    ticktick_mod._project_cache = None
    gcal_mod._creds = None
    db.init(settings.db_path)
    yield


def auth() -> dict:
    return {"Authorization": "Bearer testsecret"}
