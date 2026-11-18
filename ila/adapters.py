"""CRM and channel adapters.

The engine only talks to two small interfaces:

    Channel.send(send_key, lead, subject, body) -> channel_message_id
    Channel.find_sent(send_key) -> channel_message_id | None     (used to avoid duplicate sends)
    CRM.upsert_contact(lead) -> contact_id
    CRM.create_task(contact_id, title, body, due_iso, assigned_to) -> task_id
    CRM.add_note(contact_id, text)

Two implementations ship:
  * SimChannel / SimCRM: fully local, used by the demo and tests. No network.
  * GHLAdapter: GoHighLevel (HighLevel API v3). Endpoints, scopes and body fields follow the official docs:
      POST /contacts/upsert                      scope contacts.write
      POST /conversations/messages               scope conversations/message.write
      POST /contacts/{contactId}/tasks           scope contacts.write
    It is tested against a mocked transport only. It has never been run against a live HighLevel account.
"""
import base64
import json
import os
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

from . import config


class SendError(Exception):
    """The channel definitively did not send the message. Safe to retry."""


class SendUncertain(Exception):
    """The send may or may not have happened (timeout, 5xx). Never blind-retry; reconcile first."""


# ---------------- simulated implementations ----------------

class SimChannel:
    name = "simulated"

    def __init__(self):
        self.sent = {}            # send_key -> channel_message_id
        self.mode = "ok"          # ok | fail | timeout_after_send | timeout_before_send
        self.log = []

    def send(self, send_key, lead, subject, body):
        if self.mode == "fail":
            raise SendError("simulated channel rejected the message")
        if self.mode == "timeout_before_send":
            raise SendUncertain("simulated timeout before delivery")
        mid = f"sim-{len(self.sent) + 1}"
        self.sent[send_key] = mid
        self.log.append({"send_key": send_key, "to": lead.get("email"), "subject": subject, "body": body})
        if self.mode == "timeout_after_send":
            raise SendUncertain("simulated timeout after delivery")
        return mid

    def find_sent(self, send_key):
        return self.sent.get(send_key)


class SimCRM:
    name = "simulated"

    def __init__(self):
        self.contacts, self.tasks, self.notes = {}, [], []

    def upsert_contact(self, lead):
        key = lead.get("email") or lead.get("phone")
        self.contacts.setdefault(key, f"simcrm-{len(self.contacts) + 1}")
        return self.contacts[key]

    def create_task(self, contact_id, title, body, due_iso, assigned_to=None):
        self.tasks.append({"contact": contact_id, "title": title, "body": body, "due": due_iso, "assigned": assigned_to})
        return f"simtask-{len(self.tasks)}"

    def add_note(self, contact_id, text):
        self.notes.append({"contact": contact_id, "text": text})


# ---------------- GoHighLevel ----------------

