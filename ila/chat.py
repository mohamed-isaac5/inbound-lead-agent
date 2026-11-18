"""Setup chat: an adaptive, offline interview that turns plain answers into the business profile.

It asks one thing at a time, understands loose answers, asks again when an answer is unusable,
and never accepts credentials. The operator can re-run it or edit the profile directly.
"""
import re
from zoneinfo import ZoneInfo, available_timezones

from . import config
from .clock import clock
from .profile import Profile, load_profile, save_profile
from .qualify import LEVEL_PATTERNS

STEPS = ["offer", "size", "roles", "exclusions", "first_question", "handoff", "hours", "followups", "mode", "model", "confirm"]

INTRO = ("Hi, I'm the setup assistant. I'll ask about 10 short questions so the agent knows your business: "
         "who is a good lead, who is not, who takes over, and when it may write to people. "
         "You can answer in plain words, say 'skip' to use the default, and change anything later. "
         "Please never paste passwords or API keys here.\n\nFirst: what does your company sell, and to whom?")


def looks_like_secret(text: str) -> bool:
    t = text or ""
    if any(m in t for m in config.LOOKS_LIKE_SECRET):
        return True
    return bool(re.search(r"\b[A-Za-z0-9_\-]{32,}\b", t))


def _levels_from(text: str):
    low = text.lower()
    found = []
    mapping = {"exec": r"\b(ceo|cfo|coo|cto|chief|founder|owner|president|c-level|exec)", "vp": r"\b(vp|vice president|head of)",
               "director": r"\bdirector", "manager": r"\b(manager|lead)", "ic": r"\b(analyst|specialist|engineer|individual contributor|coordinator)"}
    for lvl, pat in mapping.items():
        if re.search(pat, low):
            found.append(lvl)
    return found


def _summary(p: Profile) -> str:
    return (f"Here is what I understood:\n"
            f"• Offer: {p.offer}\n• Good fit: {p.min_employees}+ employees" + (f", up to {p.max_employees}" if p.max_employees else "") + "\n"
            f"• Buyers: {', '.join(p.target_levels)}\n• Not a fit: {', '.join(p.excluded_keywords) or 'nothing listed'}"
            f"; competitor domains: {', '.join(p.competitor_domains) or 'none'}\n"
            f"• First thing to ask when unsure: {p.question_order[0].replace('_', ' ')}\n"
            f"• Handoff: {p.handoff_owner}, respond within {p.handoff_sla_minutes} min\n"
            f"• Sending hours: {p.quiet_end_hour}:00 to {p.quiet_start_hour}:00 {p.timezone}"
            f"{', weekdays only' if p.business_days_only else ''}\n"
            f"• Follow-ups after day(s): {', '.join(map(str, p.followup_days))}; at most {p.max_touches} messages per lead\n"
            f"• Mode: {'I draft, you approve every message' if p.mode == 'approve' else 'follow-ups send automatically after guards pass'}\n\n"
            f"Say 'confirm' to save, or tell me what to change (for example 'change size').")


