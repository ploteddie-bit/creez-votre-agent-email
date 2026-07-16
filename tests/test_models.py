"""Tests pour src.models — validation Pydantic stricte."""
from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_mail_decision_valid() -> None:
    """Une décision valide doit être acceptée."""
    from src.models import MailDecision

    d = MailDecision(
        classification="newsletter",
        executable_operation="archive",
        recommended_user_action="none",
        confidence=0.94,
        reason="Newsletter tech, pattern archivé 12 fois",
    )
    assert d.confidence == 0.94
    assert d.executable_operation == "archive"


def test_mail_decision_extra_field_forbidden() -> None:
    """L'IA ne doit pas pouvoir inventer de champs supplémentaires."""
    from src.models import MailDecision

    with pytest.raises(ValidationError) as exc_info:
        MailDecision(
            classification="newsletter",
            executable_operation="archive",
            confidence=0.94,
            reason="x",
            injected_field="injection",  # ← doit être refusé
        )
    assert "injected_field" in str(exc_info.value)


def test_mail_decision_confidence_range() -> None:
    """La confiance doit être entre 0 et 1."""
    from src.models import MailDecision

    with pytest.raises(ValidationError):
        MailDecision(
            classification="newsletter",
            executable_operation="archive",
            confidence=1.5,  # > 1
            reason="x",
        )
    with pytest.raises(ValidationError):
        MailDecision(
            classification="newsletter",
            executable_operation="archive",
            confidence=-0.1,  # < 0
            reason="x",
        )


def test_mail_decision_invalid_operation() -> None:
    """Les opérations non allowlistées doivent être refusées."""
    from src.models import MailDecision

    with pytest.raises(ValidationError):
        MailDecision(
            classification="needs_reply",
            executable_operation="send",  # ← interdit !
            confidence=0.9,
            reason="x",
        )


def test_mail_decision_reason_max_length() -> None:
    """La raison est plafonnée à 500 chars (évite prompt bloat + détection)."""
    from src.models import MailDecision

    with pytest.raises(ValidationError):
        MailDecision(
            classification="needs_reply",
            executable_operation="none",
            confidence=0.5,
            reason="x" * 501,  # ← 501 chars, trop
        )


def test_action_queue_item_idempotency_key_required() -> None:
    """L'idempotency_key est obligatoire (sinon on risque les doubles actions)."""
    from src.models import ActionQueueItem

    with pytest.raises(ValidationError):
        ActionQueueItem(
            email_id="msg_1",
            operation="archive",
            idempotency_key="",  # ← vide
        )


def test_email_in_db_default_flags() -> None:
    """Les flags par défaut doivent être False (sauf has_attachments)."""
    from datetime import datetime
    from src.models import EmailInDB

    e = EmailInDB(
        id="msg_1",
        sender="Test",
        sender_email="test@test.com",
        date_received=datetime(2026, 7, 16),
    )
    assert e.is_read is False
    assert e.is_starred is False
    assert e.is_deleted is False
    assert e.is_archived is False
    assert e.has_attachments is False
    assert e.labels == []
    assert e.recipients == []


def test_email_in_db_raw_headers_field() -> None:
    """raw_headers existe dans le modèle (contrat avec l'UPSERT ingester)."""
    from datetime import datetime
    from src.models import EmailInDB

    e = EmailInDB(
        id="msg_1",
        sender="Test",
        sender_email="test@test.com",
        date_received=datetime(2026, 7, 16),
    )
    assert "raw_headers" in e.model_dump()
    assert e.raw_headers is None

    e2 = EmailInDB(
        id="msg_2",
        sender="Test",
        sender_email="test@test.com",
        date_received=datetime(2026, 7, 16),
        raw_headers={"From": "test@test.com", "DKIM-Signature": "v=1; ..."},
    )
    assert e2.raw_headers["DKIM-Signature"].startswith("v=1")
