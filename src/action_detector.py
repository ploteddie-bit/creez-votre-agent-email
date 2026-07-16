"""ActionDetector — détection des actions utilisateur par delta de labels.

Livrable de l'agent 4 (parser-sanitizer) du PLAN DE TRAVAIL.

Principe (phase P0, apprentissage passif) : l'observer compare les
`labelIds` Gmail d'un même email entre deux synchronisations. Chaque
transition est une **action de l'utilisateur** — c'est la matière
première de l'apprentissage (le système apprend CE que l'humain fait,
sans jamais agir lui-même en P0).

Règles de détection (SPEC agent 4) :
- `INBOX` retiré, pas dans `TRASH`   → `archived`
- `INBOX` retiré vers `TRASH`        → `deleted`
- `TRASH` ajouté (hors INBOX aussi)  → `deleted`
- `UNREAD` retiré                    → `read`
- `UNREAD` ajouté                    → `unread`
- `STARRED` ajouté                   → `starred`
- `STARRED` retiré                   → `unstarred`
- ancien état vide + `INBOX` présent → `new_mail`

Les actions sont stockées dans `email_actions` (jamais effacées).
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from src.db import get_connection

logger = logging.getLogger(__name__)


class ActionDetector:
    """Détecte et journalise les actions utilisateur sur les emails."""

    # ----------------------------------------------------------------
    # Détection (fonction pure, sans I/O — facilement testable)
    # ----------------------------------------------------------------
    @staticmethod
    def detect_actions(
        old_labels: Iterable[str] | None,
        new_labels: Iterable[str] | None,
    ) -> list[str]:
        """Compare deux états de labels et retourne les actions détectées.

        Args:
            old_labels: labelIds au sync précédent (None/[] = mail inconnu).
            new_labels: labelIds au sync courant.

        Returns:
            Liste ordonnée d'actions (ex: ["read", "archived"]).
            Liste vide si aucune transition significative.
        """
        old = set(old_labels or [])
        new = set(new_labels or [])
        actions: list[str] = []

        # Suppression (INBOX → TRASH, ou TRASH ajouté à un mail archivé)
        if "TRASH" in new and "TRASH" not in old:
            actions.append("deleted")
        # Archivage : INBOX retiré sans passage à la corbeille
        elif "INBOX" in old and "INBOX" not in new:
            actions.append("archived")

        # Lecture
        if "UNREAD" in old and "UNREAD" not in new:
            actions.append("read")
        elif "UNREAD" not in old and "UNREAD" in new and old:
            # Marqué non-lu (pas pour un mail nouveau)
            actions.append("unread")

        # Suivi (étoile)
        if "STARRED" in new and "STARRED" not in old:
            actions.append("starred")
        elif "STARRED" in old and "STARRED" not in new:
            actions.append("unstarred")

        # Nouveau mail (première observation)
        if not old and "INBOX" in new:
            actions.append("new_mail")

        return actions

    # ----------------------------------------------------------------
    # Stockage
    # ----------------------------------------------------------------
    def store_actions(
        self,
        email_id: str,
        actions: list[str],
        *,
        detected_by: str = "poll_delta",
    ) -> int:
        """Insère les actions dans `email_actions`. Retourne le nombre inséré."""
        if not actions:
            return 0
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO email_actions (email_id, action, detected_by)"
                    " VALUES (%s, %s, %s)",
                    [(email_id, action, detected_by) for action in actions],
                )
            conn.commit()
        logger.info("email %s: %d action(s) détectée(s) → %s", email_id, len(actions), actions)
        return len(actions)

    # ----------------------------------------------------------------
    # Pipeline complet
    # ----------------------------------------------------------------
    def detect_and_store(
        self,
        email_id: str,
        old_labels: Iterable[str] | None,
        new_labels: Iterable[str] | None,
        *,
        detected_by: str = "poll_delta",
    ) -> list[str]:
        """Détecte puis stocke. Retourne les actions détectées."""
        actions = self.detect_actions(old_labels, new_labels)
        if actions:
            self.store_actions(email_id, actions, detected_by=detected_by)
        return actions
