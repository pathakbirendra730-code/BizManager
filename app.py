import os
from datetime import datetime
"""
app.py  — BizManager Multi-Shop ERP  |  Flask Application Factory
==================================================================
Run:  python app.py
URL:  http://127.0.0.1:5000
"""

# Load .env file in development (no-op in production where env vars are set by
# Render). Explicitly point at the .env sitting next to this file, rather than
# relying on the current working directory — Pydroid 3 (and some other
# runners) don't necessarily start the script with cwd set to the project
# folder, which silently breaks the plain load_dotenv() call.
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    loaded = load_dotenv(_env_path)
    print(f"[startup] .env path checked: {_env_path}")
    print(f"[startup] .env loaded: {loaded}")
except ImportError:
    print("[startup] ⚠️  python-dotenv not installed — .env file will NOT be read. "
          "Run: pip install python-dotenv --break-system-packages")

_token = os.environ.get("BOOTSTRAP_ADMIN_TOKEN", "")
print(f"[startup] BOOTSTRAP_ADMIN_TOKEN is set: {bool(_token)} "
      f"(length: {len(_token)})")

from flask import Flask, redirect, url_for, session, request, Response, render_template
from werkzeug.middleware.proxy_fix import ProxyFix
from config import ActiveConfig
from models.database import init_db

# ── Blueprint imports ──────────────────────────────────────────────────────────
from modules.saas_auth      import saas_auth_bp
from modules.app_admin      import app_admin_bp
from modules.unified_login  import unified_bp
from modules.public         import public_bp
from modules.saas_business  import saas_customers_bp, saas_products_bp, saas_suppliers_bp, saas_billing_bp, saas_purchase_bp, saas_finance_bp, saas_reports_bp, saas_gst_bp, saas_accounts_bp, saas_dashboard_bp


