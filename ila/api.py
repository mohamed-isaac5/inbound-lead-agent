"""HTTP API and static UI. Binds to localhost by default. See README for exposure and token settings."""
import hmac
import json
import os
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, demo, metrics
from .adapters import GHLAdapter, SimChannel, SimCRM, verify_ghl_signature
from .chat import SetupChat
from .clock import clock
from .db import DB
from .engine import Engine
from .profile import Profile, load_profile, save_profile, validate_profile
from .qualify import ModelExtractor

STATIC = Path(__file__).parent / "static"


def build_engine(db_path=None, channel=None, crm=None) -> Engine:
    db = DB(db_path)
    ghl = GHLAdapter.from_env() if os.environ.get("ILA_USE_GHL") == "1" else None
    channel = channel or ghl or SimChannel()
    crm = crm or ghl or SimCRM()
    eng = Engine(db, channel, crm, extractor=ModelExtractor() if ModelExtractor.available() else None)
    return eng


def _jl(row, *keys):
    for k in keys:
        if row.get(k) is not None and isinstance(row[k], str):
            try:
                row[k] = json.loads(row[k])
            except Exception:
                pass
    return row


MAX_BODY = 256 * 1024
MAX_FIELD = 200
MAX_TEXT = 8192


def create_app(db_path=None, channel=None, crm=None, recover=True) -> FastAPI:
    app = FastAPI(title="Inbound Lead Agent", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    eng = build_engine(db_path, channel, crm)
    app.state.engine = eng
    chat = SetupChat(eng.db)
    if recover:
        eng.recover()

    admin_token = os.environ.get("ILA_ADMIN_TOKEN")

    allowed_hosts = {"localhost", "127.0.0.1", "[::1]", "::1", "testserver"} | {
        h.strip().lower() for h in os.environ.get("ILA_ALLOWED_HOSTS", "").split(",") if h.strip()}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        # DNS-rebinding defence: only answer for hosts the operator expects.
        host = (request.headers.get("host") or "").lower()
        host = host.rsplit(":", 1)[0] if not host.endswith("]") and ":" in host else host
        if host not in allowed_hosts:
            return JSONResponse({"error": "host not allowed. Set ILA_ALLOWED_HOSTS to the name you use to reach this app"}, status_code=400)
        try:
            clen = int(request.headers.get("content-length") or 0)
        except ValueError:
            clen = 0
        if clen > MAX_BODY:
            return JSONResponse({"error": f"request body too large (limit {MAX_BODY // 1024} KB)"}, status_code=413)
        open_paths = ("/api/webhooks/", "/api/intake/")
        if path.startswith("/api/") and not path.startswith(("/api/docs", "/api/openapi")):
            if admin_token and not path.startswith(open_paths):
                if not hmac.compare_digest(request.headers.get("x-admin-token", ""), admin_token):
                    return JSONResponse({"error": "admin token required"}, status_code=401)
            # Cross-site request defence: browsers cannot add a custom header to a simple cross-site POST.
            origin = request.headers.get("origin")
            if origin and request.method not in ("GET", "HEAD", "OPTIONS") and not path.startswith(open_paths):
                from urllib.parse import urlparse
                oh = (urlparse(origin).hostname or "").lower()
                if oh not in {h.strip("[]") for h in allowed_hosts}:
                    return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
            if request.method not in ("GET", "HEAD", "OPTIONS") and not path.startswith(open_paths):
                if not request.headers.get("x-ila-client") and not (admin_token and request.headers.get("x-admin-token")):
                    return JSONResponse({"error": "missing X-ILA-Client header"}, status_code=403)
            if path.startswith("/api/demo/") and not demo_enabled():
                return JSONResponse({"error": "demo routes are disabled when a live CRM or channel is configured"}, status_code=403)
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; connect-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"
        return resp

    def check_limits(payload: dict):
        for k, v in payload.items():
            if isinstance(v, str):
                lim = MAX_TEXT if k in ("message", "text", "body") else MAX_FIELD
                if len(v) > lim:
                    raise HTTPException(413, f"field '{k}' is too long (limit {lim} characters)")

    def demo_enabled():
        return isinstance(eng.channel, SimChannel) and isinstance(eng.crm, SimCRM)

    def adapters_status():
        sec = config.secret_status()
        return {
            "channel": getattr(eng.channel, "name", "?"), "crm": getattr(eng.crm, "name", "?"),
            "ghl_live_enabled": os.environ.get("ILA_USE_GHL") == "1",
            "ghl_credentials_present": sec["ghl_token_present"] and sec["ghl_location_present"],
            "ghl_status": "implemented, tested with a mocked transport only, never run against a live account",
            "model": "configured (never run against the live API)" if ModelExtractor.available() else "offline rules (no model configured)",
            "model_key_present": sec["model_key_present"], "model_name_present": sec["model_name_present"],
            "admin_token_set": bool(admin_token), "intake_secret_set": sec["intake_secret_present"],
        }

    # ----- status -----
    @app.get("/api/status")
    def status():
        p = load_profile(eng.db)
        counts = {r["status"]: r["c"] for r in eng.db.q("SELECT status, COUNT(*) c FROM leads GROUP BY status")}
        return {"version": "0.1.0", "clock": {"simulated": clock.simulated, "now": clock.now()}, "kill_switch": eng.paused(),
                "profile_configured": p.configured, "profile": p.to_dict(), "lead_status_counts": counts,
                "pending_review": eng.db.one("SELECT COUNT(*) c FROM actions WHERE status='pending_review'")["c"],
                "open_handoffs": eng.db.one("SELECT COUNT(*) c FROM handoffs WHERE status IN ('open','escalated')")["c"],
                "integrations": adapters_status(),
                "worker_health": eng.db.get_setting("worker_health", {"ok": True, "note": "no ticks yet"})}

    @app.get("/api/metrics")
    def get_metrics():
        return metrics.compute(eng.db, clock.now(), clock.simulated)

    # ----- leads -----
    @app.get("/api/leads")
    def leads(status: Optional[str] = None, tier: Optional[str] = None, q: Optional[str] = None):
        rows = eng.db.q("SELECT * FROM leads ORDER BY id DESC")
        out = []
        for r in rows:
            r = _jl(r, "tier_reasons", "missing", "flags")
            r["effective_tier"] = r["tier_override"] or r["tier"]
            if status and r["status"] != status:
                continue
            if tier and r["effective_tier"] != tier:
                continue
            if q and q.lower() not in f"{r['first_name']} {r['last_name']} {r['company']} {r['email']}".lower():
                continue
            h = eng.db.one("SELECT status FROM handoffs WHERE lead_id=?", (r["id"],))
            r["handoff_status"] = h["status"] if h else None
            out.append(r)
        return out

    @app.get("/api/leads/{lead_id}")
    def lead_detail(lead_id: int):
        lead = eng.lead(lead_id)
        if not lead:
            raise HTTPException(404, "lead not found")
        lead["effective_tier"] = lead["tier_override"] or lead["tier"]
        h = eng.db.one("SELECT * FROM handoffs WHERE lead_id=?", (lead_id,))
        return {"lead": lead, "fields": eng.fields(lead_id, live_only=False),
                "messages": eng.db.q("SELECT * FROM messages WHERE lead_id=? ORDER BY id", (lead_id,)),
                "actions": eng.db.q("SELECT * FROM actions WHERE lead_id=? ORDER BY id", (lead_id,)),
                "followups": eng.db.q("SELECT * FROM followups WHERE lead_id=? ORDER BY step", (lead_id,)),
                "handoff": _jl(h, "card") if h else None,
                "audit": [_jl(a, "detail") for a in eng.db.q("SELECT * FROM audit WHERE lead_id=? ORDER BY id", (lead_id,))]}

    @app.post("/api/intake/form")
    async def intake(request: Request, payload: dict = Body(...)):
        secret = os.environ.get(config.INTAKE_SECRET_ENV)
        if secret:
            if not hmac.compare_digest(request.headers.get("x-intake-secret", ""), secret):
                raise HTTPException(401, "bad intake secret")
        elif request.client and request.client.host not in ("127.0.0.1", "::1", "testclient"):
            raise HTTPException(403, "set ILA_INTAKE_SECRET to accept intake from other hosts")
        check_limits(payload)
        return eng.ingest_lead(payload, source="form", external_id=payload.pop("external_id", None))

    @app.post("/api/webhooks/ghl")
    async def ghl_webhook(request: Request):
        raw = await request.body()
        sig = request.headers.get("x-ghl-signature")
        if not verify_ghl_signature(raw, sig):
            raise HTTPException(401, "invalid or missing X-GHL-Signature")
        try:
            data = json.loads(raw)
        except ValueError:
            raise HTTPException(400, "invalid JSON")
        kind, wid = data.get("type"), data.get("webhookId") or data.get("id") or data.get("messageId")
        if kind == "ContactCreate":
            tags = data.get("tags") or []
            payload = {"first_name": data.get("firstName"), "last_name": data.get("lastName"), "email": data.get("email"),
                       "phone": data.get("phone"), "company": data.get("companyName"), "consent": False,
                       "dnd": bool(data.get("dnd"))}
            payload = {k: (v[:MAX_FIELD] if isinstance(v, str) else v) for k, v in payload.items()}
            res = eng.ingest_lead(payload, source="ghl", external_id=f"contact-{data.get('id')}")
            if res.get("lead_id") and data.get("id"):
                eng.db.run("UPDATE leads SET crm_contact_id=? WHERE id=?", (data["id"], res["lead_id"]))
            return {"received": True, "result": res, "note": "consent is not inferred from a CRM contact"}
        if kind == "InboundMessage" and data.get("direction", "inbound") == "inbound":
            lead = eng.db.one("SELECT id FROM leads WHERE crm_contact_id=?", (data.get("contactId"),))
            if not lead:
                return {"received": True, "result": "unknown contact, ignored"}
            res = eng.process_inbound(lead["id"], data.get("body") or "", channel="ghl", external_id=f"msg-{data.get('messageId') or wid}")
            return {"received": True, "result": res}
        return {"received": True, "result": "event type ignored"}

    @app.post("/api/leads/{lead_id}/reply")
    def simulate_reply(lead_id: int, body: dict = Body(...)):
        check_limits(body)
        if not eng.lead(lead_id):
            raise HTTPException(404, "lead not found")
        return eng.process_inbound(lead_id, (body.get("text") or "").strip(), channel="sim-email")

    @app.post("/api/leads/{lead_id}/human-reply")
    def human_reply(lead_id: int, body: dict = Body(...)):
        check_limits(body)
        if not eng.lead(lead_id):
            raise HTTPException(404, "lead not found")
        if not (body.get("text") or "").strip():
            raise HTTPException(400, "A message is required")
        eng.record_human_message(lead_id, body.get("text"), author=body.get("author") or "human")
        return {"ok": True}

    @app.post("/api/leads/{lead_id}/override")
    def override(lead_id: int, body: dict = Body(...)):
        check_limits(body)
        if not eng.lead(lead_id):
            raise HTTPException(404, "lead not found")
        if not (body.get("reason") or "").strip():
            raise HTTPException(400, "A reason is required")
        if not eng.set_override(lead_id, body.get("tier"), body["reason"]):
            raise HTTPException(400, "invalid tier")
        return {"ok": True}

    # ----- review queue -----
    @app.get("/api/actions")
    def actions(status: str = "pending_review"):
        rows = eng.db.q("SELECT a.*, l.first_name, l.last_name, l.company, l.email, l.tier FROM actions a JOIN leads l ON l.id=a.lead_id "
                        "WHERE a.status=? ORDER BY a.id", (status,))
        return rows

    @app.post("/api/actions/{action_id}/approve")
    def approve(action_id: int, body: dict = Body(default={})):
        r = eng.approve(action_id, actor="operator", body=body.get("body"))
        if r.get("ok") is False and r.get("error") and r.get("status") is None:
            raise HTTPException(400, r["error"])
        return r

    @app.post("/api/actions/{action_id}/reject")
    def reject(action_id: int, body: dict = Body(default={})):
        return {"ok": eng.reject(action_id, body.get("reason", ""))}

    @app.post("/api/actions/{action_id}/resolve")
    def resolve(action_id: int, body: dict = Body(...)):
        r = eng.resolve_uncertain(action_id, body.get("resolution"), actor="operator", note=str(body.get("note") or ""))
        if r.get("ok") is False:
            raise HTTPException(400, r.get("error", "failed"))
        return r

    # ----- handoffs, followups, suppression -----
    @app.get("/api/handoffs")
    def handoffs():
        rows = eng.db.q("SELECT h.*, l.first_name, l.last_name, l.company FROM handoffs h JOIN leads l ON l.id=h.lead_id ORDER BY h.id DESC")
        return [_jl(r, "card") for r in rows]

    @app.post("/api/handoffs/{lead_id}/ack")
    def ack(lead_id: int):
        return {"ok": eng.ack_handoff(lead_id)}

    @app.get("/api/followups")
    def followups():
        return eng.db.q("SELECT f.*, l.first_name, l.last_name, l.company, l.status lead_status FROM followups f JOIN leads l ON l.id=f.lead_id "
                        "ORDER BY f.due_at")

    @app.get("/api/suppression")
    def suppression():
        return eng.db.q("SELECT kind, key, reason, created_at FROM suppression ORDER BY id DESC")

    @app.get("/api/audit")
    def audit(limit: int = 100):
        return [_jl(a, "detail") for a in eng.db.q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (min(limit, 500),))]

    @app.post("/api/tick")
    def tick():
        try:
            res = eng.tick()
            eng.db.set_setting("worker_health", {"ok": True, "last_tick": clock.now(), "result": res})
            return res
        except Exception as e:
            eng.db.set_setting("worker_health", {"ok": False, "error": type(e).__name__, "last_tick": clock.now()})
            raise

    @app.post("/api/kill-switch")
    def kill(body: dict = Body(...)):
        eng.set_kill_switch(bool(body.get("on")))
        return {"kill_switch": eng.paused()}

    @app.get("/api/profile")
    def get_profile():
        return load_profile(eng.db).to_dict()

    @app.put("/api/profile")
    def put_profile(body: dict = Body(...)):
        p = Profile.from_dict({**load_profile(eng.db).to_dict(), **body})
        err = validate_profile(p)
        if err:
            raise HTTPException(400, err)
        if not (p.company or "").strip() or not (p.offer or "").strip():
            raise HTTPException(400, "Company and offer are required")
        p.configured = True
        save_profile(eng.db, p)
        return p.to_dict()

    # ----- setup chat -----
    @app.get("/api/chat")
    def chat_get():
        chat.start_if_needed()
        return {"messages": chat.history(), "state": {k: v for k, v in chat.state().items() if k != "draft"}}

    @app.post("/api/chat")
    def chat_post(body: dict = Body(...)):
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "empty message")
        return {"messages": chat.say(text)}

    @app.post("/api/chat/reset")
    def chat_reset():
        chat.reset()
        return {"messages": chat.history()}

    # ----- demo lab -----
    @app.post("/api/demo/load")
    def demo_load():
        return demo.load(eng)

    @app.post("/api/demo/advance")
    def demo_advance(body: dict = Body(...)):
        if not clock.simulated:
            raise HTTPException(400, "the clock is real. Load the demo to use a simulated clock.")
        hours = float(body.get("hours", 1))
        if not (0 < hours <= 24 * 30):
            raise HTTPException(400, "hours out of range")
        clock.advance(hours * 3600)
        return {"now": clock.now(), "tick": eng.tick()}

    @app.post("/api/demo/channel-mode")
    def channel_mode(body: dict = Body(...)):
        if not isinstance(eng.channel, SimChannel):
            raise HTTPException(400, "only the simulated channel can be forced to fail")
        if body.get("mode") not in ("ok", "fail", "timeout_after_send", "timeout_before_send"):
            raise HTTPException(400, "invalid mode")
        eng.channel.mode = body["mode"]
        return {"mode": eng.channel.mode}

    @app.post("/api/demo/reset")
    def demo_reset():
        demo.reset_db(eng.db)
        clock.set_fixed(None)
        return {"ok": True}

    if STATIC.exists():
        app.mount("/assets", StaticFiles(directory=STATIC), name="assets")

        @app.get("/")
        def index():
            return FileResponse(STATIC / "index.html")

    return app


def main():
    import uvicorn
    host = os.environ.get("ILA_HOST", "127.0.0.1")
    port = int(os.environ.get("ILA_PORT", "8765"))
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
