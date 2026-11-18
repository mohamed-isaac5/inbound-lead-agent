import json
"""Regression tests for defects found by the independent review (repros A1-A6, B, C1, D, E, F, G, H, I, J, API)."""
import pytest
from fastapi.testclient import TestClient
from ila import demo, qualify as ql
from ila.adapters import GHLAdapter
from ila.api import create_app
from ila.clock import clock
from ila.db import DB
from ila.engine import Engine
from ila.profile import Profile, save_profile, validate_profile
from .conftest import lead


def drafts(eng, lid, kinds=None):
    rows = eng.db.q("SELECT * FROM actions WHERE lead_id=? AND status!='cancelled'", (lid,))
    return [r for r in rows if not kinds or r["kind"] in kinds]


# ---- A: misreads ----
def test_a1_hiring_workflow_demo_is_not_a_job_seeker(eng, mk):
    lid = mk(message="We want a demo of how to automate our hiring and onboarding workflow.")
    assert eng.lead(lid)["status"] != "closed_not_fit"
    assert not drafts(eng, lid, ["decline"])


def test_a2_not_urgent_is_not_hot(eng, mk):
    lid = mk(inquiry_type="pricing", message="Pricing question, definitely not urgent, just a quote for next year.")
    L = eng.lead(lid)
    assert L["tier"] != "hot"
    days = [f for f in eng.fields(lid) if f["name"] == "timeline_days"]
    assert days and int(days[-1]["value"]) > 180


def test_a3_team_of_8_inside_4000_company(eng, mk):
    lid = mk(company_size="", message="I run a team of 8 analysts inside a 4000 employee company and want a demo this month.")
    emp = [f for f in eng.fields(lid) if f["name"] == "employees"]
    assert emp[-1]["value"] == "4000"
    assert not drafts(eng, lid, ["decline"])


def test_weak_size_signal_never_auto_declines(eng, mk):
    lid = mk(company_size="", message="We are 8 and want a demo next month.")
    assert not drafts(eng, lid, ["decline"])


def test_a4_assistant_to_ceo_is_not_exec(eng, mk):
    lid = mk(job_title="Assistant to the CEO")
    levels = [f["value"] for f in eng.fields(lid) if f["name"] == "role_level"]
    assert levels and levels[-1] == "ic"


def test_a5_lead_generation_intern_is_not_manager():
    lvl = [lv for lv, pat in ql.LEVEL_PATTERNS if __import__("re").search(pat, "lead generation intern")]
    assert lvl[0] == "student"
    assert ("manager" not in lvl) or lvl[0] == "student"


def test_a6_no_budget_is_not_available():
    f = {x["name"]: x["value"] for x in ql.rule_extract("We don't have a budget yet but want a demo next month.")}
    assert f["budget"] == "none"
    f2 = {x["name"]: x["value"] for x in ql.rule_extract("Budget is not approved yet.")}
    assert f2.get("budget") != "available"


def test_asap_not_needed_is_not_urgent():
    f = {x["name"]: x["value"] for x in ql.rule_extract("Interested in info. ASAP not needed.")}
    assert "timeline_days" not in f


# ---- B: opt-out table ----
YES = ["Stop", "STOP", "stop.", "Unsubscribe", "Please unsubscribe", "unsubscribe me", "Please unsubscribe me from this list",
       "do not email me again", "Don't email me anymore", "Do not contact me", "please remove my address", "Remove me from your list",
       "take me off your list", "STOP emailing me", "stop contacting me", "Stop messaging me please", "opt out", "Opt me out",
       "Not interested, please remove my email", "no more emails", "leave me alone", "never contact me again", "Quit",
       "Please delete my data and stop contacting me", "I want to opt-out", "cease all communication"]
NO = ["please don't stop contacting me, I love this", "I'd like to cancel my demo", "We need to stop using spreadsheets",
      "don't hesitate to contact me", "Please do not unsubscribe me", "When can we talk?", "Stop by our office any time",
      "We stopped by your booth", "Can you remove the pricing section from the quote?"]
