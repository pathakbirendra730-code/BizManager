# BizManager-v6 — Hotfix (post Update_033)
## Delete Business: Internal Server Error on Postgres (production)

## Root Cause

`utils/business_deletion.py::delete_business_completely()` opened its
own raw connection/cursor (`conn = get_saas_db(); c = conn.cursor()`)
for the multi-table delete transaction, then read a `COUNT(*)` result
with **positional indexing**: `c.fetchone()[0]`.

That works on SQLite (`sqlite3.Row` supports both `row[0]` and
`row["col"]`), which is what my local testing used exclusively — so
every test in Update_033's testing report passed. But
`models/saas_auth.py::get_saas_db()` configures Postgres connections
with `psycopg2.extras.RealDictCursor`, whose rows are **dict-only** —
`row[0]` raises `KeyError`, not a value. That exception propagated up
through `delete_business_execute()` uncaught, producing the Internal
Server Error you hit on the live Render/Postgres deployment, right
after the PIN + "DELETE" confirmation submitted.

Every *other* read in that same file (`count_business_records()`, the
confirmation-screen preview) already went through `saas_fetchone()`,
which correctly wraps results in `dict(row)` — that's why the
confirmation page with the record-count preview loaded and displayed
fine; only the final execute step, which used a raw cursor, crashed.

## Fix

`utils/business_deletion.py` — the one `fetchone()` call on the raw
cursor now aliases the count column explicitly and reads it by key:

```python
c.execute(f"SELECT COUNT(*) as cnt FROM {table} WHERE business_id={p}", (business_id,))
deleted_counts[table] = c.fetchone()["cnt"]
```

This is correct on **both** backends — `sqlite3.Row` supports
key-based access too, so nothing changes for SQLite; on Postgres it now
reads the `RealDictRow` correctly instead of crashing.

## Verification

- Re-ran the full deletion test against SQLite — still passes
  (confirms no regression on the path that was already working).
- Directly reproduced the failure mode with a dict-only object standing
  in for `RealDictRow`: confirmed `row[0]` raises `KeyError` (the
  production crash) and `row["cnt"]` (the fix) returns the correct
  value.
- Scanned the rest of the codebase for the same anti-pattern
  (`fetchone()[0]` on a raw, non-`saas_fetchone`-wrapped cursor) —
  found only in pre-existing legacy single-tenant code
  (`utils/template_products.py`, `models/database.py`), which is
  SQLite-only by design (`sqlite3`-specific calls like
  `last_insert_rowid()`) and not part of the SaaS/Postgres code path,
  so out of scope for this fix.
- Full repo Python compile sweep: clean.

## Why local testing didn't catch this

Update_033's testing was run exclusively against a local SQLite
database (no Postgres instance available in that environment). This is
a real gap: SQLite's `Row` object is more permissive than
`RealDictCursor`, so a bug like this one is invisible in SQLite-only
testing and only surfaces against the production database backend. If
a Postgres test database becomes available, it's worth re-running the
Update_030–033 test suites against it directly rather than relying on
SQLite as a stand-in for correctness on both backends going forward.

## Deployment

Same as any other code-only fix — no schema changes, no migration, no
new dependencies. Safe to deploy immediately.
