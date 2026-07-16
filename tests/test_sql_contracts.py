"""Tests de contrat SQL <-> schema.

Ce fichier centralise les tests qui verifient que le code Python
reste coherent avec le schema defini dans alembic/versions/.

Principe : on parse statiquement les requetes SQL (extraites des
modules src/) et on verifie que toutes les colonnes/tables referencees
existent dans le schema de la migration initiale.

Pourquoi c'est important :
- Empeche les bugs du type "champ ajoute en Python mais pas en SQL"
- Empeche les bugs du type "colonne renommee en SQL mais oubliee en Python"
- Documente le schema "de fait" du code

Ces tests sont statiques (pas besoin de PostgreSQL).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Optional

import pytest


# ============================================================
# Schema attendu (extrait de alembic/versions/001_initial_schema.py)
# ============================================================
#
# On duplique ce schema ici (en miroir de la migration) pour pouvoir
# tester sans parser le SQL DDL. C'est plus rapide et plus lisible
# qu'un parseur SQL DDL.
#
# IMPORTANT : ce miroir doit rester synchronise avec la migration.

EXPECTED_SCHEMA: dict[str, set[str]] = {
    "emails": {
        "id", "thread_id", "sender", "sender_email", "sender_domain",
        "recipients", "subject", "body_text", "body_snippet", "body_html",
        "has_attachments", "attachment_text", "date_received", "labels",
        "is_read", "is_starred", "is_deleted", "is_archived",
        "raw_headers", "tsv", "created_at",
    },
    "email_actions": {
        "id", "email_id", "action", "detected_at", "detected_by",
    },
    "email_embeddings": {
        "email_id", "embedding", "created_at",
    },
    "sync_state": {
        "account_id", "last_history_id", "last_full_sync_at",
        "last_success_at", "last_error", "updated_at",
    },
    "action_queue": {
        "id", "email_id", "operation", "status", "idempotency_key",
        "attempts", "last_error", "created_at", "executed_at",
    },
    "gmail_labels": {
        "account_id", "label_id", "label_name", "type", "created_at",
    },
    "decision_journal": {
        "id", "email_id", "phase", "classification", "executable_operation",
        "recommended_user_action", "llm_confidence", "heuristic_confidence",
        "final_confidence", "similar_emails", "retrieval_distances",
        "retrieval_strategy", "rules_applied", "rules_version",
        "model_name", "model_digest", "prompt_version", "schema_version",
        "embedding_model", "embedding_version", "raw_llm_response",
        "validation_error", "user_approved", "executed_at", "execution_status",
        "gmail_request_id", "gmail_error", "rollback_status",
        "user_corrected_at", "user_correction_action", "created_at",
    },
    "learning_metrics": {
        "id", "date", "total_emails", "total_actions",
        "p1_proposals", "p1_approved", "p1_rejected",
        "p2_auto_actions", "p2_correct",
        "precision_archive", "precision_mark_read", "precision_star",
        "precision_move_review", "rules_triggered", "quota_used_today",
        "created_at",
    },
    "sandbox_alerts": {
        "id", "email_id", "level", "vm_id", "patterns_matched",
        "raw_snippet", "llm_response", "vm_duration_ms", "blocked",
        "created_at",
    },
}


# ============================================================
# Utilitaires d'extraction
# ============================================================

def extract_sql_strings_from_module(py_path: Path) -> list[tuple[int, str]]:
    """Extrait tous les strings SQL (multi-lignes) d'un fichier Python.

    Retourne une liste de (line_number, sql_text) triee par ligne.

    Detecte les strings :
    - assignes a des variables se terminant par _SQL ou _QUERY
    - passes a cur.execute("...") inline
    """
    if not py_path.exists():
        return []

    content = py_path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(content, filename=str(py_path))
    except SyntaxError:
        return []

    sql_strings: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        # Cas 1 : Assignation a une variable *_SQL / *_QUERY
        if isinstance(node, ast.Assign):
            value = node.value
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.endswith(("_SQL", "_QUERY")):
                    text = _get_const_str(value)
                    if text and _looks_like_sql(text):
                        sql_strings.append((node.lineno, text))

        # Cas 2 : cur.execute("...") inline
        if isinstance(node, ast.Call):
            if _is_execute_call(node) and node.args:
                text = _get_const_str(node.args[0])
                if text and _looks_like_sql(text):
                    sql_strings.append((node.lineno, text))

    return sql_strings


def _get_const_str(node: ast.AST) -> Optional[str]:
    """Recupere la valeur string d'un ast.Constant ou ast.JoinedStr (f-string)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # f-string : on concatene les parties statiques (entre { })
        parts: list[str] = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append(" ")  # placeholder pour les valeurs interpolees
        return "".join(parts)
    return None


