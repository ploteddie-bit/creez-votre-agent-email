"""Tests pour src.config — chargement YAML + env + défauts sûrs."""
from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_settings_default_safety() -> None:
    """Les settings par défaut doivent être sûrs : P2 désactivé, pas de secrets hardcodés."""
    from src.config import Settings

    s = Settings()
    # Kill-switch P2 désactivé par défaut
    assert s.p2.enabled is False
    # Pas de mot de passe dans le DSN si l'env n'est pas set
    dsn_no_pwd = s.postgres.dsn(password="")
    assert ":@" in dsn_no_pwd or ":@" in dsn_no_pwd  # user:@host (empty pwd)
    # Interdictions Gmail non négociables
    assert "users.messages.delete" in s.gmail.forbidden_methods
    assert "users.messages.send" in s.gmail.forbidden_methods
    # Dashboard sur IP privée uniquement
    assert s.dashboard.bind_host != "0.0.0.0"
    # CSP restrictive
    assert "default-src 'self'" in s.dashboard.csp


def test_postgres_dsn_uses_env_password() -> None:
    """Le DSN doit utiliser la variable d'environnement, pas le YAML."""
    from src.config import PostgresSettings

    pg = PostgresSettings()
    os.environ["EMAIL_LEARNER_DB_PASSWORD"] = "secret_pwd_42"
    try:
        dsn = pg.dsn()
        assert "secret_pwd_42" in dsn
        assert "sslmode=require" in dsn
    finally:
        del os.environ["EMAIL_LEARNER_DB_PASSWORD"]


def test_p2_precision_thresholds() -> None:
    """Les seuils P2 sont conformes à la SPEC (archive≥95, mark_read≥90, etc.)."""
    from src.config import Settings

    s = Settings()
    thresholds = s.p2.precision_thresholds
    assert thresholds["archive"] == 0.95
    assert thresholds["mark_read"] == 0.90
    assert thresholds["star"] == 0.85
    assert thresholds["move_ia_review"] == 0.80


def test_from_yaml_missing_file() -> None:
    """from_yaml ne doit pas planter si le fichier n'existe pas."""
    from src.config import Settings

    s = Settings.from_yaml(path=Path("/nonexistent/config.yaml"))
    assert s.p2.enabled is False


def test_env_var_overrides_yaml() -> None:
    """Une variable d'environnement doit override la valeur YAML."""
    from src.config import Settings

    os.environ["EMAIL_LEARNER_P2__ENABLED"] = "true"
    try:
        s = Settings()
        assert s.p2.enabled is True
    finally:
        del os.environ["EMAIL_LEARNER_P2__ENABLED"]


def test_kill_switch_default_on() -> None:
    """Si P2 est désactivé, le kill-switch est considéré ON (état sûr)."""
    from src.config import Settings

    s = Settings(p2={"enabled": False, "max_daily_actions": 20,
                     "window_size": 100, "precision_thresholds": {},
                     "vacation_mode": False})
    # Sans variable d'env, et P2 désactivé → kill-switch considéré ON
    assert s.is_kill_switch_on() is True


def test_env_var_overrides_yaml_via_from_yaml(tmp_path: Path) -> None:
    """Régression : via from_yaml, l'env doit AUSSI primer sur le YAML.

    pydantic-settings donne par défaut la priorité aux kwargs d'init
    (donc au YAML) — `settings_customise_sources` inverse cet ordre
    pour que EMAIL_LEARNER_* surcharge toujours la config fichier.
    """
    from src.config import Settings

    config_file = tmp_path / "config.yaml"
    config_file.write_text("p2:\n  enabled: false\n", encoding="utf-8")
    os.environ["EMAIL_LEARNER_P2__ENABLED"] = "true"
    try:
        s = Settings.from_yaml(path=config_file)
        assert s.p2.enabled is True
    finally:
        del os.environ["EMAIL_LEARNER_P2__ENABLED"]
