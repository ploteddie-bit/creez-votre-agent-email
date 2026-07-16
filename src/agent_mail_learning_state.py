"""État d'apprentissage — métriques rapides depuis PostgreSQL.

Script de diagnostic : compte les emails, les décisions du journal,
le feedback humain par opération et l'état de la queue d'actions.

Usage (depuis la racine du projet) :
    python -m src.agent_mail_learning_state
"""
from __future__ import annotations

import sys
from pathlib import Path

# Permettre l'import de `src.*` quand le script est lancé directement
# (`python src/agent_mail_learning_state.py`)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import psycopg2  # noqa: E402

from src.config import Settings  # noqa: E402

settings = Settings.from_yaml()
conn = psycopg2.connect(settings.postgres.dsn())
try:
    with conn.cursor() as cur:
        cur.execute("select count(*) from emails")
        print("emails_total", cur.fetchone()[0])
        cur.execute(
            "select count(*) from emails e where not exists "
            "(select 1 from decision_journal d where d.email_id=e.id)"
        )
        print("emails_without_decision", cur.fetchone()[0])
        cur.execute(
            "select count(*), "
            "count(*) filter (where user_approved is not null), "
            "count(*) filter (where user_approved=true), "
            "count(*) filter (where user_approved=false) "
            "from decision_journal"
        )
        print("decision_counts", cur.fetchone())
        cur.execute(
            "select executable_operation, count(*), "
            "count(*) filter (where user_approved=true), "
            "count(*) filter (where user_approved=false) "
            "from decision_journal "
            "where user_approved is not null "
            "group by executable_operation order by 2 desc"
        )
        for row in cur.fetchall():
            print("op_feedback", row)
        cur.execute(
            "select status, operation, count(*) from action_queue "
            "group by status, operation order by status, operation"
        )
        for row in cur.fetchall():
            print("queue", row)
finally:
    conn.close()
