import json
import os

os.environ["CORE_DATABASE_URL"] = "sqlite:///:memory:"
os.environ["CORE_FROZEN_TODAY"] = "2026-10-12"
os.environ["CORE_ADMIN_PASSWORD"] = "test-admin"
os.environ["CORE_MIN_KEY_LENGTH"] = "8"
os.environ["CORE_API_KEYS"] = json.dumps({
    "key-integration": {"client": "integration-service",
                        "scopes": ["identity:read", "benefits:read", "enrollments:write", "billing:read"]},
    "key-readonly": {"client": "reporting", "scopes": ["benefits:read"]},
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core_platform.main import app  # noqa: E402

INTEGRATION = {"Authorization": "Bearer key-integration"}
READONLY = {"Authorization": "Bearer key-readonly"}


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c
