"""Decider - moteur de decision autonome P2 avec tous les garde-fous.

Le Decider decide si une decision (produite par le Recommender) doit
etre executee en autonomie (P2) ou passee en revue humaine (P1).

Conditions pour auto-executer (toutes doivent etre vraies) :
  1. p2_enabled == True (kill-switch OFF -> ON)
  2. vacation_mode == False
  3. sender_domain est "connu" (deja vu)
  4. llm_confidence >= 0.3
  5. |llm_conf - heuristic_conf| <= 0.3
  6. Pas de mot-cle critique
  7. Quota quotidien pas atteint (max_daily_actions)
  8. Precision sur la fenetre glissante >= seuil specifique a l'action

Si 3 corrections consecutives sur la meme action, l'action est
temporairement desactivee (rollback auto).

Par defaut p2_enabled = FALSE -> le systeme est en P0/P1 par securite.
Aucun acte irreversible ne peut etre effectue tant que le kill-switch
n'est pas explicitement ON via le dashboard.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from src.config import get_settings
from src.db import get_connection
from src.models import MailDecision

if TYPE_CHECKING:
    from src.action_worker import ActionWorker
    from src.recommender import Recommender

logger = logging.getLogger(__name__)


# === Erreurs ===

class DeciderError(Exception):
    """Erreur de base du Decider."""


class GuardrailTriggered(DeciderError):
    """Un garde-fou a empeche l'execution (info, pas forcement erreur)."""


# === Decider ===

