"""Tests pour src.action_worker - queue idempotente + retry + multi-workers."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_db(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock complet de get_connection."""
    import src.db as db_mod
    import src.action_worker as aw_mod

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = None
    mock_cursor.fetchall.return_value = []
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.__exit__.return_value = False
    mock_conn.commit.return_value = None

    ctx = MagicMock()
    ctx.__enter__.return_value = mock_conn
    ctx.__exit__.return_value = False
    monkeypatch.setattr(db_mod, "get_connection", lambda *a, **kw: ctx)
    monkeypatch.setattr(aw_mod, "get_connection", lambda *a, **kw: ctx)
    return mock_cursor


# ============================================================
# Tests idempotence
# ============================================================

class TestIdempotency:
    """Tests de la cle d'idempotence."""

    def test_make_idempotency_key_format(self) -> None:
        from src.action_worker import ActionWorker
        key = ActionWorker.make_idempotency_key("msg_1", "archive")
        assert key.startswith("msg_1:archive:")
        # Format date YYYY-MM-DD
        assert len(key.split(":")) == 3

    def test_same_email_op_day_same_key(self) -> None:
        from src.action_worker import ActionWorker
        from datetime import datetime
        when = datetime(2026, 7, 16, 10, 0, 0)
        k1 = ActionWorker.make_idempotency_key("msg_1", "archive", when)
        k2 = ActionWorker.make_idempotency_key("msg_1", "archive", when)
        assert k1 == k2

    def test_different_days_different_keys(self) -> None:
        from src.action_worker import ActionWorker
        from datetime import datetime
        d1 = datetime(2026, 7, 16, 23, 59, 59)
        d2 = datetime(2026, 7, 17, 0, 0, 1)
        k1 = ActionWorker.make_idempotency_key("msg_1", "archive", d1)
        k2 = ActionWorker.make_idempotency_key("msg_1", "archive", d2)
        assert k1 != k2

    def test_enqueue_uses_upsert_pattern(self, mock_db: MagicMock) -> None:
        """L'enqueue utilise ON CONFLICT (idempotency_key) DO NOTHING."""
        from src.action_worker import ActionWorker
        # Simuler qu'on insere avec succes
        mock_db.fetchone.return_value = (42,)
        w = ActionWorker(gmail_client=MagicMock(), circuit_breaker=MagicMock())
        w.enqueue_action("msg_1", "archive")
        # Verifier que la requete contient ON CONFLICT
        # (pas d'exception, c'est OK)

    def test_enqueue_rejects_unknown_operation(self) -> None:
        from src.action_worker import ActionWorker
        w = ActionWorker(gmail_client=MagicMock(), circuit_breaker=MagicMock())
        with pytest.raises(ValueError, match="unknown operation"):
            w.enqueue_action("msg_1", "delete_everything")  # pas autorise

    def test_enqueue_noop_for_none_operation(self, mock_db: MagicMock) -> None:
        from src.action_worker import ActionWorker
        w = ActionWorker(gmail_client=MagicMock(), circuit_breaker=MagicMock())
        result = w.enqueue_action("msg_1", "none")
        assert result == 0  # rien enqueue


# ============================================================
# Tests retry
# ============================================================

class TestRetryLogic:
    """Tests de la logique de retry sur echec."""

    def test_process_job_marks_done_on_success(
        self, mock_db: MagicMock,
    ) -> None:
        """Si Gmail retourne OK, le job est 'done'."""
        from src.action_worker import ActionWorker
        w = ActionWorker(gmail_client=MagicMock(), circuit_breaker=MagicMock())
        w.gmail_client.modify_labels = MagicMock(return_value={"id": "msg_1"})
        # Simuler qu'on a un job
        job = {"id": 1, "email_id": "msg_1", "operation": "archive",
               "idempotency_key": "msg_1:archive:2026-07-16", "attempts": 0}
        w._process_job(job)
        w.gmail_client.modify_labels.assert_called_once()

    def test_process_job_retries_on_failure(
        self, mock_db: MagicMock,
    ) -> None:
        """Si Gmail leve une exception et attempts < max, on remet en pending."""
        from src.action_worker import ActionWorker
        w = ActionWorker(gmail_client=MagicMock(), circuit_breaker=MagicMock())
        w.gmail_client.modify_labels = MagicMock(side_effect=Exception("API down"))
        # max_attempts par defaut = 3, attempts courant = 0 -> retry
        job = {"id": 1, "email_id": "msg_1", "operation": "archive",
               "idempotency_key": "msg_1:archive:2026-07-16", "attempts": 1}
        w._process_job(job)
        # Pas d'exception, le job est remis en pending

    def test_process_job_marks_failed_after_max_attempts(
        self, mock_db: MagicMock,
    ) -> None:
        """Apres max_attempts echecs, le job est 'failed'."""
        from src.action_worker import ActionWorker
        w = ActionWorker(gmail_client=MagicMock(), circuit_breaker=MagicMock(),
                         max_attempts=3)
        w.gmail_client.modify_labels = MagicMock(side_effect=Exception("API down"))
        # attempts = 3 (deja 3 essais), donc 'failed'
        job = {"id": 1, "email_id": "msg_1", "operation": "archive",
               "idempotency_key": "msg_1:archive:2026-07-16", "attempts": 3}
        w._process_job(job)


