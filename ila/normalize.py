"""Identity and data-quality helpers. Everything here is deterministic and offline."""
import re
from typing import Optional

FREE_MAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "proton.me",
             "protonmail.com", "aol.com", "live.com", "msn.com", "gmx.com", "mail.com"}
DISPOSABLE = {"mailinator.com", "10minutemail.com", "guerrillamail.com", "tempmail.com", "yopmail.com",
              "trashmail.com", "sharklasers.com", "throwawaymail.com"}
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-']+@([A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+)$")


def normalize_email(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    e = raw.strip().lower()
    return e if EMAIL_RE.match(e) else None


def email_key(email: Optional[str]) -> Optional[str]:
    """Dedupe key: lowercase and without a +tag, so ann+demo@x.com and ann@x.com are one person."""
    if not email or "@" not in email:
        return None
    local, domain = email.split("@", 1)
    return local.split("+", 1)[0] + "@" + domain


def valid_tz(name) -> bool:
    from zoneinfo import ZoneInfo
    if not isinstance(name, str) or not name or len(name) > 64:
        return False
    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False


def email_domain(email: Optional[str]) -> Optional[str]:
    return email.split("@", 1)[1] if email and "@" in email else None


def email_quality(email: Optional[str]) -> dict:
    d = email_domain(email)
    return {"valid": bool(email), "free_mail": d in FREE_MAIL, "disposable": d in DISPOSABLE, "domain": d}


def normalize_phone(raw: Optional[str], default_cc: str = "1") -> Optional[str]:
    """Return an E.164-like string or None. Does not guess when the number is too short or too long."""
    if not raw:
        return None
    s = raw.strip()
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if s.startswith("00"):
        digits, plus = digits[2:], True
    if not plus:
        if not default_cc:
            return None   # no country code configured: do not guess
        if len(digits) == 10:
            digits = default_cc + digits
        elif len(digits) == 11 and digits.startswith(default_cc):
            pass
        else:
            return None
    if not (8 <= len(digits) <= 15):
        return None
    return "+" + digits


def parse_range(raw):
    """('51-200') -> (51, 200); ('1000+') -> (1000, None); anything else -> None."""
    t = str(raw or "").replace(",", "").strip()
    m = re.fullmatch(r"(\d+)\s*[-\u2013to]+\s*(\d+)", t)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.fullmatch(r"(\d+)\s*\+", t)
    return (int(m.group(1)), None) if m else None


def clean_name(s: Optional[str]) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip())
    if s and (s.islower() or s.isupper()):
        s = s.title()
    return s


def parse_employee_count(raw) -> Optional[int]:
    """Accepts 120, '51-200', '1,000+', '200 employees'. A range returns its lower bound (conservative)."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw) if raw > 0 else None
    t = str(raw).lower().replace(",", "")
    m = re.search(r"(\d+)\s*[-to–]+\s*(\d+)", t)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*\+", t)
    if m:
        return int(m.group(1))
    m = re.search(r"\d+", t)
    return int(m.group(0)) if m else None
