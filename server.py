#!/usr/bin/env python3
from __future__ import annotations

import base64
import html
import json
import mimetypes
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus, cookies
from pathlib import Path
from wsgiref.simple_server import WSGIRequestHandler, make_server


ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env.local"
PORT = int(os.environ.get("PORT", "4173"))
DEFAULT_PROFILE_URL = "https://open.spotify.com/user/31hpqr3lwz6jeevlxlqtazh4cszi"
DEFAULT_PROFILE_NAME = "Mahesh Aithal (RS)"
SPOTIFY_SCOPES = "user-read-currently-playing user-read-recently-played"
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}

mimetypes.add_type("application/javascript", ".js")


@dataclass
class AppResponse:
    status: int
    body: bytes
    headers: list[tuple[str, str]]


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def write_env_file(path: Path, values: dict[str, str]) -> None:
    lines = ["# Local secrets for the site Spotify integration"]
    for key in sorted(values):
        lines.append(f"{key}={values[key]}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_config() -> dict[str, str]:
    config = parse_env_file(ENV_PATH)
    for key in (
        "SPOTIFY_CLIENT_ID",
        "SPOTIFY_CLIENT_SECRET",
        "SPOTIFY_REDIRECT_URI",
        "SPOTIFY_REFRESH_TOKEN",
        "SPOTIFY_PROFILE_URL",
        "SPOTIFY_PROFILE_NAME",
    ):
        if os.environ.get(key):
            config[key] = os.environ[key]

    config.setdefault("SPOTIFY_REDIRECT_URI", f"http://127.0.0.1:{PORT}/spotify/callback")
    config.setdefault("SPOTIFY_PROFILE_URL", DEFAULT_PROFILE_URL)
    config.setdefault("SPOTIFY_PROFILE_NAME", DEFAULT_PROFILE_NAME)
    return config


def persist_config(updates: dict[str, str]) -> None:
    if os.environ.get("VERCEL"):
        return

    current = parse_env_file(ENV_PATH)
    current.update({key: value for key, value in updates.items() if value})
    if "SPOTIFY_REDIRECT_URI" not in current:
        current["SPOTIFY_REDIRECT_URI"] = f"http://127.0.0.1:{PORT}/spotify/callback"
    if "SPOTIFY_PROFILE_URL" not in current:
        current["SPOTIFY_PROFILE_URL"] = DEFAULT_PROFILE_URL
    if "SPOTIFY_PROFILE_NAME" not in current:
        current["SPOTIFY_PROFILE_NAME"] = DEFAULT_PROFILE_NAME
    write_env_file(ENV_PATH, current)


def build_response(
    body: bytes,
    *,
    status: int = 200,
    content_type: str = "text/plain; charset=utf-8",
    headers: list[tuple[str, str]] | None = None,
) -> AppResponse:
    merged_headers = [("Content-Type", content_type), ("Content-Length", str(len(body)))]
    if headers:
        merged_headers.extend(headers)
    return AppResponse(status=status, body=body, headers=merged_headers)


def json_response(payload: dict, status: int = 200) -> AppResponse:
    return build_response(
        json.dumps(payload).encode("utf-8"),
        status=status,
        content_type="application/json; charset=utf-8",
        headers=[("Cache-Control", "no-store")],
    )


def page_document(title: str, body_html: str, meta_refresh: str | None = None) -> bytes:
    refresh_tag = (
        f'<meta http-equiv="refresh" content="{html.escape(meta_refresh, quote=True)}" />'
        if meta_refresh
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    {refresh_tag}
    <title>{html.escape(title)}</title>
    <style>
      body {{
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: #11151b;
        color: #f3f5f8;
      }}

      main {{
        width: min(92vw, 720px);
        padding: 1.4rem;
        border-radius: 20px;
        background: rgba(255, 255, 255, 0.04);
        border: 1px solid rgba(255, 255, 255, 0.08);
      }}

      a {{
        color: #8ec8ff;
      }}

      code,
      pre {{
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      }}

      pre {{
        white-space: pre-wrap;
      }}
    </style>
  </head>
  <body>
    <main>
      <h1>{html.escape(title)}</h1>
      {body_html}
    </main>
  </body>
</html>""".encode("utf-8")


def html_response(title: str, body_html: str, *, status: int = 200, meta_refresh: str | None = None) -> AppResponse:
    return build_response(
        page_document(title, body_html, meta_refresh=meta_refresh),
        status=status,
        content_type="text/html; charset=utf-8",
    )


def redirect_response(location: str, *, headers: list[tuple[str, str]] | None = None, status: int = 302) -> AppResponse:
    merged_headers = [("Location", location)]
    if headers:
        merged_headers.extend(headers)
    return build_response(b"", status=status, content_type="text/plain; charset=utf-8", headers=merged_headers)


def not_found_response() -> AppResponse:
    return html_response("Not found", "<p>That page does not exist.</p>", status=404)


def spotify_token_request(config: dict[str, str], params: dict[str, str]) -> dict:
    token_url = "https://accounts.spotify.com/api/token"
    client_id = config.get("SPOTIFY_CLIENT_ID", "")
    client_secret = config.get("SPOTIFY_CLIENT_SECRET", "")
    auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("utf-8")
    request = urllib.request.Request(
        token_url,
        data=urllib.parse.urlencode(params).encode("utf-8"),
        headers={
            "Authorization": f"Basic {auth_header}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def spotify_api_request(access_token: str, path: str, params: dict[str, str] | None = None):
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    request = urllib.request.Request(
        f"https://api.spotify.com/v1{path}{query}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    return urllib.request.urlopen(request, timeout=20)


def refresh_access_token(config: dict[str, str]) -> str:
    response = spotify_token_request(
        config,
        {
            "grant_type": "refresh_token",
            "refresh_token": config["SPOTIFY_REFRESH_TOKEN"],
        },
    )
    if response.get("refresh_token"):
        try:
            persist_config({"SPOTIFY_REFRESH_TOKEN": response["refresh_token"]})
        except OSError:
            pass
    return response["access_token"]


def normalize_track(item: dict, played_at: str | None = None) -> dict:
    track = item["item"] if "item" in item else item["track"]
    artists = ", ".join(artist["name"] for artist in track.get("artists", []))
    album = track.get("album", {})
    images = album.get("images") or []
    image_url = images[0]["url"] if images else ""
    return {
        "id": track.get("id"),
        "name": track.get("name"),
        "artist": artists,
        "album": album.get("name"),
        "url": track.get("external_urls", {}).get("spotify"),
        "imageUrl": image_url,
        "playedAt": played_at,
    }


def spotify_payload(config: dict[str, str]) -> tuple[int, dict]:
    profile_name = config.get("SPOTIFY_PROFILE_NAME", DEFAULT_PROFILE_NAME)
    profile_url = config.get("SPOTIFY_PROFILE_URL", DEFAULT_PROFILE_URL)

    if not config.get("SPOTIFY_CLIENT_ID") or not config.get("SPOTIFY_CLIENT_SECRET"):
        return 200, {
            "status": "setup_required",
            "message": "Add your Spotify app credentials to .env.local or Vercel project env vars, then run the local connect step once.",
            "profileName": profile_name,
            "profileUrl": profile_url,
            "requiredKeys": ["SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"],
        }

    if not config.get("SPOTIFY_REFRESH_TOKEN"):
        return 200, {
            "status": "auth_required",
            "message": "Spotify app credentials are set. Connect your Spotify account from this machine once so the site has a refresh token.",
            "profileName": profile_name,
            "profileUrl": profile_url,
            "loginUrl": "/spotify/login",
        }

    try:
        access_token = refresh_access_token(config)

        currently_playing = None
        try:
            with spotify_api_request(access_token, "/me/player/currently-playing") as response:
                if response.status == HTTPStatus.NO_CONTENT:
                    currently_playing = None
                else:
                    payload = json.loads(response.read().decode("utf-8"))
                    if payload.get("item"):
                        currently_playing = normalize_track(payload)
        except urllib.error.HTTPError as exc:
            if exc.code != HTTPStatus.NO_CONTENT:
                raise

        with spotify_api_request(access_token, "/me/player/recently-played", {"limit": "5"}) as response:
            recent_payload = json.loads(response.read().decode("utf-8"))
            recent_tracks = [
                normalize_track(item, item.get("played_at"))
                for item in recent_payload.get("items", [])
            ]

        if currently_playing:
            recent_tracks = [track for track in recent_tracks if track.get("id") != currently_playing.get("id")]

        recent_tracks = recent_tracks[:4]

        return 200, {
            "status": "connected",
            "message": "Spotify live data connected.",
            "profileName": profile_name,
            "profileUrl": profile_url,
            "currentlyPlaying": currently_playing,
            "recentTracks": recent_tracks,
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
        }
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        return 200, {
            "status": "error",
            "message": "Spotify returned an error while fetching your listening data.",
            "detail": error_body,
            "profileName": profile_name,
            "profileUrl": profile_url,
        }
    except Exception as exc:  # noqa: BLE001
        return 200, {
            "status": "error",
            "message": "Could not load Spotify data.",
            "detail": str(exc),
            "profileName": profile_name,
            "profileUrl": profile_url,
        }


def client_address(environ: dict[str, str]) -> str:
    forwarded = environ.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        host = forwarded.split(",", 1)[0].strip()
    else:
        host = environ.get("REMOTE_ADDR", "")
    return host.split("%", 1)[0]


def is_loopback_request(environ: dict[str, str]) -> bool:
    return client_address(environ) in LOOPBACK_HOSTS


def request_path(environ: dict[str, str]) -> str:
    return urllib.parse.unquote(environ.get("PATH_INFO", "") or "/")


def resolve_public_file(url_path: str) -> Path | None:
    relative = url_path.lstrip("/") or "index.html"
    candidate = (ROOT / relative).resolve()

    try:
        relative_candidate = candidate.relative_to(ROOT)
    except ValueError:
        return None

    if any(part.startswith(".") for part in relative_candidate.parts):
        return None

    if candidate.is_dir():
        candidate = (candidate / "index.html").resolve()
        try:
            relative_candidate = candidate.relative_to(ROOT)
        except ValueError:
            return None
        if any(part.startswith(".") for part in relative_candidate.parts):
            return None

    if not candidate.is_file():
        return None

    return candidate


def serve_file(file_path: Path, *, cache_control: str | None = None) -> AppResponse:
    body = file_path.read_bytes()
    content_type, _ = mimetypes.guess_type(str(file_path))
    headers: list[tuple[str, str]] = []
    if cache_control:
        headers.append(("Cache-Control", cache_control))
    return build_response(
        body,
        content_type=content_type or "application/octet-stream",
        headers=headers,
    )


def handle_spotify_login(environ: dict[str, str]) -> AppResponse:
    if not is_loopback_request(environ):
        return html_response(
            "Spotify auth locked down",
            (
                "<p>This route only works from your local machine so nobody else can overwrite the Spotify account linked to the site.</p>"
                "<p>For Vercel, keep the deployed site read-only and set <code>SPOTIFY_REFRESH_TOKEN</code> in the project environment variables.</p>"
            ),
            status=403,
        )

    config = load_config()
    missing = [key for key in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET") if not config.get(key)]
    if missing:
        missing_html = "".join(f"<li><code>{html.escape(item)}</code></li>" for item in missing)
        return html_response(
            "Spotify setup missing credentials",
            (
                "<p>Add these keys to <code>.env.local</code> first, then try again.</p>"
                f"<ul>{missing_html}</ul>"
            ),
            status=400,
        )

    state = secrets.token_urlsafe(24)
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": config["SPOTIFY_CLIENT_ID"],
            "scope": SPOTIFY_SCOPES,
            "redirect_uri": config["SPOTIFY_REDIRECT_URI"],
            "state": state,
            "show_dialog": "true",
        }
    )
    cookie = cookies.SimpleCookie()
    cookie["spotify_auth_state"] = state
    cookie["spotify_auth_state"]["httponly"] = True
    cookie["spotify_auth_state"]["path"] = "/spotify/callback"
    cookie["spotify_auth_state"]["samesite"] = "Lax"

    return redirect_response(
        f"https://accounts.spotify.com/authorize?{query}",
        headers=[("Set-Cookie", cookie.output(header="").strip())],
    )


def handle_spotify_callback(environ: dict[str, str]) -> AppResponse:
    if not is_loopback_request(environ):
        return html_response(
            "Spotify auth locked down",
            "<p>This route only works from your local machine. The deployed Vercel site should use a saved <code>SPOTIFY_REFRESH_TOKEN</code> env var instead.</p>",
            status=403,
        )

    query = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
    if query.get("error"):
        return html_response(
            "Spotify authorization failed",
            f"<p>Spotify returned <code>{html.escape(query['error'][0])}</code>. You can close this tab and try again.</p>",
            status=400,
        )

    code = query.get("code", [None])[0]
    state = query.get("state", [None])[0]
    state_cookie = cookies.SimpleCookie(environ.get("HTTP_COOKIE", "")).get("spotify_auth_state")

    if not code or not state or not state_cookie or state_cookie.value != state:
        return html_response(
            "Spotify authorization failed",
            "<p>The Spotify callback was missing a valid authorization state. Try the login step again.</p>",
            status=400,
        )

    config = load_config()
    try:
        token_payload = spotify_token_request(
            config,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config["SPOTIFY_REDIRECT_URI"],
            },
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return html_response(
            "Spotify token exchange failed",
            f"<p>Spotify rejected the token exchange.</p><pre>{html.escape(detail)}</pre>",
            status=400,
        )

    refresh_token = token_payload.get("refresh_token")
    if not refresh_token:
        return html_response(
            "Spotify token exchange incomplete",
            "<p>Spotify did not return a refresh token. Make sure you approved the app and try again.</p>",
            status=400,
        )

    try:
        persist_config({"SPOTIFY_REFRESH_TOKEN": refresh_token})
    except OSError as exc:
        return html_response(
            "Spotify token storage failed",
            f"<p>The refresh token was created, but writing <code>.env.local</code> failed.</p><pre>{html.escape(str(exc))}</pre>",
            status=500,
        )

    cookie = cookies.SimpleCookie()
    cookie["spotify_auth_state"] = ""
    cookie["spotify_auth_state"]["path"] = "/spotify/callback"
    cookie["spotify_auth_state"]["expires"] = "Thu, 01 Jan 1970 00:00:00 GMT"

    redirect_target = "/index.html?spotify=connected#connect"
    body = (
        "<p>Your Spotify refresh token is saved in <code>.env.local</code>.</p>"
        f"<p><a href=\"{redirect_target}\">Go back to the site</a> and refresh the page if it does not update automatically.</p>"
    )
    response = html_response("Spotify connected", body, meta_refresh=f"2; url={redirect_target}")
    response.headers.append(("Set-Cookie", cookie.output(header="").strip()))
    return response


def wsgi_app(environ: dict[str, str], start_response):
    method = (environ.get("REQUEST_METHOD") or "GET").upper()
    path = request_path(environ)

    if path == "/api/resumeiq":
        if method not in {"GET", "POST", "HEAD"}:
            response = build_response(
                b"Method not allowed",
                status=405,
                headers=[("Allow", "GET, POST, HEAD")],
            )
        else:
            from resumeiq_service import analyze_payload, status_payload

            if method == "POST":
                status, payload = analyze_payload(environ)
            else:
                status, payload = status_payload()
            response = json_response(payload, status)
    elif method not in {"GET", "HEAD"}:
        response = build_response(
            b"Method not allowed",
            status=405,
            headers=[("Allow", "GET, HEAD")],
        )
    elif path in {"/", "/index.html"}:
        response = serve_file(ROOT / "index.html", cache_control="no-store")
    elif path == "/api/spotify":
        status, payload = spotify_payload(load_config())
        response = json_response(payload, status)
    elif path == "/spotify/login":
        response = handle_spotify_login(environ)
    elif path == "/spotify/callback":
        response = handle_spotify_callback(environ)
    else:
        file_path = resolve_public_file(path)
        response = serve_file(file_path) if file_path else not_found_response()

    start_response(f"{response.status} {HTTPStatus(response.status).phrase}", response.headers)
    return [] if method == "HEAD" else [response.body]


app = wsgi_app


class LocalRequestHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A003
        super().log_message(format, *args)


def main() -> None:
    with make_server("127.0.0.1", PORT, app, handler_class=LocalRequestHandler) as server:
        print(f"Serving on http://127.0.0.1:{PORT}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down.")


if __name__ == "__main__":
    main()
