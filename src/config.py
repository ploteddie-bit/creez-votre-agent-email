"""Configuration centralisée du projet email-learner.

Charge la configuration depuis `configs/config.yaml` (copié depuis
`config.yaml.example`) et les variables d'environnement
préfixées `EMAIL_LEARNER_*` (depuis `configs/.env`).

Les secrets (mots de passe, credentials OAuth) **doivent** venir
de l'environnement, jamais être hardcodés ni commités.

Usage :
    from src.config import get_settings

    settings = get_settings()
    print(settings.postgres.dsn())
    print(settings.p2.enabled)  # False par défaut (kill-switch sûr)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# === Chemins résolus une fois pour toutes ===
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
CONFIGS_DIR: Path = PROJECT_ROOT / "configs"
CONFIG_FILE: Path = CONFIGS_DIR / "config.yaml"
ENV_FILE: Path = CONFIGS_DIR / ".env"


# === Sous-modèles de configuration ===

class PostgresSettings(BaseModel):
    """Connexion PostgreSQL + pgvector.

    Le mot de passe **n'est jamais lu depuis le YAML** : il vient
    toujours de la variable d'environnement `EMAIL_LEARNER_DB_PASSWORD`.
    """

    model_config = ConfigDict(extra="ignore")

    host: str = "10.0.0.XXX"
    port: int = 5432
    database: str = "email_learner"
    user: str = "email_learner_app"
    sslmode: Literal["disable", "require", "verify-ca", "verify-full"] = "require"

    def dsn(self, *, password: str | None = None) -> str:
        """Construit le DSN PostgreSQL pour `psycopg2`.

        Le mot de passe est passé explicitement ou lu depuis l'env.
        Cette méthode ne le stocke jamais dans l'instance.
        """
        pwd = password if password is not None else os.environ.get(
            "EMAIL_LEARNER_DB_PASSWORD", ""
        )
        return (
            f"postgresql://{self.user}:{pwd}@"
            f"{self.host}:{self.port}/{self.database}?sslmode={self.sslmode}"
        )


class OllamaSettings(BaseModel):
    """Connexion au serveur Ollama local (LLM + embeddings)."""

    model_config = ConfigDict(extra="ignore")

    base_url: str = "http://10.0.0.XXX:11434"
    embedding_model: str = "bge-m3"
    llm_model: str = "llama3.1:8b"
    timeout_seconds: int = 30
    embedding_dimension: int = 1024


class PollingSettings(BaseModel):
    """Fréquence et taille de batch pour la sync Gmail."""

    model_config = ConfigDict(extra="ignore")

    interval_seconds: int = 60
    batch_size: int = 100
    sync_window_months: int = 6


class SandboxSettings(BaseModel):
    """Configuration du sandbox Firecracker (micro-VM jetable)."""

    model_config = ConfigDict(extra="ignore")

    timeout_seconds: int = 30
    vm_pool_min: int = 3
    vm_pool_max: int = 5
    max_attachment_size_mb: int = 5
    base64_mass_threshold_pct: int = 50


class P2Settings(BaseModel):
    """Configuration de la phase P2 (autonomie).

    **Le kill-switch par défaut est désactivé** : on ne passe jamais
    en P2 sans activation explicite via le dashboard.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    max_daily_actions: int = 20
    window_size: int = 100
    precision_thresholds: dict[str, float] = Field(
        default_factory=lambda: {
            "archive": 0.95,
            "mark_read": 0.90,
            "star": 0.85,
            "move_ia_review": 0.80,
        }
    )
    vacation_mode: bool = False


class GmailSettings(BaseModel):
    """Configuration Gmail API + allowlist d'interdictions.

    `forbidden_methods` est appliqué comme une contrainte **non négociable** :
    le code refuse de compiler si une de ces méthodes est appelée.
    """

    model_config = ConfigDict(extra="ignore")

    oauth_scopes: list[str] = Field(
        default_factory=lambda: ["https://www.googleapis.com/auth/gmail.modify"]
    )
    forbidden_methods: list[str] = Field(
        default_factory=lambda: [
            "users.messages.delete",
            "users.threads.delete",
            "users.messages.send",
            "users.drafts.send",
        ]
    )
    quota_costs: dict[str, int] = Field(
        default_factory=lambda: {
            "history.list": 2000,
            "messages.get": 2000,
            "messages.modify": 2000,
            "watch": 2000,
        }
    )
    quota_per_user_per_day: int = 1_000_000_000
    quota_threshold_pct: float = 0.8
    max_messages_per_minute: int = 100


class DashboardSettings(BaseModel):
    """Configuration du dashboard FastAPI / Caddy.

    `bind_host` ne doit **jamais** être 0.0.0.0 — le dashboard
    n'est visible que sur le réseau local.
    """

    model_config = ConfigDict(extra="ignore")

    bind_host: str = "10.0.0.XXX"
    bind_port: int = 8000
    caddy_https_port: int = 8080
    csp: str = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'"
    )


# === Settings globales ===

class Settings(BaseSettings):
    """Configuration globale du projet.

    Ordre de priorité (du plus fort au plus faible) :
      1. Variables d'environnement `EMAIL_LEARNER_*`
      2. Fichier `configs/config.yaml`
      3. Valeurs par défaut dans les sous-modèles
    """

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        env_prefix="EMAIL_LEARNER_",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False

    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    polling: PollingSettings = Field(default_factory=PollingSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    p2: P2Settings = Field(default_factory=P2Settings)
    gmail: GmailSettings = Field(default_factory=GmailSettings)
    dashboard: DashboardSettings = Field(default_factory=DashboardSettings)

    @classmethod
    def from_yaml(cls, path: Path | str | None = None) -> "Settings":
        """Charge la config depuis YAML + environnement.

        Les variables d'environnement `EMAIL_LEARNER_*` prennent
        priorité sur le YAML (comportement pydantic-settings).
        """
        config_path = Path(path) if path else CONFIG_FILE
        data: dict[str, Any] = {}
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
                if isinstance(loaded, dict):
                    data = loaded
        return cls(**data)

    def is_kill_switch_on(self) -> bool:
        """Vérifie le kill-switch global (env var prioritaire)."""
        env = os.environ.get("EMAIL_LEARNER_KILL_SWITCH", "").lower()
        if env in ("1", "true", "yes", "on"):
            return True
        return self.p2.enabled is False  # état sûr par défaut


# === Singleton lazy ===

_settings: Settings | None = None


def get_settings() -> Settings:
    """Retourne l'instance singleton des settings.

    L'instance est créée au premier appel et mise en cache.
    Pour forcer le rechargement (tests), utiliser `reset_settings()`.
    """
    global _settings
    if _settings is None:
        _settings = Settings.from_yaml()
    return _settings


def reset_settings() -> None:
    """Réinitialise le singleton (utile pour les tests)."""
    global _settings
    _settings = None
