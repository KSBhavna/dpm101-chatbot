"""
app.py — DPM-101 Helpdesk Chatbot: Flask application entry point

Module 2: Environment & Repository Setup
Only the /health endpoint is implemented here.
Chat, retrieval, session, and caching endpoints are added in later modules.
"""

import os
from typing import Optional
from flask import Flask, jsonify
from flask_cors import CORS

from config import config
from backend.routes.chat_routes import chat_bp
from backend.routes.stats_routes import stats_bp
from backend.services.chat_service import ChatService
from backend.services.stats_service import StatsService


def create_app(test_config: Optional[dict] = None) -> Flask:
    """
    Application factory function.

    Returns a configured Flask application instance.
    Using an app factory makes the app easier to test (each test
    gets a fresh instance) and keeps the global namespace clean.
    """
    app = Flask(
        __name__,
        static_folder=os.path.join(os.path.dirname(__file__), "frontend"),
        static_url_path="",
    )

    app.secret_key = config.SECRET_KEY

    # ── Test Configuration Overrides ──────────────────────────────────────────
    if test_config:
        app.config.update(test_config)
        if "chat_service" in test_config:
            app.extensions["chat_service"] = test_config["chat_service"]
        elif "DB_PATH" in test_config:
            app.extensions["chat_service"] = ChatService(db_path=test_config["DB_PATH"])
        if "rate_limiter" in test_config:
            app.extensions["rate_limiter"] = test_config["rate_limiter"]
        if "request_manager" in test_config:
            app.extensions["request_manager"] = test_config["request_manager"]
        if "stats_service" in test_config:
            app.extensions["stats_service"] = test_config["stats_service"]

    if "stats_service" not in app.extensions:
        app.extensions["stats_service"] = StatsService()

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Development: allow requests from VS Code Live Server (port 5500).
    # Production: not needed — Flask serves the frontend from the same origin.
    if config.FLASK_DEBUG:
        CORS(app, origins=["http://localhost:5500"])

    # ── Register Blueprints ───────────────────────────────────────────────────
    app.register_blueprint(chat_bp)
    app.register_blueprint(stats_bp)

    # ── Health endpoint ───────────────────────────────────────────────────────
    @app.route("/health", methods=["GET"])
    def health():
        """
        Liveness check endpoint.

        Returns a simple JSON payload confirming the Flask application
        is running. Does NOT call the Gemini API (health checks must
        never consume AI quota).
        """
        return jsonify({
            "status": "healthy",
            "service": "DPM-101 Helpdesk Chatbot",
        }), 200

    # ── Frontend static files ─────────────────────────────────────────────────
    @app.route("/")
    def index():
        """Serve the chat UI from the frontend/ directory."""
        return app.send_static_file("index.html")

    return app


# ── WSGI Application Instance ─────────────────────────────────────────────────
# For WSGI servers that import `app` directly (e.g., gunicorn app:app)
app = create_app()


# ── Development entry point ───────────────────────────────────────────────────
# Run with:  python app.py
# (In production, Gunicorn is used: gunicorn "app:create_app()" or gunicorn app:app)
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=config.FLASK_DEBUG)
