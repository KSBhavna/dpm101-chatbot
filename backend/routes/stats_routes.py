"""
backend/routes/stats_routes.py — Flask Blueprint for Statistics Endpoints

Module 9: Statistics & Live Counters
Exposes:
  - GET /api/stats -> Public operational statistics
"""

from __future__ import annotations

import logging
from flask import Blueprint, jsonify
from backend.services.stats_service import get_stats_service

logger = logging.getLogger(__name__)

# Register directly under /api prefix per Architecture Specification §9
stats_bp = Blueprint("stats", __name__, url_prefix="/api")


@stats_bp.route("/stats", methods=["GET"])
def get_stats():
    """
    Public statistics and operational counter endpoint.
    Returns the exact 6 fields specified in Architecture §9:
      - total_requests: int
      - cache_hits: int
      - gemini_calls: int
      - errors: int
      - uptime_seconds: int
      - worker_note: str

    Does NOT call the Gemini API, ensuring zero quota consumption.
    Safe against concurrent read/write execution.
    """
    try:
        service = get_stats_service()
        data = service.get_stats()
        return jsonify(data), 200
    except Exception as exc:
        logger.exception("Failed to retrieve statistics: %s", exc)
        return jsonify({
            "error": "Failed to retrieve statistics",
            "code": "STATS_UNAVAILABLE",
        }), 503
