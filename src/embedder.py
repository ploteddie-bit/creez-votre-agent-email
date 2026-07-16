"""Embedder — génération d'embeddings bge-m3 via Ollama local.

L'embedding est calculé sur un texte concaténé :
    Subject: {subject}
    From: {sender_email} ({sender_domain})
    Body: {body_snippet}
    Attachment: {attachment_text}

Ce texte est envoyé à Ollama (`POST /api/embeddings`) avec le modèle
`bge-m3`, qui retourne un vecteur dense de 1024 dimensions, stocké
dans `email_embeddings.embedding` (type `vector(1024)`).

Aucun appel à un LLM cloud — Ollama tourne sur le serveur local.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import logging
from typing import TYPE_CHECKING, Optional

from src.config import get_settings
from src.db import get_connection

# httpx n'est importé qu'au runtime (pour permettre les tests unitaires
# qui n'ont pas besoin du client HTTP)
if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """Erreur lors de la génération ou du stockage d'un embedding."""


class Embedder:
    """Client Ollama pour les embeddings bge-m3."""

    def __init__(self, *, timeout: Optional[float] = None):
        settings = get_settings()
        self.base_url = settings.ollama.base_url.rstrip("/")
        self.model = settings.ollama.embedding_model
        self.dimension = settings.ollama.embedding_dimension
        self.timeout = timeout or float(settings.ollama.timeout_seconds)
        self._client = None  # créé paresseusement par _get_client()

    def _get_client(self):
        """Retourne (et cache) le client httpx."""
        if self._client is None:
            import httpx  # import local pour ne pas casser les tests unitaires
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def __del__(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass

    # ----------------------------------------------------------------
    # Construction du texte à embedder
    # ----------------------------------------------------------------
    @staticmethod
    def build_embedding_text(email: dict) -> str:
        """Concatène les champs pertinents en un texte unique.

        Le `body_snippet` est déjà tronqué à 500 caractères
        en amont (parser.py). Le `body_text` complet n'est jamais
        embedé pour éviter le bruit (signatures, disclaimers).
        """
        parts: list[str] = []
        if subject := email.get("subject"):
            parts.append(f"Subject: {subject}")
        if sender_email := email.get("sender_email"):
            domain = email.get("sender_domain") or ""
            parts.append(f"From: {sender_email} ({domain})")
        if snippet := email.get("body_snippet"):
            parts.append(f"Body: {snippet}")
        if attachment := email.get("attachment_text"):
            parts.append(f"Attachment: {attachment}")
        return "\n".join(parts)

    # ----------------------------------------------------------------
    # Appel Ollama
    # ----------------------------------------------------------------
    def embed_text(self, text: str, *, retries: int = 3) -> list[float]:
        """Appelle Ollama pour générer un embedding.

        Retry 3 fois en cas de timeout / erreur 5xx.
        Retourne un vecteur de `self.dimension` floats.
        """
        if not text.strip():
            raise EmbeddingError("cannot embed empty text")

        import httpx  # import local
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                client = self._get_client()
                resp = client.post(
                    f"{self.base_url}/api/embeddings",
                    json={"model": self.model, "prompt": text},
                )
                resp.raise_for_status()
                payload = resp.json()
                vec = payload.get("embedding")
                if not vec or len(vec) != self.dimension:
                    raise EmbeddingError(
                        f"unexpected embedding dim: got {len(vec) if vec else 0}, "
                        f"expected {self.dimension}"
                    )
                return vec
            except (httpx.TimeoutException, httpx.HTTPStatusError) as e:
                last_error = e
                wait = 2 ** (attempt - 1)  # 1s, 2s, 4s
                logger.warning(
                    "ollama embed attempt %d/%d failed: %s — retry in %ds",
                    attempt,
                    retries,
                    e,
                    wait,
                )
                time.sleep(wait)
        raise EmbeddingError(f"ollama embed failed after {retries} attempts: {last_error}")

    # ----------------------------------------------------------------
    # Stockage
    # ----------------------------------------------------------------
    def store_embedding(self, email_id: str, embedding: list[float]) -> None:
        """Upsert l'embedding dans `email_embeddings`."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO email_embeddings (email_id, embedding)
                    VALUES (%s, %s::vector)
                    ON CONFLICT (email_id) DO UPDATE
                    SET embedding = EXCLUDED.embedding,
                        created_at = NOW()
                    """,
                    (email_id, embedding),
                )
            conn.commit()

    # ----------------------------------------------------------------
    # Pipeline complet
    # ----------------------------------------------------------------
    def embed_email(self, email: dict) -> list[float]:
        """Pipeline complet : texte → embedding → stockage.

        `email` peut être un dict (tel que retourné par une query SQL)
        ou un `EmailInDB`. Les champs requis sont : id, subject,
        sender_email, sender_domain, body_snippet, attachment_text.
        """
        text = self.build_embedding_text(email)
        vec = self.embed_text(text)
        if email_id := email.get("id"):
            self.store_embedding(email_id, vec)
        return vec

    def embed_batch(self, email_ids: list[str], *, batch_size: int = 50) -> int:
        """Embedde une liste d'email_ids. Retourne le nombre de succès.

        Les emails sans contenu pertinent sont skippés.
        """
        if not email_ids:
            return 0

        # Récupérer les emails en une query
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, subject, sender_email, sender_domain,
                           body_snippet, attachment_text
                    FROM emails
                    WHERE id = ANY(%s)
                    """,
                    (email_ids,),
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                emails = [dict(zip(cols, row)) for row in rows]

        done = 0
        for email in emails:
            try:
                self.embed_email(email)
                done += 1
            except EmbeddingError as e:
                logger.warning("skip email %s: %s", email.get("id"), e)
        logger.info("embedded %d/%d emails", done, len(email_ids))
        return done

    def embed_unprocessed(self, limit: int = 100) -> int:
        """Embedde les emails qui n'ont pas encore d'embedding.

        Renvoie le nombre effectivement embeddés.
        """
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT e.id FROM emails e
                    LEFT JOIN email_embeddings ee ON ee.email_id = e.id
                    WHERE ee.email_id IS NULL
                    ORDER BY e.date_received DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                ids = [row[0] for row in cur.fetchall()]
        if not ids:
            return 0
        return self.embed_batch(ids)

    # ----------------------------------------------------------------
    # Recherche de similarité
    # ----------------------------------------------------------------
    def get_similar(
        self,
        query_embedding: list[float],
        *,
        sender_email: Optional[str] = None,
        sender_domain: Optional[str] = None,
        limit: int = 5,
    ) -> list[dict]:
        """Recherche cascade : même sender → même domaine → global.

        Retourne une liste de `{"email": ..., "distance": ...}`.
        Stratégie appliquée exposée via `retrieval_strategy` (utilisée
        plus tard par le recommender pour la confiance hybride).
        """
        # Convertir en format pgvector
        vec_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

        # 1. Même expéditeur
        if sender_email:
            results = self._vector_search(vec_str, sender_email=sender_email, limit=limit)
            if len(results) >= 3:
                return results

        # 2. Même domaine
        if sender_domain:
            results = self._vector_search(vec_str, sender_domain=sender_domain, limit=limit)
            if len(results) >= 3:
                return results

        # 3. Fallback global
        return self._vector_search(vec_str, limit=limit)

    def _vector_search(
        self,
        vec_str: str,
        *,
        sender_email: Optional[str] = None,
        sender_domain: Optional[str] = None,
        limit: int = 5,
    ) -> list[dict]:
        """Recherche vectorielle cosine brute (pgvector `<=>`)."""
        where_clauses: list[str] = []
        params: list = [vec_str, limit]
        if sender_email:
            where_clauses.append("e.sender_email = %s")
            params.insert(-1, sender_email)
        if sender_domain and not sender_email:
            where_clauses.append("e.sender_domain = %s")
            params.insert(-1, sender_domain)
        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        params = [vec_str] + ([sender_email] if sender_email else []) + \
                 ([sender_domain] if sender_domain and not sender_email else []) + [limit]

        sql = f"""
            SELECT e.id, e.subject, e.sender_email,
                   ee.embedding <=> %s::vector AS distance
            FROM email_embeddings ee
            JOIN emails e ON e.id = ee.email_id
            {where_sql}
            ORDER BY distance ASC
            LIMIT %s
        """
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in rows]
