import base64
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import azure.functions as func
import pyodbc
import pytest

import function_app as fa

SERVER = os.environ["SQL_SERVER"]
ADMIN = f"{SERVER};UID=sa;PWD={os.environ['MSSQL_SA_PASSWORD']}"
DB, APP_LOGIN, APP_PASSWORD = "servicedesk_test", "servicedesk_app_test", "App-test-only-1"
AUTH = "Basic " + base64.b64encode(b"svc_copilot:secret").decode()


class FakeServiceNow(BaseHTTPRequestHandler):
    incidents, fail_next, delay = [], None, 0

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.headers["Authorization"] != AUTH:
            return self.reply(401, {"error": {"message": "User Not Authenticated"}})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        time.sleep(FakeServiceNow.delay)
        mode, FakeServiceNow.fail_next = FakeServiceNow.fail_next, None
        if mode == "down":
            return self.reply(503, {"error": {"message": "Instance unavailable"}})
        incident = {**body, "number": f"INC{10001 + len(self.incidents):07d}", "sys_id": uuid4().hex,
                    "state": "New", "assigned_to": ""}
        self.incidents.append(incident)
        if mode == "lost":
            return self.reply(504, {"error": {"message": "Gateway timeout"}})
        self.reply(201, {"result": {"number": incident["number"], "sys_id": incident["sys_id"]}})

    def do_GET(self):
        if FakeServiceNow.fail_next == "down":
            return self.reply(503, {"error": {"message": "Instance unavailable"}})
        query = parse_qs(urlparse(self.path).query)["sysparm_query"][0]
        if query.startswith("correlation_id="):
            found = [i for i in self.incidents if i["correlation_id"] == query.removeprefix("correlation_id=")]
        else:
            found = [i for i in self.incidents if i["sys_id"] in query.removeprefix("sys_idIN").split(",")]
        self.reply(200, {"result": [{k: i[k] for k in ("number", "sys_id", "state", "assigned_to")} for i in found]})

    def log_message(self, *args):
        pass


def wait_for_sql():
    for _ in range(60):
        try:
            return pyodbc.connect(ADMIN + ";Database=master", autocommit=True)
        except pyodbc.Error:
            time.sleep(2)
    raise RuntimeError("SQL Server did not start")


@pytest.fixture(scope="session", autouse=True)
def environment():
    with wait_for_sql() as master:
        master.execute(f"IF DB_ID('{DB}') IS NOT NULL BEGIN ALTER DATABASE {DB} SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
                       f"DROP DATABASE {DB}; END")
        master.execute(f"CREATE DATABASE {DB}")
        master.execute(f"IF SUSER_ID('{APP_LOGIN}') IS NULL CREATE LOGIN {APP_LOGIN} WITH PASSWORD = '{APP_PASSWORD}'")
    with pyodbc.connect(f"{ADMIN};Database={DB}", autocommit=True) as db:
        db.execute((Path(__file__).parents[2] / "sql" / "schema.sql").read_text())
        db.execute(f"CREATE USER {APP_LOGIN} FOR LOGIN {APP_LOGIN}; ALTER ROLE servicedesk_app ADD MEMBER {APP_LOGIN}")
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeServiceNow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ.update({
        "SQL_CONNECTION_STRING": f"{SERVER};Database={DB};UID={APP_LOGIN};PWD={APP_PASSWORD}",
        "SERVICENOW_URL": f"http://127.0.0.1:{server.server_port}",
        "SERVICENOW_USER": "svc_copilot",
        "SERVICENOW_PASSWORD": "secret",
        "AZURE_FUNCTIONS_ENVIRONMENT": "Development",
    })
    yield
    server.shutdown()


@pytest.fixture
def servicenow():
    FakeServiceNow.incidents, FakeServiceNow.fail_next, FakeServiceNow.delay = [], None, 0
    return FakeServiceNow


def call(handler, method="POST", body=None, params=None, route_params=None, headers=None):
    req = func.HttpRequest(method, "http://localhost/api/requests", headers=headers or {}, params=params or {},
                           route_params=route_params or {}, body=b"" if body is None else json.dumps(body).encode())
    response = handler.build().get_user_function()(req)
    return response.status_code, json.loads(response.get_body())


def submit(**overrides):
    body = {"request_id": uuid4().hex, "requester": f"Mona.{uuid4().hex[:8]}@Contoso.com",
            "summary": "Laptop will not boot", "details": "Black screen after the update.",
            "category": "hardware", "urgency": 2, "impact": 3} | overrides
    return body, *call(fa.submit_request, body=body)


def decide(request_id, outcome, approver="lead@contoso.com"):
    return call(fa.decide_request, body={"outcome": outcome, "approver": approver, "comment": "ok"},
                route_params={"request_id": request_id})


def history(requester):
    status, body = call(fa.list_requests, "GET", params={"requester": requester})
    assert status == 200
    return body["requests"]


