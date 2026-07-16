"""Tests pour src.ingester — contrat SQL ↔ modèle (sans base de données)."""
from __future__ import annotations

import re

import pytest


def test_upsert_params_covered_by_model() -> None:
    """Chaque paramètre nommé de _UPSERT_SQL doit exister dans EmailInDB.

    Régression : `raw_headers` était référencé par le SQL mais absent
    du modèle → KeyError au premier `ingest_email()`.
    """
    from src.ingester import _UPSERT_SQL
    from src.models import EmailInDB

    params = set(re.findall(r"%\((\w+)\)s", _UPSERT_SQL))
    missing = params - set(EmailInDB.model_fields)
    assert not missing, f"paramètres SQL absents du modèle EmailInDB: {missing}"


def test_to_row_adapts_raw_headers_jsonb(sample_email_pydantic) -> None:
    """Un dict raw_headers doit être wrappé en Json pour l'adaptation JSONB."""
    psycopg2_extras = pytest.importorskip("psycopg2.extras")
    from src.ingester import EmailIngester

    sample_email_pydantic.raw_headers = {"From": "a@b.c", "DKIM-Signature": "v=1; ..."}
    row = EmailIngester._to_row(sample_email_pydantic)
    assert isinstance(row["raw_headers"], psycopg2_extras.Json)


def test_to_row_raw_headers_none_passthrough(sample_email_pydantic) -> None:
    """Sans raw_headers, la valeur reste None (colonne JSONB nullable)."""
    pytest.importorskip("psycopg2.extras")
    from src.ingester import EmailIngester

    row = EmailIngester._to_row(sample_email_pydantic)
    assert "raw_headers" in row
    assert row["raw_headers"] is None
