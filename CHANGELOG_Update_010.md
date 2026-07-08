# BizManager-v6 — Update_010
## New features: Admin user management, OTP login channel choice, verified contact updates

**9 files changed (2 new backend files touched, 5 new templates, 2 existing templates extended).**
This update adds features, unlike Updates 006–009 which were pure bug fixes.

---

## 1. View / edit / delete SaaS user accounts from the App Admin panel

**Where:** App Admin → All Users → click **View →** on any row.

- **View** (`GET /app-admin/users/<id>`): full account details plus every
  business the user belongs to and their role in each.
- **Edit** (any app admin): update full name, email, mobile number. Blocks
  the save if another account already uses that email/mobile.
- **Activate / Deactivate** (any app admin): flips `is_active` — blocks
  login without touching their data. This is the safer option for most
  cases (e.g. temporarily disabling an account).
- **Delete permanently** (super admin only, since it's irreversible): removes
  the `saas_users` row and their `saas_user_roles` memberships. **Blocked**
  if the user is the sole owner of any business (checked server-side) —
  you'll get a message telling you which business is blocking it, so a
  business is never left with no owner. Reassign ownership or delete that
  business first in that case.

This is exactly what you'd use to remove a demo/test account, as long as
it isn't the only owner of a business — if it is, delete/reassign that
business first, then delete the account.

Files: `modules/app_admin/dashboard.py` (routes), 
`templates/app_admin/view_user.html` (new), 
`templates/app_admin/all_users.html` (added a "View →" link per row).

---

## 2. Login with OTP — choice of mobile SMS or email

**Where:** Login page → **"Login with OTP instead →"** link.

Previously the only SaaS-user login method was mobile number + PIN. Now:

1. `/saas/login-otp` — enter your mobile number, pick **SMS** or **Email**
   for the OTP.
2. `/saas/login-otp/verify` — enter the 6-digit code; on success you're
   signed in exactly like PIN login (single business → straight in,
   multiple → choose one, none yet → business setup).

Rate-limited the same way as PIN login (10 attempts / 10 minutes per
mobile+IP for requesting, 10 / 10 minutes for verifying), and reuses the
existing OTP infrastructure (`otp_service`, `otp_manager` audit logging) —
no new OTP delivery mechanism, just a new way to request/consume one for
login instead of only for signup/PIN-reset.

Files: `modules/saas_auth/routes.py` (`login_otp`, `login_otp_verify`
routes, and `resend_otp` extended to serve them), 
`templates/saas_auth/login_otp.html` (new), 
`templates/saas_auth/login_otp_verify.html` (new), 
`templates/saas_auth/login.html` (added the entry link).

---

## 3. Change mobile number / email from Profile, with verification

**Where:** Profile page → new **Contact Details** section.

Entering a new email or mobile number and clicking **Change** does **not**
update the account immediately — it sends an OTP to the **new** value first
(not the old one), and only updates the record once that OTP is confirmed.
This stops someone from silently taking over an account by typo-ing in a
different contact detail without proving they actually control it.

- Change email: `POST /saas/profile/change-email` → OTP sent to the new
  email → confirm at `/saas/profile/change-email/confirm`.
- Change mobile: `POST /saas/profile/change-mobile` → OTP sent (SMS) to
  the new mobile → confirm at `/saas/profile/change-mobile/confirm`.
- Both block the change if another account already uses that email/mobile,
  and are rate-limited (5 requests / 10 minutes per user).

Files: `modules/saas_auth/routes.py` (4 new routes), 
`templates/saas_auth/confirm_change.html` (new, shared by both flows), 
`templates/saas_auth/profile.html` (added the Contact Details section).

---

## Files changed

```
modules/app_admin/dashboard.py                  — view/edit/toggle/delete user routes
modules/saas_auth/routes.py                     — login-OTP + change-email/mobile routes
templates/app_admin/all_users.html              — added "View →" link
templates/app_admin/view_user.html              — new
templates/saas_auth/login.html                  — added "Login with OTP" link
templates/saas_auth/login_otp.html              — new
templates/saas_auth/login_otp_verify.html       — new
templates/saas_auth/confirm_change.html         — new
templates/saas_auth/profile.html                — added Contact Details section
```

## Testing

- `python3 -m py_compile` on all modified `.py` files — passes.
- All modified/new templates re-parsed with `jinja2.Environment().parse()`
  — passes.
- Not run against a live instance in this environment. Please redeploy and
  test: viewing/editing/deactivating/deleting a test user (including the
  sole-owner block, if you have a single-owner business to test against);
  logging in via OTP with both SMS and email chosen; and changing your own
  email and mobile number from Profile.

## Notes / things worth knowing

- Deleting a user is a hard delete (removes the row and their business
  memberships), not a soft-delete/archive. If you'd rather keep a record
  for audit purposes, use **Deactivate** instead — it's reversible and
  blocks login just the same.
- The OTP-login and change-email/mobile features reuse whatever email/SMS
  provider is already configured (`EMAIL_PROVIDER`, `SMTP_*`/`SENDGRID_*`
  etc., and your SMS provider) — no new configuration needed, but they're
  equally dependent on that provider actually being set up and working
  (per our earlier troubleshooting on Render).
