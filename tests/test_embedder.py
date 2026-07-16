"""Tests pour src.embedder — construction du texte + recherche de similarité."""
from __future__ import annotations


def test_build_embedding_text_basic(sample_email_dict: dict) -> None:
    """Le texte embeddé doit contenir subject, sender, body."""
    from src.embedder import Embedder

    text = Embedder.build_embedding_text(sample_email_dict)
    assert "Subject: Python 3.13 release" in text
    assert "From: noreply@newsletter.com (newsletter.com)" in text
    assert "Body: Python 3.13 is out" in text
    # Pas d'attachment dans cet email
    assert "Attachment:" not in text


def test_build_embedding_text_with_attachment(sample_email_dict: dict) -> None:
    """L'attachment_text est concaténé s'il est présent."""
    from src.embedder import Embedder

    email = {**sample_email_dict, "attachment_text": "Facture n° 1234, montant 42€"}
    text = Embedder.build_embedding_text(email)
    assert "Attachment: Facture n° 1234" in text


def test_build_embedding_text_omits_empty_fields(sample_email_dict: dict) -> None:
    """Les champs vides ne doivent pas polluer le texte."""
    from src.embedder import Embedder

    email = {**sample_email_dict, "subject": None, "attachment_text": None}
    text = Embedder.build_embedding_text(email)
    assert "Subject:" not in text
    assert "Attachment:" not in text
    assert "From:" in text
    assert "Body:" in text


def test_embedding_error_on_empty_text() -> None:
    """Un texte vide doit lever EmbeddingError, pas faire un appel réseau."""
    from src.embedder import Embedder, EmbeddingError

    embedder = Embedder()
    with __import__("pytest").raises(EmbeddingError, match="empty text"):
        embedder.embed_text("")
