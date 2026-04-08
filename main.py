"""HTTP entrypoint for Sidekick: Flask web app, Google sign-in, and ADK API proxy.

    Runs the Google Agent Development Kit (ADK) on an internal port and exposes a public Flask
    server that serves the static UI, handles OAuth, and forwards ``/api/*`` to ADK with the
    signed-in user's identity on paths and run payloads.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote

import httpx
import uvicorn
from authlib.integrations.base_client.errors import MismatchingStateError
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, request, send_from_directory, session

import sidekick._google_auth_patch  # noqa: F401 — before google.adk / google.auth

from google.adk.cli.fast_api import get_fast_api_app

from sidekick.flask_inventory_api import ui_api_bp
from sidekick.google_credentials import (
    persist_oauth_token_from_authlib,
    sidekick_google_oauth_scope,
)

REPO_ROOT = Path(__file__).resolve().parent
STATIC_DIR = REPO_ROOT / "static"

logger = logging.getLogger("sidekick.proxy")

_GOOGLE_DISCOVERY = "https://accounts.google.com/.well-known/openid-configuration"
_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

_users_path_re = re.compile(r"(apps/[^/]+/users/)[^/]+")


def _oauth_configured() -> bool:
    """Return whether Google OAuth client credentials are configured.

    Returns:
        bool: True if both ``GOOGLE_OAUTH_CLIENT_ID`` and ``GOOGLE_OAUTH_CLIENT_SECRET`` are set.
    """
    cid = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    csec = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    return bool(cid and csec)


def _redirect_uri() -> str:
    """Return the OAuth redirect URI from the environment.

    Returns:
        str: Value of ``OAUTH_REDIRECT_URI`` (may be empty).
    """
    return os.environ.get("OAUTH_REDIRECT_URI", "").strip()


def _use_proxy_fix() -> bool:
    """Return whether Flask should trust ``X-Forwarded-*`` headers for URLs and cookies.

    Returns:
        bool: True when explicitly enabled or when running on Cloud Run (``K_SERVICE`` set).
    """
    raw = os.environ.get("TRUST_PROXY_HEADERS", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return bool(os.environ.get("K_SERVICE"))  # Cloud Run sets this


def _rewrite_adk_path(path: str, uid: str) -> str:
    """Rewrite ADK URL paths so the user segment matches the signed-in Google ``sub``.

    Args:
        path (str): Path after ``/api/`` (ADK-relative).
        uid (str): Google subject id (OAuth ``sub``).

    Returns:
        str: Path with the ``apps/.../users/...`` segment replaced by URL-safe ``uid``.
    """
    safe = quote(uid, safe="")
    return _users_path_re.sub(r"\1" + safe, path)


def _rewrite_run_body(body: bytes, uid: str, path: str) -> bytes:
    """Inject ``user_id`` into ADK run request JSON for the authenticated user.

    Args:
        body (bytes): Raw request body.
        uid (str): Google ``sub`` to set as ``user_id``.
        path (str): ADK path segment (only ``run`` / ``run_sse`` are rewritten).

    Returns:
        bytes: Possibly modified body; unchanged if not JSON or wrong path.
    """
    if path not in ("run", "run_sse"):
        return body
    ct = request.headers.get("Content-Type", "")
    if not body or "application/json" not in ct.lower():
        return body
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body
    if isinstance(data, dict):
        data["user_id"] = uid
        return json.dumps(data, separators=(",", ":")).encode("utf-8")
    return body


def _email_allowed(email: str) -> bool:
    """Return whether ``email`` is allowed when domain restriction is configured.

    Args:
        email (str): Email from the IdP userinfo payload.

    Returns:
        bool: True if no domain filter is set, or if the address domain matches it.
    """
    domain = os.environ.get("AUTH_ALLOWED_EMAIL_DOMAIN", "").strip().lower()
    if not domain:
        return True
    if not email or "@" not in email:
        return False
    return email.split("@", 1)[1].lower() == domain


def _start_adk_server() -> None:
    """Start the ADK FastAPI application with uvicorn (blocks until process exit).

    Returns:
        None
    """
    load_dotenv(REPO_ROOT / ".env")
    os.chdir(REPO_ROOT)

    adk_port = int(os.environ.get("ADK_INTERNAL_PORT", "8001"))
    allow = os.environ.get("CORS_ALLOW_ORIGINS", "*")
    origins = [o.strip() for o in allow.split(",") if o.strip()] or ["*"]
    trace = os.environ.get("ADK_TRACE_TO_CLOUD", "").lower() in ("1", "true", "yes")
    otel = os.environ.get("ADK_OTEL_TO_CLOUD", "").lower() in ("1", "true", "yes")

    adk_app = get_fast_api_app(
        agents_dir=str(REPO_ROOT),
        allow_origins=origins,
        web=False,
        trace_to_cloud=trace,
        otel_to_cloud=otel,
        host="127.0.0.1",
        port=adk_port,
        auto_create_session=True,
    )
    uvicorn.run(
        adk_app,
        host="127.0.0.1",
        port=adk_port,
        log_level=os.environ.get("UVICORN_LOG_LEVEL", "warning"),
    )


def _wait_for_adk(base: str, timeout: float = 30.0) -> None:
    """Block until the ADK server health check succeeds or time runs out.

    Args:
        base (str): ADK base URL (e.g. ``http://127.0.0.1:8001``).
        timeout (float): Maximum seconds to wait.

    Raises:
        RuntimeError: If ``{base}/health`` never returns a non-5xx status within ``timeout``.

    Returns:
        None
    """
    deadline = time.monotonic() + timeout
    with httpx.Client() as client:
        while time.monotonic() < deadline:
            try:
                r = client.get(f"{base}/health", timeout=2.0)
                if r.status_code < 500:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.15)
    raise RuntimeError(f"ADK server did not become ready at {base}")


def main() -> None:
    """Start ADK in a background thread, then run the Flask app.

    Raises:
        RuntimeError: If OAuth is configured but ``FLASK_SECRET_KEY`` is missing.

    Returns:
        None
    """
    load_dotenv(REPO_ROOT / ".env")
    os.chdir(REPO_ROOT)

    adk_port = int(os.environ.get("ADK_INTERNAL_PORT", "8001"))
    adk_base = f"http://127.0.0.1:{adk_port}"

    thread = threading.Thread(target=_start_adk_server, daemon=True, name="adk-uvicorn")
    thread.start()
    _wait_for_adk(adk_base)

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))

    flask_app = Flask(__name__, static_folder=str(STATIC_DIR))

    if _use_proxy_fix():
        from werkzeug.middleware.proxy_fix import ProxyFix

        flask_app.wsgi_app = ProxyFix(
            flask_app.wsgi_app,
            x_for=1,
            x_proto=1,
            x_host=1,
            x_port=1,
            x_prefix=1,
        )

    sec_raw = os.environ.get("SESSION_COOKIE_SECURE", "").strip().lower()
    if sec_raw in ("0", "false", "no", "off"):
        cookie_secure = False
    elif sec_raw in ("1", "true", "yes", "on"):
        cookie_secure = True
    else:
        cookie_secure = bool(os.environ.get("K_SERVICE"))
    cookie_samesite = (os.environ.get("SESSION_COOKIE_SAMESITE") or "Lax").strip()
    flask_app.config.update(
        SESSION_COOKIE_SECURE=cookie_secure,
        SESSION_COOKIE_SAMESITE=cookie_samesite,
        SESSION_COOKIE_HTTPONLY=True,
    )

    if _oauth_configured():
        secret = os.environ.get("FLASK_SECRET_KEY", "").strip()
        if not secret:
            raise RuntimeError(
                "FLASK_SECRET_KEY must be set when GOOGLE_OAUTH_CLIENT_ID and "
                "GOOGLE_OAUTH_CLIENT_SECRET are configured."
            )
        flask_app.secret_key = secret
    else:
        flask_app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-insecure-change-me")

    oauth = OAuth(flask_app)
    if _oauth_configured():
        oauth.register(
            name="google",
            client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"].strip(),
            client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"].strip(),
            server_metadata_url=_GOOGLE_DISCOVERY,
            client_kwargs={"scope": sidekick_google_oauth_scope()},
        )

    @flask_app.get("/health")
    def health() -> Response:
        return Response("ok", status=200, mimetype="text/plain")

    @flask_app.get("/auth/me")
    def auth_me():
        if not _oauth_configured():
            return jsonify(oauth_enabled=False, sub=None, email=None)
        sub = session.get("user_sub")
        if not sub:
            return jsonify(oauth_enabled=True, error="unauthorized", login="/login/google"), 401
        return jsonify(
            oauth_enabled=True,
            sub=sub,
            email=session.get("user_email", ""),
        )

    @flask_app.get("/login/google")
    def login_google():
        if not _oauth_configured():
            return Response("OAuth is not configured", status=503)
        redir = _redirect_uri()
        if not redir:
            return Response("OAUTH_REDIRECT_URI is not set", status=500)
        extra: dict = {"access_type": "offline"}
        prompt = os.environ.get("GOOGLE_OAUTH_PROMPT", "consent").strip()
        if prompt:
            extra["prompt"] = prompt
        return oauth.google.authorize_redirect(redir, **extra)

    @flask_app.get("/auth/google/callback")
    def google_callback():
        if not _oauth_configured():
            return Response("OAuth is not configured", status=503)
        if not _redirect_uri():
            return Response("OAUTH_REDIRECT_URI is not set", status=500)
        # redirect_uri is taken from session (saved in authorize_redirect); do not pass again
        try:
            token = oauth.google.authorize_access_token()
        except MismatchingStateError:
            return Response(
                "OAuth session was lost (CSRF state missing). Common causes:\n"
                "  • Open the app and click Sign in on the SAME host/scheme as "
                "OAUTH_REDIRECT_URI (e.g. if redirect is https://api.example.com/..., "
                "do not start login from http://localhost).\n"
                "  • On HTTPS behind a proxy: set TRUST_PROXY_HEADERS=1 (auto on Cloud Run) "
                "and SESSION_COOKIE_SECURE=1.\n"
                "  • Use one stable FLASK_SECRET_KEY across all replicas.\n",
                status=400,
                mimetype="text/plain",
            )
        user = token.get("userinfo")
        if user is None:
            resp = oauth.google.get(_USERINFO_URL, token=token)
            user = resp.json()
        email = user.get("email") or ""
        if not _email_allowed(email):
            session.clear()
            return Response(
                "Sign-in not allowed for this email domain.",
                status=403,
                mimetype="text/plain",
            )
        session["user_sub"] = user["sub"]
        session["user_email"] = email
        try:
            persist_oauth_token_from_authlib(user["sub"], token)
        except Exception:
            logger.exception("Failed to persist Google OAuth tokens for user")
        return redirect("/")

    @flask_app.get("/logout")
    def logout():
        session.clear()
        return redirect("/")

    @flask_app.before_request
    def _gate_api() -> Response | None:
        if not request.path.startswith("/api") and not request.path.startswith("/ui-api"):
            return None
        if not _oauth_configured():
            return None
        if request.method == "OPTIONS":
            return None
        if "user_sub" not in session:
            return Response(
                '{"error":"unauthorized","login":"/login/google"}',
                status=401,
                mimetype="application/json",
            )
        return None

    flask_app.register_blueprint(ui_api_bp)

    @flask_app.get("/favicon.ico")
    def favicon_ico():
        return redirect("/static/favicon.svg", code=302)

    @flask_app.get("/")
    def index() -> Response:
        return send_from_directory(flask_app.static_folder, "index.html")

    @flask_app.get("/ui")
    def ui_alias() -> Response:
        return send_from_directory(flask_app.static_folder, "index.html")

    @flask_app.get("/privacy-policy")
    def privacy_policy() -> Response:
        return send_from_directory(flask_app.static_folder, "privacy-policy.html")

    @flask_app.get("/privacy")
    def privacy_legacy_redirect():
        return redirect("/privacy-policy", code=301)

    @flask_app.get("/terms-and-conditions")
    def terms_and_conditions() -> Response:
        return send_from_directory(flask_app.static_folder, "terms-and-conditions.html")

    # Drop Content-Length: body may be rewritten (_rewrite_run_body); forwarding
    # the client's length causes h11 "Too much data for declared Content-Length".
    hop_by_hop = frozenset(
        {
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailers",
            "transfer-encoding",
            "upgrade",
            "host",
            "content-length",
        }
    )

    @flask_app.route("/api", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    @flask_app.route("/api/", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    @flask_app.route("/api/<path:subpath>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    def proxy_adk(subpath: str = "") -> Response:
        """Proxy HTTP from ``/api`` to the internal ADK server.

        Args:
            subpath (str): Path segment under ``/api/``.

        Returns:
            Response: Upstream status, headers, and body from ADK.
        """
        if request.method == "OPTIONS":
            return Response(status=204)
        path = subpath.lstrip("/")
        uid = session.get("user_sub") if _oauth_configured() else None
        if uid:
            path = _rewrite_adk_path(path, uid)
        url = f"{adk_base}/{path}" if path else f"{adk_base}/"
        if request.query_string:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{request.query_string.decode()}"
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in hop_by_hop
        }
        body = request.get_data()
        if uid:
            body = _rewrite_run_body(body, uid, path)
        timeout = float(os.environ.get("ADK_PROXY_TIMEOUT", "300"))
        with httpx.Client(timeout=timeout) as client:
            upstream = client.request(
                request.method,
                url,
                headers=headers,
                content=body if body else None,
            )
        if upstream.status_code >= 400:
            logger.warning(
                "ADK proxy %s /api/%s -> HTTP %s (upstream URL %s)",
                request.method,
                path,
                upstream.status_code,
                url,
            )
        out_headers = [
            (k, v)
            for k, v in upstream.headers.items()
            if k.lower() not in ("content-encoding", "transfer-encoding", "connection")
        ]
        return Response(
            upstream.content,
            status=upstream.status_code,
            headers=out_headers,
        )

    if not logging.root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(levelname)s %(name)s: %(message)s",
        )
    flask_app.run(host=host, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