def create_app():
    app = Flask(__name__)
    app.secret_key               = ActiveConfig.SECRET_KEY
    app.config["SESSION_PERMANENT"]     = ActiveConfig.SESSION_PERMANENT
    app.config["SESSION_COOKIE_NAME"]   = ActiveConfig.SESSION_COOKIE_NAME
    app.config["MAX_CONTENT_LENGTH"]    = ActiveConfig.MAX_CONTENT_LENGTH

    # Cookie security — defined in config.py's ProductionConfig but never
    # previously copied into app.config, so they had zero effect even in
    # production. getattr(...) with a safe default so DevelopmentConfig
    # (which doesn't define these) doesn't crash.
    app.config["SESSION_COOKIE_SECURE"]   = getattr(ActiveConfig, "SESSION_COOKIE_SECURE", False)
    app.config["SESSION_COOKIE_HTTPONLY"] = getattr(ActiveConfig, "SESSION_COOKIE_HTTPONLY", True)
    app.config["SESSION_COOKIE_SAMESITE"] = getattr(ActiveConfig, "SESSION_COOKIE_SAMESITE", "Lax")
    app.config["PREFERRED_URL_SCHEME"]    = getattr(ActiveConfig, "PREFERRED_URL_SCHEME", "http")

    # Render (like effectively every PaaS) terminates TLS at a reverse proxy
    # and forwards the request to Gunicorn over plain HTTP internally,
    # setting X-Forwarded-Proto/X-Forwarded-For/X-Forwarded-Host. Without
    # this, request.is_secure is FALSE for every single request even in
    # production — which means SESSION_COOKIE_SECURE above (and any HSTS
    # header, and url_for(_external=True)) would silently misbehave.
    # x_for/x_proto/x_host=1 matches Render's single proxy hop exactly —
    # trusting more hops than actually exist would let a client spoof
    # these headers themselves.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    @app.template_filter("dtfmt")
    def dtfmt(value, chars=16):
        """Safely truncate a timestamp for display, whether the DB driver
        returned a plain string (SQLite TEXT columns) or a datetime object
        (PostgreSQL TIMESTAMP columns, via psycopg2). Templates across this
        app used to slice timestamp values directly (e.g. created_at[:10]),
        which only works for strings — on PostgreSQL that raises
        'datetime.datetime object is not subscriptable' and produces a 500
        the first time the column actually holds a value. This filter
        normalizes to the same ISO-style string either backend would give,
        so existing template output (and any '| replace(\"T\",\" \")' chains)
        is unchanged.
        """
        if not value:
            return ""
        if isinstance(value, datetime):
            value = value.isoformat(sep="T", timespec="seconds")
        return str(value)[:chars]

    @app.template_filter("inr")
    def inr(value):
        """Format a number the way Indian ledgers do: 1,23,45,678 (not
        1,234,5678 Western-style) — grouped in 2s after the first 3 digits."""
        try:
            n = float(value or 0)
        except (TypeError, ValueError):
            return "0"
        neg = n < 0
        n = abs(n)
        whole = int(n)
        paise = round((n - whole) * 100)
        s = str(whole)
        if len(s) > 3:
            head, tail = s[:-3], s[-3:]
            parts = []
            while len(head) > 2:
                parts.insert(0, head[-2:])
                head = head[:-2]
            if head:
                parts.insert(0, head)
            s = ",".join(parts) + "," + tail
        out = f"{s}.{paise:02d}"
        return f"-{out}" if neg else out

    # ── Register blueprints ────────────────────────────────────────────────────
    app.register_blueprint(saas_auth_bp)                          # prefix /saas built-in
    app.register_blueprint(app_admin_bp)                          # prefix /app-admin built-in
    app.register_blueprint(unified_bp)                            # /login — single entry point
    app.register_blueprint(public_bp)                             # /about /contact /privacy /terms — trust pages
    app.register_blueprint(saas_customers_bp)                     # /biz/customers — SaaS-native
    app.register_blueprint(saas_products_bp)                      # /biz/products — SaaS-native
    app.register_blueprint(saas_suppliers_bp)                     # /biz/suppliers — SaaS-native
    app.register_blueprint(saas_billing_bp)                       # /biz/billing — SaaS-native
    app.register_blueprint(saas_purchase_bp)                      # /biz/purchase — SaaS-native
    app.register_blueprint(saas_finance_bp)                       # /biz/finance — SaaS-native
    app.register_blueprint(saas_reports_bp)                       # /biz/reports — SaaS-native
    app.register_blueprint(saas_gst_bp)                           # /biz/gst — SaaS-native
    app.register_blueprint(saas_accounts_bp)                      # /biz/accounts — SaaS-native
    app.register_blueprint(saas_dashboard_bp)                     # /biz/dashboard — SaaS-native

    # ── Root redirect ──────────────────────────────────────────────────────────
    @app.route("/")
    def index():
        if "saas_user_id" in session:
            return redirect(url_for("saas_dashboard.index"))
        if session.get("admin_id"):
            return redirect(url_for("app_admin.dashboard"))
        # Default to the unified login for new visitors
        return redirect(url_for("unified_login.login"))

    # ── Health check (required by Render) ────────────────────────────────────
    @app.route("/health")
    def health():
        from flask import jsonify
        from datetime import datetime
        try:
            # Check the ACTUAL active backend (PostgreSQL in production, via
            # DATABASE_URL) rather than always pinging the legacy SQLite file
            # from models.database — otherwise this endpoint can report "ok"
            # on Render even when the real production database is down.
            from models.saas_auth import get_saas_db, _is_postgres
            conn = get_saas_db()
            c = conn.cursor()
            c.execute("SELECT 1")
            c.fetchone()
            conn.close()
            db_ok = True
        except Exception:
            db_ok = False
        return jsonify({
            "status":    "ok" if db_ok else "degraded",
            "db":        "ok" if db_ok else "error",
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "env":       os.environ.get("APP_ENV", "development")
        }), 200 if db_ok else 500

    # ── Security headers ──────────────────────────────────────────────────────
    @app.after_request
    def set_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"]        = "SAMEORIGIN"
        response.headers["X-XSS-Protection"]       = "1; mode=block"
        response.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"

        # Content-Security-Policy: this app relies heavily on inline <script>
        # and inline style="" throughout its templates (a larger, separate
        # cleanup to remove — see Update_015 changelog), so 'unsafe-inline'
        # is required for script-src/style-src for now rather than breaking
        # every page. This CSP still meaningfully blocks the actual attack
        # this protects against: an injected <script src="evil.com/x.js">
        # or <iframe src="evil.com">, since only 'self' and the two CDNs
        # this app actually uses are allowed as SOURCES of script files.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; "
            "font-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )

        # Only send HSTS over an actual HTTPS request (now correctly
        # detected thanks to ProxyFix above) — sending it over plain HTTP
        # (e.g. local development) has no effect and is simply noise.
        if request.is_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=(), payment=()"
        )
        return response

    @app.route("/robots.txt")
    def robots_txt():
        # Every /app-admin/* and /saas/* auth/secondary page already sends
        # <meta name="robots" content="noindex, nofollow"> individually —
        # this is the second, crawler-level layer of the same rule, and is
        # also a "this is a real, maintained site" legitimacy signal in its
        # own right. /biz/ (the authenticated business app) is disallowed
        # too: it requires a login and redirects unauthenticated crawlers
        # to /login anyway, so there's nothing there worth indexing.
        # The public marketing/trust pages are explicitly allowed so
        # nothing here accidentally blocks them.
        lines = [
            "User-agent: *",
            "Allow: /$",
            "Allow: /login$",
            "Allow: /about",
            "Allow: /contact",
            "Allow: /privacy",
            "Allow: /terms",
            "Disallow: /app-admin/",
            "Disallow: /saas/",
            "Disallow: /biz/",
            "Disallow: /login/submit",
            "Disallow: /login/identify",
            f"Sitemap: {url_for('sitemap_xml', _external=True)}",
        ]
        return Response("\n".join(lines) + "\n", mimetype="text/plain")

    @app.route("/sitemap.xml")
    def sitemap_xml():
        # Only the pages that are actually public and meant to be indexed.
        # Every one of these is a plain GET with no auth requirement — never
        # add an authenticated or state-changing route here.
        pages = [
            ("unified_login.login", {}),
            ("public.about", {}),
            ("public.contact", {}),
            ("public.privacy", {}),
            ("public.terms", {}),
        ]
        xml = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for endpoint, values in pages:
            xml.append("<url><loc>%s</loc></url>" %
                       url_for(endpoint, _external=True, **values))
        xml.append("</urlset>")
        return Response("\n".join(xml), mimetype="application/xml")

    # ── Global 404 handler (Update_021) ──────────────────────────────────────
    # Every 404 in the app — a genuinely missing route, a bad link, or the
    # App Admin bootstrap route's intentional 404-on-failure (see
    # modules/app_admin/routes.py) — now renders the same branded page
    # instead of Flask's default "Not Found" text response. This is purely
    # presentational: it does not change which requests return 404, only
    # what the response body looks like.
    @app.errorhandler(404)
    def handle_404(e):
        return render_template("errors/404.html"), 404

    # ── Context processor: injects into every template ──────────────────────
    @app.context_processor
    def inject_globals():
        from utils.saas_helpers import generate_csrf_token

        # Ensure csrf_token is always available, in every blueprint's templates —
        # previously only saas_auth_bp/app_admin_bp injected this via their own
        # context_processor, leaving every other blueprint (including new SaaS
        # business modules) silently rendering an empty token that happened to
        # pass validate_csrf() by accident (empty == empty). Centralising here
        # closes that gap for all current and future blueprints.
        csrf_token = generate_csrf_token()

        # Low-stock count badge for sidebar — tenant-scoped, SaaS-native table
        low_stock_count = 0
        biz_id = session.get("saas_business_id")
        if biz_id:
            from models.saas_auth import saas_fetchone, _is_postgres
            p = "%s" if _is_postgres() else "?"
            row = saas_fetchone(
                f"""SELECT COUNT(*) as cnt FROM saas_products
                    WHERE business_id={p} AND stock_quantity<=low_stock_threshold
                      AND is_active=TRUE""",
                (biz_id,)
            )
            low_stock_count = row["cnt"] if row else 0

        return {
            # CSRF token — available in every blueprint's templates
            "csrf_token": csrf_token,

            # Used by templates/_public_footer.html's copyright line
            "current_year": datetime.utcnow().year,

            # SaaS auth context (available in all templates)
            "saas_user_id":     session.get("saas_user_id"),
            "saas_fullname":    session.get("saas_fullname", ""),
            "saas_role":        session.get("saas_role", ""),
            "saas_business_id": session.get("saas_business_id"),
            "saas_biz_name":    session.get("saas_biz_name", ""),
            "saas_biz_plan":    session.get("saas_biz_plan", "free"),
            "low_stock_count":  low_stock_count,
        }

    # ── DB init ────────────────────────────────────────────────────────────────
    with app.app_context():
        init_db()
        # SaaS Auth tables (SQLite dev / PostgreSQL prod)
        from models.saas_auth import init_saas_db
        init_saas_db()
        from models.saas_business_data import init_saas_business_tables
        init_saas_business_tables()
        from models.saas_ledger_engine import init_ledger_engine_tables
        init_ledger_engine_tables()

    return app


if __name__ == "__main__":
    app = create_app()
    print("\n" + "═"*55)
    print("  BizManager v6 — SaaS")
    print("  http://127.0.0.1:5000")
    print("═"*55 + "\n")
    app.run(host=ActiveConfig.HOST, port=ActiveConfig.PORT, debug=ActiveConfig.DEBUG)