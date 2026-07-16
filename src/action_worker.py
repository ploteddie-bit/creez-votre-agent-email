"""ActionWorker - consomme action_queue et execute les actions Gmail API.

Le worker est le SEUL composant qui declenche des appels
Gmail ecrire (modify labels). Il :
  1. Prend un job pending de action_queue (FOR UPDATE SKIP LOCKED)
  2. Le marque en 'executing'
  3. Appelle gmail_client.modify_labels()
  4. Met a jour le statut selon succes/echec
  5. Retry 3x max avant de passer en 'failed'

Idempotence : la cle idempotency_key (format {email_id}:{operation}:{date})
est UNIQUE en base, donc 2 enqueues identiques ne produisent qu'un job.

Multi-workers safe : SELECT ... FOR UPDATE SKIP LOCKED permet a
plusieurs workers de tourner en parallele sans traiter le meme job.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from src.config import get_settings
from src.db import get_connection

if TYPE_CHECKING:
    from src.gmail_client import GmailClient
    from src.observer import CircuitBreaker

logger = logging.getLogger(__name__)


# === Constantes ===

MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 5
QUEUE_POLL_SLEEP = 2  # secondes quand la queue est vide
LABEL_ID_REQUIRED = "IA-Review"  # nom (l'ID reel est dans gmail_labels)


# Mapping operation -> label Gmail
OPERATION_TO_LABELS: dict[str, dict[str, list[str]]] = {
    "mark_read": {"remove": ["UNREAD"]},
    "archive": {"remove": ["INBOX"]},
    "star": {"add": ["STARRED"]},
    "unstar": {"remove": ["STARRED"]},
    "move_ia_review": {"add_ia_review": True},  # special : ID dynamique
    "none": {},
}


# === Erreurs ===

class ActionWorkerError(Exception):
    """Erreur de base de l'ActionWorker."""


class QuotaPausedError(ActionWorkerError):
    """Le circuit-breaker a pause le worker."""


# === ActionWorker ===

