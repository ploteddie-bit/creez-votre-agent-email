"""AttachmentParser — extraction de texte des pièces jointes PDF.

Livrable de l'agent 4 (parser-sanitizer) du PLAN DE TRAVAIL.

Règles (SPEC) :
- Extraction via `pypdf` uniquement — aucun envoi à un service externe.
- Pièces jointes > `sandbox.max_attachment_size_mb` (défaut 5 Mo) → ignorées.
- PDF corrompu / illisible → log + `None` (jamais d'exception vers le caller).
- Le texte extrait est destiné à être stocké dans `emails.attachment_text`
  puis concaténé au texte embeddé (voir `src/embedder.build_embedding_text`).

Les PJ sont du contenu NON FIABLE au même titre que le corps du mail :
le texte extrait passe par les mêmes garde-fous anti-injection en aval.
"""
from __future__ import annotations

import base64
import io
import logging
from typing import Optional

from src.config import get_settings

logger = logging.getLogger(__name__)


class AttachmentParser:
    """Extracteur de texte PDF pour les pièces jointes Gmail."""

    def __init__(self, *, max_size_mb: float | None = None):
        """`max_size_mb` surcharge la config (utile pour les tests)."""
        self.max_size_mb = (
            float(max_size_mb)
            if max_size_mb is not None
            else float(get_settings().sandbox.max_attachment_size_mb)
        )

    # ----------------------------------------------------------------
    # Extraction unitaire
    # ----------------------------------------------------------------
    def extract_pdf_text(self, attachment_data: bytes | str | None) -> Optional[str]:
        """Extrait le texte d'un PDF.

        Args:
            attachment_data: bytes bruts du PDF, ou chaîne base64url
                (format `body.data` de l'API Gmail), ou None.

        Returns:
            Le texte concaténé des pages, ou None si la pièce est
            absente, trop grosse, pas un PDF, ou illisible.
        """
        raw = self._to_bytes(attachment_data)
        if not raw:
            return None

        limit_bytes = self.max_size_mb * 1024 * 1024
        if len(raw) > limit_bytes:
            logger.info(
                "attachment skipped: %.2f Mo > limite %.2f Mo",
                len(raw) / (1024 * 1024),
                self.max_size_mb,
            )
            return None

        if not raw.startswith(b"%PDF"):
            logger.debug("attachment skipped: not a PDF (bad magic bytes)")
            return None

        try:
            from pypdf import PdfReader  # lazy import (tests unitaires)

            reader = PdfReader(io.BytesIO(raw), strict=False)
            pages: list[str] = []
            for page in reader.pages:
                try:
                    text = page.extract_text() or ""
                except Exception as e:  # page individuelle corrompue
                    logger.debug("PDF page skipped: %s", e)
                    continue
                if text.strip():
                    pages.append(text.strip())
            return "\n".join(pages).strip() or None
        except Exception as e:
            logger.warning("PDF extraction failed: %s", e)
            return None

    # ----------------------------------------------------------------
    # Extraction multi-pièces
    # ----------------------------------------------------------------
    def extract_all(self, attachments: list[dict] | None) -> Optional[str]:
        """Concatène le texte de toutes les PJ PDF d'un email.

        Args:
            attachments: liste de dicts `{"filename", "mimeType", "data"}`
                où `data` est bytes ou base64url (déjà récupéré via
                `attachments.get` de l'API Gmail).

        Returns:
            Texte concaténé (double \n entre pièces), ou None si rien
            d'exploitable. Les pièces non-PDF sont ignorées (SPEC :
            seul le PDF est supporté à ce stade).
        """
        texts: list[str] = []
        for att in attachments or []:
            if (att.get("mimeType") or "").lower() != "application/pdf":
                continue
            text = self.extract_pdf_text(att.get("data"))
            if text:
                texts.append(text)
        return "\n\n".join(texts) or None

    # ----------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------
    @staticmethod
    def _to_bytes(data: bytes | str | None) -> Optional[bytes]:
        """Normalise l'entrée en bytes (base64url Gmail accepté)."""
        if not data:
            return None
        if isinstance(data, bytes):
            return data
        if isinstance(data, str):
            try:
                padded = data + "=" * (-len(data) % 4)
                return base64.urlsafe_b64decode(padded)
            except Exception:
                return None
        return None