def _is_execute_call(node: ast.Call) -> bool:
    """Verifie si l'appel est cur.execute(...) ou executemany(...)."""
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in ("execute", "executemany"):
        if isinstance(func.value, ast.Name) and func.value.id in ("cur", "cursor"):
            return True
    return False


def _looks_like_sql(text: str) -> bool:
    """Heuristique simple : le texte contient-il un mot-cle SQL ?"""
    keywords = (
        "SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER",
        "DROP", "TRUNCATE", "FROM", "INTO", "WHERE", "JOIN",
    )
    text_upper = text.upper()
    return any(kw in text_upper for kw in keywords)


_SQL_KEYWORDS_NOT_TABLES: frozenset[str] = frozenset({
    # Mots-cles SQL qui peuvent suivre FROM/JOIN/INTO/UPDATE par erreur
    "set", "select", "values", "where", "order", "group", "having",
    "limit", "offset", "returning", "on", "do", "conflict", "and", "or",
    "not", "null", "true", "false", "default", "primary", "foreign",
    "key", "references", "check", "unique", "index", "using",
    "with", "as", "case", "when", "then", "else", "end",
    "asc", "desc", "distinct", "all", "any", "exists", "between",
    "in", "is", "like", "ilike", "similar", "to",
})


def extract_table_names(sql: str) -> set[str]:
    """Extrait les noms de tables d'une requete SQL.

    Couvre : FROM, INTO, UPDATE, JOIN.
    Retourne les noms en lowercase, filtres pour eviter les
    faux positifs (mots-cles SQL comme SET, VALUES, etc.).
    """
    tables: set[str] = set()
    sql_one_line = re.sub(r"\s+", " ", sql)
    patterns = [
        r"\bFROM\s+([a-z_][a-z0-9_]*)",
        r"\bJOIN\s+([a-z_][a-z0-9_]*)",
        r"\bINTO\s+([a-z_][a-z0-9_]*)",
        r"\bUPDATE\s+([a-z_][a-z0-9_]*)",
    ]
    for pat in patterns:
        for m in re.findall(pat, sql_one_line, re.IGNORECASE):
            name = m.lower()
            if name not in _SQL_KEYWORDS_NOT_TABLES:
                tables.add(name)
    return tables


def extract_column_references(sql: str) -> set[str]:
    """Extrait les references de colonnes d'une requete SQL.

    Pour les besoins de nos tests, on verifie surtout les colonnes
    explicites (SELECT col, INSERT INTO ... (col1, col2), etc.).
    """
    columns: set[str] = set()

    # INSERT INTO table (col1, col2, ...) - colonnes entre parentheses apres table
    insert_match = re.search(
        r"INSERT\s+INTO\s+\w+\s*\(([^)]+)\)",
        sql, re.IGNORECASE,
    )
    if insert_match:
        cols = insert_match.group(1)
        for c in cols.split(","):
            c = c.strip().strip('"').strip("`")
            if c:
                columns.add(c.lower())

    # ON CONFLICT (col) DO UPDATE SET col = ...
    conflict_match = re.search(
        r"ON\s+CONFLICT\s*\(([^)]+)\)\s*DO\s+UPDATE\s+SET\s+(.+?)(?:WHERE|$)",
        sql, re.IGNORECASE | re.DOTALL,
    )
    if conflict_match:
        for c in conflict_match.group(1).split(","):
            c = c.strip().strip('"').strip("`")
            if c:
                columns.add(c.lower())
        # Extraire les colonnes assignees dans le SET
        set_part = conflict_match.group(2)
        for c in re.findall(r"(\w+)\s*=", set_part):
            columns.add(c.lower())

    return columns


# ============================================================
# Tests : extraction fonctionne
# ============================================================

def test_extract_table_names_basic() -> None:
    """L'extraction de tables couvre FROM, JOIN, INTO, UPDATE."""
    assert "emails" in extract_table_names("SELECT * FROM emails")
    assert "emails" in extract_table_names("SELECT * FROM emails JOIN users ON ...")
    assert "emails" in extract_table_names("INSERT INTO emails (id) VALUES (1)")
    assert "emails" in extract_table_names("UPDATE emails SET is_read = true")