MAYBE = ["How do I unsubscribe me from your competitor?", "Please do not contact me before Monday", "Should I unsubscribe?"]


@pytest.mark.parametrize("t", YES)
def test_opt_out_yes(t):
    assert ql.opt_out_signal(t) == "yes", t


@pytest.mark.parametrize("t", NO)
def test_opt_out_no(t):
    assert ql.opt_out_signal(t) == "no", t


@pytest.mark.parametrize("t", MAYBE)
def test_opt_out_maybe_is_not_a_yes(t):
    assert ql.opt_out_signal(t) == "maybe", t


def test_maybe_reply_holds_everything_for_a_human(eng, mk):
    lid = mk(company_size="", job_title="Analyst", message="info please")
    for a in drafts(eng, lid):
        eng.approve(a["id"])
    n = len(eng.channel.sent)
    eng.process_inbound(lid, "How do I unsubscribe me from your competitor?")
    assert eng.lead(lid)["status"] != "opted_out"
    assert "possible_opt_out" in eng.lead(lid)["flags"]
    assert not [a for a in drafts(eng, lid) if a["status"] == "pending_review"]
    clock.advance(5 * 86400); eng.tick()
    assert len(eng.channel.sent) == n


# ---- C1: opt-out in the intake message ----
def test_c1_optout_in_form_message(eng, mk):
    lid = mk(message="Unsubscribe me, I did not mean to submit this.")
    assert eng.lead(lid)["status"] == "opted_out"
    assert not drafts(eng, lid)
    assert eng.db.q("SELECT * FROM suppression")


# ---- D: missing never contains a known fact ----
def test_d_missing_never_contains_known_fact(eng, mk):
    lid = mk(company_size="", job_title="Analyst", message="We want a demo next month. We are about 300 employees.")
    L = eng.lead(lid)
    facts = {f["name"] for f in eng.fields(lid)}
    assert "timeline" not in L["missing"] and "timeline_days" in facts
    assert "employees" not in L["missing"]
    q = [a for a in drafts(eng, lid, ["question"])]
    assert all("when" not in a["body"].lower() or "timeline" not in (a["reason"] or "") for a in q)


def test_d_demo_has_no_contradiction():
    from ila.adapters import SimChannel, SimCRM
    db = DB(":memory:"); e = Engine(db, SimChannel(), SimCRM(), None)
    demo.load(e)
    for L in db.q("SELECT id, missing, tier_reasons FROM leads"):
        import json
        miss, reasons = json.loads(L["missing"]), json.loads(L["tier_reasons"])
        if any(r.startswith("Timeline about") for r in reasons):
            assert "timeline" not in miss
    clock.set_fixed(None)


# ---- E: time zones ----
def test_e_invalid_lead_timezone_does_not_crash(eng, mk):
    lid = mk(timezone="Mars/Base", company_size="", job_title="Analyst", message="info please")
    assert eng.lead(lid)["tz"] is None
    a = drafts(eng, lid)[0]
    assert eng.approve(a["id"])["status"] in ("sent", "deferred")


def test_e_bad_profile_tz_on_read_falls_back(eng, mk):
    eng.db.set_setting("profile", {**eng.profile.to_dict(), "timezone": "Nope/Zone"})
    assert eng.in_window(clock.now()) in (True, False)


@pytest.mark.parametrize("body", [{"max_touches": -5}, {"daily_send_cap": "abc"}, {"timezone": "Nope/Zone"}, {"followup_days": ["x"]},
                                  {"handoff_sla_minutes": 0}, {"min_employees": 0}, {"quiet_start_hour": 30}, {"default_cc": "abcd"},
                                  {"mode": "x"}, {"max_touches": True}])