class Decider:
    """Moteur de decision P2 avec garde-fous multiples."""

    def __init__(
        self,
        recommender: Optional["Recommender"] = None,
        action_worker: Optional["ActionWorker"] = None,
        *,
        p2_enabled: Optional[bool] = None,
        vacation_mode: Optional[bool] = None,
    ) -> None:
        settings = get_settings()

        # Lecture depuis settings (avec override possible pour les tests)
        self.p2_enabled: bool = (
            p2_enabled if p2_enabled is not None else settings.p2.enabled
        )
        self.vacation_mode: bool = (
            vacation_mode if vacation_mode is not None else settings.p2.vacation_mode
        )
        self.precision_thresholds: dict[str, float] = dict(
            settings.p2.precision_thresholds
        )
        self.max_daily_actions: int = settings.p2.max_daily_actions
        self.window_size: int = settings.p2.window_size

        # Compteurs de corrections consecutives (par action)
        # In-memory seulement, reset au redemarrage
        self._consecutive_rejections: dict[str, int] = defaultdict(int)

        # Lazy imports
        self._recommender = recommender
        self._action_worker = action_worker

    @property
    def recommender(self) -> "Recommender":
        if self._recommender is None:
            from src.recommender import Recommender
            self._recommender = Recommender()
        return self._recommender

    @property
    def action_worker(self) -> "ActionWorker":
        if self._action_worker is None:
            from src.action_worker import ActionWorker
            self._action_worker = ActionWorker()
        return self._action_worker

    # ----------------------------------------------------------------
    # should_auto_execute : le coeur du P2
    # ----------------------------------------------------------------
    def should_auto_execute(
        self,
        mail_decision: MailDecision,
        email: dict,
    ) -> bool:
        """Decide si une decision peut etre executee en autonomie.

        Toutes les conditions doivent etre satisfaites. La moindre
        precaution ratee -> False (P1 par defaut).
        """
        # 1. Kill-switch global
        if not self.p2_enabled:
            logger.debug("P2 disabled (kill-switch), forcing P1")
            return False

        # 2. Mode Vacances
        if self.vacation_mode:
            logger.debug("vacation mode, forcing P1")
            return False

        # 3. Sender inconnu -> prudence
        if not self._is_known_sender(email.get("sender_domain", "")):
            logger.info("unknown sender domain %s, forcing P1",
                       email.get("sender_domain"))
            return False

        # 4. LLM confiance trop basse
        if mail_decision.confidence < 0.3:
            logger.debug("LLM confidence %.2f < 0.3, forcing P1",
                        mail_decision.confidence)
            return False

        # 5. Divergence LLM/heuristique trop grande
        # (on essaie de recuperer la confiance hybride stockee)
        heuristic_conf = self._get_recent_heuristic_confidence(email.get("id", ""))
        if heuristic_conf is not None:
            divergence = abs(mail_decision.confidence - heuristic_conf)
            if divergence > 0.3:
                logger.info("divergence %.2f > 0.3, forcing P1", divergence)
                return False

        # 6. Mots-cles critiques -> JAMAIS auto-archiver
        subject = (email.get("subject") or "").lower()
        body = (email.get("body_snippet") or email.get("body_text") or "").lower()
        if self._contains_critical_keywords(f"{subject} {body}"):
            logger.warning("CRITICAL keyword detected, forcing P1 for safety")
            return False

        # 7. Quota quotidien
        if self._today_actions_count() >= self.max_daily_actions:
            logger.info("daily quota reached (%d), forcing P1",
                       self.max_daily_actions)
            return False

        # 8. Precision sur la fenetre glissante
        action = mail_decision.executable_operation
        if action == "none":
            return False  # rien a executer
        threshold = self.precision_thresholds.get(action, 0.95)
        precision = self.get_window_precision(action)
        if precision < threshold:
            logger.info("precision %.2f < threshold %.2f for %s, forcing P1",
                       precision, threshold, action)
            return False

        # 9. Action temporairement desactivee (3 corrections consecutives)
        if self._consecutive_rejections.get(action, 0) >= 3:
            logger.warning(
                "action %s temporarily disabled (3+ consecutive rejections)",
                action,
            )
            return False

        return True

    # ----------------------------------------------------------------
    # auto_execute : l'action qui enqueue
    # ----------------------------------------------------------------
    def auto_execute(
        self,
        email_id: str,
        mail_decision: MailDecision,
        email: Optional[dict] = None,
    ) -> Optional[int]:
        """Enqueue l'action si should_auto_execute() retourne True.

        Returns: l'ID de l'item action_queue, ou None si pas execute.
        """
        if not self.should_auto_execute(mail_decision, email or {"id": email_id}):
            logger.info("not auto-executing %s: should_auto_execute=False", email_id)
            return None

        if mail_decision.executable_operation == "none":
            return None

        # 1. Marquer la decision comme 'pending execution'
        self._mark_decision_pending(email_id, mail_decision)

        # 2. Enqueue l'action
        item_id = self.action_worker.enqueue_action(
            email_id=email_id,
            operation=mail_decision.executable_operation,
        )
        logger.info(
            "auto-executed: email=%s op=%s queue_id=%d",
            email_id, mail_decision.executable_operation, item_id,
        )
        return item_id

    # ----------------------------------------------------------------
    # Precision sur la fenetre glissante
    # ----------------------------------------------------------------
    def get_window_precision(self, action_type: str) -> float:
        """Precision mesuree sur les N dernieres decisions pour cette action.

        precision = approved / (approved + rejected)
        Retourne 1.0 si pas encore de data (ne pas penaliser le cold start).
        """
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE user_approved = TRUE) AS approved,
                        COUNT(*) FILTER (WHERE user_approved = FALSE) AS rejected,
                        COUNT(*) AS total
                    FROM (
                        SELECT user_approved
                        FROM decision_journal
                        WHERE executable_operation = %s
                          AND phase = 'P2'
                        ORDER BY created_at DESC
                        LIMIT %s
                    ) recent
                    """,
                    (action_type, self.window_size),
                )
                row = cur.fetchone()
                if row is None or len(row) < 3 or row[2] == 0:
                    return 1.0  # cold start : on laisse passer
                approved, rejected, total = row[0], row[1], row[2]
                if (approved + rejected) == 0:
                    return 1.0
                return approved / (approved + rejected)

    def get_window_stats(self) -> dict[str, dict[str, Any]]:
        """Stats par action sur la fenetre glissante (pour le dashboard)."""
        stats: dict[str, dict[str, Any]] = {}
        for action in self.precision_thresholds.keys():
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT
                            COUNT(*) FILTER (WHERE user_approved = TRUE) AS approved,
                            COUNT(*) FILTER (WHERE user_approved = FALSE) AS rejected,
                            COUNT(*) FILTER (WHERE user_approved IS NULL) AS pending
                        FROM (
                            SELECT user_approved
                            FROM decision_journal
                            WHERE executable_operation = %s
                              AND phase = 'P2'
                            ORDER BY created_at DESC
                            LIMIT %s
                        ) recent
                        """,
                        (action, self.window_size),
                    )
                    row = cur.fetchone()
                    if row is None or len(row) < 3:
                        # Cold start : pas encore de donnees pour cette action
                        approved, rejected, pending = 0, 0, 0
                    else:
                        approved, rejected, pending = row[0], row[1], row[2]
                    total = approved + rejected
                    precision = (approved / total) if total > 0 else 1.0
                    threshold = self.precision_thresholds.get(action, 0.95)
                    stats[action] = {
                        "precision": round(precision, 3),
                        "threshold": threshold,
                        "approved": approved,
                        "rejected": rejected,
                        "pending": pending,
                        "above_threshold": precision >= threshold,
                        "consecutive_rejections": self._consecutive_rejections.get(action, 0),
                    }
        return stats

    # ----------------------------------------------------------------
    # Tracking des corrections utilisateur
    # ----------------------------------------------------------------
    def record_user_correction(
        self, email_id: str, action_type: str, was_correct: bool,
    ) -> None:
        """Appele par le dashboard quand l'utilisateur approuve/rejette une P2.

        - was_correct=True : reset le compteur
        - was_correct=False : increment, desactive l'action si >= 3
        """
        if was_correct:
            self._consecutive_rejections[action_type] = 0
            return

        self._consecutive_rejections[action_type] += 1
        if self._consecutive_rejections[action_type] >= 3:
            logger.warning(
                "ACTION %s TEMPORARILY DISABLED (3 consecutive rejections)",
                action_type,
            )
            # En production, on pourrait alerter le dashboard / desactiver via DB

    # ----------------------------------------------------------------
    # Helpers prives
    # ----------------------------------------------------------------
    def _is_known_sender(self, domain: str) -> bool:
        """Un domaine est 'connu' si on a deja ingere >= 5 emails de ce domaine."""
        if not domain:
            return False
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM emails WHERE sender_domain = %s",
                    (domain,),
                )
                count = cur.fetchone()[0]
        return count >= 5

    def _contains_critical_keywords(self, text: str) -> bool:
        """Verifie la presence de mots-cles critiques dans le mail."""
        from src.rules_engine import RulesEngine
        engine = RulesEngine()
        return bool(engine.contains_critical_keywords(text))

    def _today_actions_count(self) -> int:
        """Nombre d'actions executees aujourd'hui (toutes actions confondues)."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM action_queue
                    WHERE status = 'done'
                      AND executed_at >= CURRENT_DATE
                    """
                )
                return cur.fetchone()[0]

    def _get_recent_heuristic_confidence(self, email_id: str) -> Optional[float]:
        """Recupere la confiance heuristique stockee dans decision_journal."""
        if not email_id:
            return None
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT heuristic_confidence FROM decision_journal "
                    "WHERE email_id = %s ORDER BY created_at DESC LIMIT 1",
                    (email_id,),
                )
                row = cur.fetchone()
                return row[0] if row and row[0] is not None else None

    def _mark_decision_pending(
        self, email_id: str, mail_decision: MailDecision,
    ) -> None:
        """Marque la derniere decision comme 'execution pending'."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE decision_journal SET execution_status = 'pending' "
                    "WHERE email_id = %s ORDER BY created_at DESC LIMIT 1",
                    (email_id,),
                )
            conn.commit()

    # ----------------------------------------------------------------
    # Snapshot pour le dashboard
    # ----------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Etat complet du decider pour /api/health."""
        return {
            "p2_enabled": self.p2_enabled,
            "vacation_mode": self.vacation_mode,
            "max_daily_actions": self.max_daily_actions,
            "today_actions": self._today_actions_count(),
            "window_size": self.window_size,
            "precision_thresholds": self.precision_thresholds,
            "window_stats": self.get_window_stats(),
        }
