"""Schéma initial — Agent Mail 24/7

Création de toutes les tables, triggers, index et contraintes :
- 9 tables : emails, email_actions, email_embeddings, sync_state,
             action_queue, gmail_labels, decision_journal,
             learning_metrics, sandbox_alerts
- 2 triggers : tsvector FR sur emails
- ~10 index : GIN, B-tree, IVFFlat cosine, partiels

Revision ID: 001_initial_schema
Revises:
Create Date: 2026-07-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ============================================================
    # Extensions PostgreSQL
    # ============================================================
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ============================================================
    # TABLE : emails
    # ============================================================
    op.create_table(
        "emails",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("thread_id", sa.Text),
        sa.Column("sender", sa.Text, nullable=False),
        sa.Column("sender_email", sa.Text, nullable=False),
        sa.Column("sender_domain", sa.Text),
        sa.Column("recipients", postgresql.ARRAY(sa.Text), server_default="{}"),
        sa.Column("subject", sa.Text),
        sa.Column("body_text", sa.Text),
        sa.Column("body_snippet", sa.Text),
        sa.Column("body_html", sa.Text),
        sa.Column("has_attachments", sa.Boolean, server_default=sa.text("false")),
        sa.Column("attachment_text", sa.Text),
        sa.Column("date_received", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("labels", postgresql.ARRAY(sa.Text), server_default="{}"),
        sa.Column("is_read", sa.Boolean),
        sa.Column("is_starred", sa.Boolean),
        sa.Column("is_deleted", sa.Boolean, server_default=sa.text("false")),
        sa.Column("is_archived", sa.Boolean, server_default=sa.text("false")),
        sa.Column("raw_headers", postgresql.JSONB),
        sa.Column("tsv", postgresql.TSVECTOR),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
    )

    # Trigger tsvector FR (subject + body_text)
    op.execute("""
        CREATE OR REPLACE FUNCTION emails_tsv_trigger() RETURNS trigger AS $$
        BEGIN
            NEW.tsv := to_tsvector(
                'french',
                COALESCE(NEW.subject, '') || ' ' || COALESCE(NEW.body_text, '')
            );
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER tsv_update
        BEFORE INSERT OR UPDATE ON emails
        FOR EACH ROW EXECUTE FUNCTION emails_tsv_trigger();
    """)

    # Index emails
    op.create_index("idx_emails_tsv", "emails", ["tsv"], unique=False, postgresql_using="gin")
    op.create_index("idx_emails_sender", "emails", ["sender_email"])
    op.create_index("idx_emails_domain", "emails", ["sender_domain"])
    op.create_index("idx_emails_date", "emails", [sa.text("date_received DESC")])

    # ============================================================
    # TABLE : email_actions
    # ============================================================
    op.create_table(
        "email_actions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("email_id", sa.Text, sa.ForeignKey("emails.id", ondelete="CASCADE")),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("detected_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("detected_by", sa.Text, server_default="poll_delta"),
    )
    op.create_index("idx_actions_email", "email_actions", ["email_id"])

    # ============================================================
    # TABLE : email_embeddings (pgvector 1024d)
    # ============================================================
    op.create_table(
        "email_embeddings",
        sa.Column("email_id", sa.Text, sa.ForeignKey("emails.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("embedding", postgresql.ARRAY(sa.Float), nullable=False),  # sera vector(1024) en raw SQL
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
    )
    # Forcer le type vector(1024) sur la colonne embedding (Alchemy ne gère pas le type vector nativement)
    op.execute("ALTER TABLE email_embeddings ALTER COLUMN embedding TYPE vector(1024) USING embedding::vector(1024)")

    # Index IVFFlat cosine (100 listes)
    op.execute("""
        CREATE INDEX idx_emb_cosine ON email_embeddings
        USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)
    """)

    # ============================================================
    # TABLE : sync_state
    # ============================================================
    op.create_table(
        "sync_state",
        sa.Column("account_id", sa.Text, primary_key=True),
        sa.Column("last_history_id", sa.Text),
        sa.Column("last_full_sync_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("last_success_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("last_error", sa.Text),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
    )

    # ============================================================
    # TABLE : action_queue (idempotente)
    # ============================================================
    op.create_table(
        "action_queue",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("email_id", sa.Text, sa.ForeignKey("emails.id", ondelete="CASCADE"), nullable=False),
        sa.Column("operation", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("idempotency_key", sa.Text, nullable=False, unique=True),
        sa.Column("attempts", sa.Integer, server_default="0"),
        sa.Column("last_error", sa.Text),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("executed_at", sa.TIMESTAMP(timezone=True)),
    )
    # Index partiel : seulement les pending (très utilisé par le worker)
    op.execute("CREATE INDEX idx_queue_status ON action_queue(status) WHERE status = 'pending'")

    # ============================================================
    # TABLE : gmail_labels
    # ============================================================
    op.create_table(
        "gmail_labels",
        sa.Column("account_id", sa.Text, nullable=False),
        sa.Column("label_id", sa.Text, nullable=False),
        sa.Column("label_name", sa.Text),
        sa.Column("type", sa.Text),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("account_id", "label_id"),
    )

    # ============================================================
    # TABLE : decision_journal (append-only)
    # ============================================================
    op.create_table(
        "decision_journal",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("email_id", sa.Text, sa.ForeignKey("emails.id", ondelete="SET NULL")),
        sa.Column("phase", sa.Text, nullable=False),
        # Classification IA
        sa.Column("classification", sa.Text, nullable=False),
        sa.Column("executable_operation", sa.Text, nullable=False),
        sa.Column("recommended_user_action", sa.Text),
        # Confiance
        sa.Column("llm_confidence", sa.Float),
        sa.Column("heuristic_confidence", sa.Float),
        sa.Column("final_confidence", sa.Float),
        # RAG
        sa.Column("similar_emails", postgresql.ARRAY(sa.Text), server_default="{}"),
        sa.Column("retrieval_distances", postgresql.ARRAY(sa.Float), server_default="{}"),
        sa.Column("retrieval_strategy", sa.Text),
        # Règles
        sa.Column("rules_applied", sa.Text),
        sa.Column("rules_version", sa.Text),
        # Modèle
        sa.Column("model_name", sa.Text),
        sa.Column("model_digest", sa.Text),
        sa.Column("prompt_version", sa.Text),
        sa.Column("schema_version", sa.Text),
        sa.Column("embedding_model", sa.Text),
        sa.Column("embedding_version", sa.Text),
        sa.Column("raw_llm_response", postgresql.JSONB),
        sa.Column("validation_error", sa.Text),
        # Validation humaine
        sa.Column("user_approved", sa.Boolean),
        # Exécution
        sa.Column("executed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("execution_status", sa.Text),
        sa.Column("gmail_request_id", sa.Text),
        sa.Column("gmail_error", sa.Text),
        sa.Column("rollback_status", sa.Text),
        # Correction
        sa.Column("user_corrected_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("user_correction_action", sa.Text),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_journal_email", "decision_journal", ["email_id"])
    op.create_index("idx_journal_phase", "decision_journal", ["phase"])
    op.create_index("idx_journal_created", "decision_journal", [sa.text("created_at DESC")])
    op.create_index("idx_journal_classification", "decision_journal", ["classification"])

    # ============================================================
    # TABLE : learning_metrics
    # ============================================================
    op.create_table(
        "learning_metrics",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("date", sa.Date, nullable=False, unique=True),
        sa.Column("total_emails", sa.Integer),
        sa.Column("total_actions", sa.Integer),
        sa.Column("p1_proposals", sa.Integer),
        sa.Column("p1_approved", sa.Integer),
        sa.Column("p1_rejected", sa.Integer),
        sa.Column("p2_auto_actions", sa.Integer),
        sa.Column("p2_correct", sa.Integer),
        sa.Column("precision_archive", sa.Float),
        sa.Column("precision_mark_read", sa.Float),
        sa.Column("precision_star", sa.Float),
        sa.Column("precision_move_review", sa.Float),
        sa.Column("rules_triggered", sa.Integer),
        sa.Column("quota_used_today", sa.Integer),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
    )

    # ============================================================
    # TABLE : sandbox_alerts
    # ============================================================
    op.create_table(
        "sandbox_alerts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("email_id", sa.Text, sa.ForeignKey("emails.id", ondelete="SET NULL")),
        sa.Column("level", sa.Text, nullable=False),
        sa.Column("vm_id", sa.Text),
        sa.Column("patterns_matched", postgresql.ARRAY(sa.Text), server_default="{}"),
        sa.Column("raw_snippet", sa.Text),
        sa.Column("llm_response", postgresql.JSONB),
        sa.Column("vm_duration_ms", sa.Integer),
        sa.Column("blocked", sa.Boolean, server_default=sa.text("false")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
    )
    # Index partiel : alertes dangereuses uniquement (alerting)
    op.execute("CREATE INDEX idx_alerts_dangerous ON sandbox_alerts(level) WHERE level = 'dangerous'")


def downgrade() -> None:
    # Ordre inverse : supprimer les tables avant les extensions
    op.drop_table("sandbox_alerts")
    op.drop_table("learning_metrics")
    op.drop_index("idx_journal_classification", table_name="decision_journal")
    op.drop_index("idx_journal_created", table_name="decision_journal")
    op.drop_index("idx_journal_phase", table_name="decision_journal")
    op.drop_index("idx_journal_email", table_name="decision_journal")
    op.drop_table("decision_journal")
    op.drop_table("gmail_labels")
    op.drop_index("idx_queue_status", table_name="action_queue")
    op.drop_table("action_queue")
    op.drop_table("sync_state")
    op.drop_table("email_embeddings")
    op.drop_index("idx_actions_email", table_name="email_actions")
    op.drop_table("email_actions")
    op.execute("DROP TRIGGER IF EXISTS tsv_update ON emails")
    op.execute("DROP FUNCTION IF EXISTS emails_tsv_trigger()")
    op.drop_index("idx_emails_date", table_name="emails")
    op.drop_index("idx_emails_domain", table_name="emails")
    op.drop_index("idx_emails_sender", table_name="emails")
    op.drop_index("idx_emails_tsv", table_name="emails")
    op.drop_table("emails")
    # Note : on ne drop pas les extensions pour ne pas casser d'autres bases