def test_profile_validation_rejects(body):
    c = TestClient(create_app(":memory:"), headers={"x-ila-client": "1"})
    assert c.put("/api/profile", json=body).status_code == 400


def test_profile_validation_accepts_defaults():
    assert validate_profile(Profile(configured=True)) is None


# ---- F: follow-ups across a second cycle ----
def test_f_followups_rescheduled_after_reply_and_second_question(eng, mk):
    lid = mk(company_size="", job_title="Analyst", message="info please")
    eng.approve(drafts(eng, lid)[0]["id"])
    eng.process_inbound(lid, "We are looking at a few tools.")
    for a in eng.db.q("SELECT id FROM actions WHERE lead_id=? AND status='pending_review'", (lid,)):
        eng.approve(a["id"])
    live = eng.db.q("SELECT * FROM followups WHERE lead_id=? AND status='scheduled'", (lid,))
    assert {f["step"] for f in live} == {1, 2}
    clock.advance(3 * 86400); eng.tick()
    assert [a for a in drafts(eng, lid, ["followup"]) if a["status"] == "pending_review"]


# ---- G: phone-only lead ----
def test_g_phone_only_lead_is_not_drafted_to_none(eng):
    r = eng.ingest_lead(dict(first_name="P", last_name="O", phone="+14155550123", company_size="300", job_title="VP Sales",
                             inquiry_type="demo", message="demo next month", consent=True), source="t", external_id="ph")
    assert not drafts(eng, r["lead_id"])
    assert eng.db.one("SELECT kind FROM handoffs WHERE lead_id=?", (r["lead_id"],))["kind"] == "review"


# ---- H: consent withdrawal ----
def test_h_dnd_resubmission_opts_out(eng, mk):
    lid = mk(email="same@co.example", company_size="", job_title="Analyst", message="info please")
    eng.approve(drafts(eng, lid)[0]["id"])
    r = eng.ingest_lead(dict(first_name="A", email="same@co.example", consent=False, dnd=True, message="info"), source="t", external_id="again")
    assert r["opted_out"] and eng.lead(lid)["status"] == "opted_out" and eng.lead(lid)["consent"] == 0
    clock.advance(9 * 86400); eng.tick()
    assert len(eng.channel.sent) == 1


def test_h_unchecked_box_on_resubmission_pauses_for_review(eng, mk):
    lid = mk(email="same2@co.example", company_size="", job_title="Analyst", message="info please")
    a = drafts(eng, lid)[0]
    eng.ingest_lead(dict(first_name="A", email="same2@co.example", consent=False, message="info"), source="t", external_id="again2")
    eng.approve(a["id"])
    assert not eng.channel.sent


# ---- I: dedupe policy and phone ----
def test_i_plus_tag_is_the_same_person(eng, mk):
    mk(email="ann@co.example")
    r = eng.ingest_lead(lead(email="Ann+demo@co.example"), source="t", external_id="plus")
    assert r["status"] == "duplicate_lead"


def test_phone_without_plus_needs_configured_country_code():
    from ila.normalize import normalize_phone
    assert normalize_phone("2125551212", "") is None
    assert normalize_phone("2125551212", "1") == "+12125551212"
    assert normalize_phone("+442071234567", "") == "+442071234567"


# ---- J: operator resolution of an uncertain send ----
def _uncertain(eng, mk):
    lid = mk(company_size="", job_title="Analyst", message="info please")
    a = drafts(eng, lid)[0]
    eng.channel.mode = "timeout_before_send"
    eng.approve(a["id"])
    assert eng.db.one("SELECT status FROM actions WHERE id=?", (a["id"],))["status"] == "uncertain"
    eng.channel.mode = "ok"
    return lid, a


def test_j_confirmed_sent(eng, mk):
    lid, a = _uncertain(eng, mk)
    assert eng.resolve_uncertain(a["id"], "confirmed_sent")["status"] == "sent"
    assert eng.lead(lid)["touches"] == 1 and not eng.channel.sent