class ActionWorker:
    """Worker qui consomme action_queue et execute les actions Gmail."""

    def __init__(
        self,
        gmail_client: Optional["GmailClient"] = None,
        circuit_breaker: Optional["CircuitBreaker"] = None,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self._gmail_client = gmail_client
        self._circuit_breaker = circuit_breaker
        self.max_attempts = max_attempts
        self._ia_review_label_id: Optional[str] = None
        self._stop_requested = False

    @property
    def gmail_client(self) -> "GmailClient":
        if self._gmail_client is None:
            from src.gmail_client import GmailClient
            self._gmail_client = GmailClient()
        return self._gmail_client

    @property
    def circuit_breaker(self) -> "CircuitBreaker":
        if self._circuit_breaker is None:
            from src.observer import CircuitBreaker
            self._circuit_breaker = CircuitBreaker()
        return self._circuit_breaker

    # ----------------------------------------------------------------
    # Enqueue (utilise par recommender.decider.auto_execute, etc.)
    # ----------------------------------------------------------------
    @staticmethod
    def make_idempotency_key(email_id: str, operation: str,
                              when: Optional[datetime] = None) -> str:
        """Construit la cle d'idempotence.

        Format : {email_id}:{operation}:{YYYY-MM-DD}
        Meme email + meme operation + meme jour = meme cle = pas de doublon.
        """
        when = when or datetime.now(timezone.utc)
        return f"{email_id}:{operation}:{when.strftime('%Y-%m-%d')}"

    def enqueue_action(self, email_id: str, operation: str) -> int:
        """Ajoute une action a la queue. Retourne l'ID de l'item insere.

        Si (email_id, operation, day) existe deja, retourne l'ID existant
        (idempotent grace a ON CONFLICT DO NOTHING).
        """
        if operation not in OPERATION_TO_LABELS:
            raise ValueError(f"unknown operation: {operation}")
        if operation == "none":
            return 0  # rien a faire

        idem_key = self.make_idempotency_key(email_id, operation)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO action_queue (
                        email_id, operation, status, idempotency_key, created_at
                    ) VALUES (%s, %s, 'pending', %s, NOW())
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING id
                    """,
                    (email_id, operation, idem_key),
                )
                row = cur.fetchone()
                if row is None:
                    # Conflit : retrouver l'ID existant
                    cur.execute(
                        "SELECT id FROM action_queue WHERE idempotency_key = %s",
                        (idem_key,),
                    )
                    row = cur.fetchone()
                item_id = row[0] if row else 0
            conn.commit()
        logger.info("enqueued action: email=%s op=%s id=%d", email_id, operation, item_id)
        return item_id

    # ----------------------------------------------------------------
    # Boucle principale
    # ----------------------------------------------------------------
    def request_stop(self) -> None:
        """Demande au worker de s'arreter apres le job en cours."""
        self._stop_requested = True

    def run(self, *, max_iterations: Optional[int] = None) -> int:
        """Boucle principale. Bloque jusqu'a request_stop() ou max_iterations.

        Returns: nombre de jobs traites.
        """
        processed = 0
        iteration = 0
        while not self._stop_requested:
            if max_iterations and iteration >= max_iterations:
                break
            iteration += 1
            try:
                job = self._claim_next_job()
                if job is None:
                    time.sleep(QUEUE_POLL_SLEEP)
                    continue
                self._process_job(job)
                processed += 1
            except QuotaPausedError as e:
                logger.warning("worker paused (quota): %s", e)
                time.sleep(60)  # attendre 1 min et reessayer
            except Exception as e:
                logger.exception("worker iteration error: %s", e)
                time.sleep(QUEUE_POLL_SLEEP)
        return processed

    # ----------------------------------------------------------------
    # Gestion d'un job
    # ----------------------------------------------------------------
    def _claim_next_job(self) -> Optional[dict[str, Any]]:
        """Prend le prochain job pending (FOR UPDATE SKIP LOCKED).

        Returns: dict avec les infos du job, ou None si la queue est vide.
        """
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, email_id, operation, idempotency_key, attempts
                    FROM action_queue
                    WHERE status = 'pending'
                    ORDER BY created_at ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """
                )
                row = cur.fetchone()
                if row is None:
                    return None
                cols = [d[0] for d in cur.description]
                job = dict(zip(cols, row))
                # Marquer en cours
                cur.execute(
                    "UPDATE action_queue SET status = 'executing', attempts = attempts + 1 "
                    "WHERE id = %s",
                    (job["id"],),
                )
            conn.commit()
        return job

    def _process_job(self, job: dict[str, Any]) -> None:
        """Execute un job : appel Gmail + mise a jour statut."""
        job_id = job["id"]
        email_id = job["email_id"]
        operation = job["operation"]

        # 1. Verifier le circuit-breaker
        try:
            self.circuit_breaker.can_proceed()
        except Exception as e:
            # Remettre en pending pour retry plus tard
            self._mark_pending(job_id, str(e))
            raise QuotaPausedError(str(e)) from e

        # 2. Executer l'action
        try:
            self._execute_action(email_id, operation)
            self._mark_done(job_id)
            self.circuit_breaker.register_call("messages.modify")
            logger.info("job %d done: email=%s op=%s", job_id, email_id, operation)
        except Exception as e:
            logger.warning("job %d failed: email=%s op=%s err=%s",
                          job_id, email_id, operation, e)
            self._mark_failed_or_retry(job_id, str(e), job.get("attempts", 0))

    def _execute_action(self, email_id: str, operation: str) -> None:
        """Execute l'action Gmail reelle via gmail_client."""
        labels = OPERATION_TO_LABELS.get(operation, {})
        if not labels:
            logger.debug("no-op action %s, skipping", operation)
            return

        # Cas special : move_ia_review necessite l'ID reel du label
        if labels.get("add_ia_review"):
            label_id = self._get_ia_review_label_id()
            self.gmail_client.modify_labels(
                email_id, add=[label_id],
            )
            return

        # Cas general
        self.gmail_client.modify_labels(
            email_id,
            add=labels.get("add"),
            remove=labels.get("remove"),
        )

    def _get_ia_review_label_id(self) -> str:
        """Recupere (et cache) l'ID du label IA-Review depuis gmail_labels."""
        if self._ia_review_label_id:
            return self._ia_review_label_id

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT label_id FROM gmail_labels "
                    "WHERE label_name = 'IA-Review' LIMIT 1"
                )
                row = cur.fetchone()
        if row is None:
            # Pas encore connu, on demande a l'observer de le creer
            from src.observer import GmailObserver
            obs = GmailObserver(gmail_client=self.gmail_client)
            label_id = obs.ensure_ia_review_label()
            self._ia_review_label_id = label_id
            return label_id
        self._ia_review_label_id = row[0]
        return self._ia_review_label_id

    # ----------------------------------------------------------------
    # Mise a jour des statuts
    # ----------------------------------------------------------------
    def _mark_done(self, job_id: int) -> None:
        """Marque un job comme 'done' et met a jour decision_journal."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE action_queue SET status = 'done', executed_at = NOW() "
                    "WHERE id = %s RETURNING email_id, operation",
                    (job_id,),
                )
                row = cur.fetchone()
                if row:
                    # Mettre a jour decision_journal
                    cur.execute(
                        "UPDATE decision_journal SET execution_status = 'success', "
                        "executed_at = NOW() "
                        "WHERE email_id = %s AND executable_operation = %s "
                        "AND execution_status = 'pending'",
                        (row[0], row[1]),
                    )
            conn.commit()

    def _mark_failed_or_retry(
        self, job_id: int, error: str, attempts: int,
    ) -> None:
        """Si attempts < max, remet en pending. Sinon, marque 'failed'."""
        new_attempts = attempts  # deja incremente dans _claim_next_job
        if new_attempts < self.max_attempts:
            # Retry : remettre en pending apres un backoff
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE action_queue SET status = 'pending', last_error = %s "
                        "WHERE id = %s",
                        (error[:500], job_id),
                    )
            conn.commit()
            logger.info("job %d will retry (attempts=%d/%d)",
                       job_id, new_attempts, self.max_attempts)
            time.sleep(RETRY_BACKOFF_SECONDS)
        else:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE action_queue SET status = 'failed', last_error = %s "
                        "WHERE id = %s RETURNING email_id, operation",
                        (error[:500], job_id),
                    )
                    row = cur.fetchone()
                    if row:
                        cur.execute(
                            "UPDATE decision_journal SET execution_status = 'failed', "
                            "gmail_error = %s WHERE email_id = %s AND executable_operation = %s",
                            (error[:500], row[0], row[1]),
                        )
            conn.commit()
            logger.error("job %d permanently failed after %d attempts",
                        job_id, new_attempts)

    def _mark_pending(self, job_id: int, reason: str) -> None:
        """Remet un job en pending (utilise quand le circuit-breaker pause)."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE action_queue SET status = 'pending', last_error = %s "
                    "WHERE id = %s",
                    (reason[:500], job_id),
                )
            conn.commit()

    # ----------------------------------------------------------------
    # Stats
    # ----------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        """Stats pour le dashboard : nombre de jobs par statut."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, COUNT(*) FROM action_queue GROUP BY status"
                )
                by_status = dict(cur.fetchall())
                cur.execute(
                    "SELECT COUNT(*) FROM action_queue WHERE last_error IS NOT NULL"
                )
                with_errors = cur.fetchone()[0]
        return {
            "by_status": by_status,
            "with_errors": with_errors,
            "max_attempts": self.max_attempts,
        }
