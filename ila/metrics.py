"""Dashboard metrics. Each metric carries its definition, source and sample size.

Rules: a ratio under MIN_N events is shown as a count only. Revenue and wins are never claimed:
they would have to come from the CRM, and this build has no real CRM data.
"""
import statistics

MIN_N = 20


def _ratio(num, den, definition, source):
    m = {"definition": definition, "source": source, "numerator": num, "denominator": den}
    if den >= MIN_N:
        m["value"], m["display"] = round(num / den, 3), f"{round(100 * num / den)}%"
    else:
        m["value"], m["display"] = None, f"{num} of {den} (too few for a percentage; needs {MIN_N}+)"
    return m


def compute(db, now, simulated: bool) -> dict:
    leads = db.q("SELECT * FROM leads")
    n = len(leads)
    out = {"simulated_clock": simulated, "metrics": {}}
    M = out["metrics"]
    M["leads_received"] = {"definition": "Leads created from the web form, the CRM webhook or the manual test form (duplicates excluded)",
                           "source": "leads table", "display": str(n), "value": n}
    dup = db.one("SELECT COUNT(*) c FROM audit WHERE event IN ('duplicate_submission','duplicate_event_ignored')")["c"]
    M["duplicates_blocked"] = {"definition": "Repeat submissions or repeated webhook events that were ignored",
                               "source": "audit log (duplicate_submission, duplicate_event_ignored)", "display": str(dup), "value": dup}
    times = [l["first_response_at"] - l["created_at"] for l in leads if l["first_response_at"]]
    if times:
        med = statistics.median(times)
        disp = f"{round(med / 60, 1)} min (median of {len(times)})"
    else:
        med, disp = None, "no sent replies yet"
    M["first_response_time"] = {"definition": "Median time from lead creation to the first message actually sent (agent or human)",
                                "source": "leads.created_at and leads.first_response_at (timestamps recorded at those events)",
                                "display": disp, "value": med, "n": len(times),
                                "note": "Includes time a draft waited for approval or for allowed sending hours"}
    with_out = [l for l in leads if l["last_outbound_at"]]
    replied = db.q("SELECT DISTINCT m.lead_id FROM messages m WHERE m.direction='in' AND m.channel!='form' "
                   "AND m.author='lead' AND m.status IN ('processed','processing','received')")
    rset = {r["lead_id"] for r in replied} & {l["id"] for l in with_out}
    M["reply_rate"] = _ratio(len(rset), len(with_out), "Leads who wrote back after we sent them a message, out of leads we messaged",
                             "messages table (inbound non-form messages)")
    dist = {t: 0 for t in ("hot", "warm", "nurture", "not_fit", "unknown")}
    for l in leads:
        t = l["tier_override"] or l["tier"]
        if t in dist:
            dist[t] += 1
    M["tier_distribution"] = {"definition": "Current classification of every lead (after any human override)",
                              "source": "leads.tier / leads.tier_override", "counts": dist, "display": ", ".join(f"{k}: {v}" for k, v in dist.items()), "n": n}
    M["unknown_rate"] = _ratio(dist["unknown"], n, "Share of leads we could not classify yet", "leads.tier")
    ov = [l for l in leads if l["tier_override"]]
    decided = [l for l in leads if l["tier"]]
    M["human_override_rate"] = _ratio(len(ov), len(decided), "Leads where a human changed the agent's classification",
                                      "leads.tier_override (with the reason on each)")
    ho = db.q("SELECT * FROM handoffs")
    ack_late = [h for h in ho if (h["ack_at"] and h["ack_at"] > h["sla_due"]) or h["escalated_at"]]
    M["handoffs"] = {"definition": "Leads passed to a human, and how many missed the response SLA (acknowledged late or escalated)",
                     "source": "handoffs table", "display": f"{len(ho)} handed off, {len(ack_late)} missed SLA", "value": len(ho),
                     "sla_missed": len(ack_late)}
    opt = db.one("SELECT COUNT(*) c FROM leads WHERE status='opted_out'")["c"]
    M["opt_outs"] = {"definition": "Leads who asked us to stop", "source": "leads.status", "display": str(opt), "value": opt}
    fails = db.one("SELECT COUNT(*) c FROM actions WHERE status IN ('failed','uncertain')")["c"]
    M["send_problems"] = {"definition": "Sends that failed or whose outcome is not confirmed", "source": "actions.status",
                          "display": str(fails), "value": fails}
    pend = db.one("SELECT COUNT(*) c FROM actions WHERE status='pending_review'")["c"]
    M["pending_review"] = {"definition": "Drafts waiting for a human decision right now (live count)", "source": "actions.status",
                           "display": str(pend), "value": pend}
    M["wins_and_revenue"] = {"definition": "Not measured here. Won deals and revenue must come from your CRM.", "source": "none",
                             "display": "not measured", "value": None}
    return out