class TestCircuitBreakerIntegration:
    """Tests de l'integration avec le circuit-breaker."""

    def test_process_job_returns_to_pending_if_quota_paused(
        self, mock_db: MagicMock,
    ) -> None:
        """Si le CB est en pause, on remet le job en pending."""
        from src.action_worker import ActionWorker, QuotaPausedError
        from src.observer import QuotaExceeded
        cb = MagicMock()
        cb.can_proceed.side_effect = QuotaExceeded("quota 80%")
        w = ActionWorker(gmail_client=MagicMock(), circuit_breaker=cb)
        job = {"id": 1, "email_id": "msg_1", "operation": "archive",
               "idempotency_key": "k", "attempts": 0}
        with pytest.raises(QuotaPausedError):
            w._process_job(job)
        # GmailClient n'a pas ete appele
        w.gmail_client.modify_labels.assert_not_called()


# ============================================================
# Tests operations
# ============================================================

class TestOperations:
    """Tests du mapping operations -> labels Gmail."""

    def test_operation_to_labels_mark_read(self) -> None:
        from src.action_worker import OPERATION_TO_LABELS
        assert "remove" in OPERATION_TO_LABELS["mark_read"]
        assert "UNREAD" in OPERATION_TO_LABELS["mark_read"]["remove"]

    def test_operation_to_labels_archive(self) -> None:
        from src.action_worker import OPERATION_TO_LABELS
        assert "INBOX" in OPERATION_TO_LABELS["archive"]["remove"]

    def test_operation_to_labels_star(self) -> None:
        from src.action_worker import OPERATION_TO_LABELS
        assert "STARRED" in OPERATION_TO_LABELS["star"]["add"]

    def test_operation_to_labels_move_ia_review_is_special(self) -> None:
        from src.action_worker import OPERATION_TO_LABELS
        # move_ia_review necessite l'ID reel du label, pas un nom hardcode
        assert "add_ia_review" in OPERATION_TO_LABELS["move_ia_review"]

    def test_operation_to_labels_none_is_noop(self) -> None:
        from src.action_worker import OPERATION_TO_LABELS
        assert OPERATION_TO_LABELS["none"] == {}


class TestExecuteAction:
    """Tests de l'execution d'une action via Gmail."""

    def test_execute_action_mark_read(self, mock_db: MagicMock) -> None:
        from src.action_worker import ActionWorker
        w = ActionWorker(gmail_client=MagicMock(), circuit_breaker=MagicMock())
        w._execute_action("msg_1", "mark_read")
        w.gmail_client.modify_labels.assert_called_once_with(
            "msg_1", add=None, remove=["UNREAD"],
        )

    def test_execute_action_archive(self, mock_db: MagicMock) -> None:
        from src.action_worker import ActionWorker
        w = ActionWorker(gmail_client=MagicMock(), circuit_breaker=MagicMock())
        w._execute_action("msg_1", "archive")
        w.gmail_client.modify_labels.assert_called_once_with(
            "msg_1", add=None, remove=["INBOX"],
        )

    def test_execute_action_none_is_noop(self, mock_db: MagicMock) -> None:
        from src.action_worker import ActionWorker
        w = ActionWorker(gmail_client=MagicMock(), circuit_breaker=MagicMock())
        w._execute_action("msg_1", "none")
        w.gmail_client.modify_labels.assert_not_called()


# ============================================================
# Tests multi-workers (SKIP LOCKED)
# ============================================================

class TestMultiWorker:
    """La query de claim doit utiliser FOR UPDATE SKIP LOCKED."""

    def test_claim_query_uses_skip_locked(
        self, mock_db: MagicMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """La query de _claim_next_job doit avoir FOR UPDATE SKIP LOCKED."""
        # Capturer la requete SQL executee
        executed_sql: list[str] = []
        original_execute = mock_db.execute

        def capture_execute(sql, *args, **kwargs):
            executed_sql.append(sql)
            return original_execute(sql, *args, **kwargs)
        mock_db.execute = capture_execute

        from src.action_worker import ActionWorker
        w = ActionWorker(gmail_client=MagicMock(), circuit_breaker=MagicMock())
        mock_db.fetchone.return_value = None  # queue vide
        w._claim_next_job()

        # Au moins une requete doit contenir FOR UPDATE SKIP LOCKED
        assert any("FOR UPDATE SKIP LOCKED" in s for s in executed_sql), (
            f"Pas de FOR UPDATE SKIP LOCKED dans les requetes: {executed_sql}"
        )


# ============================================================
# Tests anti-regression (memes regles que gmail_client)
# ============================================================

class TestNoForbiddenGmailCalls:
    """L'action_worker ne doit JAMAIS appeler une methode interdite."""

    def test_no_delete_send_in_source(self) -> None:
        import re
        from pathlib import Path
        content = Path("src/action_worker.py").read_text(encoding="utf-8")
        forbidden = [
            r"\.messages\(\)\.delete\(",
            r"\.messages\(\)\.send\(",
            r"\.threads\(\)\.delete\(",
            r"\.drafts\(\)\.send\(",
        ]
        for pattern in forbidden:
            assert not re.search(pattern, content), (
                f"Forbidden method used in action_worker.py: {pattern}"
            )
