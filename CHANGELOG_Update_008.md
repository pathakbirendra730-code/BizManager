# BizManager-v6 — Update_008
## Fix: Saving Platform Settings (API keys) crashed with 500 on PostgreSQL

**1 file changed.**

---

## The bug (a regression introduced by Update_006 — my mistake)

Update_006 changed `saas_execute()` so that on PostgreSQL it automatically
appends `RETURNING id` to any `INSERT` statement that doesn't already have
a `RETURNING` clause, since psycopg2's `cursor.lastrowid` is always `None`
and most tables in this schema use `id` as their primary key.

`platform_settings` is the **one table in the whole schema that doesn't
have an `id` column** — its primary key is `key` (see
`models/saas_auth.py`):

```sql
CREATE TABLE platform_settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL DEFAULT '',
    updated_by      INTEGER,
    updated_at      TEXT DEFAULT (datetime('now'))
)
```

So when `utils/platform_settings.py:set_setting()` saved a setting (e.g.
an API key) via `saas_execute(...)`, Update_006's new logic silently
appended `RETURNING id` to the `INSERT INTO platform_settings ...`
statement — and PostgreSQL rejected it with `column "id" does not exist`,
an unhandled exception surfacing as the generic Internal Server Error you
hit on the Settings page.

This never showed up in the audit because every other table in the schema
does use `id`; `platform_settings` was the single exception, and it's the
only table this bug could affect.

## The fix

`saas_execute()` already supports a `returning=None` parameter to opt out
of the auto-`RETURNING` behavior for exactly this kind of table. Both
`INSERT` statements in `set_setting()` (the PostgreSQL upsert and the
SQLite upsert) now pass it explicitly:

```python
saas_execute(sql, params, returning=None)
```

`set_setting()` never used the return value anyway, so this has zero
effect on behavior other than removing the crash.

## Files changed

```
utils/platform_settings.py
```

## Testing

- `python3 -m py_compile utils/platform_settings.py` — passes.
- Re-checked the rest of the schema: `platform_settings` is confirmed to be
  the only table without an `id` primary key, and the only place that
  inserts into it via `saas_execute()` is `set_setting()` — so this was
  the single remaining call site affected by this edge case.
- Not run against a live PostgreSQL instance in this environment. Please
  redeploy and re-test saving a setting (e.g. an email/SMS provider API
  key) on the App Admin Settings page.
