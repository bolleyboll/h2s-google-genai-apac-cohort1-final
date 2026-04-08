"""Patch google.auth._helpers.utcnow() to return timezone-aware UTC.

    google.oauth2 refresh_grant can set credential expiry as aware UTC while
    ``_helpers.utcnow()`` returns naive UTC, which can cause TypeError in expiry checks.
    Applied once on import before other google.auth code uses ``utcnow()``.
"""

from __future__ import annotations

from datetime import datetime, timezone


def _apply_utcnow_patch() -> None:
    """Monkey-patch ``google.auth._helpers.utcnow`` to return timezone-aware UTC.

    Prevents naive/aware datetime mismatches during OAuth token expiry checks. Safe to call
    repeatedly; no-ops if already patched.

    Returns:
        None: This function mutates ``google.auth._helpers`` in place.
    """
    import google.auth._helpers as gh

    if getattr(gh, "_sidekick_patched_utcnow", False):
        return

    def utcnow() -> datetime:
        return datetime.now(timezone.utc)

    gh.utcnow = utcnow  # type: ignore[method-assign, assignment]
    gh._sidekick_patched_utcnow = True


_apply_utcnow_patch()
