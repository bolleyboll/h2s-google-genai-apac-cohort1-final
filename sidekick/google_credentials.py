"""Google OAuth scope selection, token persistence, and credential loading for API tools.

    Tokens are stored per user (Google ``sub``) in ``sidekick_google_oauth``; refresh is applied
    when access tokens expire.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import sidekick._google_auth_patch  # noqa: F401 — patch utcnow before google.oauth2 use

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from sqlalchemy import text

from sidekick.db import db_connection

logger = logging.getLogger(__name__)

# OAuth scopes: sign-in + optional Calendar / Tasks / Keep (see SIDEKICK_ENABLE_GOOGLE_*).
# ``calendar.events``, ``tasks``, and ``keep`` are read/write scopes and cover create/update/delete
# for Sidekick tools (no extra scope needed for patch or delete).


def _env_api_enabled(var: str) -> bool:
    """Return whether an env flag treats a Google API as enabled.

    Args:
        var (str): Environment variable name.

    Returns:
        bool: False if the value is ``0``, ``false``, ``no``, or ``off``; True otherwise (including unset).
    """
    v = os.environ.get(var, "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def calendar_api_enabled_in_oauth() -> bool:
    """Return whether Google Calendar API scopes should be requested.

    Returns:
        bool: True unless ``SIDEKICK_ENABLE_GOOGLE_CALENDAR`` disables Calendar.
    """
    return _env_api_enabled("SIDEKICK_ENABLE_GOOGLE_CALENDAR")


def tasks_api_enabled_in_oauth() -> bool:
    """Return whether Google Tasks API scopes should be requested.

    Returns:
        bool: True unless ``SIDEKICK_ENABLE_GOOGLE_TASKS`` disables Tasks.
    """
    return _env_api_enabled("SIDEKICK_ENABLE_GOOGLE_TASKS")


def keep_api_enabled_in_oauth() -> bool:
    """Return whether Google Keep API scopes should be requested.

    Returns:
        bool: True unless ``SIDEKICK_ENABLE_GOOGLE_KEEP`` disables Keep.
    """
    return _env_api_enabled("SIDEKICK_ENABLE_GOOGLE_KEEP")


def sidekick_google_oauth_scope() -> str:
    """Build the OAuth scope string for Sidekick sign-in and Google APIs.

    Returns:
        str: Space-separated scopes (OpenID, email, profile, plus optional product scopes).
    """
    parts = [
        "openid",
        "email",
        "profile",
    ]
    if calendar_api_enabled_in_oauth():
        parts.append("https://www.googleapis.com/auth/calendar.events")
    if tasks_api_enabled_in_oauth():
        parts.append("https://www.googleapis.com/auth/tasks")
    if keep_api_enabled_in_oauth():
        parts.append("https://www.googleapis.com/auth/keep")
    return " ".join(parts)

_TOKEN_URI = "https://oauth2.googleapis.com/token"


def _expiry_for_google_credentials(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize a datetime to timezone-aware UTC for ``google.oauth2`` credentials.

    Args:
        dt (Optional[datetime]): Expiry from storage or the token response.

    Returns:
        Optional[datetime]: Aware UTC, or None if input is missing/invalid.
    """
    if dt is None or not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _expiry_for_db(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize a datetime for PostgreSQL ``TIMESTAMPTZ`` columns.

    Args:
        dt (Optional[datetime]): Expiry instant.

    Returns:
        Optional[datetime]: Aware UTC, or None if input is missing/invalid.
    """
    if dt is None or not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_creds_expiry(creds: Credentials) -> None:
    """Normalize ``creds.expiry`` to timezone-aware UTC in place.

    Args:
        creds (Credentials): OAuth credentials object to mutate.

    Returns:
        None
    """
    if creds.expiry is not None:
        creds.expiry = _expiry_for_google_credentials(creds.expiry)


def _client_id_secret() -> tuple[str, str]:
    """Load OAuth web client credentials from the environment.

    Returns:
        tuple[str, str]: ``(client_id, client_secret)``, each possibly empty if unset.
    """
    cid = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    csec = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    return cid, csec


def persist_oauth_token_from_authlib(owner_sub: str, token: dict[str, Any]) -> None:
    """Persist or merge OAuth tokens after a successful Authlib callback.

    Args:
        owner_sub (str): Google subject identifier for the user.
        token (dict[str, Any]): Token payload from Authlib (access, refresh, expiry, scope).

    Returns:
        None: Always; skips database writes when OAuth client id/secret are not configured.
    """
    cid, csec = _client_id_secret()
    if not cid or not csec:
        return

    access = token.get("access_token") or ""
    refresh = token.get("refresh_token")
    expires_in = token.get("expires_in")
    raw_scope = token.get("scope") or ""
    if isinstance(raw_scope, list):
        scope = " ".join(str(s) for s in raw_scope).strip()
    else:
        scope = str(raw_scope).strip()

    expires_at: Optional[datetime] = None
    if expires_in is not None:
        try:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
        except (TypeError, ValueError):
            pass

    with db_connection() as conn:
        conn.execute(
            text(
                """
                INSERT INTO sidekick_google_oauth (
                    owner_sub, refresh_token, access_token, expires_at, scope, updated_at
                )
                VALUES (:owner, :rt, :at, :exp, :scope, NOW())
                ON CONFLICT (owner_sub) DO UPDATE SET
                    refresh_token = COALESCE(
                        EXCLUDED.refresh_token,
                        sidekick_google_oauth.refresh_token
                    ),
                    access_token = EXCLUDED.access_token,
                    expires_at = EXCLUDED.expires_at,
                    scope = CASE
                        WHEN EXCLUDED.scope IS NOT NULL AND EXCLUDED.scope != ''
                        THEN EXCLUDED.scope
                        ELSE sidekick_google_oauth.scope
                    END,
                    updated_at = NOW()
                """
            ),
            {
                "owner": owner_sub,
                "rt": refresh,
                "at": access or None,
                "exp": expires_at,
                "scope": scope or None,
            },
        )


def _persist_refreshed_access(owner_sub: str, access_token: str, expiry: datetime) -> None:
    """Write a refreshed access token and expiry to ``sidekick_google_oauth``.

    Args:
        owner_sub (str): Google subject identifier.
        access_token (str): New access token string.
        expiry (datetime): Token expiry instant.

    Returns:
        None
    """
    exp = _expiry_for_db(expiry)
    with db_connection() as conn:
        conn.execute(
            text(
                """
                UPDATE sidekick_google_oauth
                SET access_token = :at, expires_at = :exp, updated_at = NOW()
                WHERE owner_sub = :owner
                """
            ),
            {"owner": owner_sub, "at": access_token, "exp": exp},
        )


def get_google_api_credentials(
    owner_sub: str,
) -> tuple[Optional[Credentials], Optional[str]]:
    """Load Google API credentials and surface failures as a string.

    Args:
        owner_sub (str): Google subject identifier.

    Returns:
        tuple[Optional[Credentials], Optional[str]]: ``(creds, None)`` on success, or
        ``(None, error_message)`` on failure.
    """
    try:
        return _get_valid_credentials_impl(owner_sub), None
    except Exception as e:
        logger.exception("Google OAuth credential handling failed")
        return None, str(e)


def get_valid_credentials(owner_sub: str) -> Optional[Credentials]:
    """Return usable OAuth credentials for API calls, refreshing when needed.

    Args:
        owner_sub (str): Google subject identifier.

    Returns:
        Optional[Credentials]: Valid credentials, or None on failure or missing tokens.
    """
    creds, _ = get_google_api_credentials(owner_sub)
    return creds


def load_credentials_for_google_api(
    owner_sub: str,
) -> tuple[Optional[Credentials], Optional[dict[str, Any]]]:
    """Load credentials for ADK tools, returning JSON-serializable errors.

    Args:
        owner_sub (str): Google subject identifier.

    Returns:
        tuple[Optional[Credentials], Optional[dict[str, Any]]]: ``(creds, None)`` on success,
        or ``(None, error_dict)`` for embedding in tool output JSON.
    """
    creds, err = get_google_api_credentials(owner_sub)
    if err:
        return None, {"error": "oauth_credential_error", "message": err}
    if creds is None:
        return None, {"error": "no_credentials", "hint": google_api_auth_error_message()}
    return creds, None


def _get_valid_credentials_impl(owner_sub: str) -> Optional[Credentials]:
    """Internal: build ``Credentials`` from DB and refresh if expired.

    Args:
        owner_sub (str): Google subject identifier.

    Returns:
        Optional[Credentials]: Usable credentials, or None if OAuth is unconfigured or tokens missing.
    """
    cid, csec = _client_id_secret()
    if not cid or not csec:
        return None

    with db_connection() as conn:
        r = conn.execute(
            text(
                "SELECT refresh_token, access_token, expires_at, scope "
                "FROM sidekick_google_oauth WHERE owner_sub = :owner"
            ),
            {"owner": owner_sub},
        )
        row = r.fetchone()
    if row is None:
        return None
    mapping = dict(row._mapping)
    refresh_token = mapping.get("refresh_token")
    if not refresh_token:
        return None

    scopes = (mapping.get("scope") or sidekick_google_oauth_scope()).split()
    expires_at = _expiry_for_google_credentials(mapping.get("expires_at"))

    creds = Credentials(
        token=mapping.get("access_token"),
        refresh_token=refresh_token,
        token_uri=_TOKEN_URI,
        client_id=cid,
        client_secret=csec,
        scopes=scopes,
        expiry=expires_at,
    )
    _normalize_creds_expiry(creds)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        _normalize_creds_expiry(creds)
        if creds.token and creds.expiry:
            _persist_refreshed_access(owner_sub, creds.token, creds.expiry)
    return creds


def google_api_auth_error_message() -> str:
    """Build guidance text when Google API tools cannot obtain credentials.

    Returns:
        str: Hints covering sign-in and enabled product APIs/scopes.
    """
    bits = [
        "Sign in via /login/google with a stored refresh token.",
    ]
    if calendar_api_enabled_in_oauth():
        bits.append("Enable Calendar API and calendar.events scope when Calendar integration is on.")
    if tasks_api_enabled_in_oauth():
        bits.append("Enable Tasks API and tasks scope when Tasks integration is on.")
    if keep_api_enabled_in_oauth():
        bits.append(
            "Enable Keep API and .../auth/keep when Keep is on (often Workspace-only)."
        )
    return " ".join(bits)
