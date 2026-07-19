"""
modules/public.py — Public trust-signal pages
================================================
Blueprint: public  |  URL prefix: (none)

About, Contact, Privacy Policy, and Terms of Service. These are plain,
unauthenticated, indexable pages that exist so a first-time visitor (or
Google's Safe Browsing / Search Console crawlers) can see who BizManager is,
what it does, and how to reach a real support channel — the trust signals
a bare login page doesn't provide on its own.

Nothing here touches session state, the database, or user input, so there's
no auth surface, no CSRF-relevant form, and nothing that needs rate limiting.
"""

import os
from flask import Blueprint, render_template
from datetime import date

public_bp = Blueprint("public", __name__)

# Support contact details are read from the environment so real values can
# be set per-deployment without editing templates. See the TODO note in
# templates/public/contact.html — set these in production before submitting
# the Search Console review request.
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "support@bizmanager.app")
SUPPORT_PHONE = os.environ.get("SUPPORT_PHONE", "")

# Bump this when the policy text actually changes.
POLICY_UPDATED = "19 July 2026"


@public_bp.route("/about")
def about():
    return render_template("public/about.html")


@public_bp.route("/contact")
def contact():
    return render_template(
        "public/contact.html",
        support_email=SUPPORT_EMAIL,
        support_phone=SUPPORT_PHONE,
    )


@public_bp.route("/privacy")
def privacy():
    return render_template("public/privacy.html", policy_updated=POLICY_UPDATED)


@public_bp.route("/terms")
def terms():
    return render_template("public/terms.html", policy_updated=POLICY_UPDATED)
