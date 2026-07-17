"""
notification/utils.py — Shared helpers: template rendering + env config.

Templates live in notification/templates/ and are rendered with a
standalone Jinja2 environment (not Flask's), so this package can be
used from anywhere — including scripts and background jobs — without
needing an active Flask app/request context.
"""

import os
from datetime import datetime
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .exceptions import TemplateRenderError

_TEMPLATE_DIR = Path(__file__).parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)
_env.globals["current_year"] = lambda: datetime.utcnow().year


def render_template(name: str, **context) -> str:
    """Render an HTML email template from notification/templates/."""
    try:
        template = _env.get_template(name)
        return template.render(**context)
    except Exception as e:
        raise TemplateRenderError(f"Failed to render '{name}': {e}") from e


# ── Environment config helpers ─────────────────────────────────────────────
# Read live on every call (never cached at import time) so changes to env
# vars — or to os.environ in tests — take effect immediately, matching the
# convention already used elsewhere in this app (utils/otp_service.py).

def app_env() -> str:
    return os.environ.get("APP_ENV", "development").lower()


def is_production() -> bool:
    return app_env() == "production"


def default_from_address() -> str:
    """
    The BARE email address (no display name) messages appear to come
    from, if a provider doesn't have a more specific one configured.

    Always returns a bare address — e.g. "no-reply@yourdomain.com", never
    "BizManager <no-reply@yourdomain.com>" — because this value is used
    directly as the JSON "email" field by API-based providers (Brevo,
    SendGrid), which require a bare address and carry the display name
    separately (see brand_name() below, and each provider's own payload
    construction). SMTP_FROM — one of this function's fallback sources —
    is documented and conventionally set in this app's own .env.example
    in the combined "Name <email>" form, which is correct for an SMTP
    `From:` header (smtp.py uses it exactly that way) but is NOT a valid
    value for Brevo/SendGrid's sender-email field. Previously, setting
    SMTP_FROM in that documented format and using Brevo/SendGrid as the
    email provider meant Brevo received the entire "Name <email>" string
    as the sender address and rejected it as invalid — this was reported
    and traced to this exact function.

    email.utils.parseaddr() is the standard library's own RFC 5322
    address parser — it correctly extracts just the address portion
    whether the input is already bare or in the combined form, so this
    is safe regardless of which format any given setting happens to use.
    """
    from email.utils import parseaddr

    def _bare(value):
        return parseaddr(value)[1] if value else ""

    try:
        from utils.platform_settings import get_setting
        db_val = get_setting("mail_from").strip()
        bare = _bare(db_val)
        if bare:
            return bare
    except Exception:
        pass

    for candidate in (os.environ.get("MAIL_FROM"),
                     os.environ.get("SMTP_FROM"),
                     os.environ.get("SMTP_USER")):
        bare = _bare(candidate)
        if bare:
            return bare

    return "noreply@bizmanager.app"


def brand_name() -> str:
    try:
        from utils.platform_settings import get_setting
        db_val = get_setting("mail_from_name").strip()
        if db_val:
            return db_val
    except Exception:
        pass
    return os.environ.get("MAIL_BRAND_NAME", "BizManager")
