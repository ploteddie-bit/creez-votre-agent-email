"""Ingester — insertion idempotente des emails dans PostgreSQL.

Toute la logique d'upsert des emails est centralisée ici.
L'idempotence est garantie par `INSERT ... ON CONFLICT (id) DO UPDATE`,
ce qui permet de ré-ingérer le même mail sans créer de doublon
(utile après un crash/restart du pipeline).

Usage :
    from src.ingester import EmailIngester
    from src.models import EmailInDB

    ingester = EmailIngester()
    ingester.ingest_email(parsed_email)
    ingester.ingest_batch([email1, email2, ...])
"""
from __future__ import annotations

import logging
from typing import Iterable

from src.db import get_connection
from src.models import EmailInDB

logger = logging.getLogger(__name__)


# Requête UPSERT : si l'email existe déjà, on met à jour les champs
# qui peuvent changer (labels, is_read, is_starred, is_archived).
# On NE réécrit PAS body_text, body_html, raw_headers (figés à l'ingestion).
_UPSERT_SQL = """
INSERT INTO emails (
    id, thread_id, sender, sender_email, sender_domain, recipients,
    subject, body_text, body_snippet, body_html,
    has_attachments, attachment_text,
    date_received, labels, is_read, is_starred, is_deleted, is_archived,
    raw_headers
) VALUES (
    %(id)s, %(thread_id)s, %(sender)s, %(sender_email)s, %(sender_domain)s, %(recipients)s,
    %(subject)s, %(body_text)s, %(body_snippet)s, %(body_html)s,
    %(has_attachments)s, %(attachment_text)s,
    %(date_received)s, %(labels)s, %(is_read)s, %(is_starred)s, %(is_deleted)s, %(is_archived)s,
    %(raw_headers)s
)
ON CONFLICT (id) DO UPDATE SET
    labels = EXCLUDED.labels,
    is_read = EXCLUDED.is_read,
    is_starred = EXCLUDED.is_starred,
    is_archived = EXCLUDED.is_archived,
    is_deleted = EXCLUDED.is_deleted
"""


class EmailIngester:
    """Service d'ingestion idempotente des emails."""

    @staticmethod
    def _to_row(email: EmailInDB) -> dict:
        """Convertit le modèle en dict de paramètres SQL.

        `raw_headers` (dict Python) est adapté en JSONB via
        `psycopg2.extras.Json` — psycopg2 ne sait pas adapter
        un dict nativement.
        """
        from psycopg2.extras import Json  # lazy import (tests unitaires)

        data = email.model_dump()
        if data.get("raw_headers") is not None:
            data["raw_headers"] = Json(data["raw_headers"])
        return data

    def ingest_email(self, email: EmailInDB) -> bool:
        """Insère ou met à jour un email. Retourne True si succès.

        En cas d'erreur, log et retourne False (sans remonter
        d'exception) — l'email sera retenté au prochain batch.
        """
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(_UPSERT_SQL, self._to_row(email))
                conn.commit()
            logger.debug("ingested email %s", email.id)
            return True
        except Exception as e:
            logger.error("failed to ingest email %s: %s", email.id, e)
            return False

    def ingest_batch(self, emails: Iterable[EmailInDB], *, batch_size: int = 100) -> int:
        """Insère un batch d'emails en une seule transaction.

        Retourne le nombre d'emails effectivement ingérés.
        En cas d'erreur sur un email, on rollback tout le batch
        (le caller retentera avec un batch plus petit).
        """
        emails_list = list(emails)
        if not emails_list:
            return 0

        ingested = 0
        # On découpe en sous-batches de `batch_size` pour éviter
        # les transactions trop longues
        for i in range(0, len(emails_list), batch_size):
            chunk = emails_list[i : i + batch_size]
            try:
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        from psycopg2.extras import execute_batch
                        execute_batch(cur, _UPSERT_SQL, [self._to_row(e) for e in chunk])
                    conn.commit()
                ingested += len(chunk)
                logger.info("ingested batch of %d emails", len(chunk))
            except Exception as e:
                logger.error("batch ingest failed at offset %d: %s", i, e)
        return ingested

    def count(self) -> int:
        """Nombre total d'emails en base (utile pour les healthchecks)."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM emails")
                return cur.fetchone()[0]
