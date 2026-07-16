"""Wrapper Gmail API avec allowlist stricte d'interdictions.

Ce module est le **seul point de接触 avec l'API Gmail**. Toutes
les autres parties du code doivent passer par lui. Sa raison d'être :
rendre **impossible** (et non pas seulement improbable) l'appel aux
méthodes interdites (`messages().delete`, `messages().send`, etc.).

Mécanisme de défense :
  1. Allowlist explicite des méthodes autorisées
  2. Blocage runtime via `validate_call()` qui lève une exception
  3. Scope OAuth minimal : `gmail.modify` (pas `gmail.compose`)
  4. Tests automatisés qui scannent le code pour s'assurer qu'aucune
     méthode interdite n'est appelée (voir `tests/test_gmail_client.py`)

Usage :
    from src.gmail_client import GmailClient
    client = GmailClient()
    client.validate_call("users.messages.list")   # OK
    client.validate_call("users.messages.delete") # raises GmailForbiddenCall
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from src.config import get_settings

logger = logging.getLogger(__name__)


# === Erreurs ===

class GmailClientError(Exception):
    """Erreur de base du client Gmail."""


class GmailAuthError(GmailClientError):
    """Échec d'authentification OAuth2."""


class GmailForbiddenCall(GmailClientError):
    """Tentative d'appel à une méthode interdite.

    Cette exception est levée **avant tout appel réseau**.
    Elle ne doit jamais être catchée silencieusement.
    """


class GmailQuotaExceeded(GmailClientError):
    """Quota Gmail dépassé (trop d'unités consommées)."""


# === Méthodes autorisées / interdites ===

# Allowlist stricte : seuls ces préfixes de méthodes sont autorisés.
# Toute autre méthode lèvera GmailForbiddenCall.
ALLOWED_METHODS: frozenset[str] = frozenset({
    "users.messages.list",
    "users.messages.get",
    "users.messages.modify",
    "users.messages.batchModify",
    "users.threads.list",
    "users.threads.get",
    "users.threads.modify",
    "users.history.list",
    "users.labels.list",
    "users.labels.get",
    "users.labels.create",
    "users.labels.update",
    "users.labels.delete",
    "users.profile",
    "users.watch",
    "users.stop",
    "users.drafts.list",
    "users.drafts.get",
    # NOTE: délibérément absents :
    # - users.messages.delete (interdit)
    # - users.threads.delete (interdit)
    # - users.messages.send (interdit)
    # - users.drafts.send (interdit)
    # - users.drafts.create (interdit — créerait un brouillon envoyable)
    # - users.drafts.update (interdit — modifierait un brouillon)
    # - users.drafts.delete (interdit)
})


# === Client ===

class GmailClient:
    """Wrapper Gmail API avec allowlist stricte.

    Le `service` Google API n'est créé qu'à la demande (lazy), pour
    permettre les tests unitaires qui n'ont pas besoin d'OAuth.
    """

    def __init__(self, credentials_path: Optional[str] = None,
                 token_path: Optional[str] = None) -> None:
        settings = get_settings()
        self.scopes: list[str] = list(settings.gmail.oauth_scopes)
        self._credentials_path = credentials_path
        self._token_path = token_path
        self._service: Any | None = None  # googleapiclient.discovery.Resource

    # ----------------------------------------------------------------
    # Allowlist
    # ----------------------------------------------------------------
    def allowed_methods(self) -> frozenset[str]:
        """Retourne l'allowlist courante des méthodes autorisées."""
        return ALLOWED_METHODS

    def validate_call(self, method_name: str) -> None:
        """Vérifie qu'une méthode est autorisée. Lève sinon.

        Cette méthode est le **verrou de sécurité** : elle doit être
        appelée avant chaque appel Gmail. Ne JAMAIS la bypasser.
        """
        # Nettoyer les espaces et normaliser
        clean = re.sub(r"\s+", "", method_name)
        # Accepter avec ou sans parenthèses
        clean = clean.rstrip("()")

        if clean not in ALLOWED_METHODS:
            settings = get_settings()
            forbidden = settings.gmail.forbidden_methods
            logger.error(
                "🚫 FORBIDDEN Gmail API call attempted: %s "
                "(allowed: %d, forbidden list size: %d)",
                method_name, len(ALLOWED_METHODS), len(forbidden),
            )
            raise GmailForbiddenCall(
                f"Method '{method_name}' is not in the allowlist. "
                f"This is a hard security constraint. "
                f"See GmailClient.ALLOWED_METHODS."
            )

    # ----------------------------------------------------------------
    # Helpers haut-niveau (méthodes métier)
    # ----------------------------------------------------------------
    def list_messages(self, query: str = "", max_results: int = 100) -> list[dict]:
        """Liste les messages Gmail (wrapper sûr de `messages.list`)."""
        self.validate_call("users.messages.list")
        service = self._get_service()
        result = service.users().messages().list(
            userId="me", q=query, maxResults=max_results,
        ).execute()
        return result.get("messages", [])

    def get_message(self, msg_id: str, format: str = "full") -> dict:
        """Récupère un message complet (wrapper sûr de `messages.get`)."""
        self.validate_call("users.messages.get")
        service = self._get_service()
        return service.users().messages().get(
            userId="me", id=msg_id, format=format,
        ).execute()

    def modify_labels(self, msg_id: str, *, add: Optional[list[str]] = None,
                      remove: Optional[list[str]] = None) -> dict:
        """Modifie les labels d'un message (mark_read, archive, star, etc.).

        Args:
            msg_id: ID du message Gmail
            add: liste de labelIds à ajouter
            remove: liste de labelIds à retirer

        Returns:
            Le message modifié
        """
        self.validate_call("users.messages.modify")
        if not add and not remove:
            raise ValueError("add or remove must be non-empty")
        service = self._get_service()
        body: dict[str, list[str]] = {}
        if add:
            body["addLabelIds"] = add
        if remove:
            body["removeLabelIds"] = remove
        return service.users().messages().modify(
            userId="me", id=msg_id, body=body,
        ).execute()

    def list_history(self, start_history_id: str) -> list[dict]:
        """Récupère l'historique Gmail depuis un historyId (delta sync)."""
        self.validate_call("users.history.list")
        service = self._get_service()
        result = service.users().history().list(
            userId="me", startHistoryId=start_history_id,
        ).execute()
        return result.get("history", [])

    def list_labels(self) -> list[dict]:
        """Liste tous les labels Gmail (jamais hardcodés)."""
        self.validate_call("users.labels.list")
        service = self._get_service()
        result = service.users().labels().list(userId="me").execute()
        return result.get("labels", [])

    # ----------------------------------------------------------------
    # Helpers privés
    # ----------------------------------------------------------------
    def _get_service(self) -> Any:
        """Retourne (et cache) le service Google API. Lazy import."""
        if self._service is None:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            creds = self._load_credentials(Credentials)
            self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    def _load_credentials(self, CredentialsClass: type) -> Any:
        """Charge le refresh token OAuth2. (À implémenter par subagent 2)."""
        # En production : lire token.json ou faire le flow OAuth complet
        # Pour l'instant on lève — c'est testé avec un mock ailleurs
        raise GmailAuthError(
            "OAuth credentials not configured. "
            "Run `python -m src.main --setup-oauth` first."
        )
