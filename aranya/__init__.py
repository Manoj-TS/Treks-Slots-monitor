"""Aranya — trek slot board. Flask app factory."""

from flask import Flask


def create_app():
    app = Flask(__name__)

    from . import routes
    app.register_blueprint(routes.bp)

    # Start workers on creation too, so a production WSGI server (e.g. waitress)
    # that imports this module also runs them — not just `python -m aranya`.
    from . import bootstrap
    bootstrap.start_background()

    return app
