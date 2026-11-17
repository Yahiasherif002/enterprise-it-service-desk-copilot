import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing

import azure.functions as func
import pyodbc

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

CATEGORIES = ("hardware", "software", "network", "database", "inquiry")
NEEDS_APPROVAL = {"software"}
REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,100}")
EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]+\.[^@\s]+")
SERVICENOW_ERRORS = (OSError, ValueError, KeyError, TypeError)


def respond(body, status=200):
    return func.HttpResponse(json.dumps(body), status_code=status, mimetype="application/json")


def caller(req):
    principal = req.headers.get("x-ms-client-principal-name") or req.headers.get("x-ms-client-principal-id")
    if principal:
        return principal
    return "local" if os.environ.get("AZURE_FUNCTIONS_ENVIRONMENT") == "Development" else None


def json_body(req):
    try:
        return req.get_json()
    except ValueError:
        return None


def connect():
    return closing(pyodbc.connect(os.environ["SQL_CONNECTION_STRING"]))


def servicenow(method, path, body=None):
    token = base64.b64encode(f"{os.environ['SERVICENOW_USER']}:{os.environ['SERVICENOW_PASSWORD']}".encode()).decode()
    request = urllib.request.Request(
        os.environ["SERVICENOW_URL"].rstrip("/") + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Basic {token}"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)["result"]


def ticket_url(sys_id):
    return f"{os.environ['SERVICENOW_URL'].rstrip('/')}/incident.do?sys_id={sys_id}" if sys_id else None


def audit(cur, request_id, event, actor, detail=None):
    cur.execute("INSERT INTO dbo.audit_event (request_id, event, actor, detail) VALUES (?, ?, ?, ?)",
                request_id, event, actor, detail)


def validate(body):
    if not isinstance(body, dict):
        return None, "Body must be a JSON object."
    try:
        data = {
            "request_id": body["request_id"],
            "requester": body["requester"].strip().lower(),
            "summary": body["summary"].strip(),
            "details": (body.get("details") or "").strip(),
            "category": body["category"].strip().lower(),
            "urgency": int(body.get("urgency", 3)),
            "impact": int(body.get("impact", 3)),
        }
    except (KeyError, AttributeError, TypeError, ValueError):
        return None, "request_id, requester, summary and category are required; urgency and impact are numbers."
    checks = [
        (isinstance(data["request_id"], str) and REQUEST_ID.fullmatch(data["request_id"]),
         "request_id must be 1-100 letters, digits, '.', '_', ':' or '-'."),
        (len(data["requester"]) <= 320 and EMAIL.fullmatch(data["requester"]), "requester must be an email address."),
        (0 < len(data["summary"]) <= 160, "summary must be 1-160 characters."),
        (len(data["details"]) <= 4000, "details must be at most 4000 characters."),
        (data["category"] in CATEGORIES, f"category must be one of: {', '.join(CATEGORIES)}."),
        (data["urgency"] in (1, 2, 3) and data["impact"] in (1, 2, 3), "urgency and impact must be 1, 2 or 3."),
    ]
    return data, next((message for ok, message in checks if not ok), None)


def create_ticket(conn, request_id, actor):
    cur = conn.cursor()
    cur.execute(
        "UPDATE dbo.service_request SET status = 'creating', updated_at = SYSUTCDATETIME() "
        "OUTPUT deleted.status, inserted.requester, inserted.summary, inserted.details, inserted.category, "
        "inserted.urgency, inserted.impact, inserted.approver "
        "WHERE request_id = ? AND (status IN ('ready', 'failed') "
        "OR (status = 'creating' AND updated_at < DATEADD(minute, -5, SYSUTCDATETIME())))", request_id)
    claimed = cur.fetchone()
    conn.commit()
    if not claimed:
        return
    previous, requester, summary, details, category, urgency, impact, approver = claimed
    description = f"Requested by {requester} through the IT Service Desk Copilot."
    if approver:
        description += f" Approved by {approver}."
    try:
        existing = [] if previous == "ready" else servicenow(
            "GET", "/api/now/table/incident?sysparm_fields=number,sys_id&sysparm_limit=1&sysparm_query="
            + urllib.parse.quote(f"correlation_id={request_id}"))
        ticket = existing[0] if existing else servicenow("POST", "/api/now/table/incident?sysparm_fields=number,sys_id", {
            "short_description": summary, "description": f"{description}\n\n{details}".strip(),
            "category": category, "urgency": str(urgency), "impact": str(impact),
            "contact_type": "self-service", "correlation_id": request_id})
        cur.execute("UPDATE dbo.service_request SET status = 'created', ticket_number = ?, ticket_sys_id = ?, "
                    "updated_at = SYSUTCDATETIME() WHERE request_id = ?", ticket["number"], ticket["sys_id"], request_id)
        audit(cur, request_id, "ticket_created", actor, ticket["number"])
    except SERVICENOW_ERRORS as e:
        reason = str(e)
        if isinstance(e, urllib.error.HTTPError):
            reason += ": " + e.read().decode(errors="replace")
        cur.execute("UPDATE dbo.service_request SET status = 'failed', updated_at = SYSUTCDATETIME() "
                    "WHERE request_id = ?", request_id)
        audit(cur, request_id, "ticket_failed", actor, reason[:4000])
    conn.commit()


def result(conn, request_id):
    cur = conn.cursor()
    cur.execute("SELECT status, ticket_number, ticket_sys_id FROM dbo.service_request WHERE request_id = ?", request_id)
    found = cur.fetchone()
    if not found:
        return respond({"error": "Request not found."}, 404)
    status, number, sys_id = found
    return respond({"request_id": request_id, "status": status, "ticket_number": number,
                    "ticket_url": ticket_url(sys_id)}, {"failed": 502, "creating": 503}.get(status, 200))


@app.route(route="requests", methods=["POST"])
def submit_request(req: func.HttpRequest) -> func.HttpResponse:
    actor = caller(req)
    if not actor:
        return respond({"error": "Authentication required."}, 401)
    data, error = validate(json_body(req))
    if error:
        return respond({"error": error}, 400)
    pending = data["category"] in NEEDS_APPROVAL
    with connect() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO dbo.service_request (request_id, requester, summary, details, category, urgency, impact, "
                "status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", data["request_id"], data["requester"], data["summary"],
                data["details"], data["category"], data["urgency"], data["impact"],
                "pending_approval" if pending else "ready")
            audit(cur, data["request_id"], "submitted", data["requester"], data["summary"])
            if pending:
                audit(cur, data["request_id"], "approval_requested", actor)
            conn.commit()
        except pyodbc.IntegrityError:
            conn.rollback()
        create_ticket(conn, data["request_id"], actor)
        return result(conn, data["request_id"])


@app.route(route="requests/{request_id}/decision", methods=["POST"])
def decide_request(req: func.HttpRequest) -> func.HttpResponse:
    actor = caller(req)
    if not actor:
        return respond({"error": "Authentication required."}, 401)
    body = json_body(req)
    body = body if isinstance(body, dict) else {}
    request_id, outcome = req.route_params["request_id"], body.get("outcome")
    approver = str(body.get("approver") or "").strip().lower()
    if outcome not in ("approved", "rejected") or not EMAIL.fullmatch(approver):
        return respond({"error": "outcome must be approved or rejected, and approver must be an email address."}, 400)
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE dbo.service_request SET status = ?, approver = ?, updated_at = SYSUTCDATETIME() "
                    "WHERE request_id = ? AND status = 'pending_approval'",
                    "ready" if outcome == "approved" else "rejected", approver, request_id)
        if cur.rowcount == 1:
            audit(cur, request_id, outcome, approver, str(body.get("comment") or "")[:4000] or None)
        conn.commit()
        create_ticket(conn, request_id, actor)
        return result(conn, request_id)