def test_extract_column_references_insert() -> None:
    """L'extraction de colonnes couvre INSERT et ON CONFLICT."""
    cols = extract_column_references(
        "INSERT INTO emails (id, subject, body_text) VALUES (1, 's', 'b') "
        "ON CONFLICT (id) DO UPDATE SET is_read = true"
    )
    assert "id" in cols
    assert "subject" in cols
    assert "body_text" in cols
    assert "is_read" in cols


def test_extract_column_references_with_aliases() -> None:
    """L'extraction de colonnes gere les aliases (e.col) et les guillemets."""
    cols = extract_column_references(
        "INSERT INTO emails (\"id\", subject) VALUES (1, 's')"
    )
    assert "id" in cols
    assert "subject" in cols


# ============================================================
# Tests : contrat SQL <-> schema attendu
# ============================================================

def _get_src_sql_strings() -> list[tuple[Path, int, str]]:
    """Recupere toutes les chaines SQL de tous les modules src/."""
    src_dir = Path("src")
    if not src_dir.exists():
        return []
    results: list[tuple[Path, int, str]] = []
    for py_file in src_dir.rglob("*.py"):
        for lineno, sql in extract_sql_strings_from_module(py_file):
            results.append((py_file, lineno, sql))
    return results


def test_all_referenced_tables_exist_in_schema() -> None:
    """Toutes les tables referencees dans le code existent dans la migration."""
    referenced: set[str] = set()
    for _path, _lineno, sql in _get_src_sql_strings():
        referenced.update(extract_table_names(sql))

    # Ignorer les tables systeme
    referenced -= {"information_schema"}

    unknown = referenced - set(EXPECTED_SCHEMA.keys())
    assert not unknown, (
        f"Code references unknown tables: {sorted(unknown)}\n"
        f"Tables connues: {sorted(EXPECTED_SCHEMA.keys())}\n"
        f"=> Ajouter la table dans EXPECTED_SCHEMA ou corriger le code."
    )


def test_all_insert_columns_exist_in_schema() -> None:
    """Toutes les colonnes des INSERT/UPDATE correspondent au schema."""
    errors: list[str] = []
    for path, lineno, sql in _get_src_sql_strings():
        sql_upper = sql.upper()
        # Ne s'applique qu'aux INSERT
        if "INSERT INTO" not in sql_upper and "ON CONFLICT" not in sql_upper:
            continue
        # Trouver la table cible
        tables = extract_table_names(sql)
        if not tables:
            continue
        # On suppose une seule table par requete
        table = next(iter(tables))
        expected_cols = EXPECTED_SCHEMA.get(table)
        if expected_cols is None:
            continue  # deja gere par test_all_referenced_tables_exist_in_schema
        # Verifier chaque colonne referencee
        for col in extract_column_references(sql):
            if col not in expected_cols:
                errors.append(
                    f"  {path}:{lineno} -> INSERT/UPDATE on '{table}': "
                    f"column '{col}' not in schema "
                    f"(expected one of {sorted(expected_cols)})"
                )
    assert not errors, "SQL column/schema mismatches:\n" + "\n".join(errors)


# ============================================================
# Test : coherence du miroir EXPECTED_SCHEMA vs alembic
# ============================================================

def test_schema_mirror_is_non_empty() -> None:
    """Le miroir de schema doit contenir au moins les 9 tables principales."""
    assert len(EXPECTED_SCHEMA) >= 9
    # Tables explicitement mentionnees dans la SPEC section 5
    for required in (
        "emails", "email_actions", "email_embeddings", "sync_state",
        "action_queue", "gmail_labels", "decision_journal",
        "learning_metrics", "sandbox_alerts",
    ):
        assert required in EXPECTED_SCHEMA, f"Missing table in mirror: {required}"


def test_schema_mirror_matches_alembic_basic() -> None:
    """Verification croisee : la migration alembic doit contenir les
    memes tables que EXPECTED_SCHEMA.

    C'est un test "best-effort" : il verifie au minimum que chaque
    table du miroir apparait dans le fichier de migration.
    """
    migration = Path("alembic/versions/001_initial_schema.py")
    if not migration.exists():
        pytest.skip("alembic migration not found")

    content = migration.read_text(encoding="utf-8")
    for table in EXPECTED_SCHEMA:
        # Le format alembic utilise sa.Column("name", ...) ou op.create_table("name", ...)
        assert f'"{table}"' in content or f"'{table}'" in content, (
            f"Table '{table}' in EXPECTED_SCHEMA but not found in migration file"
        )
