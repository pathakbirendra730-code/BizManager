"""
utils/business_settings.py — Per-business, owner-editable configuration.

Update_027 follow-up: Document Numbering (prefix, separator, suffix,
starting number, digit length, auto reset, manual numbering) moves from a
single platform-wide setting to something every business can configure
for itself, from its own Business Settings page — no super-admin needed.

Design — override with platform fallback, not a fork:
  • The schema is NOT redefined here. `BUSINESS_SETTINGS_SCHEMA` is simply
    the "Document Numbering" group already declared in
    utils/platform_settings.py's SETTINGS_SCHEMA — the exact same key
    names, labels, types, options, and validators. This means the App
    Admin form and every business's own form always agree on what a
    valid value looks like, and there's only ever one place to add a
    new numbering setting in the future.
  • get_business_setting() checks saas_business_settings (this business's
    own override) first; if this business has never saved that key, it
    falls back to utils.platform_settings.get_setting() — the platform
    default (itself falling back to the schema default). This means:
      - A business that never opens this page behaves exactly as it did
        before this module existed (reads the platform-wide value, same
        as every other business).
      - The App Admin "Document Numbering" settings remain meaningful as
        the platform-wide default for every business that hasn't
        customized its own — not orphaned dead settings.
      - A business can override any subset of the keys (e.g. just its
        own prefixes) while still inheriting the platform default for
        the rest.
  • set_business_setting() validates against the same schema/validators
    as the platform version, so a business owner can't save something
    the App Admin form itself would reject.
"""

from models.saas_auth import saas_fetchone, saas_execute, _is_postgres
from utils.platform_settings import SETTINGS_SCHEMA as _PLATFORM_SCHEMA, get_setting as _platform_get_setting

P = lambda: "%s" if _is_postgres() else "?"

# The one settings group that's currently business-overridable. Pulled
# straight from the platform schema (see module docstring) — not a copy.
_OVERRIDABLE_GROUP = "Document Numbering"
BUSINESS_SETTINGS_SCHEMA = [s for s in _PLATFORM_SCHEMA if s.get("group") == _OVERRIDABLE_GROUP]
_SCHEMA_BY_KEY = {s["key"]: s for s in BUSINESS_SETTINGS_SCHEMA}


def _business_row(business_id: int, key: str):
    return saas_fetchone(
        f"SELECT value FROM saas_business_settings WHERE business_id={P()} AND key={P()}",
        (business_id, key)
    )


def get_business_setting(business_id: int, key: str) -> str:
    """
    This business's own value for `key` if it has ever saved one,
    otherwise the platform-wide value (which itself falls back to the
    schema default) — see module docstring for the override/fallback
    design.
    """
    row = _business_row(business_id, key)
    if row is not None:
        return row["value"]
    return _platform_get_setting(key)


def get_business_bool_setting(business_id: int, key: str) -> bool:
    return get_business_setting(business_id, key).strip().lower() == "true"


def get_business_int_setting(business_id: int, key: str) -> int:
    """Numeric settings (Starting Number, Digit Length) always resolve to
    something via the platform/schema fallback, so this never has an
    empty value to fail on."""
    value = get_business_setting(business_id, key).strip()
    if value:
        return int(value)
    schema = _SCHEMA_BY_KEY.get(key)
    return int(schema["default"]()) if schema else 0


def set_business_setting(business_id: int, key: str, value: str, updated_by=None) -> None:
    schema = _SCHEMA_BY_KEY.get(key)
    if schema is None:
        raise ValueError(f"Unknown business setting: {key}")

    validator = schema.get("validate")
    if validator:
        error = validator(value)
        if error:
            raise ValueError(f"{schema['label']}: {error}")

    p = P()
    if _is_postgres():
        saas_execute(
            f"""INSERT INTO saas_business_settings (business_id, key, value, updated_by, updated_at)
                VALUES ({p},{p},{p},{p}, NOW())
                ON CONFLICT (business_id, key) DO UPDATE
                SET value={p}, updated_by={p}, updated_at=NOW()""",
            (business_id, key, value, updated_by, value, updated_by),
            returning=None
        )
    else:
        saas_execute(
            f"""INSERT INTO saas_business_settings (business_id, key, value, updated_by, updated_at)
                VALUES ({p},{p},{p},{p}, datetime('now'))
                ON CONFLICT (business_id, key) DO UPDATE
                SET value=excluded.value, updated_by=excluded.updated_by,
                    updated_at=datetime('now')""",
            (business_id, key, value, updated_by),
            returning=None
        )


def reset_business_setting(business_id: int, key: str) -> None:
    """Delete this business's override for `key`, reverting it to
    whatever the platform-wide default currently is."""
    saas_execute(
        f"DELETE FROM saas_business_settings WHERE business_id={P()} AND key={P()}",
        (business_id, key),
        returning=None
    )


def reset_all_business_numbering_settings(business_id: int) -> None:
    """Clear every Document Numbering override for this business in one
    go — the "Reset to platform defaults" button on the business
    settings page."""
    for schema in BUSINESS_SETTINGS_SCHEMA:
        reset_business_setting(business_id, schema["key"])


def all_business_settings(business_id: int) -> list:
    """Every business-overridable setting, in schema order, with this
    business's current effective value and whether it's a business-level
    override or an inherited platform default — what the Business
    Settings page renders itself from. Each entry also carries
    `is_prefix` (True for the six per-document-type prefix fields, False
    for the general format fields) so templates can group them without
    needing a regex test."""
    result = []
    for s in BUSINESS_SETTINGS_SCHEMA:
        entry = dict(s)
        entry["value"] = get_business_setting(business_id, s["key"])
        entry["is_prefix"] = s["key"].startswith("prefix_")
        entry["is_override"] = _business_row(business_id, s["key"]) is not None
        result.append(entry)
    return result
