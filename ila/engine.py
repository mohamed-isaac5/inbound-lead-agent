"""Core pipeline: intake, qualification, drafting, guarded sending, follow-ups, handoff.

Durability rules (each has a test):
  * Event idempotency: (source, external_id) is unique, so webhook retries and double submits are ignored.
  * Dedupe: one lead per normalized email or phone. A second submission never starts a second conversation.
  * Claim by rowcount: a conditional UPDATE decides the single winner of any race. A SELECT never does.
  * Crash recovery: rows stuck in 'processing' return to 'received' at startup and are processed once.
  * Send identity: every send attempt persists a send_key BEFORE the channel call. After a failure or timeout
    the engine reconciles against the channel first. If it cannot verify, it does not send.
  * Send-time guard: opt-out, consent, human takeover, kill switch, quiet hours, touch limits are checked
    immediately before sending, never only when the message was scheduled.
"""
import datetime as dt
import hashlib
import json
import re
import uuid
from typing import Optional
from zoneinfo import ZoneInfo

from . import normalize as nz
from . import qualify as ql
from . import templates as tpl
from .adapters import SendError, SendUncertain, SimChannel
from .clock import clock
from .profile import load_profile

TERMINAL = {"closed_not_fit", "opted_out", "no_consent", "declined_interest"}


