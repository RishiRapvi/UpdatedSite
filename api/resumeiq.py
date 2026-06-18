from __future__ import annotations

import sys
from http import HTTPStatus
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from resumeiq_service import analyze_payload, status_payload
from server import build_response, json_response


def app(environ, start_response):
    method = (environ.get("REQUEST_METHOD") or "GET").upper()

    if method not in {"GET", "POST", "HEAD"}:
        response = build_response(
            b"Method not allowed",
            status=405,
            headers=[("Allow", "GET, POST, HEAD")],
        )
    elif method == "POST":
        status, payload = analyze_payload(environ)
        response = json_response(payload, status)
    else:
        status, payload = status_payload()
        response = json_response(payload, status)

    start_response(f"{response.status} {HTTPStatus(response.status).phrase}", response.headers)
    return [] if method == "HEAD" else [response.body]
