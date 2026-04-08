"""Configurable text label embedded in titles, bodies, and Google metadata for Sidekick-owned data."""

from __future__ import annotations

import os
from typing import Any, Optional

_DEFAULT = "sidekick.amngupta.com"


def sidekick_resource_label() -> str:
    """Return the configured marker string for Sidekick-created resources.

    Returns:
        str: Label from ``SIDEKICK_RESOURCE_LABEL``, or the default if unset/blank.
    """
    raw = os.environ.get("SIDEKICK_RESOURCE_LABEL", _DEFAULT).strip()
    return raw or _DEFAULT


def ensure_title_tagged(title: str) -> str:
    """Ensure the Sidekick label appears in a title (typically as ``[label]``).

    Args:
        title (str): Raw title text.

    Returns:
        str: Title unchanged if the label is present; otherwise label appended.
    """
    label = sidekick_resource_label()
    if label in title:
        return title
    t = title.rstrip()
    return f"{t} [{label}]" if t else f"[{label}]"


def ensure_body_lines_tagged(body: Optional[str]) -> str:
    """Ensure the Sidekick label appears in note or event body text.

    Args:
        body (Optional[str]): Body text, or None/empty for label-only body.

    Returns:
        str: Body with label prepended when missing, or label alone if empty.
    """
    label = sidekick_resource_label()
    if body is None or not str(body).strip():
        return label
    text = str(body)
    if label in text:
        return text
    return f"{label}\n\n{text.lstrip()}"


def ensure_calendar_description(description: Optional[str]) -> str:
    """Ensure the Sidekick label appears in a calendar event description.

    Args:
        description (Optional[str]): Event description, or None.

    Returns:
        str: Description with label appended when missing.
    """
    label = sidekick_resource_label()
    base = (description or "").rstrip()
    if label in base:
        return base
    if not base:
        return label
    return f"{base}\n\n{label}"


def ensure_task_notes(notes: Optional[str]) -> Optional[str]:
    """Ensure the Sidekick label appears in Google Tasks notes.

    Args:
        notes (Optional[str]): Task notes, or None/empty.

    Returns:
        Optional[str]: Notes with label prepended when missing, or label if empty.
    """
    label = sidekick_resource_label()
    if notes is None or not str(notes).strip():
        return label
    text = str(notes)
    if label in text:
        return text
    return f"{label}\n\n{text.lstrip()}"


def title_or_text_has_label(title: str, text: Optional[str] = None) -> bool:
    """Check whether the Sidekick label appears in title or optional text.

    Args:
        title (str): Title string.
        text (Optional[str]): Optional body or notes text.

    Returns:
        bool: True if the label appears in either argument.
    """
    label = sidekick_resource_label()
    if label in (title or ""):
        return True
    return label in (text or "")


def calendar_event_has_label(ev: dict[str, Any]) -> bool:
    """Return True if a Calendar API event is tagged as Sidekick-owned.

    Args:
        ev (dict[str, Any]): Google Calendar event resource dict.

    Returns:
        bool: True if summary, description, or private extended properties carry the label.
    """
    label = sidekick_resource_label()
    if label in (ev.get("summary") or "") or label in (ev.get("description") or ""):
        return True
    priv = (ev.get("extendedProperties") or {}).get("private") or {}
    if not isinstance(priv, dict):
        return False
    if priv.get("sidekick_label") == label:
        return True
    return label in priv or label in priv.values()


def task_item_has_label(task: dict[str, Any]) -> bool:
    """Return True if a Tasks API item carries the Sidekick label.

    Args:
        task (dict[str, Any]): Google Tasks task resource dict.

    Returns:
        bool: True if title or notes contain the Sidekick label.
    """
    return title_or_text_has_label(task.get("title") or "", task.get("notes"))
