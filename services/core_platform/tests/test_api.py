from .conftest import INTEGRATION, READONLY


def test_health(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_requires_a_key(client):
    assert client.get("/v1/employees/E-1001").status_code == 401
    assert client.get("/v1/employees/E-1001", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_scope_is_enforced(client):
    r = client.get("/v1/people/lookup", params={"email": "maria.lopez@acmebakery.example"}, headers=READONLY)
    assert r.status_code == 403


def test_lookup_by_email_returns_every_company(client):
    r = client.get("/v1/people/lookup", params={"email": "sam.rivera@harborpine.example"}, headers=INTEGRATION)
    assert r.status_code == 200
    roles = {(m["company_name"], m["role"]) for m in r.json()["memberships"]}
    assert roles == {("Harbor & Pine Accounting", "admin"), ("Bluebird Design Studio", "employee")}


def test_lookup_unknown_is_404(client):
    r = client.get("/v1/people/lookup", params={"email": "someone@unknown.example"}, headers=INTEGRATION)
    assert r.status_code == 404


def test_admin_can_act_for_employee_of_same_company_only(client):
    ok = client.post("/v1/authorizations/check", json={"actor_person_id": "P-1002", "employee_id": "E-1001"},
                     headers=INTEGRATION).json()
    assert ok["allowed"] is True
    no = client.post("/v1/authorizations/check", json={"actor_person_id": "P-1002", "employee_id": "E-1004"},
                     headers=INTEGRATION).json()
    assert no["allowed"] is False


def test_coverage_has_no_sensitive_fields(client):
    body = client.get("/v1/employees/E-1001/coverage", headers=INTEGRATION).json()
    assert body["medical"]["plan_name"] == "Northwind Gold PPO"
    text = str(body).lower()
    for word in ("ssn", "social", "salary", "diagnosis"):
        assert word not in text


def test_evaluate_window(client):
    r = client.post("/v1/qle/evaluate", json={"employee_id": "E-1001", "event_type": "newborn",
                                              "event_date": "2026-10-06"}, headers=INTEGRATION)
    assert r.json() == {"window_open": True, "window_days": 30, "last_day": "2026-11-05",
                        "deadline": "2026-11-06T04:59:00Z"}


def test_future_event_date_is_rejected(client):
    r = client.post("/v1/qle/evaluate", json={"employee_id": "E-1001", "event_type": "newborn",
                                              "event_date": "2026-12-01"}, headers=INTEGRATION)
    assert r.status_code == 400


ENROLL = {"employee_id": "E-1001", "event_type": "newborn", "event_date": "2026-10-06",
          "dependent": {"first_name": "Sofia", "last_name": "Lopez", "date_of_birth": "2026-10-06"},
          "approved_by": "zendesk:900001", "source_ref": "zendesk-ticket:11"}


def test_enrollment_is_idempotent(client):
    h = {**INTEGRATION, "Idempotency-Key": "zd-ticket-11-approve"}
    first = client.post("/v1/enrollments", json=ENROLL, headers=h)
    assert first.status_code == 201
    again = client.post("/v1/enrollments", json=ENROLL, headers=h)
    assert again.status_code == 200
    assert again.json()["enrollment_id"] == first.json()["enrollment_id"]
    # same key, different data: refused
    other = {**ENROLL, "dependent": {**ENROLL["dependent"], "first_name": "Sara"}}
    assert client.post("/v1/enrollments", json=other, headers=h).status_code == 409


def test_enrollment_refused_without_active_plan(client):
    body = {**ENROLL, "employee_id": "E-1003", "source_ref": "zendesk-ticket:12"}
    r = client.post("/v1/enrollments", json=body, headers={**INTEGRATION, "Idempotency-Key": "zd-ticket-12-approve"})
    assert r.status_code == 409


def test_enrollment_refused_when_window_closed(client):
    body = {**ENROLL, "employee_id": "E-1004", "event_date": "2026-09-01", "source_ref": "zendesk-ticket:13",
            "dependent": {**ENROLL["dependent"], "date_of_birth": "2026-09-01"}}
    r = client.post("/v1/enrollments", json=body, headers={**INTEGRATION, "Idempotency-Key": "zd-ticket-13-approve"})
    assert r.status_code == 409


def test_admin_screens_need_a_password_and_render(client):
    assert client.get("/admin").status_code == 401
    auth = ("admin", "test-admin")
    for path in ["/admin", "/admin/employees", "/admin/employees/E-1001", "/admin/enrollments",
                 "/admin/billing", "/admin/audit"]:
        r = client.get(path, auth=auth)
        assert r.status_code == 200, path
    assert "charged 2 times" in client.get("/admin/billing", auth=auth).text


def test_short_or_unresolved_keys_never_match(monkeypatch):
    from core_platform.config import Settings
    s = Settings(api_keys='{"": {"scopes": ["benefits:read"]}, "${{shared.X}}": {}, "%s": {}}' % ("k" * 40),
                 min_key_length=32)
    assert list(s.keys()) == ["k" * 40]


def test_postgres_url_gets_the_driver():
    from core_platform.config import Settings
    assert Settings(database_url="postgresql://u:p@h:5432/db").sqlalchemy_url == "postgresql+psycopg://u:p@h:5432/db"
    assert Settings(database_url="postgres://u:p@h/db").sqlalchemy_url == "postgresql+psycopg://u:p@h/db"
