"""Tests pour src.attachment_parser — extraction PDF sécurisée."""
from __future__ import annotations

import base64

import pytest

pypdf = pytest.importorskip("pypdf", reason="pypdf non installé (requirements.txt)")

from src.attachment_parser import AttachmentParser

def _build_minimal_pdf() -> bytes:
    """Construit un PDF valide (xref correct) contenant 'Facture numero 42'.

    pypdf ≥ 6 exige une table xref cohérente — les offsets sont donc
    calculés à la construction plutôt qu'écrits en dur.
    """
    stream = b"BT /F1 12 Tf 72 720 Td (Facture numero 42) Tj ET"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
        b" /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(pdf))
        pdf += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(pdf)
    pdf += f"xref\n0 {len(objs) + 1}\n".encode()
    pdf += b"0000000000 65535 f \n"
    for off in offsets:
        pdf += f"{off:010d} 00000 n \n".encode()
    pdf += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(pdf)


MINIMAL_PDF = _build_minimal_pdf()


def test_extract_pdf_text_basic() -> None:
    """Un PDF valide doit donner son texte."""
    parser = AttachmentParser()
    text = parser.extract_pdf_text(MINIMAL_PDF)
    assert text is not None
    assert "Facture numero 42" in text


def test_extract_pdf_text_base64url_input() -> None:
    """Le format base64url de l'API Gmail doit être accepté."""
    parser = AttachmentParser()
    b64 = base64.urlsafe_b64encode(MINIMAL_PDF).decode("ascii").rstrip("=")
    text = parser.extract_pdf_text(b64)
    assert text is not None
    assert "Facture numero 42" in text


def test_extract_oversized_attachment_ignored() -> None:
    """Une pièce au-delà de la limite est ignorée (None), sans exception."""
    parser = AttachmentParser(max_size_mb=0.0001)  # ~105 octets
    assert parser.extract_pdf_text(MINIMAL_PDF) is None


def test_extract_corrupt_pdf_returns_none() -> None:
    """Un PDF corrompu → None (log), jamais d'exception."""
    parser = AttachmentParser()
    assert parser.extract_pdf_text(b"%PDF-1.4 garbage-corrupted-bytes") is None


def test_extract_non_pdf_returns_none() -> None:
    """Un contenu qui n'est pas un PDF (magic bytes) → None."""
    parser = AttachmentParser()
    assert parser.extract_pdf_text(b"MZ\x90\x00 fake exe") is None


def test_extract_empty_input() -> None:
    """Entrées vides → None."""
    parser = AttachmentParser()
    assert parser.extract_pdf_text(None) is None
    assert parser.extract_pdf_text(b"") is None
    assert parser.extract_pdf_text("") is None


def test_extract_all_filters_and_concatenates() -> None:
    """Seules les PJ PDF sont extraites, concaténées entre elles."""
    parser = AttachmentParser()
    attachments = [
        {"filename": "facture.pdf", "mimeType": "application/pdf", "data": MINIMAL_PDF},
        {"filename": "photo.png", "mimeType": "image/png", "data": b"\x89PNG fake"},
        {"filename": "relance.pdf", "mimeType": "application/pdf", "data": MINIMAL_PDF},
    ]
    text = parser.extract_all(attachments)
    assert text is not None
    assert text.count("Facture numero 42") == 2  # les 2 PDF, pas le PNG


def test_extract_all_empty() -> None:
    parser = AttachmentParser()
    assert parser.extract_all(None) is None
    assert parser.extract_all([]) is None
