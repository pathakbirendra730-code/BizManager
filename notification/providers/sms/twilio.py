"""
notification/providers/sms/twilio.py — Twilio SMS provider.

Reads from platform_settings first (App Admin -> Settings -> Twilio),
falling back to TWILIO_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM env vars.

Requires the `twilio` package. Not installed by default — add it to
requirements.txt only if you actually use Twilio.
"""

import os

from .. import SMSProvider
from ...exceptions import SendError, ProviderNotConfiguredError


def _setting(key: str, env_key: str) -> str:
    try:
        from utils.platform_settings import get_setting
        val = get_setting(key).strip()
        if val:
            return val
    except Exception:
        pass
    return os.environ.get(env_key, "").strip()


class TwilioSMSProvider(SMSProvider):
    name = "twilio"

    def _sid(self):   return _setting("twilio_sid", "TWILIO_SID")
    def _token(self): return _setting("twilio_auth_token", "TWILIO_AUTH_TOKEN")
    def _from(self):  return _setting("twilio_from", "TWILIO_FROM")

    def is_configured(self) -> bool:
        return bool(self._sid() and self._token() and self._from())

    def send(self, to_mobile: str, message: str) -> bool:
        self.require_configured()
        try:
            from twilio.rest import Client
        except ImportError as e:
            raise ProviderNotConfiguredError(
                "twilio package is not installed. Run: pip install twilio"
            ) from e

        try:
            from twilio.http.http_client import TwilioHttpClient
        except ImportError:
            TwilioHttpClient = None

        try:
            # Explicit 15s timeout, matching every other provider in this
            # package (SMTP/Gmail/Fast2SMS/MSG91/SMS-Brevo/SendGrid all use
            # timeout=15) — Twilio's SDK has no bounded default timeout on
            # its own, which would otherwise let a hung request block this
            # worker thread indefinitely, defeating the whole point of
            # NotificationManager's bounded retry/failover logic.
            if TwilioHttpClient is not None:
                http_client = TwilioHttpClient(timeout=15)
                client = Client(self._sid(), self._token(), http_client=http_client)
            else:
                client = Client(self._sid(), self._token())
            msg = client.messages.create(
                body=message, from_=self._from(), to=to_mobile
            )
            return msg.sid is not None
        except Exception as e:
            raise SendError(f"Twilio send failed: {e}") from e