class Engine:
    def __init__(self, db, channel, crm, extractor=None):
        self.db, self.channel, self.crm = db, channel, crm
        self.extractor = extractor  # optional ModelExtractor; None means offline rules only

    # ---------- helpers ----------
    @property
    def profile(self):
        return load_profile(self.db)

    def now(self):
        return clock.now()

    def audit(self, lead_id, actor, event, detail=None):
        self.db.run("INSERT INTO audit(ts,lead_id,actor,event,detail) VALUES(?,?,?,?,?)",
                    (self.now(), lead_id, actor, event, json.dumps(detail or {})))

    def lead(self, lead_id) -> Optional[dict]:
        r = self.db.one("SELECT * FROM leads WHERE id=?", (lead_id,))
        if r:
            for k in ("tier_reasons", "missing", "flags"):
                r[k] = json.loads(r[k] or "[]")
        return r

    def fields(self, lead_id, live_only=True):
        sql = "SELECT * FROM fields WHERE lead_id=?" + (" AND superseded=0" if live_only else "") + " ORDER BY id"
        return self.db.q(sql, (lead_id,))

    def paused(self) -> bool:
        return bool(self.db.get_setting("kill_switch", False))

    def effective_tier(self, lead):
        return lead.get("tier_override") or lead.get("tier")

    def add_flag(self, lead_id, flag):
        lead = self.lead(lead_id)
        if flag not in lead["flags"]:
            self.db.run("UPDATE leads SET flags=? WHERE id=?", (json.dumps(lead["flags"] + [flag]), lead_id))

    # ---------- fields & qualification ----------
    def add_fields(self, lead_id, new_fields, message_id=None):
        added = []
        live = self.fields(lead_id)
        for f in new_fields:
            same = [x for x in live if x["name"] == f["name"]]
            if any(x["value"] == f["value"] for x in same):
                continue
            conflict = False
            if f["name"] == "employees" and same:
                a, b = int(same[-1]["value"]), int(f["value"])
                conflict = min(a, b) > 0 and max(a, b) / min(a, b) > 3
            if same and not conflict:
                self.db.run("UPDATE fields SET superseded=1 WHERE lead_id=? AND name=? AND superseded=0",
                            (lead_id, f["name"]))
            self.db.run("INSERT INTO fields(lead_id,name,value,evidence,confidence,source,message_id,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (lead_id, f["name"], f["value"], f.get("evidence"), f.get("confidence"), f["source"],
                         message_id, self.now()))
            added.append(f["name"])
            live = self.fields(lead_id)
        return added

    def extract(self, text, source):
        out = ql.rule_extract(text, source)
        if self.extractor and ql.ModelExtractor.available():
            try:
                names = {f["name"] for f in out}
                out += [f for f in self.extractor.extract(text, source) if f["name"] not in names]
            except Exception as e:  # model failure must never block intake
                self.audit(None, "system", "model_extract_failed", {"error": type(e).__name__})
        return out

    def refresh(self, lead_id) -> dict:
        lead = self.lead(lead_id)
        facts = ql.current_facts(self.fields(lead_id))
        conflict = ql.size_conflict(self.fields(lead_id))
        used = {k: v for k, v in facts.items() if not (conflict and k == "employees")}   # a disputed size is not used by any rule
        q = ql.classify(used, self.profile, {**lead, "disposable_email": "disposable_email" in lead["flags"]})
        if conflict:
            q["reasons"] = [r for r in q["reasons"] if r != "Company size is unknown"]
            if q["tier"] == "not_fit":
                q["tier"] = "unknown"      # not-fit signals plus a disputed size: a human decides, nothing is auto-closed
                q["reasons"].insert(0, "Not-fit signals were found, but the company size is disputed, so nothing is closed automatically")
            q["conflicts"].append(conflict)
            self.add_flag(lead_id, "size_conflict")
            q["reasons"].insert(0, conflict + ". Neither number is treated as verified, so this tier is provisional until a human confirms the size")
        self.db.run("UPDATE leads SET tier=?, tier_reasons=?, missing=?, updated_at=? WHERE id=?",
                    (q["tier"], json.dumps(q["reasons"]), json.dumps(q["missing"]), self.now(), lead_id))
        q["facts"] = facts
        return q

    # ---------- intake ----------
    def ingest_lead(self, payload: dict, source="form", external_id: Optional[str] = None) -> dict:
        email = nz.normalize_email(payload.get("email"))
        phone = nz.normalize_phone(payload.get("phone"), self.profile.default_cc)
        if not email and not phone:
            return {"status": "rejected", "reason": "A valid email or phone is required"}
        ext = external_id or hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
        try:
            self.db.run("INSERT INTO events(source,external_id,kind,received_at) VALUES(?,?,?,?)",
                        (source, ext, "lead", self.now()))
        except Exception:
            self.audit(None, "system", "duplicate_event_ignored", {"source": source, "external_id": ext})
            return {"status": "duplicate_event"}
        ekey = nz.email_key(email)
        existing = self.db.one("SELECT id,status FROM leads WHERE (email_key=? AND email_key IS NOT NULL) "
                               "OR (phone_key=? AND phone_key IS NOT NULL)", (ekey, phone))
        payload = dict(payload)
        payload["message"] = ql.head_tail(payload.get("message") or "")   # same bound as the API: head plus tail
        msg_in = payload["message"]
        if existing:
            self.audit(existing["id"], "system", "duplicate_submission",
                       {"note": "same person submitted again; no new conversation started"})
            sig = ql.opt_out_signal(msg_in)
            if payload.get("dnd") is True or sig == "yes":
                self._opt_out(existing["id"], "resubmission with do-not-contact or an opt-out message")
                return {"status": "duplicate_lead", "lead_id": existing["id"], "opted_out": True}
            if payload.get("consent") is False or sig == "maybe":
                self.add_flag(existing["id"], "consent_unclear")
                self.cancel_pending(existing["id"], "consent unclear after resubmission")
                self.cancel_followups(existing["id"], "consent unclear after resubmission")
                self.handoff(existing["id"], "review", "The person submitted the form again without consent or with wording that may be a "
                             "request to stop. Nothing is sent until a human confirms.")
                return {"status": "duplicate_lead", "lead_id": existing["id"], "needs_review": True}
            self.add_fields(existing["id"], self._form_fields(payload, "form:resubmission"))
            self.refresh(existing["id"])
            return {"status": "duplicate_lead", "lead_id": existing["id"]}
        qual = nz.email_quality(email)
        flags = []
        if qual["disposable"]:
            flags.append("disposable_email")
        if qual["free_mail"]:
            flags.append("personal_email")
        consent = 1 if payload.get("consent") is True and not payload.get("dnd") else 0
        now = self.now()
        lead_id = self.db.run(
            "INSERT INTO leads(email,email_key,phone,phone_key,first_name,last_name,company,job_title,domain,source,"
            "status,consent,consent_source,consent_at,tz,flags,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (email, ekey, phone, phone, nz.clean_name(payload.get("first_name")), nz.clean_name(payload.get("last_name")),
             (payload.get("company") or "").strip(), (payload.get("job_title") or "").strip(),
             None if qual["free_mail"] else qual["domain"], source,
             "new" if consent else "no_consent", consent,
             payload.get("consent_source") or ("web form checkbox" if consent else None), now if consent else None,
             (payload.get("timezone") if nz.valid_tz(payload.get("timezone")) else None), json.dumps(flags), now, now))
        if payload.get("timezone") and not nz.valid_tz(payload.get("timezone")):
            self.audit(lead_id, "system", "invalid_timezone_ignored", {"note": "lead time zone not recognised; operator time zone used"})
            self.add_flag(lead_id, "invalid_timezone")
        if payload.get("dnd") is True:
            self._add_suppression(self.lead(lead_id), "do-not-contact flag on intake")
        self.audit(lead_id, "system", "lead_created", {"source": source, "consent": bool(consent), "flags": flags})
        self.db.run("INSERT INTO messages(lead_id,direction,channel,body,status,created_at,author) VALUES(?,?,?,?,?,?,?)",
                    (lead_id, "in", "form", payload.get("message") or "(no message)", "processed", now, "lead"))
        self.add_fields(lead_id, self._form_fields(payload, "form"))
        self._crm_sync(lead_id)
        if not consent:
            self.audit(lead_id, "system", "no_consent_no_outreach", {"reason": "no recorded permission to contact"})
            self.refresh(lead_id)
            return {"status": "created_no_consent", "lead_id": lead_id}
        self.refresh(lead_id)
        sup = self.suppressed(lead_id)
        if sup:
            self.db.run("UPDATE leads SET status='opted_out' WHERE id=?", (lead_id,))
            self.audit(lead_id, "system", "suppressed_on_intake", {"reason": sup})
            return {"status": "suppressed", "lead_id": lead_id}
        sig = ql.opt_out_signal(msg_in)
        if sig == "yes":
            self._opt_out(lead_id, "opt-out wording in the form message")
            return {"status": "opted_out", "lead_id": lead_id}
        if sig == "maybe":
            self.add_flag(lead_id, "possible_opt_out")
            self.handoff(lead_id, "review", "The form message may be a request to stop. Nothing is sent until a human confirms.")
            return {"status": "created", "lead_id": lead_id, "decision": "review"}
        if not email:
            self.handoff(lead_id, "review", "No email address on this lead and this build only sends email. A human should follow up by phone.")
            return {"status": "created", "lead_id": lead_id, "decision": "review"}
        if ql.detect_injection(payload.get("message") or ""):
            self.add_flag(lead_id, "suspicious_text")
            self.audit(lead_id, "system", "suspicious_text", {"note": "form message tried to instruct the agent; no automatic action"})
            self.handoff(lead_id, "review", "The form message contains text that tries to instruct the agent. Treated as data only.")
            return {"status": "created", "lead_id": lead_id, "decision": "review"}
        self._decide(lead_id)
        return {"status": "created", "lead_id": lead_id}

    def _form_fields(self, payload, source):
        out = []
        size = nz.parse_employee_count(payload.get("company_size"))
        if size:
            raw_size = str(payload.get("company_size")).strip()
            note = f" (range, lower bound {size} used)" if re.search(r"\d\s*[-\u2013]\s*\d|\+", raw_size) else ""
            conf, rng = 0.8, nz.parse_range(raw_size)
            if rng and rng[1] is not None and rng[0] < self.profile.min_employees <= rng[1]:
                conf = 0.5
                note += f"; the range straddles the minimum of {self.profile.min_employees}, so the real size is unknown"
            out.append({"name": "employees", "value": str(size), "evidence": f"form company size: {raw_size}{note}",
                        "confidence": conf, "source": source})
        title = payload.get("job_title") or ""
        for level, pat in ql.LEVEL_PATTERNS:
            m = re.search(pat, title.lower())
            if m:
                out.append({"name": "role_level", "value": level, "evidence": f"form job title: {title}",
                            "confidence": 0.85, "source": source})
                break
        if payload.get("inquiry_type") in ("demo", "pricing", "info"):
            out.append({"name": "intent", "value": payload["inquiry_type"],
                        "evidence": f"form inquiry type: {payload['inquiry_type']}", "confidence": 0.9, "source": source})
        if payload.get("message"):
            out += self.extract(payload["message"], source + ":message")
        return out

    def _crm_sync(self, lead_id):
        lead = self.lead(lead_id)
        try:
            cid = self.crm.upsert_contact(lead)
            self.db.run("UPDATE leads SET crm_contact_id=? WHERE id=?", (cid, lead_id))
        except Exception as e:
            self.add_flag(lead_id, "crm_sync_failed")
            self.audit(lead_id, "system", "crm_sync_failed", {"error": type(e).__name__})

    def _add_suppression(self, lead, reason):
        for kind, key in (("email", lead.get("email_key")), ("phone", lead.get("phone_key"))):
            if key:
                try:
                    self.db.run("INSERT INTO suppression(kind,key,reason,created_at) VALUES(?,?,?,?)", (kind, key, reason, self.now()))
                except Exception:
                    pass

    def _opt_out(self, lead_id, via):
        lead = self.lead(lead_id)
        self._add_suppression(lead, "opt-out: " + via)
        self.db.run("UPDATE leads SET status='opted_out', consent=0, updated_at=? WHERE id=?", (self.now(), lead_id))
        self.cancel_pending(lead_id, "opted out")
        self.cancel_followups(lead_id, "opted out")
        self.audit(lead_id, "system", "opted_out", {"via": via})

    def suppressed(self, lead_id) -> Optional[str]:
        lead = self.lead(lead_id)
        for kind, key in (("email", lead.get("email_key")), ("phone", lead.get("phone_key"))):
            if key and self.db.one("SELECT 1 x FROM suppression WHERE kind=? AND key=?", (kind, key)):
                return f"{kind} on suppression list"
        return None

    # ---------- decisions ----------
    def _decide(self, lead_id, trigger="intake"):
        lead = self.lead(lead_id)
        q = self.refresh(lead_id)
        lead = self.lead(lead_id)
        facts, tier = q["facts"], self.effective_tier(lead)
        profile = self.profile
        if q["conflicts"]:
            return self.handoff(lead_id, "review", "; ".join(q["conflicts"]) + ". A human should check the real size.")
        if tier == "not_fit":
            return self._close_not_fit(lead_id, q)
        if lead["status"] == "closed_not_fit":
            self.db.run("UPDATE leads SET status='qualifying' WHERE id=?", (lead_id,))
            self.audit(lead_id, "system", "reopened", {"reason": "new facts changed the classification"})
        if tier == "hot":
            return self.handoff(lead_id, "sales", "Hot: " + "; ".join(q["reasons"][:3]))
        if tier == "nurture":
            self.db.run("UPDATE leads SET status='nurture' WHERE id=?", (lead_id,))
            subj, body = tpl.nurture_draft(lead, facts, profile)
            self._draft(lead_id, "nurture_ack", "nurture-ack", subj, body)
            self._schedule_followup(lead_id, 9, 60)
            return {"decision": "nurture"}
        asked_all = self.db.one("SELECT COUNT(*) c FROM actions WHERE lead_id=? AND kind='question'", (lead_id,))["c"]
        asked_live = self.db.one("SELECT COUNT(*) c FROM actions WHERE lead_id=? AND kind='question' AND status IN "
                                 "('sent','pending_review','approved','sending','uncertain','failed')", (lead_id,))["c"]
        if q["missing"] and asked_live < profile.max_questions:
            field = q["missing"][0]
            subj, body = tpl.question_draft(lead, facts, profile, field, first=(lead["touches"] == 0))
            self._draft(lead_id, "question", f"q-{asked_all + 1}", subj, body, reason=f"asks about: {field}")
            self.db.run("UPDATE leads SET status='awaiting_reply' WHERE id=?", (lead_id,))
            return {"decision": "ask", "field": field}
        if tier == "warm":
            return self.handoff(lead_id, "sales", "Warm, question budget used. Unknowns: " + (", ".join(q["missing"]) or "none"))
        return self.handoff(lead_id, "review", "Not enough information after asking. A human should decide.")

    def _close_not_fit(self, lead_id, q):
        lead = self.lead(lead_id)
        self.db.run("UPDATE leads SET status='closed_not_fit' WHERE id=?", (lead_id,))
        self.cancel_followups(lead_id, "not fit")
        self.cancel_pending(lead_id, "not fit", keep_kinds=())
        size_reason = any("Company size" in r for r in q["reasons"])
        will_draft = bool(size_reason and lead["consent"] and self.profile.decline_policy == "size_only")
        self.audit(lead_id, "system", "closed_not_fit", {"reasons": q["reasons"], "no_message_sent": not will_draft,
                                                         "decline_policy": self.profile.decline_policy})
        if will_draft:
            subj, body = tpl.decline_draft(lead, self.profile)
            self._draft(lead_id, "decline", "decline", subj, body, reason="; ".join(q["reasons"]))
        return {"decision": "not_fit", "reasons": q["reasons"]}

    def handoff(self, lead_id, kind, reason):
        lead = self.lead(lead_id)
        facts = ql.current_facts(self.fields(lead_id))
        profile = self.profile
        msgs = self.db.q("SELECT direction,body,author FROM messages WHERE lead_id=? AND status IN ('processed','sent') "
                         "ORDER BY id DESC LIMIT 3", (lead_id,))
        missing = lead["missing"]
        opening = (f"Ask about {missing[0].replace('_', ' ')}" if missing
                   else f"Pick up on: {facts['use_case']['value'][:120]}" if "use_case" in facts else "Confirm needs and timeline")
        card = {"name": f"{lead['first_name']} {lead['last_name']}".strip(), "company": lead["company"],
                "job_title": lead["job_title"], "email": lead["email"], "phone": lead["phone"],
                "tier": self.effective_tier(lead), "reasons": lead["tier_reasons"], "unknown": missing,
                "kind": kind, "why_handed_off": reason, "suggested_opening": opening, "flags": lead["flags"],
                "facts": {k: {"value": v["value"], "evidence": v["evidence"]} for k, v in facts.items()},
                "recent_messages": list(reversed(msgs)), "consent": {"source": lead["consent_source"], "at": lead["consent_at"]}}
        disputed = ql.size_conflict(self.fields(lead_id))
        if disputed:
            card["facts"]["employees"] = {"value": "disputed", "evidence": disputed + ". Not verified."}
        now = self.now()
        sla_due = now + profile.handoff_sla_minutes * 60
        existing = self.db.one("SELECT id FROM handoffs WHERE lead_id=?", (lead_id,))
        if existing:
            self.db.run("UPDATE handoffs SET card=?, status='open', kind=? WHERE lead_id=?", (json.dumps(card), kind, lead_id))
        else:
            task_id = None
            try:
                if lead.get("crm_contact_id"):
                    due = dt.datetime.fromtimestamp(sla_due, dt.timezone.utc).isoformat().replace("+00:00", "Z")
                    task_id = self.crm.create_task(lead["crm_contact_id"], f"[{kind}] Follow up: {card['name'] or lead['email']}",
                                                   json.dumps({k: card[k] for k in ("tier", "why_handed_off", "suggested_opening")}),
                                                   due, None)
            except Exception as e:
                self.add_flag(lead_id, "crm_task_failed")
                self.audit(lead_id, "system", "crm_task_failed", {"error": type(e).__name__})
            self.db.run("INSERT INTO handoffs(lead_id,card,owner,status,kind,created_at,sla_due,crm_task_id) VALUES(?,?,?,?,?,?,?,?)",
                        (lead_id, json.dumps(card), profile.handoff_owner, "open", kind, now, sla_due, task_id))
        self.db.run("UPDATE leads SET status='handed_off', owner=? WHERE id=?", (profile.handoff_owner, lead_id))
        self.cancel_followups(lead_id, "handed off")
        self.cancel_pending(lead_id, "handed off", keep_kinds=("handoff_ack",))
        self.audit(lead_id, "agent", "handed_off", {"kind": kind, "reason": reason})
        if kind == "sales" and lead["consent"] and self.effective_tier(lead) in ("hot", "warm"):
            subj, body = tpl.ack_draft(lead, facts, profile)
            self._draft(lead_id, "handoff_ack", "handoff-ack", subj, body, reason="confirms the handoff to the lead")
        return {"decision": "handoff", "kind": kind}

    def ack_handoff(self, lead_id, actor="operator"):
        n = self.db.run_rowcount("UPDATE handoffs SET status='acknowledged', ack_at=? WHERE lead_id=? AND status IN ('open','escalated')",
                                 (self.now(), lead_id))
        if n:
            self.audit(lead_id, actor, "handoff_acknowledged", {})
        return bool(n)

    # ---------- drafting ----------
    def _draft(self, lead_id, kind, step_key, subject, body, reason=None):
        try:
            aid = self.db.run("INSERT INTO actions(lead_id,kind,step_key,subject,body,status,created_at,reason) "
                              "VALUES(?,?,?,?,?,?,?,?)", (lead_id, kind, step_key, subject, body, "pending_review", self.now(), reason))
        except Exception:
            return None  # same step already drafted: uniqueness makes drafting idempotent
        self.audit(lead_id, "agent", "draft_created", {"action_id": aid, "kind": kind})
        if self.profile.mode == "auto_followups" and kind in ("followup",):
            self.approve(aid, actor="auto_followups")
        return aid

    def cancel_pending(self, lead_id, reason, keep_kinds=()):
        rows = self.db.q("SELECT id,kind FROM actions WHERE lead_id=? AND status IN ('pending_review','approved')", (lead_id,))
        for r in rows:
            if r["kind"] in keep_kinds:
                continue
            self.db.run("UPDATE actions SET status='cancelled', reason=?, decided_at=? WHERE id=? AND status IN ('pending_review','approved')",
                        (reason, self.now(), r["id"]))
            self.audit(lead_id, "system", "draft_cancelled", {"action_id": r["id"], "reason": reason})

    # ---------- approval and guarded send ----------
    def reject(self, action_id, reason="", actor="operator"):
        n = self.db.run_rowcount("UPDATE actions SET status='rejected', reason=?, decided_by=?, decided_at=? "
                                 "WHERE id=? AND status='pending_review'", (reason or "rejected", actor, self.now(), action_id))
        if n:
            a = self.db.one("SELECT lead_id FROM actions WHERE id=?", (action_id,))
            self.audit(a["lead_id"], actor, "draft_rejected", {"action_id": action_id, "reason": reason})
        return bool(n)

    def approve(self, action_id, actor="operator", body=None) -> dict:
        act = self.db.one("SELECT * FROM actions WHERE id=?", (action_id,))
        if not act:
            return {"ok": False, "error": "not found"}
        if body is not None:
            if not body.strip():
                return {"ok": False, "error": "Empty message body"}
            edited = body.strip() != act["body"].strip()
            self.db.run("UPDATE actions SET body=? WHERE id=? AND status IN ('pending_review','approved','failed','uncertain')",
                        (body.strip(), action_id))
            if edited:
                self.audit(act["lead_id"], actor, "draft_edited", {"action_id": action_id})
        if act["status"] in ("pending_review",):
            self.db.run_rowcount("UPDATE actions SET status='approved', decided_by=?, decided_at=? WHERE id=? AND status='pending_review'",
                                 (actor, self.now(), action_id))
            self.audit(act["lead_id"], actor, "draft_approved", {"action_id": action_id})
        elif act["status"] not in ("approved", "failed", "uncertain"):
            return {"ok": False, "error": f"cannot approve an action that is {act['status']}"}
        return self.deliver(action_id)

    def _tz(self, lead=None):
        for name in ((lead or {}).get("tz"), self.profile.timezone):
            if name and nz.valid_tz(name):
                return ZoneInfo(name)
        return ZoneInfo("UTC")

    def in_window(self, ts, lead=None):
        p = self.profile
        tz = self._tz(lead)
        local = dt.datetime.fromtimestamp(ts, tz)
        if p.business_days_only and local.weekday() >= 5:
            return False
        return p.quiet_end_hour <= local.hour < p.quiet_start_hour

    def next_window(self, ts, lead=None):
        t = ts
        for _ in range(24 * 2 * 9):
            if self.in_window(t, lead):
                return t
            t += 1800
        return ts

    def guard(self, act, lead) -> tuple:
        """Return ('ok'|'defer'|'cancel'|'hold', reason, defer_until)."""
        p = self.profile
        if self.paused():
            return "hold", "kill switch is on", None
        if not isinstance(self.channel, SimChannel) and not (p.postal_address or "").strip():
            return "hold", "sender identity and postal address are missing in Settings (needed in the footer of real emails)", None
        if lead["status"] in TERMINAL and act["kind"] != "decline":
            return "cancel", f"lead is {lead['status']}", None
        if lead["status"] == "opted_out" or self.suppressed(lead["id"]):
            return "cancel", "opted out", None
        if not lead["consent"]:
            return "cancel", "no recorded consent", None
        if lead["paused_human"]:
            return "cancel", "a human has taken over this conversation", None
        if lead["status"] == "handed_off" and act["kind"] != "handoff_ack":
            return "cancel", "lead already handed off to a human", None
        if lead["last_inbound_at"] and lead["last_inbound_at"] > act["created_at"] and act["kind"] in ("followup", "question"):
            return "cancel", "lead replied after this draft was written", None
        if act["kind"] in ("followup", "question", "nurture_ack") and lead["touches"] >= p.max_touches:
            return "cancel", f"touch limit {p.max_touches} reached", None
        sent_24h = self.db.one("SELECT COUNT(*) c FROM actions WHERE status='sent' AND decided_at>?", (self.now() - 86400,))["c"]
        if sent_24h >= p.daily_send_cap:
            return "defer", "daily send cap reached", self.now() + 3600
        if not self.in_window(self.now(), lead):
            return "defer", "outside allowed sending hours", self.next_window(self.now(), lead)
        return "ok", "", None

    def deliver(self, action_id) -> dict:
        act = self.db.one("SELECT * FROM actions WHERE id=?", (action_id,))
        lead = self.lead(act["lead_id"])
        # Reconcile before any retry: an uncertain send may already have landed.
        if act["status"] == "uncertain" and act["send_key"]:
            try:
                mid = self.channel.find_sent(act["send_key"])
            except SendUncertain:
                self.audit(lead["id"], "system", "send_unverifiable",
                           {"action_id": action_id, "note": "channel cannot confirm delivery; not resending"})
                return {"ok": False, "status": "uncertain", "error": "Cannot verify whether the earlier send landed. Not resending."}
            if mid:
                self._mark_sent(act, lead, mid, note="already_landed")
                return {"ok": True, "status": "sent", "note": "already_landed"}
        verdict, why, until = self.guard(act, lead)
        if verdict == "cancel":
            self.db.run("UPDATE actions SET status='cancelled', reason=?, decided_at=? WHERE id=?", (why, self.now(), action_id))
            self.audit(lead["id"], "system", "send_blocked", {"action_id": action_id, "reason": why})
            return {"ok": False, "status": "cancelled", "error": why}
        if verdict in ("defer", "hold"):
            self.db.run("UPDATE actions SET not_before=?, status='approved', reason=? WHERE id=?", (until, why, action_id))
            self.audit(lead["id"], "system", "send_deferred", {"action_id": action_id, "reason": why})
            return {"ok": True, "status": "deferred", "reason": why, "until": until}
        final = tpl.ensure_footer(act["body"], self.profile)
        if final != act["body"]:
            self.db.run("UPDATE actions SET body=? WHERE id=?", (final, action_id))
            self.audit(lead["id"], "system", "footer_applied", {"action_id": action_id, "note": "STOP line and/or postal address added at send time"})
            act = dict(act, body=final)
        send_key = act["send_key"] or f"ila-{act['lead_id']}-{act['step_key']}-{uuid.uuid4().hex[:8]}"
        # persist the attempt identity BEFORE calling the channel
        won = self.db.run_rowcount(
            "UPDATE actions SET status='sending', send_key=?, send_state='attempting' WHERE id=? AND status IN ('approved','failed','uncertain')",
            (send_key, action_id))
        if won != 1:
            return {"ok": False, "status": "busy", "error": "another worker owns this send"}
        try:
            mid = self.channel.send(send_key, lead, act["subject"], act["body"])
        except SendError as e:
            self.db.run("UPDATE actions SET status='failed', send_state='failed', reason=? WHERE id=?", (str(e), action_id))
            self.audit(lead["id"], "system", "send_failed", {"action_id": action_id, "error": str(e)})
            return {"ok": False, "status": "failed", "error": str(e)}
        except SendUncertain as e:
            self.db.run("UPDATE actions SET status='uncertain', send_state='uncertain', reason=? WHERE id=?", (str(e), action_id))
            self.audit(lead["id"], "system", "send_uncertain", {"action_id": action_id, "error": str(e)})
            return {"ok": False, "status": "uncertain", "error": str(e)}
        act = self.db.one("SELECT * FROM actions WHERE id=?", (action_id,))
        self._mark_sent(act, lead, mid)
        return {"ok": True, "status": "sent"}

    def resolve_uncertain(self, action_id, resolution, actor="operator", note="") -> dict:
        """Operator exit for a send whose outcome could not be verified: confirmed_sent | resend | drop."""
        act = self.db.one("SELECT * FROM actions WHERE id=?", (action_id,))
        if not act:
            return {"ok": False, "error": "not found"}
        if act["status"] != "uncertain":
            return {"ok": False, "error": f"action is {act['status']}, not uncertain"}
        lead = self.lead(act["lead_id"])
        self.audit(lead["id"], actor, "uncertain_resolved", {"action_id": action_id, "resolution": resolution, "note": note[:300]})
        if resolution == "confirmed_sent":
            self._mark_sent(act, lead, "operator-confirmed", note="operator confirmed delivery out of band")
            return {"ok": True, "status": "sent"}
        if resolution == "drop":
            self.db.run("UPDATE actions SET status='cancelled', reason=?, decided_at=? WHERE id=?", ("operator dropped an unverified send", self.now(), action_id))
            return {"ok": True, "status": "cancelled"}
        if resolution == "resend":
            self.db.run("UPDATE actions SET status='approved', send_state=NULL, send_key=NULL, reason='operator chose to resend' WHERE id=? AND status='uncertain'", (action_id,))
            return self.deliver(action_id)
        return {"ok": False, "error": "resolution must be confirmed_sent, resend or drop"}

    def _mark_sent(self, act, lead, mid, note=None):
        now = self.now()
        self.db.run("UPDATE actions SET status='sent', send_state='sent', channel_message_id=?, decided_at=? WHERE id=?",
                    (mid, now, act["id"]))
        self.db.run("INSERT INTO messages(lead_id,direction,channel,body,status,created_at,sent_at,send_key,author,external_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (lead["id"], "out", getattr(self.channel, "name", "email"), act["body"], "sent", now, now,
                     act["send_key"], "agent", mid))
        sets = "touches=touches+1, last_outbound_at=?, updated_at=?"
        if act["kind"] == "question":
            sets += ", questions_asked=questions_asked+1"
        self.db.run(f"UPDATE leads SET {sets} WHERE id=?", (now, now, lead["id"]))
        self.db.run("UPDATE leads SET first_response_at=? WHERE id=? AND first_response_at IS NULL", (now, lead["id"]))
        self.audit(lead["id"], "agent", "message_sent", {"action_id": act["id"], "kind": act["kind"], "note": note})
        if act["kind"] in ("question",):
            for i, d in enumerate(self.profile.followup_days, start=1):
                self._schedule_followup(lead["id"], i, d)

    # ---------- follow-ups ----------
    def _schedule_followup(self, lead_id, step, days):
        due = self.now() + days * 86400
        # a finished or cancelled step from an earlier cycle must not block a new cycle
        self.db.run("DELETE FROM followups WHERE lead_id=? AND step=? AND status IN ('cancelled','done')", (lead_id, step))
        try:
            self.db.run("INSERT INTO followups(lead_id,step,due_at) VALUES(?,?,?)", (lead_id, step, due))
        except Exception:
            pass

    def cancel_followups(self, lead_id, reason):
        n = self.db.run_rowcount("UPDATE followups SET status='cancelled', cancel_reason=? WHERE lead_id=? AND status='scheduled'",
                                 (reason, lead_id))
        if n:
            self.audit(lead_id, "system", "followups_cancelled", {"reason": reason, "count": n})

    def tick(self) -> dict:
        now = self.now()
        out = {"sent_deferred": 0, "followups_drafted": 0, "followups_cancelled": 0, "escalated": 0}
        if self.paused():
            return {**out, "paused": True}
        for a in self.db.q("SELECT id FROM actions WHERE status='approved' AND (not_before IS NULL OR not_before<=?)", (now,)):
            r = self.deliver(a["id"])
            if r.get("status") == "sent":
                out["sent_deferred"] += 1
        for f in self.db.q("SELECT * FROM followups WHERE status='scheduled' AND due_at<=?", (now,)):
            lead = self.lead(f["lead_id"])
            if lead["status"] in TERMINAL or lead["status"] in ("handed_off",) or lead["paused_human"] or not lead["consent"] \
                    or self.suppressed(lead["id"]):
                self.db.run("UPDATE followups SET status='cancelled', cancel_reason=? WHERE id=?", (f"lead is {lead['status']}", f["id"]))
                out["followups_cancelled"] += 1
                continue
            if lead["touches"] >= self.profile.max_touches:
                self.db.run("UPDATE followups SET status='cancelled', cancel_reason='touch limit' WHERE id=?", (f["id"],))
                out["followups_cancelled"] += 1
                continue
            facts = ql.current_facts(self.fields(lead["id"]))
            missing = (lead["missing"] or [None])[0]
            subj, body = tpl.followup_draft(lead, facts, self.profile, f["step"], missing if f["step"] == 1 else None)
            aid = self._draft(lead["id"], "followup", f"followup-{f['step']}-t{lead['touches']}", subj, body, reason=f"no reply (follow-up {f['step']})")
            self.db.run("UPDATE followups SET status='done' WHERE id=?", (f["id"],))
            if aid:
                out["followups_drafted"] += 1
        for h in self.db.q("SELECT * FROM handoffs WHERE status='open' AND sla_due<? AND escalated_at IS NULL", (now,)):
            self.db.run("UPDATE handoffs SET status='escalated', escalated_at=? WHERE id=?", (now, h["id"]))
            self.audit(h["lead_id"], "system", "handoff_escalated", {"to": self.profile.escalate_to})
            out["escalated"] += 1
        return out

    # ---------- inbound replies ----------
    def process_inbound(self, lead_id, body, channel="email", external_id=None, author="lead") -> dict:
        lead = self.lead(lead_id)
        if not lead:
            return {"status": "unknown_lead"}
        body = ql.head_tail(body)
        ext = external_id or f"in-{uuid.uuid4().hex[:12]}"
        try:
            self.db.run("INSERT INTO events(source,external_id,kind,received_at) VALUES(?,?,?,?)", (channel, ext, "inbound", self.now()))
        except Exception:
            return {"status": "duplicate"}
        mid = self.db.run("INSERT INTO messages(lead_id,direction,channel,body,status,created_at,external_id,author) VALUES(?,?,?,?,?,?,?,?)",
                          (lead_id, "in", channel, body, "received", self.now(), ext, author))
        return self._process_message(mid)

    def _process_message(self, mid) -> dict:
        # The conditional UPDATE decides the single winner. A SELECT would not.
        if self.db.run_rowcount("UPDATE messages SET status='processing' WHERE id=? AND status='received'", (mid,)) != 1:
            return {"status": "duplicate"}
        msg = self.db.one("SELECT * FROM messages WHERE id=?", (mid,))
        lead_id, text = msg["lead_id"], msg["body"]
        lead = self.lead(lead_id)
        now = self.now()
        self.db.run("UPDATE leads SET last_inbound_at=?, updated_at=? WHERE id=?", (now, now, lead_id))
        self.cancel_followups(lead_id, "lead replied")
        self.cancel_pending(lead_id, "lead replied; draft is out of date", keep_kinds=("handoff_ack",))
        try:
            sig = ql.opt_out_signal(text)
            if sig == "yes":
                self._opt_out(lead_id, "reply")
                return {"status": "processed", "decision": "opted_out"}
            if sig == "maybe" and lead["status"] != "opted_out":
                self.add_flag(lead_id, "possible_opt_out")
                self.cancel_pending(lead_id, "possible opt-out; waiting for a human")
                self.audit(lead_id, "system", "possible_opt_out", {"note": "wording may be a request to stop; nothing is sent"})
                if lead["status"] != "handed_off":
                    self.handoff(lead_id, "review", "The reply may be a request to stop contact. Nothing is sent until a human confirms.")
                return {"status": "processed", "decision": "review"}
            if ql.declined_interest(text) and lead["status"] != "handed_off" and not lead["paused_human"]:
                self.db.run("UPDATE leads SET status='declined_interest' WHERE id=?", (lead_id,))
                self.audit(lead_id, "system", "declined_interest", {"no_message_sent": True, "note": "the lead said they are not interested; no reply and no follow-ups. A human can reopen."})
                return {"status": "processed", "decision": "declined_interest"}
            if ql.conditional_refusal(text) and lead["status"] != "handed_off" and not lead["paused_human"]:
                quote = text.strip()[:240]
                self.add_flag(lead_id, "conditional_refusal")
                self.audit(lead_id, "system", "conditional_refusal", {"quote": quote, "no_message_sent": True})
                return {"status": "processed", **self.handoff(lead_id, "review", f"Conditional refusal. The lead wrote: \"{quote}\". No reply was drafted. A human should read it and decide, including any referral.")}
            if lead["paused_human"] or lead["status"] == "handed_off":
                self.audit(lead_id, "system", "reply_after_handoff", {"note": "stored for the human; no automatic reply"})
                if lead["status"] == "handed_off":
                    self.db.run("UPDATE handoffs SET status='open' WHERE lead_id=? AND status='acknowledged'", (lead_id,))
                    self.add_flag(lead_id, "new_reply_after_handoff")
                return {"status": "processed", "decision": "stored_for_human"}
            if lead["status"] == "opted_out":
                return {"status": "processed", "decision": "ignored_opted_out"}
            if ql.detect_injection(text):
                self.add_flag(lead_id, "suspicious_text")
                self.audit(lead_id, "system", "suspicious_text", {"note": "message tried to give instructions to the agent; no automatic action"})
                self.handoff(lead_id, "review", "The reply contains text that tries to instruct the agent. Treated as data only.")
                return {"status": "processed", "decision": "review"}
            self.add_fields(lead_id, self.extract(text, f"reply#{mid}"), message_id=mid)
            return {"status": "processed", **self._decide(lead_id, "reply")}
        finally:
            self.db.run("UPDATE messages SET status='processed' WHERE id=? AND status='processing'", (mid,))

    def record_human_message(self, lead_id, body, author="human"):
        now = self.now()
        self.db.run("INSERT INTO messages(lead_id,direction,channel,body,status,created_at,sent_at,author) VALUES(?,?,?,?,?,?,?,?)",
                    (lead_id, "out", "email", body, "sent", now, now, author))
        self.db.run("UPDATE leads SET paused_human=1, last_outbound_at=?, updated_at=? WHERE id=?", (now, now, lead_id))
        self.db.run("UPDATE leads SET first_response_at=? WHERE id=? AND first_response_at IS NULL", (now, lead_id))
        self.cancel_followups(lead_id, "human took over")
        self.cancel_pending(lead_id, "human took over")
        self.audit(lead_id, author, "human_takeover", {})

    def set_override(self, lead_id, tier, reason):
        if tier not in ql.TIERS:
            return False
        self.db.run("UPDATE leads SET tier_override=?, override_reason=? WHERE id=?", (tier, reason, lead_id))
        self.audit(lead_id, "operator", "tier_override", {"tier": tier, "reason": reason})
        if self.lead(lead_id)["status"] == "closed_not_fit" and tier != "not_fit":
            self._decide(lead_id, "override")      # reopen: a human overrode the automatic close
        return True

    def set_kill_switch(self, on: bool):
        self.db.set_setting("kill_switch", bool(on))
        self.audit(None, "operator", "kill_switch", {"on": bool(on)})

    # ---------- startup recovery ----------
    def recover(self) -> dict:
        """Run at startup. Anything left 'processing' by a crash goes back to 'received' and is processed once."""
        n = self.db.run_rowcount("UPDATE messages SET status='received' WHERE direction='in' AND status='processing'")
        done = 0
        for m in self.db.q("SELECT id FROM messages WHERE direction='in' AND status='received'"):
            self._process_message(m["id"])
            done += 1
        stuck = self.db.run_rowcount("UPDATE actions SET status='uncertain', send_state='uncertain', reason='crashed during send' "
                                     "WHERE status='sending'")
        return {"reprocessed_messages": done, "recovered_claims": n, "sends_marked_uncertain": stuck}
