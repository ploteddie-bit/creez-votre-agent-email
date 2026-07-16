"""Observer Gmail - polling delta + circuit-breaker anti-quota.

Ce module est le **moteur de synchronisation** entre Gmail et la base
PostgreSQL. Il respecte la philosophie du projet :
- sync_full au premier lancement (6 derniers mois, sans spam/promotions)
- sync_delta ensuite via history.list (delta incrémental)
- circuit-breaker qui PAUSE l'observateur si on approche des quotas
- JAMAIS de suppression, JAMAIS d'appel aux méthodes interdites
- last_history_id mis à jour SEULEMENT après ingestion réussie

Composants :
  - CircuitBreaker : anti-quota, exposé au dashboard
  - GmailObserver : la machine à état de la sync
"""
from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from src.config import get_settings
from src.db import get_connection

logger = logging.getLogger(__name__)


# === Erreurs ===

class ObserverError(Exception):
    """Erreur de base de l'observateur."""


class QuotaExceeded(ObserverError):
    """Le circuit-breaker a déclenché une pause."""


class HistoryExpired(ObserverError):
    """L'historyId n'existe plus côté Gmail → full-resync requis."""


# ============================================================
# Circuit Breaker
# ============================================================

class CircuitBreaker:
    """Compteur de quota Gmail + détection de seuil.

    Le Gmail API a un quota quotidien d'"unités" :
      - history.list : 2000 unités
      - messages.get : 2000 unités
      - messages.modify : 2000 unités
      - watch : 2000 unités

    Au-delà de `quota_threshold_pct` (80% par défaut), on **pause**
    l'observateur. On respecte aussi une limite de messages/minute.

    Le circuit-breaker est **stateful** (en mémoire). Au redémarrage
    du daemon, les compteurs repartent à zéro — c'est un comportement
    acceptable : le quota Gmail réel reste la source de vérité, ce
    breaker est juste un garde-fou local.
    """

    def __init__(
        self,
        quota_costs: Optional[dict[str, int]] = None,
        quota_per_user_per_day: Optional[int] = None,
        quota_threshold_pct: Optional[float] = None,
        max_messages_per_minute: Optional[int] = None,
        pause_seconds: int = 600,  # 10 min par défaut
    ) -> None:
        settings = get_settings()
        self.quota_costs = quota_costs or settings.gmail.quota_costs
        self.quota_per_user_per_day = (
            quota_per_user_per_day or settings.gmail.quota_per_user_per_day
        )
        self.quota_threshold_pct = (
            quota_threshold_pct
            if quota_threshold_pct is not None
            else settings.gmail.quota_threshold_pct
        )
        self.max_messages_per_minute = (
            max_messages_per_minute
            if max_messages_per_minute is not None
            else settings.gmail.max_messages_per_minute
        )
        self.pause_seconds = pause_seconds

        # État
        self.quota_used_today: int = 0
        self.quota_window_start: datetime = datetime.now(timezone.utc)
        self.calls_per_minute: deque[float] = deque(maxlen=1000)
        self.retries: int = 0
        self.paused_until: Optional[datetime] = None

    # ----------------------------------------------------------------
    # API publique
    # ----------------------------------------------------------------
    def can_proceed(self) -> bool:
        """Retourne True si on peut faire un appel maintenant.

        Lève QuotaExceeded si on est en pause active.
        """
        if self.paused_until is not None:
            now = datetime.now(timezone.utc)
            if now < self.paused_until:
                remaining = (self.paused_until - now).total_seconds()
                raise QuotaExceeded(
                    f"Circuit-breaker paused for {remaining:.0f}s more "
                    f"(quota={self.quota_used_today}/{self.quota_per_user_per_day})"
                )
            # Pause expirée, on repart
            self.paused_until = None
        return True

    def register_call(self, method_name: str) -> None:
        """Enregistre un appel API et déclenche une pause si seuil atteint.

        Doit être appelé APRÈS chaque appel Gmail réussi (ou échoué
        contrôlément, mais avant qu'on sature le quota).
        """
        now = datetime.now(timezone.utc)

        # Reset quotidien si on a changé de jour
        if now.date() > self.quota_window_start.date():
            self.quota_used_today = 0
            self.quota_window_start = now

        # Coût de l'appel
        cost = self.quota_costs.get(method_name, 1)
        self.quota_used_today += cost

        # Tracking du rate
        self.calls_per_minute.append(time.time())

        # Pause si quota > seuil
        threshold = self.quota_per_user_per_day * self.quota_threshold_pct
        if self.quota_used_today > threshold:
            self._pause(f"quota {self.quota_used_today}/{self.quota_per_user_per_day}")

        # Pause si trop de messages/minute
        recent = [t for t in self.calls_per_minute if now.timestamp() - t < 60]
        if len(recent) > self.max_messages_per_minute:
            self._pause(f"rate {len(recent)}/min > {self.max_messages_per_minute}/min")

    def register_retry(self) -> None:
        """Incrémente le compteur de retries (pour le dashboard)."""
        self.retries += 1

    def _pause(self, reason: str) -> None:
        """Passe en mode pause pour `pause_seconds`."""
        self.paused_until = datetime.now(timezone.utc) + timedelta(
            seconds=self.pause_seconds
        )
        logger.warning(
            "circuit-breaker TRIPPED: %s — pause for %ds until %s",
            reason, self.pause_seconds, self.paused_until.isoformat(),
        )

    # ----------------------------------------------------------------
    # Snapshot pour le dashboard
    # ----------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """État du circuit-breaker pour `/api/health` ou le dashboard."""
        now = datetime.now(timezone.utc)
        recent = sum(1 for t in self.calls_per_minute if now.timestamp() - t < 60)
        return {
            "quota_used_today": self.quota_used_today,
            "quota_per_user_per_day": self.quota_per_user_per_day,
            "quota_threshold_pct": self.quota_threshold_pct,
            "quota_pct": round(
                100.0 * self.quota_used_today / max(1, self.quota_per_user_per_day), 2
            ),
            "calls_per_minute": recent,
            "max_messages_per_minute": self.max_messages_per_minute,
            "retries": self.retries,
            "paused": self.paused_until is not None and now < self.paused_until,
            "paused_until": self.paused_until.isoformat() if self.paused_until else None,
        }