def test_j_resend(eng, mk):
    lid, a = _uncertain(eng, mk)
    assert eng.resolve_uncertain(a["id"], "resend")["status"] == "sent"
    assert len(eng.channel.sent) == 1


def test_j_drop(eng, mk):
    lid, a = _uncertain(eng, mk)
    assert eng.resolve_uncertain(a["id"], "drop")["status"] == "cancelled"
    assert not eng.channel.sent
    assert not eng.resolve_uncertain(a["id"], "resend")["ok"]


def test_j_unverifiable_ghl_timeout_can_be_resolved():
    def tr(m, u, h, b):
        if u.endswith("/conversations/messages"):
            raise TimeoutError()
        return 200, {}, {"contact": {"id": "c1"}, "id": "t"}
    g = GHLAdapter("t", "loc", transport=tr, sleep=lambda s: None)
    db = DB(":memory:"); save_profile(db, Profile(configured=True, postal_address="1 Test St, Testville")); e = Engine(db, g, g, None)
    clock.set_fixed(demo.tuesday_at(10, "America/New_York"))
    r = e.ingest_lead(lead(company_size="", job_title="Analyst", message="info please"), source="t", external_id="g")
    a = e.db.one("SELECT id FROM actions WHERE lead_id=?", (r["lead_id"],))
    e.approve(a["id"]); out = e.approve(a["id"])
    assert out["status"] == "uncertain"
    assert e.resolve_uncertain(a["id"], "drop")["status"] == "cancelled"
    clock.set_fixed(None)


# ---- API: local request security ----
@pytest.fixture
def raw():
    return TestClient(create_app(":memory:"))


@pytest.mark.parametrize("path", ["/api/demo/reset", "/api/demo/load", "/api/tick", "/api/chat/reset", "/api/kill-switch"])
def test_cross_site_style_post_is_refused(raw, path):
    r = raw.post(path, content='{"on": true}', headers={"content-type": "text/plain"})
    assert r.status_code == 403


def test_put_profile_text_plain_refused(raw):
    assert raw.put("/api/profile", content='{"mode":"auto_followups"}', headers={"content-type": "text/plain"}).status_code == 403


def test_with_client_header_works(raw):
    assert raw.post("/api/tick", headers={"x-ila-client": "1"}).status_code == 200


def test_foreign_host_refused(raw):
    assert raw.get("/api/status", headers={"host": "evil.example"}).status_code == 400


def test_allowed_hosts_env(monkeypatch):
    monkeypatch.setenv("ILA_ALLOWED_HOSTS", "leads.internal.example")
    c = TestClient(create_app(":memory:"))
    assert c.get("/api/status", headers={"host": "leads.internal.example:8765"}).status_code == 200


def test_demo_routes_disabled_with_live_adapter(monkeypatch):
    g = GHLAdapter("t", "loc", transport=lambda *a: (200, {}, {}), sleep=lambda s: None)
    c = TestClient(create_app(":memory:", channel=g, crm=g), headers={"x-ila-client": "1"})
    assert c.post("/api/demo/reset").status_code == 403


def test_security_headers(raw):
    r = raw.get("/api/status")
    assert r.headers["x-frame-options"] == "DENY" and "default-src" in r.headers["content-security-policy"]


def test_xss_payload_is_stored_as_text(raw):
    c = TestClient(create_app(":memory:"), headers={"x-ila-client": "1"})
    r = c.post("/api/intake/form", json={"first_name": "<img src=x onerror=alert(1)>", "email": "x@y.example", "message": "demo",
                                          "consent": True, "company_size": "300", "job_title": "VP"})
    assert r.status_code == 200


