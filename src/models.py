"""Modèles Pydantic pour les structures de données du projet.

Tous les modèles utilisés par l'IA (MailDecision), le sandbox
(SandboxAlert), la sync Gmail et la queue d'actions sont définis
ici, en un seul endroit. Le typage strict (extra="forbid")
empêche l'IA d'inventer des champs non déclarés.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, confloat, constr


# === Types littéraux partagés ===

Classification = Literal[
    "needs_reply",
    "newsletter",
    "receipt",
    "security_alert",
    "personal",
    "unknown",
]

ExecutableOperation = Literal[
    "none",
    "mark_read",
    "archive",
    "star",
    "move_ia_review",
]

RecommendedUserAction = Literal[
    "none",
    "reply_manually",
    "check_manually",
]

ActionQueueStatus = Literal["pending", "executing", "done", "failed"]

Phase = Literal["P0", "P1", "P2"]

SandboxLevel = Literal["info", "warning", "dangerous"]

# === Décision IA (sortie du LLM, validée par Pydantic) ===

class MailDecision(BaseModel):
    """Décision produite par l'IA de classification.

    Cette structure est la sortie forcée du LLM via Ollama
    `format=MailDecision.model_json_schema()`. Le `extra="forbid"`
    garantit que l'IA ne peut pas inventer de champs supplémentaires.
    """

    model_config = ConfigDict(extra="forbid")

    classification: Classification
    executable_operation: ExecutableOperation
    recommended_user_action: RecommendedUserAction = "none"
    confidence: confloat(ge=0.0, le=1.0) = Field(
        description="Confiance déclarée par le LLM (0.0 à 1.0). "
        "Ce n'est PAS une probabilité calibrée — les seuils P2 "
        "sont basés sur la précision mesurée, pas cette valeur."
    )
    reason: constr(max_length=500) = Field(
        description="Justification courte (max 500 chars). "
        "Vérifiée par le sandbox pour détecter les anomalies."
    )


# === Sandbox ===

class SandboxAlert(BaseModel):
    """Alerte émise par le sandbox Firecracker.

    Toute anomalie (JSON invalide, instruction cachée, comportement
    suspect) produit une alerte. Les alertes `dangerous` bloquent
    l'action et poussent un événement WebSocket au dashboard.
    """

    model_config = ConfigDict(extra="forbid")

    level: SandboxLevel
    patterns_matched: list[str] = Field(default_factory=list)
    raw_snippet: Optional[str] = None
    blocked: bool = False


# === Sync Gmail ===

class SyncState(BaseModel):
    """État de la synchronisation Gmail (un par compte)."""

    model_config = ConfigDict(extra="ignore")

    account_id: str
    last_history_id: Optional[str] = None
    last_full_sync_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    last_error: Optional[str] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def age_seconds(self) -> Optional[float]:
        """Âge du dernier sync réussi (None si jamais synchronisé)."""
        if self.last_success_at is None:
            return None
        return (datetime.utcnow() - self.last_success_at).total_seconds()


class GmailLabel(BaseModel):
    """Label Gmail. Jamais hardcodé : on stocke toujours l'ID réel."""

    model_config = ConfigDict(extra="ignore")

    account_id: str
    label_id: str
    label_name: str
    type: str  # "system" | "user"


# === Queue d'actions (idempotente) ===

class ActionQueueItem(BaseModel):
    """Item de la queue d'actions.

    L'`idempotency_key` empêche la double exécution après un crash/restart.
    Format recommandé : `{email_id}:{operation}:{date_iso}`.
    """

    model_config = ConfigDict(extra="ignore")

    id: Optional[int] = None
    email_id: str
    operation: ExecutableOperation
    status: ActionQueueStatus = "pending"
    idempotency_key: constr(min_length=1, max_length=512) = Field(
        description="Clé unique d'idempotence (ex: '{email_id}:{operation}:{date_iso}'). "
        "Empêche la double exécution après un crash/restart."
    )
    attempts: int = 0
    last_error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    executed_at: Optional[datetime] = None


# === Email stocké ===

class EmailInDB(BaseModel):
    """Représentation d'un email tel qu'il est stocké dans PostgreSQL.

    Le `body_text` est la version nettoyée (nh3 + sanitization).
    Le `body_html` brut n'est jamais affiché — uniquement archivé.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    thread_id: Optional[str] = None
    sender: str
    sender_email: str
    sender_domain: Optional[str] = None
    recipients: list[str] = Field(default_factory=list)
    subject: Optional[str] = None
    body_text: Optional[str] = None
    body_snippet: Optional[str] = None  # 500 premiers chars
    body_html: Optional[str] = None  # archivage seul, jamais affiché
    has_attachments: bool = False
    attachment_text: Optional[str] = None
    date_received: datetime
    labels: list[str] = Field(default_factory=list)
    is_read: bool = False
    is_starred: bool = False
    is_deleted: bool = False
    is_archived: bool = False


# === Décision complète (pour le journal append-only) ===

class DecisionRecord(BaseModel):
    """Entrée complète du journal des décisions (decision_journal).

    Ce modèle couvre tout ce qui est journalisé : la classification,
    le contexte RAG, le modèle utilisé, la validation humaine, etc.
    Aucune de ces informations n'est jamais effacée (append-only).
    """

    model_config = ConfigDict(extra="ignore")

    id: Optional[int] = None
    email_id: str
    phase: Phase

    # Classification IA
    classification: Classification
    executable_operation: ExecutableOperation
    recommended_user_action: RecommendedUserAction

    # Confiance
    llm_confidence: Optional[float] = None
    heuristic_confidence: Optional[float] = None
    final_confidence: Optional[float] = None

    # Contexte RAG
    similar_emails: list[str] = Field(default_factory=list)
    retrieval_distances: list[float] = Field(default_factory=list)
    retrieval_strategy: Optional[str] = None

    # Règles
    rules_applied: Optional[str] = None
    rules_version: Optional[str] = None

    # Modèle & prompt
    model_name: Optional[str] = None
    model_digest: Optional[str] = None
    prompt_version: Optional[str] = None
    schema_version: Optional[str] = None
    embedding_model: Optional[str] = None
    embedding_version: Optional[str] = None
    raw_llm_response: Optional[dict] = None
    validation_error: Optional[str] = None

    # Validation humaine
    user_approved: Optional[bool] = None

    # Exécution
    executed_at: Optional[datetime] = None
    execution_status: Optional[str] = None
    gmail_request_id: Optional[str] = None
    gmail_error: Optional[str] = None
    rollback_status: Optional[str] = None

    # Correction
    user_corrected_at: Optional[datetime] = None
    user_correction_action: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
