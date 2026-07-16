"""Alembic environment — charge l'URL depuis la config Pydantic du projet.

Ce fichier est appelé par `alembic upgrade` et `alembic downgrade`.
Il injecte l'URL de la base depuis `src.config.Settings` (qui lit
le YAML + les variables d'environnement) au lieu de la hardcoder
dans `alembic.ini`.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Permettre l'import de `src.*` depuis la racine du projet
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Importer la config Pydantic du projet
from src.config import get_settings  # noqa: E402

# Alembic Config object (lit alembic.ini)
config = context.config

# Configurer le logger depuis alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override de l'URL SQLAlchemy avec celle des settings Pydantic
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.postgres.dsn())

# Metadata des modèles (vide pour l'instant, sera rempli par SQLAlchemy declarative_base)
target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Émet les scripts SQL sans se connecter à la base. Utile pour
    générer un dump de migration sans DB active.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — connexion réelle à la DB."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