# ---- second review round ----
MORE_YES = ["no thanks, stop", "Please stop", "Stopp", "This is spam", "STOP.", "stop it", "Please stop now", "you are spamming me", "report as spam"]
MORE_NO = ["I'd like to unsubscribe from this thread but keep talking about pricing", "Don't stop, keep going!",
           "How do I stop my team from using spreadsheets?", "We want to stop emailing spreadsheets around", "Our spam filter blocked your mail",
           "stop by our booth", "We need to stop using spreadsheets"]


@pytest.mark.parametrize("t", MORE_YES)
def test_short_stop_and_spam_are_opt_outs(t):
    assert ql.opt_out_signal(t) == "yes", t


@pytest.mark.parametrize("t", MORE_NO)
def test_business_stop_wording_is_not_an_opt_out(t):
    assert ql.opt_out_signal(t) != "yes", t


def test_range_straddling_minimum_is_not_declined_and_asks_for_size(eng, mk):
    lid = mk(job_title="Co-Founder", company_size="11-50", message="demo")
    L = eng.lead(lid)
    assert L["status"] != "closed_not_fit" and not drafts(eng, lid, ["decline"])
    assert "employees" in L["missing"]
    ev = [f["evidence"] for f in eng.fields(lid) if f["name"] == "employees"][0]
    assert "straddles" in ev


def test_range_entirely_below_minimum_still_declines(eng, mk):
    lid = mk(company_size="11-30", message="demo")
    assert eng.lead(lid)["status"] == "closed_not_fit"


def test_student_wording_with_company_buying_signals_goes_to_a_human(eng, mk):
    lid = mk(company_size="", job_title="Intern", message="We are a company of 2,000 people and want a demo this month.")
    h = eng.db.one("SELECT kind FROM handoffs WHERE lead_id=?", (lid,))
    assert h and h["kind"] == "review" and eng.lead(lid)["status"] == "handed_off"
    assert eng.lead(lid)["tier"] != "not_fit"


def test_student_plus_conflicting_size_is_not_auto_closed_or_contradictory(eng, mk):
    lid = mk(message="I am a student but my company has 5000 employees and we want a demo this month")
    L = eng.lead(lid)
    assert L["tier"] != "not_fit" and L["status"] == "handed_off"


def test_conflicting_size_is_not_asserted_anywhere(eng, mk):
    import json
    lid = mk(company_size="51-200", message="We are 1200 employees and want a demo this month.")
    L = eng.lead(lid)
    assert L["tier"] != "hot" and "size_conflict" in L["flags"]
    assert not any(r.startswith("Company size 1200 fits") or r.startswith("Company size 51 fits") for r in L["tier_reasons"])
    card = json.loads(eng.db.one("SELECT card FROM handoffs WHERE lead_id=?", (lid,))["card"])
    assert card["facts"]["employees"]["value"] == "disputed"


def test_decline_policy_none_drafts_nothing_and_audit_says_so(eng, mk):
    eng.db.set_setting("profile", {**eng.profile.to_dict(), "decline_policy": "none"})
    lid = mk(company_size="10", message="demo")
    assert not drafts(eng, lid)
    import json
    d = json.loads(eng.db.one("SELECT detail FROM audit WHERE lead_id=? AND event='closed_not_fit'", (lid,))["detail"])
    assert d["no_message_sent"] is True and d["decline_policy"] == "none"


def test_override_reopens_a_closed_lead(eng, mk):
    lid = mk(job_title="Student", company_size="", message="demo for a class project")
    assert eng.lead(lid)["status"] == "closed_not_fit"
    eng.set_override(lid, "warm", "this is a genuine buyer")
    assert eng.lead(lid)["status"] != "closed_not_fit"


def test_conflicting_timeline_phrases_are_unknown():
    f = {x["name"] for x in ql.rule_extract("Within 2 weeks, but no rush")}
    assert "timeline_days" not in f


