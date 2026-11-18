"""Message drafts. Plain templates, so the offline demo needs no model and every word is reviewable."""
from .profile import Profile

QUESTIONS = {
    "employees": "roughly how many people work at {company}?",
    "timeline": "when are you hoping to have something in place?",
    "use_case": "which process are you hoping to fix first?",
    "role_level": "what is your role on the team that would use this?",
}
STOP_LINE = "\n\nReply STOP at any time and I will not contact you again."


def ensure_footer(body: str, profile) -> str:
    """Applied at send time: whatever the draft said or the operator edited, the STOP line (and the postal address,
    when one is set) are present in what actually goes out."""
    out = (body or "").rstrip()
    if "reply stop" not in out.lower():
        out += STOP_LINE
    addr = (getattr(profile, "postal_address", "") or "").strip()
    if addr and addr.lower() not in out.lower():
        out += f"\n{addr}"
    return out


def footer(profile) -> str:
    addr = (getattr(profile, "postal_address", "") or "").strip()
    return STOP_LINE + (f"\n{addr}" if addr else "")


def _first(lead):
    return (lead.get("first_name") or "there").strip() or "there"


def _topic(facts):
    intent = facts.get("intent", {}).get("value")
    return {"demo": "a demo", "pricing": "pricing"}.get(intent, "workflow automation")


def sla_text(minutes: int) -> str:
    if minutes % 60 == 0:
        h = minutes // 60
        return f"{h} hour" + ("s" if h != 1 else "")
    return f"{minutes} minutes"


def question_draft(lead, facts, profile: Profile, field: str, first: bool):
    q = QUESTIONS[field].format(company=lead.get("company") or "your company")
    if first:
        body = (f"Hi {_first(lead)},\n\nThanks for reaching out to {profile.company.split(' (')[0]} about {_topic(facts)}. "
                f"So I can point you to the right person, one quick question: {q}\n\n{profile.signature}")
        subject = f"Your {profile.company.split(' (')[0]} inquiry"
    else:
        body = (f"Hi {_first(lead)},\n\nThanks, that helps. One more thing: {q}\n\n{profile.signature}")
        subject = "Re: your inquiry"
    return subject, body + footer(profile)


def ack_draft(lead, facts, profile: Profile):
    body = (f"Hi {_first(lead)},\n\nThanks for the details. This looks like a good fit, so I have passed your request to our "
            f"sales team. Someone will contact you within {sla_text(profile.handoff_sla_minutes)} during business hours."
            f"\n\n{profile.signature}")
    return "Your request is with our sales team", body + footer(profile)


def decline_draft(lead, profile: Profile):
    body = (f"Hi {_first(lead)},\n\nThanks for your interest in {profile.company.split(' (')[0]}. Based on what you shared, "
            f"we are not the right fit right now: our platform is built for teams of {profile.min_employees}+ employees. "
            f"If that changes, you are always welcome to write back.\n\n{profile.signature}")
    return "Thanks for your interest", body + footer(profile)


def nurture_draft(lead, facts, profile: Profile):
    body = (f"Hi {_first(lead)},\n\nThanks, that is helpful. It sounds like the timing is further out, so I will not push. "
            f"I will check back in a couple of months, and you can write to me any time before then.\n\n{profile.signature}")
    return "Noted, talk later", body + footer(profile)


def followup_draft(lead, facts, profile: Profile, step: int, missing_field: str = None):
    ask = (" " + QUESTIONS[missing_field].format(company=lead.get("company") or "your company").capitalize()) if missing_field else ""
    if step == 1:
        body = (f"Hi {_first(lead)},\n\nJust checking that my earlier note reached you.{ask or ' Happy to answer any question about the platform.'}"
                f"\n\n{profile.signature}")
        subject = "Re: your inquiry"
    elif step >= 9:
        body = (f"Hi {_first(lead)},\n\nChecking in as promised. Has anything changed on your side since we last talked?"
                f"\n\n{profile.signature}")
        subject = "Checking in"
    else:
        body = (f"Hi {_first(lead)},\n\nThis is my last note so I do not clutter your inbox. If automating this is still on your list, "
                f"just reply and I will pick it up from there.\n\n{profile.signature}")
        subject = "Last note from me"
    return subject, body + footer(profile)