class SetupChat:
    def __init__(self, db):
        self.db = db

    def state(self):
        return self.db.get_setting("chat_state", {"step": 0, "draft": None, "started": False})

    def history(self):
        return self.db.q("SELECT role,text,ts FROM chat ORDER BY id")

    def _say(self, text):
        self.db.run("INSERT INTO chat(role,text,ts) VALUES('assistant',?,?)", (text, clock.now()))

    def reset(self):
        self.db.run("DELETE FROM chat")
        self.db.set_setting("chat_state", {"step": 0, "draft": None, "started": True})
        self._say(INTRO)

    def start_if_needed(self):
        if not self.history():
            self.reset()

    def say(self, user_text: str) -> list:
        self.start_if_needed()
        st = self.state()
        if looks_like_secret(user_text):
            self.db.run("INSERT INTO chat(role,text,ts) VALUES('user',?,?)", ("[removed: this looked like a credential]", clock.now()))
            self._say("That looked like a password or API key, so I did not keep it. Keys never go in chat: set them as environment "
                      "variables on the machine that runs the agent (see Settings). Let's continue with the question above.")
            return self.history()
        self.db.run("INSERT INTO chat(role,text,ts) VALUES('user',?,?)", (user_text, clock.now()))
        p = Profile.from_dict(st["draft"]) if st.get("draft") else load_profile(self.db)
        step = STEPS[min(st["step"], len(STEPS) - 1)]
        t = user_text.strip()
        low = t.lower()
        skip = low in ("skip", "default", "idk", "not sure")
        reply, advance = None, True
        if low.startswith("change ") and step == "confirm":
            target = low.replace("change", "").strip()
            mapping = {"offer": "offer", "size": "size", "buyers": "roles", "roles": "roles", "hours": "hours", "handoff": "handoff",
                       "followups": "followups", "follow-ups": "followups", "mode": "mode", "question": "first_question"}
            for k, v in mapping.items():
                if k in target:
                    st["step"] = STEPS.index(v)
                    st["draft"] = p.to_dict()
                    self.db.set_setting("chat_state", st)
                    self._say(self._prompt(v))
                    return self.history()
            self._say("I can change: offer, size, roles, exclusions, question, handoff, hours, followups, mode. Which one?")
            return self.history()
        if step == "offer":
            if not skip:
                if len(t) < 20:
                    reply, advance = "Could you add a bit more? One sentence on what you sell and who buys it is enough.", False
                else:
                    p.offer = t[:300]
                    reply_note = " Since you mentioned enterprise customers, I'd suggest a high minimum company size next." if "enterprise" in low else ""
                    reply = "Got it." + reply_note
        elif step == "size":
            m = re.search(r"\d[\d,]*", t)
            if skip:
                pass
            elif any(w in low for w in ("no minimum", "any size", "anyone", "all sizes")):
                p.min_employees = 1
                reply = "No minimum size then. I'll rely on role and intent instead."
            elif m:
                p.min_employees = int(m.group(0).replace(",", ""))
                mx = re.findall(r"\d[\d,]*", t)
                if len(mx) > 1 and ("up to" in low or "max" in low or "-" in t or "to" in low):
                    p.max_employees = int(mx[1].replace(",", ""))
                reply = f"Minimum {p.min_employees} employees."
                if p.min_employees < 10:
                    reply += " That is small, so expect many leads to count as a fit."
            else:
                reply, advance = "I need a number. What is the smallest company (in employees) that is a good customer?", False
        elif step == "roles":
            if not skip:
                lv = _levels_from(t)
                if lv:
                    p.target_levels = lv
                    reply = f"Buyer levels: {', '.join(lv)}."
                else:
                    reply, advance = ("I did not recognise a level. Use words like founder, VP, director, manager, or analyst.", False)
        elif step == "exclusions":
            if not skip:
                doms = re.findall(r"[a-z0-9\-]+\.[a-z]{2,}", low)
                words = [w for w in ("students", "agencies", "freelancers", "competitors", "job seekers", "resellers") if w.rstrip("s") in low]
                if doms:
                    p.competitor_domains = doms
                if words:
                    p.excluded_keywords = words
                reply = "Noted. I always filter job seekers, vendor pitches, support requests and disposable emails regardless."
        elif step == "first_question":
            order = []
            for key, words in (("employees", ("size", "employee", "how many")), ("timeline", ("timeline", "when", "timing")),
                               ("use_case", ("use case", "problem", "process", "why")), ("role_level", ("role", "title", "position"))):
                if any(w in low for w in words):
                    order.append(key)
            if not skip and order:
                p.question_order = order + [k for k in p.question_order if k not in order]
                reply = f"I'll ask about {order[0].replace('_', ' ')} first."
            elif not skip:
                reply, advance = "Pick one: company size, timeline, use case or role?", False
        elif step == "handoff":
            if not skip:
                mins = re.search(r"(\d+)\s*(min|minute|hour|hr|h)", low)
                if mins:
                    n = int(mins.group(1))
                    p.handoff_sla_minutes = n * 60 if mins.group(2).startswith(("hour", "hr", "h")) else n
                name = re.split(r"[,;]| within | in ", t, maxsplit=1)[0].strip()
                if name and not name.lower().startswith(("within", "in ")):
                    p.handoff_owner = name[:80]
                reply = f"{p.handoff_owner} takes ready leads and should respond within {p.handoff_sla_minutes} minutes."
                if not mins:
                    reply += " You did not give a response time, so I kept the default."
        elif step == "hours":
            if not skip:
                tz = next((z for z in re.findall(r"[A-Za-z]+/[A-Za-z_]+", t)), None)
                if tz and tz in available_timezones():
                    p.timezone = tz
                elif tz:
                    reply, advance = f"I do not know the time zone '{tz}'. Use a name like Europe/Berlin or America/Chicago.", False
                hrs = re.findall(r"\b(\d{1,2})\b", t)
                if advance and len(hrs) >= 2 and 0 <= int(hrs[0]) < int(hrs[1]) <= 24:
                    p.quiet_end_hour, p.quiet_start_hour = int(hrs[0]), int(hrs[1])
                if advance:
                    p.business_days_only = "weekend" not in low and "every day" not in low
                    reply = f"Messages go out {p.quiet_end_hour}:00 to {p.quiet_start_hour}:00 {p.timezone}" + (", weekdays only." if p.business_days_only else ".")
        elif step == "followups":
            if not skip:
                days = [int(x) for x in re.findall(r"\d+", t)][:3]
                if "no follow" in low or "none" in low or "never" in low:
                    p.followup_days = []
                    reply = "No automatic follow-ups."
                elif days and all(0 < d <= 60 for d in days):
                    p.followup_days = sorted(set(days))
                    reply = f"Follow-ups after day(s) {', '.join(map(str, p.followup_days))} if there is no reply."
                else:
                    reply, advance = "Give me day numbers, for example '2 and 7', or say 'no follow-ups'.", False
        elif step == "mode":
            if "auto" in low:
                p.mode = "auto_followups"
                reply = "Follow-ups will send automatically after every safety check passes. First messages still need your approval."
            else:
                p.mode = "approve"
                reply = "You approve every message. That is the safest start."
        elif step == "model":
            reply = ("Good. The agent works fully offline with rules and templates, and it says so on every page. "
                     "If you later want a model to read free-text replies, set ANTHROPIC_API_KEY and ILA_ANTHROPIC_MODEL on the machine "
                     "running the agent. Each model-extracted fact must quote the lead's own words, or it is discarded.")
        elif step == "confirm":
            if low in ("confirm", "yes", "save", "looks good", "ok"):
                p.configured = True
                save_profile(self.db, p)
                st = {"step": len(STEPS) - 1, "draft": None, "started": True}
                self.db.set_setting("chat_state", st)
                self._say("Saved. The agent now uses these settings. You can edit them under Settings or run this chat again.")
                return self.history()
            reply, advance = "Say 'confirm' to save, or 'change size', 'change hours', and so on.", False
        if advance and step != "confirm":
            st["step"] += 1
            st["draft"] = p.to_dict()
            self.db.set_setting("chat_state", st)
            nxt = STEPS[st["step"]]
            msg = (reply + "\n\n" if reply else "") + (_summary(p) if nxt == "confirm" else self._prompt(nxt))
            self._say(msg)
        else:
            st["draft"] = p.to_dict()
            self.db.set_setting("chat_state", st)
            self._say(reply or self._prompt(step))
        return self.history()

    def _prompt(self, step):
        return {
            "offer": "What does your company sell, and to whom?",
            "size": "What is the smallest company that is a good customer? Give a number of employees (add a maximum if there is one).",
            "roles": "Which job levels usually buy? For example: founder, VP, director, manager.",
            "exclusions": "Who should the agent not pursue? For example students, agencies, or competitor domains like rival.com.",
            "first_question": "When a lead gives little information, what do you most want to know first: company size, timeline, use case or role?",
            "handoff": "Who takes over a ready lead, and how fast must they respond? For example: 'Dana, within 30 minutes'.",
            "hours": "When may the agent write to people? Give a time zone and hours, for example 'America/Chicago, 9 to 17'.",
            "followups": "If a lead does not reply, after how many days should I follow up? For example '2 and 7', or 'no follow-ups'.",
            "mode": "Do you want to approve every message (recommended at first), or let follow-ups send automatically once checks pass?",
            "model": "Last thing: do you want me to explain how to add an AI model later? (yes or skip)",
            "confirm": "Say 'confirm' to save.",
        }[step]