def test_foreign_origin_refused(raw):
    c = TestClient(create_app(":memory:"), headers={"x-ila-client": "1"})
    assert c.post("/api/tick", headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.post("/api/tick", headers={"Origin": "http://localhost:8765"}).status_code == 200


@pytest.mark.parametrize("t", ["remove", "dnc", "DNC.", "Do not contact"])
def test_bare_removal_words_opt_out(t):
    assert ql.opt_out_signal(t) == "yes", t


@pytest.mark.parametrize("t", ["please do not call, email is fine", "This isn't spam, it's relevant, thanks",
                               "Please don't phone me, email only", "this is not spam, send the demo link"])
def test_channel_preference_and_not_spam_are_not_opt_out(t):
    assert ql.opt_out_signal(t) != "yes", t


@pytest.mark.parametrize("t", ["not interested", "No thanks", "Not interested, thanks", "We're not interested", "no thank you"])
def test_not_interested_reply_sends_and_schedules_nothing(eng, mk, t):
    lid = mk(company_size="", message="hello")          # size unknown: the agent asked a question and scheduled follow-ups
    for a in eng.db.q("SELECT id FROM actions WHERE lead_id=?", (lid,)):
        eng.approve(a["id"])
    assert eng.db.q("SELECT 1 FROM followups WHERE lead_id=? AND status='scheduled'", (lid,))
    r = eng.process_inbound(lid, t)
    assert r["decision"] == "declined_interest"
    assert eng.lead(lid)["status"] == "declined_interest"
    assert not eng.db.q("SELECT 1 FROM followups WHERE lead_id=? AND status='scheduled'", (lid,))
    assert not eng.db.q("SELECT 1 FROM actions WHERE lead_id=? AND status IN ('pending_review','approved')", (lid,))
    sent = len(eng.db.q("SELECT 1 FROM actions WHERE lead_id=? AND status='sent'", (lid,)))
    for _ in range(10):
        clock.advance(3 * 86400); eng.tick()
    assert len(eng.db.q("SELECT 1 FROM actions WHERE lead_id=? AND status='sent'", (lid,))) == sent
    assert not eng.db.q("SELECT 1 FROM actions WHERE lead_id=? AND status='pending_review'", (lid,))


@pytest.mark.parametrize("t", ["Not interested in pricing but we want a demo", "no thanks, maybe next quarter", "Not interested right now, ask again in March",
                               "Why would we not be interested?"])
def test_conditional_refusals_are_not_treated_as_final(t):
    assert not ql.declined_interest(t), t


@pytest.mark.parametrize("t", ["remove my name from the list", "Please remove my address from your list", "remove my email from the list",
                               "remove my contact details"])
def test_remove_my_details_is_opt_out(t):
    assert ql.opt_out_signal(t) == "yes", t


@pytest.mark.parametrize("t", ["Not interested right now, maybe next quarter", "not interested, but my colleague Dan might be",
                               "No thanks, we already use a competitor",
                               "We are not interested in the 50 seat plan but want a demo for 500"])
def test_conditional_refusal_goes_to_human_with_quote(eng, mk, t):
    lid = mk(job_title="Analyst", company_size="100", message="info please")
    for a in eng.db.q("SELECT id FROM actions WHERE lead_id=?", (lid,)):
        eng.approve(a["id"])
    r = eng.process_inbound(lid, t)
    assert r.get("decision") == "handoff" and r.get("kind") == "review", r
    assert not eng.db.q("SELECT 1 FROM actions WHERE lead_id=? AND status IN ('pending_review','approved')", (lid,))
    assert not eng.db.q("SELECT 1 FROM followups WHERE lead_id=? AND status='scheduled'", (lid,))
    h = eng.db.one("SELECT * FROM handoffs WHERE lead_id=? ORDER BY id DESC", (lid,))
    assert t in json.loads(h["card"])["why_handed_off"]                      # the quote, and so any referral, is shown to the human
    assert "conditional_refusal" in (eng.lead(lid)["flags"] or "")


import time


def test_long_text_extracts_fast():
    for txt in ["automate " * 900, "a" * 8000, ("x " * 4000) + "workflow", "workflow " + "," * 8000, "process " * 3000 + "."]:
        t0 = time.time(); ql.rule_extract(txt); assert time.time() - t0 < 0.05, txt[:20]
    t0 = time.time(); ql.rule_extract("a" * 3_000_000); assert time.time() - t0 < 0.2


def test_take_my_name_off_is_opt_out():
    for t in ["Take my name off", "please take my email off your list", "take me off"]:
        assert ql.opt_out_signal(t) == "yes", t


def test_intake_limits_return_413_quickly(raw):
    c = TestClient(create_app(":memory:"), headers={"X-ILA-Client": "1"})
    t0 = time.time()
    r = c.post("/api/intake/form", json={"email": "a@b.example", "consent": True, "message": "x" * 3_000_000})
    assert r.status_code == 413 and time.time() - t0 < 1
    assert c.post("/api/intake/form", json={"email": "a@b.example", "consent": True, "message": "x" * 9000}).status_code == 413
    assert c.post("/api/intake/form", json={"email": "a4@b.example", "consent": True, "first_name": "x" * 201}).status_code == 413
    assert c.post("/api/intake/form", json={"email": "a5@b.example", "consent": True, "first_name": "x" * 200, "message": "demo"}).status_code == 200


def test_unknown_lead_and_empty_inputs(raw):
    c = TestClient(create_app(":memory:"), headers={"X-ILA-Client": "1"})
    assert c.post("/api/leads/999/reply", json={"text": "x"}).status_code == 404
    assert c.post("/api/leads/999/human-reply", json={"text": "x"}).status_code == 404
    assert c.post("/api/leads/999/override", json={"tier": "hot", "reason": "r"}).status_code == 404
    lid = c.post("/api/intake/form", json={"email": "q@b.example", "consent": True, "message": "demo"}).json()["lead_id"]
    assert c.post(f"/api/leads/{lid}/human-reply", json={"text": "  "}).status_code == 400
    assert c.post(f"/api/leads/{lid}/override", json={"tier": "hot", "reason": " "}).status_code == 400
    assert c.post(f"/api/leads/{lid}/override", json={"tier": "bogus", "reason": "r"}).status_code == 400
    assert c.put("/api/profile", json={"company": " ", "offer": "x"}).status_code == 400


def test_invalid_lead_timezone_is_flagged(eng, mk):
    lid = mk(timezone="Mars/Olympus")
    assert "invalid_timezone" in (eng.lead(lid)["flags"] or "")


def test_footer_carries_postal_address_and_live_send_needs_it(eng, mk):
    from ila.adapters import SimChannel
    eng.db.set_setting("profile", {**eng.profile.to_dict(), "postal_address": "1 Test St, Testville"})
    lid = mk(company_size="", message="hi")
    a = eng.db.one("SELECT body FROM actions WHERE lead_id=?", (lid,))
    assert "1 Test St, Testville" in a["body"]
    eng.db.set_setting("profile", {**eng.profile.to_dict(), "postal_address": ""})
    class Live:        # any channel that is not the simulator
        name = "live"
        def send(self, *a, **k): raise AssertionError("must not send")
    eng.channel = Live()
    act = eng.db.one("SELECT * FROM actions WHERE lead_id=?", (lid,))
    verdict = eng.guard(act, eng.lead(lid))
    assert verdict[0] == "hold" and "postal address" in verdict[1]


def test_conflicting_metrics_text_has_no_csv():
    from ila import metrics
    import inspect
    assert "CSV" not in inspect.getsource(metrics)


def test_footer_applied_at_send_time_to_old_and_edited_drafts(eng, mk):
    lid = mk(company_size="", message="hi")                       # draft written before any address exists
    a = eng.db.one("SELECT id FROM actions WHERE lead_id=?", (lid,))
    eng.db.set_setting("profile", {**eng.profile.to_dict(), "postal_address": "9 Late St, Addressville"})
    eng.approve(a["id"], body="Hi, quick question about your plans.")     # operator deleted STOP line and address
    row = eng.db.one("SELECT body, status FROM actions WHERE id=?", (a["id"],))
    assert row["status"] == "sent"
    assert "Reply STOP" in row["body"] and "9 Late St, Addressville" in row["body"]
    m = eng.db.one("SELECT body FROM messages WHERE lead_id=? AND direction='out' ORDER BY id DESC", (lid,))
    assert "Reply STOP" in m["body"] and "9 Late St, Addressville" in m["body"]


def test_stop_at_bottom_of_long_message_is_seen(eng, mk):
    lid = mk(company_size="100", message="hello")
    r = eng.process_inbound(lid, ("quoted earlier thread. " * 900) + "\nPlease stop emailing me.")
    assert r["decision"] == "opted_out"
    t0 = time.time(); ql.opt_out_signal(ql.head_tail("x " * 20000)); assert time.time() - t0 < 0.1


@pytest.mark.parametrize("t", ["Please take my name off the invoice", "take my name off the contract", "remove my name from the invoice"])
def test_name_off_document_is_not_clear_opt_out(t):
    assert ql.opt_out_signal(t) == "maybe", t


def test_held_for_address_says_so(eng, mk):
    class Live:
        name = "live"
        def send(self, *a, **k): raise AssertionError("no send")
    eng.channel = Live()
    lid = mk(company_size="", message="hi")
    a = eng.db.one("SELECT id FROM actions WHERE lead_id=?", (lid,))
    r = eng.approve(a["id"])
    assert r["status"] == "deferred" and "postal address" in r["reason"] and r["until"] is None


@pytest.mark.parametrize("t", ["Thanks for the info.\nI will think about it.\n\nSTOP", "STOP\n" + "later text " * 900, "Hello\n> quoted old mail\nSTOP\n> more quoted",
                               "STOP\nThanks"])
def test_bare_stop_first_or_last_line_is_opt_out(t):
    assert ql.opt_out_signal(ql.head_tail(t)) == "yes", t


def test_long_message_starting_with_stop_never_gets_a_normal_reply(eng, mk):
    lid = mk(company_size="100", message="hello")
    r = eng.process_inbound(lid, "STOP\n" + "x " * 4500 + "end")
    assert r["decision"] == "opted_out"
    assert not eng.db.q("SELECT 1 FROM actions WHERE lead_id=? AND status IN ('pending_review','approved')", (lid,))


def test_engine_intake_keeps_head_and_tail(eng):
    msg = "Hello. " + "filler " * 1800 + "\nPlease unsubscribe me."
    r = eng.ingest_lead(lead(message=msg), source="t", external_id="long1")
    assert r["status"] == "opted_out"
    stored = eng.db.one("SELECT body FROM messages WHERE lead_id=? AND direction='in'", (r["lead_id"],))
    assert "unsubscribe me" in stored["body"] and len(stored["body"]) < 11000


@pytest.mark.parametrize("t", ["Thanks. " + "filler " * 1300 + "\nSTOP\n--\nJohn", "Hi\nSTOP\nJohn Smith\nAcme Corp", "Please note\nstop\n\n--\nJohn\nCEO, Acme\n+1 555 0100 ext 4",
                               "Hello\nUnsubscribe\nRegards,\nJohn"])
def test_stop_before_signature_block_is_opt_out(t):
    assert ql.opt_out_signal(ql.head_tail(t)) == "yes", t


@pytest.mark.parametrize("t", ["Can you stop by our office?\n--\nJohn", "We need to stop the presses on the old process.\nThanks\nJohn",
                               "Let us stop by on Tuesday\nBest\nJohn", "Hello\nWe stop at nothing to automate.\nJohn"])
def test_stop_phrases_in_normal_mail_stay_normal(t):
    assert ql.opt_out_signal(t) != "yes", t
