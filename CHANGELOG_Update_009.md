# BizManager-v6 — Update_009
## Fix: "Internal Server Error" on Admins tab, and the same bug across every timestamp shown in the UI

**15 files changed.**

---

## The bug

Clicking **Admins** crashed with a 500 the moment your own account had a
real `last_login` value (i.e., right after you'd successfully logged in
once) because of `templates/app_admin/list_admins.html`:

```jinja
{{ a.last_login[:16] | replace('T',' ') if a.last_login else 'Never' }}
```

Same root cause as the OTP bug from Update_007, different symptom:
`app_admins.last_login` is `TEXT` on SQLite (a plain string — slicing
works) but `TIMESTAMP` on PostgreSQL, where **psycopg2 hands templates a
native `datetime.datetime` object**, not a string. Jinja's `[:16]` slice
only works on strings/lists — on a `datetime` object it raises
`'datetime.datetime' object is not subscriptable`, an unhandled exception
→ generic 500 page.

Once I knew to look for this exact pattern, I searched the entire
codebase for it and found it in **13 more places** — every one of them
would have broken the same way on PostgreSQL the first time that
particular timestamp was populated, even though nothing in that view was
touched by earlier updates:

| Template | Field |
|---|---|
| `app_admin/list_admins.html` | `a.last_login` *(what you hit)* |
| `app_admin/all_businesses.html` | `b.created_at` |
| `app_admin/all_users.html` | `u.created_at` |
| `app_admin/all_invites.html` | `inv.expires_at` |
| `saas_auth/team.html` | `member.last_login`, `inv.expires_at` (2 spots) |
| `saas_auth/profile.html` | `log.created_at` |
| `saas_auth/business_settings.html` | `biz.trial_ends_at` |
| `saas_business/customers/history.html` | `customer.created_at`, `inv.created_at` (2 spots) |
| `saas_business/billing/history.html` | `inv.created_at` |
| `saas_business/billing/invoice.html` | `invoice.created_at` |
| `saas_business/purchase/view.html` | `purchase.created_at` |
| `saas_business/purchase/history.html` | `p.created_at` |

...plus one instance of the identical pattern in **Python** code (not a
template) — `modules/saas_business/billing.py`, in the payment-reminders
export:
```python
r["date"] = r["created_at"][:10] if r.get("created_at") else ""
```

## The fix

Rather than patch each of these 15 spots with one-off conditionals, added
two small shared helpers that both accept either a string or a `datetime`
object and produce the same display string either way:

- **`dtfmt`** — a new Jinja template filter (registered in `app.py`), for
  use in templates:
  ```jinja
  {{ a.last_login | dtfmt(16) | replace('T',' ') if a.last_login else 'Never' }}
  ```
- **`fmt_dt(value, chars=16)`** — a Python-side twin in `models/saas_auth.py`
  (next to the `parse_dt()` helper from Update_007), for use in route code:
  ```python
  r["date"] = fmt_dt(r.get("created_at"), 10)
  ```

Both normalize a `datetime` object to the same ISO string SQLite would
have returned, then slice — so the **visible output is unchanged** on
both backends; this only fixes the crash.

All 14 template call sites and the 1 Python call site were updated to use
these helpers instead of raw slicing.

## Files changed

```
app.py                                              — new `dtfmt` template filter
models/saas_auth.py                                 — new `fmt_dt()` helper
modules/saas_business/billing.py                    — use fmt_dt()
templates/app_admin/list_admins.html                — the bug you hit
templates/app_admin/all_businesses.html
templates/app_admin/all_users.html
templates/app_admin/all_invites.html
templates/saas_auth/team.html
templates/saas_auth/profile.html
templates/saas_auth/business_settings.html
templates/saas_business/customers/history.html
templates/saas_business/billing/history.html
templates/saas_business/billing/invoice.html
templates/saas_business/purchase/view.html
templates/saas_business/purchase/history.html
```

## Testing

- `python3 -m py_compile` on all modified `.py` files — passes.
- All modified templates re-parsed with `jinja2.Environment().parse()` to
  confirm valid syntax — passes.
- Re-swept the whole repo for the same slicing pattern
  (`created_at[`, `last_login[`, `expires_at[`, `trial_ends_at[`,
  `updated_at[`, plus `.split('T'/' ')` variants and misapplied
  `.strftime()` calls) — nothing else found. All remaining `.strftime()`
  calls in the codebase are on fresh `datetime.now()` objects, which are
  always real `datetime` instances regardless of DB backend, so those are
  fine as-is.
- Not run against a live PostgreSQL instance in this environment — please
  redeploy and click through: Admins tab, All Users, All Businesses,
  Invites, Team page, Profile (activity log), Business Settings (trial
  banner), and a customer/invoice/purchase history page, to confirm each
  renders instead of 500ing once those rows have real timestamp data.

## Note on process

This is the second follow-up patch after Update_006 (Update_007 was the
OTP/PIN datetime bug, Update_008 was the platform_settings regression).
Both this and Update_007 are the *same underlying class of bug*
(SQLite returns timestamp columns as strings; PostgreSQL returns them as
`datetime` objects) showing up in different code paths — Update_007 was
comparisons via `datetime.fromisoformat()`, this one is display slicing.
I've now swept the whole codebase for both variants of the pattern, so I
don't expect further instances of this specific class of bug to surface
page-by-page.
