"""Aranya — trek slot board. Flask app factory."""

from flask import Flask, g

from . import config


def create_app():
    app = Flask(__name__)

    if config.SECRET_KEY:
        app.secret_key = config.SECRET_KEY
    else:
        # Never fall back to a hardcoded constant: it would be public in the
        # repo and make signed cookies (and therefore pre-login CSRF tokens)
        # forgeable. A random key is safe but doesn't survive a restart, so
        # say so loudly.
        import secrets
        app.secret_key = secrets.token_hex(32)
        print("[Config] WARNING: FLASK_SECRET_KEY is not set. Using a random key — "
              "sessions and pending sign-ins will be dropped on every restart. "
              "Set FLASK_SECRET_KEY in .env.")
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = config.PUBLIC_BASE_URL.startswith("https://")

    from . import security

    @app.before_request
    def _before():
        security.load_session()
        return security.check_csrf()

    from . import oauth
    google_ready = oauth.init_app(app)
    if not google_ready:
        print("[Config] Google sign-in not configured (no GOOGLE_CLIENT_ID/SECRET).")

    @app.context_processor
    def _inject():
        # Available to every template without threading it through each render.
        return {"user": getattr(g, "user", None),
                "asset_version": config.ASSET_VERSION,
                "google_enabled": oauth.enabled()}

    from . import routes
    app.register_blueprint(routes.bp)

    from . import auth_routes
    app.register_blueprint(auth_routes.bp)

    from . import admin_routes
    app.register_blueprint(admin_routes.bp)

    # Start workers on creation too, so a production WSGI server (e.g. waitress)
    # that imports this module also runs them — not just `python wsgi.py`.
    from . import bootstrap
    bootstrap.start_background()

    return app
