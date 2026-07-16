"""Fixtures pytest partagées.

Ce fichier définit les fixtures de base (settings, mocks, exemples
d'emails) utilisées par tous les tests du projet.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Permettre l'import de `src.*` depuis la racine
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> None:
    """Réinitialise le cache de settings entre chaque test."""
    from src.config import reset_settings
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def sample_email_dict() -> dict:
    """Un email exemple (dict, comme retourné par une query SQL)."""
    return {
        "id": "msg_abc123",
        "thread_id": "thread_xyz",
        "sender": "Newsletter Tech",
        "sender_email": "noreply@newsletter.com",
        "sender_domain": "newsletter.com",
        "recipients": ["eddie@example.com"],
        "subject": "Python 3.13 release",
        "body_snippet": "Python 3.13 is out with the new GIL-free mode...",
        "attachment_text": None,
    }


@pytest.fixture
def sample_email_pydantic():
    """Un email exemple (EmailInDB Pydantic)."""
    from datetime import datetime
    from src.models import EmailInDB
    return EmailInDB(
        id="msg_abc123",
        thread_id="thread_xyz",
        sender="Newsletter Tech",
        sender_email="noreply@newsletter.com",
        sender_domain="newsletter.com",
        recipients=["eddie@example.com"],
        subject="Python 3.13 release",
        body_text="Python 3.13 is out with the new GIL-free mode...",
        body_snippet="Python 3.13 is out with the new GIL-free mode...",
        date_received=datetime(2026, 7, 16, 12, 0, 0),
    )
