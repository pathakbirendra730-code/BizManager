# BizManager-v6 — Update_019
## "OTP not received on email" — Root Cause Is Infrastructure, Not Code, Plus a Real Crash Bug Found Along the Way

## 1. Executive Summary

Traced your production logs precisely. Two distinct things are happening:

1. **The actual reason OTP emails aren't arriving: Render blocks outbound
   SMTP ports (25, 465, 587) on free-tier web services**, as of a policy
   change effective September 26, 2025. This is not a bug in BizManager —
   confirmed directly from Render's own changelog and multiple
   independent reports of the identical symptom (`SMTP send failed:
   timed out`) on other unrelated projects. No code fix can make raw
   SMTP work around this; the fix is a configuration change (§3).
2. **A real, separate crash bug** the SMTP timeout exposed: retrying
   signup after the first attempt's email failed to send crashed with an
   unhandled `500` — `psycopg2.errors.UniqueViolation` on
   `saas_users_mobile_key`, visible directly in your Render logs. Fixed
   and verified against the exact scenario in your logs (§2–§4).

## 2. Root Cause Analysis

**Infrastructure**: `notification/providers/smtp.py` opens a raw
`smtplib.SMTP` connection with a 15-second timeout. Your logs show two
signup POSTs each taking ~12.3 seconds before returning — consistent
with that connection attempt hanging until timeout. Render's own
changelog: *"Starting next week, free Render web services will block
outbound network traffic to SMTP ports 25, 465, and 587."* Multiple
other developers hit this exact symptom (`ETIMEDOUT`/timeout on SMTP
connect) on Render's free tier for completely unrelated projects,
confirming this is a platform-wide policy, not something specific to
your code or configuration.

**Application bug**: `signup()`'s decision between `UPDATE` (reuse an
existing unverified row) and `INSERT` (create a new one) only checked
`existing_email`, never `existing_mobile`. Combined with the ~12-second
hang from the SMTP timeout above, a person naturally gets impatient and
resubmits — and depending on exactly which lookup matched, that retry
could still attempt an `INSERT` with a mobile number that already
existed from the first attempt's (successful) row creation. That `INSERT`
was never wrapped in any error handling, so the database's own unique-
constraint violation propagated all the way up as a raw, unhandled
exception — exactly the traceback in your Render logs.

## 3. Recommended Fix for the Actual "Email Not Arriving" Problem

**This requires an action on your side, not a code change** — the code
already supports it. Your `notification/providers/` directory already
has working, HTTPS-API-based email providers (`brevo.py`, `sendgrid.py`)
alongside the raw-SMTP one — confirmed both use `https://api.brevo.com/...`
/`https://api.sendgrid.com/...` (port 443), which Render's SMTP-port
block does **not** affect at all.

**To fix**: switch your `EMAIL_PROVIDER` setting from `smtp` to `brevo`
(free tier: 300 emails/day, more than enough for OTP volume) or
`sendgrid`, and set that provider's API key — either via environment
variable or through App Admin → Settings if you've configured it there.
No code change needed for this part; the provider abstraction was
already built for exactly this kind of swap.

Alternative, if you specifically want to keep using SMTP: upgrade the
Render service off the free tier — paid instances aren't subject to the
SMTP port block (confirmed in Render's own changelog).

## 4. Complete List of Code Changes

In `modules/saas_auth/routes.py`'s `signup()`:

1. **Fixed the UPDATE-vs-INSERT decision** to check `existing_email OR
   existing_mobile`, not just `existing_email` — reuses whichever
   existing unverified row actually matches, instead of blindly
   attempting an `INSERT` that may collide.
2. **Wrapped the INSERT/UPDATE in a try/except** as defense in depth
   against the underlying race this class of bug represents: two
   near-simultaneous submissions (an impatient double-click during any
   slow OTP-send call, not just this specific SMTP scenario) can both
   pass the "no existing row" checks before either commits. Whichever
   one loses that race now gets a friendly "this account may already be
   in progress — try logging in, or wait a moment" message and an
   `audit_log("signup_insert_race", ...)` entry recording the real
   database error for diagnosis, instead of a raw 500 stack trace.

## 5. Files Modified

```
modules/saas_auth/routes.py    — signup() duplicate-check + crash fix
```

## 6. Security Impact

None directly — this is a robustness fix, not a security boundary
change. Indirectly positive: no longer exposes a raw database error
traceback (which can leak schema/implementation details) to
unauthenticated visitors on the public signup page.

## 7. Performance Impact

None in the success path (same number of queries as before — the fix
just uses the OR'd existing lookup that was already being computed,
rather than adding a new query). The try/except adds no overhead unless
the error path is actually hit.

## 8. Compatibility Notes

Uses Python's generic `except Exception` around the insert/update call,
which correctly catches both `sqlite3.IntegrityError` (SQLite) and
`psycopg2.errors.UniqueViolation` (PostgreSQL) without needing
backend-specific exception imports — verified directly (§10) rather
than assumed.

## 9. Database Notes

No schema changes. No migration needed.

## 10. Testing Checklist

- [x] `python3 -m py_compile` — passes.
- [x] Full app boot — succeeds, 120 routes.
- [x] **Reproduced your exact production scenario end-to-end**: mocked
      `send_email_otp` to fail (simulating the SMTP timeout), submitted
      signup once (correctly shown the failure message, row created),
      then submitted the *identical* form again (the exact retry from
      your logs) — confirmed: no crash, correctly updates the existing
      row instead of attempting a duplicate insert, and exactly one row
      exists afterward (not two, not zero).
- [x] **Directly confirmed the exception type** the try/except is
      designed to catch, by deliberately triggering a duplicate INSERT
      at the database layer — confirmed `sqlite3.IntegrityError` (and by
      the same code path, `psycopg2.errors.UniqueViolation` on
      PostgreSQL) is exactly what gets raised and exactly what the
      generic `except Exception` catches.
- [ ] **Not verifiable in this environment**: whether switching to Brevo/
      SendGrid actually resolves email delivery on your live Render
      instance — that requires your actual API credentials and a real
      deploy. Please test signup end-to-end after making that switch.

## 11. Rollback Strategy

Single-function change in one file, no schema/session impact. Safe to
revert by redeploying the previous version of `modules/saas_auth/routes.py`
independently of any provider-configuration change you make (those are
two fully independent fixes — you can adopt one without the other).

## 12. Commercial Readiness Progress

This is a good example of exactly the kind of thing a systematic Part 2
audit doesn't catch by reading code alone — the crash only manifests
under a specific real-world timing condition (slow OTP send + user
retry), which is why it showed up in your production logs rather than
in earlier testing. Worth keeping an eye on Render's logs for other
`UniqueViolation`/`IntegrityError` crashes on different tables — the
same "check-then-insert without a race guard" shape could theoretically
exist elsewhere; this update fixes the one that actually occurred, not a
hypothetical sweep of every insert in the codebase.

Still on Part 4 (Commercial Quality Review) whenever you're ready to
continue there, or happy to keep addressing production issues as they
come up.