class GHLAdapter:
    """HighLevel API v3 over an injectable transport: transport(method, url, headers, json_body) -> (status, headers, body)."""

    BASE = "https://services.leadconnectorhq.com"
    name = "gohighlevel"

    def __init__(self, token, location_id, transport: Optional[Callable] = None, sleep=time.sleep,
                 max_retries_429=3, channel_type="Email", email_from=None):
        self._token = token
        self.location_id = location_id
        self.transport = transport or self._http
        self.sleep = sleep
        self.max_retries_429 = max_retries_429
        self.channel_type = channel_type
        self.email_from = email_from

    @classmethod
    def from_env(cls, **kw):
        tok, loc = os.environ.get(config.GHL_TOKEN_ENV), os.environ.get(config.GHL_LOCATION_ENV)
        return cls(tok, loc, **kw) if tok and loc else None

    def _headers(self):
        return {"Authorization": f"Bearer {self._token}", "Version": "v3", "Content-Type": "application/json",
                "Accept": "application/json"}

    def _http(self, method, url, headers, body):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 (fixed https base URL)
                return r.status, dict(r.headers), json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read() or b"{}")
            except Exception:
                payload = {}
            return e.code, dict(e.headers), payload

    def _call(self, method, path, body, idempotent):
        """HTTP 429 means HighLevel rejected the request, so retrying is always safe.
        Other failures are retried only when the call is idempotent. A send is not."""
        url = self.BASE + path
        attempt = 0
        while True:
            try:
                status, headers, payload = self.transport(method, url, self._headers(), body)
            except (TimeoutError, OSError) as e:
                if idempotent and attempt < 2:
                    attempt += 1
                    self.sleep(2 ** attempt)
                    continue
                raise SendUncertain(f"network error: {type(e).__name__}") from None
            if status == 429 and attempt < self.max_retries_429:
                ra = headers.get("Retry-After") or headers.get("retry-after")
                delay = float(ra) if ra and str(ra).replace(".", "", 1).isdigit() else min(2 ** attempt, 30)
                attempt += 1
                self.sleep(delay)
                continue
            if 200 <= status < 300:
                return payload
            if status == 429 or status in (400, 401, 403, 404, 422):
                raise SendError(f"HighLevel returned {status}")
            if idempotent and status >= 500 and attempt < 2:
                attempt += 1
                self.sleep(2 ** attempt)
                continue
            raise SendUncertain(f"HighLevel returned {status}")

    def upsert_contact(self, lead):
        body = {"locationId": self.location_id, "firstName": lead.get("first_name") or "",
                "lastName": lead.get("last_name") or "", "email": lead.get("email"), "phone": lead.get("phone"),
                "companyName": lead.get("company"), "source": lead.get("source") or "inbound-lead-agent",
                "tags": ["inbound-lead-agent"]}
        body = {k: v for k, v in body.items() if v not in (None, "")}
        res = self._call("POST", "/contacts/upsert", body, idempotent=True)
        contact = res.get("contact") or res
        cid = contact.get("id")
        if not cid:
            raise SendError("upsert response had no contact id")
        return cid

    def send(self, send_key, lead, subject, body):
        if not lead.get("crm_contact_id"):
            raise SendError("lead has no CRM contact id yet")
        payload = {"type": self.channel_type, "contactId": lead["crm_contact_id"], "message": body}
        if self.channel_type == "Email":
            payload.update({"subject": subject or "", "html": "<p>" + body.replace("\n", "<br>") + "</p>"})
            if self.email_from:
                payload["emailFrom"] = self.email_from
        res = self._call("POST", "/conversations/messages", payload, idempotent=False)
        return res.get("messageId") or res.get("emailMessageId") or "ghl-sent"

    def find_sent(self, send_key):
        # HighLevel's send endpoint has no idempotency key, so we cannot look a message up by our key.
        # Returning None means "cannot confirm", and the engine then refuses to resend automatically.
        raise SendUncertain("HighLevel cannot confirm delivery by our send key")

    def create_task(self, contact_id, title, body, due_iso, assigned_to=None):
        payload = {"title": title, "body": body, "dueDate": due_iso, "completed": False}
        if assigned_to:
            payload["assignedTo"] = assigned_to
        res = self._call("POST", f"/contacts/{contact_id}/tasks", payload, idempotent=False)
        t = res.get("task") or res
        return t.get("id") or "ghl-task"

    def add_note(self, contact_id, text):
        return None  # notes are not in the verified endpoint set yet


# ---------------- webhook verification ----------------

def verify_ghl_signature(raw_body: bytes, signature_b64: Optional[str], public_key_pem: str = None) -> bool:
    """Verify X-GHL-Signature: Ed25519 over the raw request body, signature base64-encoded."""
    if not signature_b64 or signature_b64 == "N/A":
        return False
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    try:
        key = load_pem_public_key((public_key_pem or config.GHL_ED25519_PUBLIC_KEY_PEM).encode())
        key.verify(base64.b64decode(signature_b64), raw_body)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