@app.route(route="requests", methods=["GET"])
def list_requests(req: func.HttpRequest) -> func.HttpResponse:
    if not caller(req):
        return respond({"error": "Authentication required."}, 401)
    requester = req.params.get("requester", "").strip().lower()
    if not EMAIL.fullmatch(requester):
        return respond({"error": "requester must be an email address."}, 400)
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT TOP 10 request_id, summary, category, status, ticket_number, ticket_sys_id, created_at "
                    "FROM dbo.service_request WHERE requester = ? ORDER BY created_at DESC, request_id", requester)
        rows = cur.fetchall()
        events = []
        if rows:
            cur.execute("SELECT request_id, event, actor, detail, at FROM dbo.audit_event WHERE request_id IN "
                        f"({', '.join('?' * len(rows))}) ORDER BY id", *[r.request_id for r in rows])
            events = cur.fetchall()
    live = {}
    sys_ids = [r.ticket_sys_id for r in rows if r.ticket_sys_id]
    if sys_ids:
        try:
            live = {t["sys_id"]: t for t in servicenow(
                "GET", "/api/now/table/incident?sysparm_display_value=true&sysparm_fields=sys_id,state,assigned_to"
                "&sysparm_query=" + urllib.parse.quote("sys_idIN" + ",".join(sys_ids)))}
        except SERVICENOW_ERRORS:
            pass
    return respond({"requests": [{
        "request_id": r.request_id,
        "summary": r.summary,
        "category": r.category,
        "status": r.status,
        "ticket_number": r.ticket_number,
        "ticket_url": ticket_url(r.ticket_sys_id),
        "ticket_state": live.get(r.ticket_sys_id, {}).get("state"),
        "assigned_to": (live.get(r.ticket_sys_id, {}).get("assigned_to") or {}).get("display_value"),
        "submitted_at": r.created_at.isoformat() + "Z",
        "history": [{"event": e.event, "actor": e.actor, "detail": e.detail, "at": e.at.isoformat() + "Z"}
                    for e in events if e.request_id == r.request_id],
    } for r in rows]})
