"""Runtime configuration. Secrets only ever come from environment variables."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("ILA_DB", str(ROOT / "data" / "ila.db"))

# Secrets: read from the environment only. They are never stored in the database,
# never returned by the API and never written to logs.
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
ANTHROPIC_MODEL_ENV = "ILA_ANTHROPIC_MODEL"
GHL_TOKEN_ENV = "GHL_PRIVATE_INTEGRATION_TOKEN"
GHL_LOCATION_ENV = "GHL_LOCATION_ID"
INTAKE_SECRET_ENV = "ILA_INTAKE_SECRET"

# GoHighLevel's published Ed25519 public key for X-GHL-Signature, copied from
# https://marketplace.gohighlevel.com/docs/webhook/WebhookIntegrationGuide.md
GHL_ED25519_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAi2HR1srL4o18O8BRa7gVJY7G7bupbN3H9AwJrHCDiOg=
-----END PUBLIC KEY-----
"""

LOOKS_LIKE_SECRET = ("sk-", "sk_", "pit-", "AKIA", "xoxb-", "ghp_", "AIza")


def secret_status() -> dict:
    """Which optional integrations are configured. Reports presence only, never values."""
    return {
        "model_key_present": bool(os.environ.get(ANTHROPIC_API_KEY_ENV)),
        "model_name_present": bool(os.environ.get(ANTHROPIC_MODEL_ENV)),
        "ghl_token_present": bool(os.environ.get(GHL_TOKEN_ENV)),
        "ghl_location_present": bool(os.environ.get(GHL_LOCATION_ENV)),
        "intake_secret_present": bool(os.environ.get(INTAKE_SECRET_ENV)),
    }
