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
from typing import Any
from urllib.parse import quote

import httpx
import uvicorn
from authlib.integrations.base_client.errors import MismatchingStateError
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, request, send_from_directory, session

import sidekick._google_auth_patch  # noqa: F401 — before google.adk / google.auth

from google.adk.cli.fast_api import get_fast_api_app

from sidekick.chat_history import (
    fetch_recent_decrypted_history,
    history_as_adk_events,
)
from sidekick.chat_naming import maybe_autoname_chat
from sidekick.crypto import assert_encryption_ready
from sidekick.db import db_connection, get_chat
from sidekick.flask_chats_api import append_chat_message, ui_chats_bp
from sidekick.flask_inventory_api import ui_api_bp
from sidekick.flask_speech_api import ui_speech_bp
from sidekick.memory import top_relevant_memories
from sidekick.run_telemetry import persist_run_telemetry
from sidekick.google_credentials import (
    persist_oauth_token_from_authlib,
    sidekick_google_oauth_scope,
)

REPO_ROOT = Path(__file__).resolve().parent
STATIC_DIR = REPO_ROOT / "static"
# Vite builds the SPA to ``static/dist/`` so the production payload is one HTML
# + one JS bundle + one CSS bundle. Legal pages still live next to ``static/``.
SPA_INDEX = STATIC_DIR / "dist" / "index.html"

logger = logging.getLogger("sidekick.proxy")

_GOOGLE_DISCOVERY = "https://accounts.google.com/.well-known/openid-configuration"
_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

_users_path_re = re.compile(r"(apps/[^/]+/users/)[^/]+")

# Per-process cache of ADK session_ids the proxy has already verified or
# seeded. ADK sessions live in-memory, so this cache is correct for one process
# lifetime: after a restart the cache empties, the GET below 404s, and we
# re-seed from the encrypted log.
_seeded_chat_sessions: set[str] = set()


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


_MEMORY_PREAMBLE_HEADER = "[CONTEXT — Memories about this user, ranked by relevance:"
_MEMORY_PREAMBLE_FOOTER = (
    "End of memories. Use them as background; don't quote ids back. "
    "User message follows.]"
)


def _format_memory_preamble(memories: list[dict[str, Any]]) -> str:
    """Render a compact preamble from the top relevant memories.

    Args:
        memories (list[dict[str, Any]]): Output of :func:`top_relevant_memories`.

    Returns:
        str: Multi-line block ready to prepend to ``new_message`` text. Empty when
        there are no memories — callers should branch on truthiness.
    """
    if not memories:
        return ""
    lines = [_MEMORY_PREAMBLE_HEADER]
    for m in memories:
        text_value = (m.get("text") or "").strip().replace("\n", " ")
        if not text_value:
            continue
        # Keep entries terse so we don't burn tokens; trim to 240 chars.
        if len(text_value) > 240:
            text_value = text_value[:237].rstrip() + "…"
        lines.append(f"- {text_value} (relevance {m.get('score', 0):.2f})")
    lines.append(_MEMORY_PREAMBLE_FOOTER)
    return "\n".join(lines) + "\n\n"