def test_request_without_approval_creates_one_ticket_and_replays_idempotently(servicenow):
    body, status, out = submit()
    assert status == 200 and out["status"] == "created" and out["ticket_number"] == "INC0010001"
    incident, = servicenow.incidents
    assert incident["correlation_id"] == body["request_id"]
    assert (incident["category"], incident["urgency"], incident["impact"]) == ("hardware", "2", "3")
    assert body["requester"].lower() in incident["description"]
    assert out["ticket_url"].endswith(f"/incident.do?sys_id={incident['sys_id']}")

    _, status, again = submit(**body)
    assert status == 200 and again["ticket_number"] == out["ticket_number"] and len(servicenow.incidents) == 1


def test_software_request_waits_for_approval_then_creates_ticket(servicenow):
    body, status, out = submit(category="Software", summary="Install Visio")
    assert status == 200 and out["status"] == "pending_approval" and not servicenow.incidents

    status, out = decide(body["request_id"], "approved")
    assert status == 200 and out["status"] == "created"
    assert "Approved by lead@contoso.com." in servicenow.incidents[0]["description"]
    events = [e["event"] for e in history(body["requester"])[0]["history"]]
    assert events == ["submitted", "approval_requested", "approved", "ticket_created"]


def test_rejected_request_never_reaches_servicenow(servicenow):
    body, _, _ = submit(category="software")
    assert decide(body["request_id"], "rejected")[1]["status"] == "rejected"
    assert decide(body["request_id"], "approved")[1]["status"] == "rejected"
    assert not servicenow.incidents
    assert [e["actor"] for e in history(body["requester"])[0]["history"]][-1] == "lead@contoso.com"


def test_decision_for_unknown_request_is_404(servicenow):
    assert decide("missing-request", "approved")[0] == 404


def test_outage_fails_then_retry_creates_the_ticket(servicenow):
    servicenow.fail_next = "down"
    body, status, out = submit()
    assert status == 502 and out["status"] == "failed" and not servicenow.incidents

    _, status, out = submit(**body)
    assert status == 200 and out["status"] == "created" and len(servicenow.incidents) == 1


def test_retry_after_lost_response_adopts_the_ticket_instead_of_duplicating_it(servicenow):
    servicenow.fail_next = "lost"
    body, status, _ = submit()
    assert status == 502 and len(servicenow.incidents) == 1

    _, status, out = submit(**body)
    assert status == 200 and out["ticket_number"] == servicenow.incidents[0]["number"]
    assert len(servicenow.incidents) == 1
    events = [e["event"] for e in history(body["requester"])[0]["history"]]
    assert events == ["submitted", "ticket_failed", "ticket_created"]


def test_concurrent_retries_create_one_ticket(servicenow):
    servicenow.delay = 1
    body = {"request_id": uuid4().hex, "requester": "race@contoso.com", "summary": "VPN down", "category": "network"}
    statuses = []
    threads = [threading.Thread(target=lambda: statuses.append(call(fa.submit_request, body=body)[0]))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(statuses) == [200, 503] and len(servicenow.incidents) == 1


def test_list_shows_live_state_for_the_requesters_own_requests_only(servicenow):
    mine, _, _ = submit()
    submit()
    servicenow.incidents[0].update(state="In Progress", assigned_to={"display_value": "Beth Anglin"})

    item, = history(mine["requester"].upper())
    assert (item["status"], item["ticket_state"], item["assigned_to"]) == ("created", "In Progress", "Beth Anglin")
    assert [e["event"] for e in item["history"]] == ["submitted", "ticket_created"]


def test_list_still_answers_from_sql_when_servicenow_is_down(servicenow):
    mine, _, _ = submit()
    servicenow.fail_next = "down"
    item, = history(mine["requester"])
    assert item["ticket_number"] and item["ticket_state"] is None


@pytest.mark.parametrize("overrides", [
    {"category": "printer"},
    {"request_id": "a^b"},
    {"requester": "not-an-email"},
    {"summary": "   "},
    {"urgency": 5},
    {"impact": "high"},
])
def test_invalid_requests_are_rejected(servicenow, overrides):
    assert submit(**overrides)[1] == 400 and not servicenow.incidents


def test_non_json_body_is_rejected():
    req = func.HttpRequest("POST", "http://localhost/api/requests", body=b"not json")
    assert fa.submit_request.build().get_user_function()(req).status_code == 400


def test_requires_easy_auth_principal_outside_local_runs(servicenow, monkeypatch):
    monkeypatch.setenv("AZURE_FUNCTIONS_ENVIRONMENT", "Production")
    assert call(fa.list_requests, "GET", params={"requester": "a@contoso.com"})[0] == 401
    headers = {"x-ms-client-principal-name": "flow-owner@contoso.com"}
    assert call(fa.list_requests, "GET", params={"requester": "a@contoso.com"}, headers=headers)[0] == 200


def test_app_role_cannot_rewrite_audit_history(servicenow):
    submit()
    with pyodbc.connect(os.environ["SQL_CONNECTION_STRING"], autocommit=True) as db:
        for statement in ("DELETE FROM dbo.audit_event", "UPDATE dbo.audit_event SET actor = 'x'",
                          "DELETE FROM dbo.service_request"):
            with pytest.raises(pyodbc.ProgrammingError, match="permission was denied"):
                db.execute(statement)
