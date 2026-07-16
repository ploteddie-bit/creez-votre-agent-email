"""Helper de connexion PostgreSQL pour les modules du projet.

Toutes les opérations base de données passent par `get_connection()`,
qui retourne une connexion psycopg2 configurée (hostssl, timeouts,
autocommit contrôlé).

Usage :
    from src.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
"""
from __future__ import annotations

import contextlib
import logging
import os
from typing import TYPE_CHECKING, Iterator

from src.config import ENV_FILE, get_settings

# Imports lourds placés en lazy pour permettre les tests unitaires
# qui n'ont pas besoin de PostgreSQL
if TYPE_CHECKING:
    import psycopg2
    import psycopg2.extensions

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def get_connection(
    autocommit: bool = False,
) -> Iterator[psycopg2.extensions.connection]:
    """Context manager qui ouvre et ferme une connexion PostgreSQL.

    - autocommit=False (défaut) : transaction explicite, l'appelant
      fait `conn.commit()` ou un rollback est joué si exception.
    - autocommit=True : chaque statement est commité individuellement
      (utile pour les scripts de maintenance).
    """
    settings = get_settings()
    pwd = _read_env_var("EMAIL_LEARNER_DB_PASSWORD")
    import psycopg2  # lazy import
    conn = psycopg2.connect(settings.postgres.dsn(password=pwd))
    conn.autocommit = autocommit
    try:
        yield conn
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def _read_env_var(name: str) -> str | None:
    """Lit une variable : environnement réel d'abord, puis `configs/.env`.

    Cohérent avec pydantic-settings (qui lit `configs/.env` via
    `ENV_FILE`) : l'ancienne version cherchait un `.env` dans le
    répertoire courant, que le projet n'utilise pas.
    """
    if (val := os.environ.get(name)) is not None:
        return val
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() == name:
            return v.strip().strip('"').strip("'")
    return None


def healthcheck() -> bool:
    """Vérifie que la DB est accessible. Retourne True/False.

    Utilisé par le healthcheck du dashboard (`/api/health`).
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                return cur.fetchone() == (1,)
    except Exception as e:
        logger.warning("DB healthcheck failed: %s", e)
        return False
