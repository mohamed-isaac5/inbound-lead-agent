"""Business profile: everything the operator teaches the agent about their business."""
from dataclasses import dataclass, field, asdict
from typing import List, Optional

DEFAULT_QUESTION_ORDER = ["employees", "timeline", "use_case", "role_level"]


@dataclass
class Profile:
    company: str = "Brightlane (fictional demo company)"
    offer: str = "Workflow automation platform for mid-market operations teams"
    sender_name: str = "Jordan from Brightlane"
    signature: str = "Jordan Lee, Brightlane (demo)"
    min_employees: int = 50
    max_employees: Optional[int] = None
    target_levels: List[str] = field(default_factory=lambda: ["exec", "vp", "director", "manager"])
    excluded_keywords: List[str] = field(default_factory=lambda: ["competitor", "agency partnership"])
    competitor_domains: List[str] = field(default_factory=lambda: ["flowrival.example", "automatr.example"])
    question_order: List[str] = field(default_factory=lambda: list(DEFAULT_QUESTION_ORDER))
    hot_timeline_days: int = 90
    nurture_timeline_days: int = 180
    max_questions: int = 2
    handoff_owner: str = "Sales team (round robin)"
    handoff_sla_minutes: int = 60
    escalate_to: str = "Head of Sales"
    timezone: str = "America/New_York"
    quiet_start_hour: int = 19   # local time: no sends from 19:00 ...
    quiet_end_hour: int = 8      # ... until 08:00
    business_days_only: bool = True
    followup_days: List[int] = field(default_factory=lambda: [2, 7])
    max_touches: int = 4
    daily_send_cap: int = 50
    mode: str = "approve"        # approve | auto_followups
    channel: str = "email"
    decline_policy: str = "size_only"   # size_only: draft a polite decline only for size; none: never draft a decline
    default_cc: str = "1"        # assumed country code for phone numbers without a +; blank means "never guess"
    postal_address: str = ""   # sender identity and postal address printed in every email footer; required before real sending
    tone: str = "friendly, concise, plain English"
    configured: bool = False

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        known = {k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__}
        return cls(**known)


def load_profile(db) -> Profile:
    return Profile.from_dict(db.get_setting("profile", {}))


def save_profile(db, p: Profile):
    db.set_setting("profile", p.to_dict())


def validate_profile(p: "Profile") -> Optional[str]:
    """Return an error message, or None when the profile is safe to store and use."""
    from .normalize import valid_tz

    def is_int(v, lo, hi):
        return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi
    if not valid_tz(p.timezone):
        return "timezone must be a valid IANA name such as America/New_York"
    if not is_int(p.min_employees, 1, 1000000):
        return "min_employees must be a whole number of at least 1"
    if p.max_employees is not None and not (is_int(p.max_employees, 1, 10000000) and p.max_employees >= p.min_employees):
        return "max_employees must be empty or at least min_employees"
    if not (is_int(p.quiet_start_hour, 1, 24) and is_int(p.quiet_end_hour, 0, 23) and p.quiet_end_hour < p.quiet_start_hour):
        return "sending hours must satisfy 0 <= end < start <= 24"
    if not is_int(p.max_touches, 1, 20):
        return "max_touches must be 1 to 20"
    if not is_int(p.max_questions, 0, 5):
        return "max_questions must be 0 to 5"
    if not is_int(p.daily_send_cap, 1, 10000):
        return "daily_send_cap must be 1 to 10000"
    if not is_int(p.handoff_sla_minutes, 1, 10080):
        return "handoff_sla_minutes must be 1 to 10080"
    if not isinstance(p.followup_days, list) or len(p.followup_days) > 5 or not all(is_int(d, 1, 90) for d in p.followup_days):
        return "followup_days must be up to 5 whole numbers between 1 and 90"
    if not is_int(p.hot_timeline_days, 1, 1000) or not is_int(p.nurture_timeline_days, 1, 3000):
        return "timeline thresholds must be whole numbers of days"
    if p.mode not in ("approve", "auto_followups"):
        return "mode must be approve or auto_followups"
    if p.decline_policy not in ("size_only", "none"):
        return "decline_policy must be size_only or none"
    if not isinstance(p.default_cc, str) or not (p.default_cc == "" or (p.default_cc.isdigit() and len(p.default_cc) <= 3)):
        return "default_cc must be blank or 1 to 3 digits"
    for name in ("company", "offer", "sender_name", "signature", "handoff_owner", "escalate_to", "postal_address"):
        v = getattr(p, name)
        if not isinstance(v, str) or len(v) > 300:
            return f"{name} must be text of at most 300 characters"
    return None