# ============================================================
# Gmail Observer
# ============================================================

# Label créé au premier lancement pour le soft-delete IA-Review
IA_REVIEW_LABEL_NAME = "IA-Review"
IA_REVIEW_LABEL_COLOR = "#3E3AE6"  # même accent que le cours


class GmailObserver:
    """Synchronise Gmail vers PostgreSQL via historyId + circuit-breaker.

    États possibles :
      - Pas de sync_state → premier lancement, faire un sync_full
      - sync_state.last_history_id existe → sync_delta
      - sync_delta → 404 sur historyId → HistoryExpired → sync_full
    """

    def __init__(
        self,
        gmail_client: Any = None,  # src.gmail_client.GmailClient (lazy pour tests)
        ingester: Any = None,      # src.ingester.EmailIngester
        account_id: str = "me",
        circuit_breaker: Optional[CircuitBreaker] = None,
    ) -> None:
        self.account_id = account_id
        self._gmail_client = gmail_client
        self._ingester = ingester
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self._ia_review_label_id: Optional[str] = None

    # ----------------------------------------------------------------
    # Accès au client (lazy pour les tests)
    # ----------------------------------------------------------------
    @property
    def gmail_client(self) -> Any:
        if self._gmail_client is None:
            from src.gmail_client import GmailClient
            self._gmail_client = GmailClient()
        return self._gmail_client

    @property
    def ingester(self) -> Any:
        if self._ingester is None:
            from src.ingester import EmailIngester
            self._ingester = EmailIngester()
        return self._ingester

    # ----------------------------------------------------------------
    # Sync state
    # ----------------------------------------------------------------
    def get_sync_state(self) -> Optional[dict[str, Any]]:
        """Lit la table sync_state. Retourne None si pas encore initialisé."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT account_id, last_history_id, last_full_sync_at, "
                    "last_success_at, last_error, updated_at "
                    "FROM sync_state WHERE account_id = %s",
                    (self.account_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))

    def update_sync_state(
        self,
        last_history_id: Optional[str] = None,
        last_error: Optional[str] = None,
        mark_full_sync: bool = False,
        mark_success: bool = True,
    ) -> None:
        """Met à jour sync_state.

        Important : last_history_id n'est mis à jour QUE si
        mark_success=True. Si on a une erreur, on stocke l'erreur
        mais on NE BOUGE PAS l'historyId (sinon on perdrait des
        messages en cas de reprise).
        """
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sync_state (
                        account_id, last_history_id,
                        last_full_sync_at, last_success_at, last_error, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (account_id) DO UPDATE SET
                        last_history_id = COALESCE(
                            EXCLUDED.last_history_id,
                            sync_state.last_history_id
                        ),
                        last_full_sync_at = COALESCE(
                            EXCLUDED.last_full_sync_at,
                            sync_state.last_full_sync_at
                        ),
                        last_success_at = CASE
                            WHEN %s THEN EXCLUDED.last_success_at
                            ELSE sync_state.last_success_at
                        END,
                        last_error = EXCLUDED.last_error,
                        updated_at = NOW()
                    """,
                    (
                        self.account_id,
                        last_history_id if mark_success else None,
                        datetime.now(timezone.utc) if mark_full_sync else None,
                        datetime.now(timezone.utc) if mark_success else None,
                        last_error,
                        mark_success,
                    ),
                )
            conn.commit()

    # ----------------------------------------------------------------
    # Labels Gmail
    # ----------------------------------------------------------------
    def ensure_ia_review_label(self) -> str:
        """S'assure que le label `IA-Review` existe.

        Crée le label s'il n'existe pas, stocke son ID dans
        `gmail_labels` pour tous les modify ultérieurs.

        Returns: label_id réel (jamais hardcodé)
        """
        if self._ia_review_label_id:
            return self._ia_review_label_id

        # 1. Lister les labels existants
        labels = self.gmail_client.list_labels()
        existing = {lbl["name"]: lbl["id"] for lbl in labels}

        # 2. Si IA-Review existe déjà, utiliser son ID
        if IA_REVIEW_LABEL_NAME in existing:
            self._ia_review_label_id = existing[IA_REVIEW_LABEL_NAME]
        else:
            # 3. Sinon, créer le label
            self.gmail_client.validate_call("users.labels.create")
            service = self.gmail_client._get_service()
            result = service.users().labels().create(
                userId="me",
                body={
                    "name": IA_REVIEW_LABEL_NAME,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                    "color": {"backgroundColor": IA_REVIEW_LABEL_COLOR, "textColor": "#ffffff"},
                },
            ).execute()
            self._ia_review_label_id = result["id"]
            logger.info("created IA-Review label: %s", self._ia_review_label_id)

        # 4. Stocker dans gmail_labels (PostgreSQL) pour usage futur
        self._store_label(self._ia_review_label_id, IA_REVIEW_LABEL_NAME, "user")
        return self._ia_review_label_id

    def _store_label(self, label_id: str, label_name: str, type_: str) -> None:
        """Upsert un label dans gmail_labels (jamais hardcodé en mémoire)."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO gmail_labels (account_id, label_id, label_name, type, created_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (account_id, label_id) DO UPDATE
                    SET label_name = EXCLUDED.label_name
                    """,
                    (self.account_id, label_id, label_name, type_),
                )
            conn.commit()

    def get_label_id(self, name: str) -> Optional[str]:
        """Récupère un label_id depuis gmail_labels par son nom.

        Retourne None si le label n'a jamais été vu.
        """
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT label_id FROM gmail_labels "
                    "WHERE account_id = %s AND label_name = %s",
                    (self.account_id, name),
                )
                row = cur.fetchone()
                return row[0] if row else None

    # ----------------------------------------------------------------
    # Sync full (premier lancement ou fallback)
    # ----------------------------------------------------------------
    def sync_full(self, max_results: int = 2000,
                  query: str = "newer_than:6m -label:spam -label:promotions"
                  ) -> int:
        """Synchronisation complète (6 mois par défaut au 1er lancement).

        Consomme TOUS les `nextPageToken` jusqu'à exhaustion de la
        pagination. A chaque page, ingère les messages, marque le
        progress dans sync_state.

        Returns: nombre d'emails ingérés
        """
        logger.info("starting sync_full (max_results=%d, query=%s)", max_results, query)
        self.circuit_breaker.can_proceed()

        ingested = 0
        page_token: Optional[str] = None
        page_count = 0
        safety_max_pages = 100  # garde-fou anti-boucle infinie

        while page_count < safety_max_pages:
            self.circuit_breaker.can_proceed()
            self.circuit_breaker.register_call("messages.list")
            page_count += 1

            # Appel API avec pagination
            messages, next_token = self.gmail_client.list_messages(
                query=query, max_results=max_results, page_token=page_token,
            )

            logger.info("sync_full page %d: %d messages (next_token=%s)",
                       page_count, len(messages),
                       "yes" if next_token else "no")

            # Ingère chaque message
            for msg in messages:
                if self._ingest_one(msg["id"]):
                    ingested += 1

            # Pagination : continuer si nextPageToken
            if not next_token:
                break
            page_token = next_token

        if page_count >= safety_max_pages:
            logger.warning("sync_full stopped at safety_max_pages=%d, "
                          "il y a probablement plus de pages", safety_max_pages)

        # Marquer dans sync_state
        self.update_sync_state(
            last_history_id=None,  # sera mis après le premier sync_delta
            mark_full_sync=True,
            mark_success=True,
        )
        logger.info("sync_full done: %d emails ingested across %d pages",
                   ingested, page_count)
        return ingested

    # ----------------------------------------------------------------
    # Sync delta (via historyId)
    # ----------------------------------------------------------------
    def sync_delta(self) -> int:
        """Synchronisation delta via history.list.

        Stratégie :
          1. Lire last_history_id depuis sync_state
          2. Si absent → bascule sur sync_full
          3. Appeler history.list(startHistoryId=...)
          4. Si Gmail retourne 404 → HistoryExpired → sync_full
          5. Ingérer tous les messages référencés
          6. Mettre à jour last_history_id (seulement si succès)

        Returns: nombre d'emails traités
        """
        state = self.get_sync_state()
        if not state or not state.get("last_history_id"):
            logger.info("no last_history_id, falling back to sync_full")
            return self.sync_full()

        last_history_id = state["last_history_id"]
        logger.info("starting sync_delta from historyId=%s", last_history_id)
        self.circuit_breaker.can_proceed()

        try:
            history = self.gmail_client.list_history(last_history_id)
        except Exception as e:
            # 404 sur startHistoryId → full resync
            if "404" in str(e) or "notFound" in str(e):
                logger.warning("historyId %s expired, full resync required", last_history_id)
                self.update_sync_state(last_error=f"history_expired: {e}")
                raise HistoryExpired(str(e)) from e
            raise

        self.circuit_breaker.register_call("history.list")

        ingested = 0
        max_history_id = last_history_id

        for record in history:
            for msg in record.get("messages", []):
                if self._ingest_one(msg["id"]):
                    ingested += 1
            # Garder le plus gros historyId pour reprise
            if int(record.get("id", 0)) > int(max_history_id):
                max_history_id = str(record["id"])

        # Mise à jour du sync_state (succès)
        self.update_sync_state(
            last_history_id=max_history_id,
            mark_success=True,
        )
        logger.info("sync_delta done: %d messages, new historyId=%s",
                    ingested, max_history_id)
        return ingested

    # ----------------------------------------------------------------
    # Ingestion d'un message individuel
    # ----------------------------------------------------------------
    def _ingest_one(self, msg_id: str) -> bool:
        """Récupère un message, le parse, l'ingère. Retourne True si succès."""
        self.circuit_breaker.can_proceed()
        self.circuit_breaker.register_call("messages.get")
        try:
            raw = self.gmail_client.get_message(msg_id, format="full")
        except Exception as e:
            logger.warning("failed to get message %s: %s", msg_id, e)
            self.circuit_breaker.register_retry()
            return False

        # Lazy imports pour éviter les cycles
        from src.parser import parse_raw_message
        from src.models import EmailInDB

        try:
            parsed = parse_raw_message(raw)
            email = EmailInDB(**parsed)
            return self.ingester.ingest_email(email)
        except Exception as e:
            logger.warning("failed to ingest message %s: %s", msg_id, e)
            return False

    # ----------------------------------------------------------------
    # Health check
    # ----------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        """État de l'observer pour le dashboard."""
        state = self.get_sync_state()
        result = {
            "circuit_breaker": self.circuit_breaker.snapshot(),
            "sync_state": None,
        }
        if state:
            now = datetime.now(timezone.utc)
            last_success = state.get("last_success_at")
            age = None
            if last_success:
                if last_success.tzinfo is None:
                    last_success = last_success.replace(tzinfo=timezone.utc)
                age = (now - last_success).total_seconds()
            result["sync_state"] = {
                "account_id": state.get("account_id"),
                "last_history_id": state.get("last_history_id"),
                "last_full_sync_at": (
                    state["last_full_sync_at"].isoformat()
                    if state.get("last_full_sync_at") else None
                ),
                "last_success_at": (
                    last_success.isoformat() if last_success else None
                ),
                "last_success_age_seconds": age,
                "last_error": state.get("last_error"),
            }
        return result
