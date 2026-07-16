"""Tests pour src.action_detector — delta de labels + stockage."""
from __future__ import annotations

import contextlib

from src.action_detector import ActionDetector


# ----------------------------------------------------------------
# Détection pure (toutes les règles de la SPEC agent 4)
# ----------------------------------------------------------------

def test_detect_archived() -> None:
    """INBOX retiré, pas de TRASH → archived."""
    actions = ActionDetector.detect_actions(["INBOX", "UNREAD"], ["UNREAD"])
    assert "archived" in actions
    assert "deleted" not in actions


def test_detect_deleted_from_inbox() -> None:
    """INBOX → TRASH → deleted (pas archived)."""
    actions = ActionDetector.detect_actions(["INBOX"], ["TRASH"])
    assert actions == ["deleted"]


def test_detect_deleted_from_archive() -> None:
    """TRASH ajouté à un mail déjà archivé → deleted."""
    actions = ActionDetector.detect_actions(["STARRED"], ["STARRED", "TRASH"])
    assert "deleted" in actions


def test_detect_read() -> None:
    """UNREAD retiré → read."""
    actions = ActionDetector.detect_actions(["INBOX", "UNREAD"], ["INBOX"])
    assert actions == ["read"]


def test_detect_unread() -> None:
    """UNREAD rajouté sur un mail connu → unread."""
    actions = ActionDetector.detect_actions(["INBOX"], ["INBOX", "UNREAD"])
    assert actions == ["unread"]


def test_detect_starred_and_unstarred() -> None:
    assert ActionDetector.detect_actions(["INBOX"], ["INBOX", "STARRED"]) == ["starred"]
    assert ActionDetector.detect_actions(["INBOX", "STARRED"], ["INBOX"]) == ["unstarred"]


def test_detect_new_mail() -> None:
    """Première observation (état vide) avec INBOX → new_mail."""
    actions = ActionDetector.detect_actions(None, ["INBOX", "UNREAD"])
    assert "new_mail" in actions
    assert "unread" not in actions  # UNREAD initial ≠ marqué non-lu


def test_detect_no_change() -> None:
    """Aucune transition → liste vide."""
    assert ActionDetector.detect_actions(["INBOX", "UNREAD"], ["INBOX", "UNREAD"]) == []
    assert ActionDetector.detect_actions(None, None) == []


def test_detect_multiple_transitions() -> None:
    """Lu + archivé en une seule passe (cas réel fréquent)."""
    actions = ActionDetector.detect_actions(["INBOX", "UNREAD"], [])
    assert actions == ["archived", "read"]


# ----------------------------------------------------------------
# Stockage (connexion simulée — pas de PostgreSQL requis)
# ----------------------------------------------------------------

class _FakeCursor:
    def __init__(self, log: list) -> None:
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def executemany(self, sql: str, rows: list) -> None:
        self.log.append((sql, rows))


class _FakeConn:
    def __init__(self, log: list) -> None:
        self.log = log
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.log)

    def commit(self) -> None:
        self.committed = True


def test_store_actions_inserts_all(monkeypatch) -> None:
    """Chaque action détectée produit une ligne dans email_actions."""
    import src.action_detector as module

    log: list = []
    conn = _FakeConn(log)

    @contextlib.contextmanager
    def fake_get_connection():
        yield conn

    monkeypatch.setattr(module, "get_connection", fake_get_connection)

    detector = ActionDetector()
    inserted = detector.store_actions("msg_1", ["read", "archived"])

    assert inserted == 2
    assert conn.committed is True
    sql, rows = log[0]
    assert "INSERT INTO email_actions" in sql
    assert rows == [("msg_1", "read", "poll_delta"), ("msg_1", "archived", "poll_delta")]


def test_store_actions_empty_noop(monkeypatch) -> None:
    """Aucune action → aucune requête (pas de connexion ouverte)."""
    detector = ActionDetector()
    assert detector.store_actions("msg_1", []) == 0


def test_detect_and_store_pipeline(monkeypatch) -> None:
    """Le pipeline détecte puis stocke en un appel."""
    import src.action_detector as module

    log: list = []

    @contextlib.contextmanager
    def fake_get_connection():
        yield _FakeConn(log)

    monkeypatch.setattr(module, "get_connection", fake_get_connection)

    detector = ActionDetector()
    actions = detector.detect_and_store("msg_2", ["INBOX", "UNREAD"], ["INBOX"])
    assert actions == ["read"]
    assert len(log) == 1