def _rewrite_run_body(
    body: bytes, uid: str, path: str, chat_id: int | None
) -> bytes:
    """Inject ``user_id``, ``state_delta.active_chat_id``, and a memory preamble.

    The memory preamble runs the user's incoming message through the embedding
    model, fetches the top-K relevant stored memories for ``uid``, and prepends
    them as a labelled context block on ``new_message.parts[0].text``. The
    agent's instruction tells it to treat that block as background.

    Args:
        body (bytes): Raw request body.
        uid (str): Google ``sub`` to set as ``user_id``.
        path (str): ADK path segment (only ``run`` / ``run_sse`` are rewritten).
        chat_id (int | None): Active chat id to seed into session state, or None.

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
    if not isinstance(data, dict):
        return body
    data["user_id"] = uid
    if chat_id is not None:
        sd = data.get("state_delta")
        if not isinstance(sd, dict):
            sd = {}
        sd["active_chat_id"] = int(chat_id)
        data["state_delta"] = sd

    # ---- Memory preamble injection -----------------------------------------
    # We only do the embed lookup once we have plaintext for the new turn;
    # everything is best-effort and silently no-ops on failure.
    user_text = _extract_user_text_from_run_body(body)
    if user_text and uid:
        try:
            memories = top_relevant_memories(uid, user_text, k=4, min_score=0.55)
        except Exception:
            logger.exception("memory injection skipped")
            memories = []
        if memories:
            preamble = _format_memory_preamble(memories)
            msg = data.get("new_message")
            if isinstance(msg, dict):
                parts = msg.get("parts")
                if isinstance(parts, list):
                    seeded = False
                    for p in parts:
                        if isinstance(p, dict) and isinstance(p.get("text"), str):
                            p["text"] = preamble + p["text"]
                            seeded = True
                            break
                    if not seeded:
                        parts.insert(0, {"text": preamble})
    return json.dumps(data, separators=(",", ":")).encode("utf-8")


def _peek_run_request(body: bytes) -> dict[str, Any]:
    """Parse just enough of an ADK ``/run`` request body to learn its routing fields.

    Args:
        body (bytes): Raw request body.

    Returns:
        dict[str, Any]: ``{"session_id", "app_name"}`` (each may be empty), with
        any other JSON keys passed through. Returns an empty dict on parse error.
    """
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _extract_user_text_from_run_body(body: bytes) -> str:
    """Pull plain-text content out of an ADK ``/run`` request body's ``new_message``.

    Args:
        body (bytes): JSON body sent to ``/api/run``.

    Returns:
        str: Concatenated text from ``new_message.parts[*].text``, empty when none.
    """
    if not body:
        return ""
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    msg = data.get("new_message") or {}
    parts = msg.get("parts") or []
    out: list[str] = []
    for p in parts:
        if isinstance(p, dict):
            t = p.get("text")
            if isinstance(t, str) and t:
                out.append(t)
    return "\n".join(out).strip()


def _extract_assistant_text_from_events(raw: bytes) -> str:
    """Pull assistant-text content out of an ADK ``/run`` JSON events response.

    Args:
        raw (bytes): Response body from ADK ``/run``.

    Returns:
        str: Concatenated assistant text, or empty when nothing found / not JSON.
    """
    if not raw:
        return ""
    try:
        events = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    if not isinstance(events, list):
        return ""
    pieces: list[str] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("author") == "user":
            continue
        content = ev.get("content") or {}
        for p in content.get("parts") or []:
            if isinstance(p, dict):
                t = p.get("text")
                if isinstance(t, str) and t:
                    pieces.append(t)
    return "\n".join(pieces).strip()


def _decode_request_payload(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_active_chat_id(uid: str | None) -> int | None:
    """Read ``X-Sidekick-Chat-Id`` and confirm ownership; return the chat id or None.

    Args:
        uid (str | None): Authenticated user sub (None when OAuth disabled).

    Returns:
        int | None: Validated chat id, or None when missing / unauthorized.
    """
    raw = request.headers.get("X-Sidekick-Chat-Id", "").strip()
    if not raw:
        return None
    try:
        cid = int(raw)
    except ValueError:
        return None
    owner = uid or "web-ui"
    try:
        with db_connection() as conn:
            chat = get_chat(conn, cid, owner)
    except Exception:
        logger.exception("Failed to resolve chat id %s for user %s", cid, owner)
        return None
    return cid if chat is not None else None


def _ensure_adk_session_seeded(
    adk_base: str,
    app_name: str,
    user_id: str,
    session_id: str,
    chat_id: int,
) -> None:
    """Make sure the ADK session for this chat has prior turns loaded.

    Cheap fast-path: if the proxy already saw this session in the current
    process, do nothing. Otherwise GET the session from ADK; on 404, fetch the
    decrypted history from ``sidekick_chat_messages`` and POST a fresh session
    populated with those events plus the chat's ``active_chat_id`` state.

    Best-effort throughout — failures fall back to ADK's ``auto_create_session``
    on the subsequent ``/run`` (i.e. the agent loses memory but the request still
    succeeds).

    Args:
        adk_base (str): Internal ADK base URL (e.g. ``http://127.0.0.1:8001``).
        app_name (str): ADK app name (always ``sidekick`` here).
        user_id (str): Authenticated user sub.
        session_id (str): Chat's ``agent_session_id``.
        chat_id (int): Chat primary key.

    Returns:
        None
    """
    if not session_id or session_id in _seeded_chat_sessions:
        return
    safe_user = quote(user_id, safe="")
    safe_session = quote(session_id, safe="")
    base_path = f"/apps/{app_name}/users/{safe_user}/sessions"
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(f"{adk_base}{base_path}/{safe_session}")
            if r.status_code == 200:
                _seeded_chat_sessions.add(session_id)
                return
            if r.status_code != 404:
                logger.warning(
                    "ADK session probe %s -> HTTP %s; skipping seed",
                    session_id, r.status_code,
                )
                return
            history = fetch_recent_decrypted_history(chat_id)
            payload: dict[str, Any] = {
                "session_id": session_id,
                "state": {"active_chat_id": int(chat_id)},
            }
            if history:
                payload["events"] = history_as_adk_events(history)
            create = client.post(f"{adk_base}{base_path}", json=payload)
            if create.status_code >= 400:
                logger.warning(
                    "ADK session create %s -> HTTP %s body=%s",
                    session_id, create.status_code, create.text[:200],
                )
                return
            _seeded_chat_sessions.add(session_id)
            if history:
                logger.info(
                    "Seeded ADK session %s with %d prior turns",
                    session_id, len(history),
                )
    except httpx.HTTPError:
        logger.exception("ADK session seed failed for %s", session_id)


def _persist_chat_turn(
    chat_id: int, user_text: str, assistant_text: str
) -> None:
    """Append the user message and assistant reply for one ``/run`` turn (encrypted).

    Best-effort: never raises into the request handler.

    Args:
        chat_id (int): Active chat id.
        user_text (str): Plaintext user message.
        assistant_text (str): Plaintext assistant final reply.

    Returns:
        None
    """
    try:
        with db_connection() as conn:
            if user_text:
                append_chat_message(conn, chat_id, "user", user_text)
            if assistant_text:
                append_chat_message(conn, chat_id, "assistant", assistant_text)
    except Exception:
        logger.exception(
            "Failed to persist encrypted chat messages for chat_id=%s", chat_id
        )
    # Auto-name on the first turn — runs only when the chat is still untitled.
    try:
        maybe_autoname_chat(chat_id, user_text, assistant_text)
    except Exception:
        logger.exception("Auto-naming failed for chat_id=%s", chat_id)


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

    assert_encryption_ready()

    flask_app.register_blueprint(ui_api_bp)
    flask_app.register_blueprint(ui_chats_bp)
    flask_app.register_blueprint(ui_speech_bp)

    @flask_app.get("/favicon.ico")
    def favicon_ico():
        return redirect("/static/favicon.svg", code=302)

    def _send_spa_index() -> Response:
        if not SPA_INDEX.is_file():
            return Response(
                "Frontend bundle is missing. Run `npm install && npm run build` "
                "in the `frontend/` directory (or rebuild the Docker image), "
                "then reload.",
                status=503,
                mimetype="text/plain",
            )
        return send_from_directory(SPA_INDEX.parent, SPA_INDEX.name)

    @flask_app.get("/")
    def index() -> Response:
        return _send_spa_index()

    @flask_app.get("/ui")
    def ui_alias() -> Response:
        return _send_spa_index()

    # Legal pages: same URLs, but the content now lives in the Vue SPA. Hand
    # the bundle to the browser and let the client read window.location.pathname
    # to render the right view (so the chrome stays consistent with the rest
    # of the app and post-login users get the in-app links).
    @flask_app.get("/privacy-policy")
    def privacy_policy() -> Response:
        return _send_spa_index()

    @flask_app.get("/privacy")
    def privacy_legacy_redirect():
        return redirect("/privacy-policy", code=301)

    @flask_app.get("/terms-and-conditions")
    def terms_and_conditions() -> Response:
        return _send_spa_index()

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
        request_payload = _decode_request_payload(body) if body else {}
        chat_id = _resolve_active_chat_id(uid) if path in ("run", "run_sse") else None
        original_user_text = (
            _extract_user_text_from_run_body(body) if chat_id is not None else ""
        )
        if chat_id is not None and path in ("run", "run_sse") and uid:
            run_meta = _peek_run_request(body)
            session_id = run_meta.get("session_id") or ""
            app_name = run_meta.get("app_name") or "sidekick"
            if session_id:
                _ensure_adk_session_seeded(
                    adk_base, app_name, uid, session_id, chat_id
                )
        if uid:
            body = _rewrite_run_body(body, uid, path, chat_id)
        timeout = float(os.environ.get("ADK_PROXY_TIMEOUT", "300"))
        started = time.monotonic()
        with httpx.Client(timeout=timeout) as client:
            upstream = client.request(
                request.method,
                url,
                headers=headers,
                content=body if body else None,
            )
        duration_ms = int((time.monotonic() - started) * 1000)
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
        if (
            chat_id is not None
            and path == "run"
            and 200 <= upstream.status_code < 300
        ):
            assistant_text = _extract_assistant_text_from_events(upstream.content)
            _persist_chat_turn(chat_id, original_user_text, assistant_text)
            persist_run_telemetry(
                chat_id=chat_id,
                session_id=str(request_payload.get("session_id") or ""),
                run_path=path,
                request_payload=request_payload,
                response_payload={},
                raw_events=upstream.content,
                content_type=upstream.headers.get("content-type", ""),
                assistant_text=assistant_text,
                user_text=original_user_text,
                status_code=upstream.status_code,
                duration_ms=duration_ms,
            )
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
