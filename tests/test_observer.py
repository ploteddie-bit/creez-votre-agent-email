"""Tests pour src.observer - GmailObserver + CircuitBreaker (avec mocks)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest


# ============================================================
# Tests CircuitBreaker
# ============================================================

class TestCircuitBreaker:
    """Tests du circuit-breaker sans dependance externe."""

    def test_initial_state_is_clean(self) -> None:
        """Au demarrage, le compteur est a zero et pas en pause."""
        from src.observer import CircuitBreaker
        cb = CircuitBreaker(
            quota_costs={"history.list": 1},
            quota_per_user_per_day=100,
            quota_threshold_pct=0.8,
        )
        assert cb.quota_used_today == 0
        assert cb.paused_until is None
        assert cb.can_proceed() is True

    def test_register_call_increments_quota(self) -> None:
        from src.observer import CircuitBreaker
        cb = CircuitBreaker(
            quota_costs={"history.list": 2000},
            quota_per_user_per_day=1000000,
            quota_threshold_pct=0.8,
        )
        cb.register_call("history.list")
        assert cb.quota_used_today == 2000
        cb.register_call("history.list")
        assert cb.quota_used_today == 4000

    def test_unknown_method_costs_one_unit(self) -> None:
        """Une methode inconnue coute 1 unite (defaut securitaire)."""
        from src.observer import CircuitBreaker
        cb = CircuitBreaker(
            quota_costs={},
            quota_per_user_per_day=1000,
        )
        cb.register_call("unknown.method")
        assert cb.quota_used_today == 1

    def test_pause_when_quota_exceeds_threshold(self) -> None:
        """Au-dela de 80% du quota, on passe en pause."""
        from src.observer import CircuitBreaker
        cb = CircuitBreaker(
            quota_costs={"x.list": 1},
            quota_per_user_per_day=10,
            quota_threshold_pct=0.8,
            pause_seconds=60,
        )
        # 8 appels = 8/10 = 80% (egal au seuil, ne pause pas)
        for _ in range(8):
            cb.register_call("x.list")
        assert cb.paused_until is None
        # 9eme appel = 9/10 = 90% > 80%, pause !
        cb.register_call("x.list")
        assert cb.paused_until is not None
        # can_proceed leve QuotaExceeded
        from src.observer import QuotaExceeded
        with pytest.raises(QuotaExceeded):
            cb.can_proceed()

    def test_pause_clears_after_expiration(self) -> None:
        """Une fois la pause expiree, on peut repartir."""
        from src.observer import CircuitBreaker
        cb = CircuitBreaker(
            quota_costs={"x.list": 100},
            quota_per_user_per_day=100,
            quota_threshold_pct=0.5,
            pause_seconds=0,  # expire tout de suite
        )
        cb.register_call("x.list")  # 100/100 = 100% > 50%
        assert cb.paused_until is not None
        # La pause a dure 0s, donc deja expiree
        cb.can_proceed()  # ne leve pas
        assert cb.paused_until is None

    def test_pause_when_rate_exceeds_max_per_minute(self) -> None:
        """Plus de N appels en 60s = pause."""
        from src.observer import CircuitBreaker
        cb = CircuitBreaker(
            quota_costs={"x.list": 1},
            quota_per_user_per_day=10000,
            quota_threshold_pct=0.99,  # eleve pour ne pas declencher par quota
            max_messages_per_minute=5,
        )
        for _ in range(6):
            cb.register_call("x.list")
        assert cb.paused_until is not None

    def test_register_retry_increments_counter(self) -> None:
        from src.observer import CircuitBreaker
        cb = CircuitBreaker()
        assert cb.retries == 0
        cb.register_retry()
        cb.register_retry()
        assert cb.retries == 2

    def test_snapshot_returns_dashboard_dict(self) -> None:
        from src.observer import CircuitBreaker
        cb = CircuitBreaker(
            quota_costs={"x.list": 100},
            quota_per_user_per_day=1000,
            quota_threshold_pct=0.8,
        )
        cb.register_call("x.list")
        snap = cb.snapshot()
        assert "quota_used_today" in snap
        assert "quota_pct" in snap
        assert "calls_per_minute" in snap
        assert "retries" in snap
        assert "paused" in snap
        assert snap["quota_used_today"] == 100
        assert snap["quota_pct"] == 10.0
        assert snap["paused"] is False

    def test_quota_resets_on_new_day(self) -> None:
        """Quand on change de jour, le compteur repart a zero."""
        from src.observer import CircuitBreaker
        cb = CircuitBreaker(
            quota_costs={"x.list": 1},
            quota_per_user_per_day=100,
        )
        cb.register_call("x.list")
        assert cb.quota_used_today == 1
        # Simuler qu'on est le lendemain
        cb.quota_window_start = datetime.now(timezone.utc) - timedelta(days=2)
        cb.register_call("x.list")
        assert cb.quota_used_today == 1  # reset puis +1


# ============================================================
# Tests GmailObserver (avec mocks)
# ============================================================


@pytest.fixture
def mock_db(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock complet de get_connection (pas de vraie DB)."""
    import src.db as db_mod
    import src.observer as obs_mod

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

    # Patch sur les 2 modules (observer importe get_connection)
    monkeypatch.setattr(db_mod, "get_connection", lambda *a, **kw: ctx)
    monkeypatch.setattr(obs_mod, "get_connection", lambda *a, **kw: ctx)
    return mock_cursor


@pytest.fixture
def mock_gmail() -> MagicMock:
    """Mock du GmailClient."""
    client = MagicMock()
    # list_messages retourne maintenant (messages, next_page_token)
    client.list_messages.return_value = ([], None)
    client.list_history.return_value = []
    client.get_message.return_value = {"id": "msg_1", "payload": {"headers": []}}
    client.list_labels.return_value = []
    return client


@pytest.fixture
def mock_ingester() -> MagicMock:
    ing = MagicMock()
    ing.ingest_email.return_value = True
    return ing


class TestGmailObserverSync:
    """Tests de la machine a etat de sync (full + delta + 404 fallback)."""

    @pytest.fixture
    def observer(self, mock_gmail, mock_ingester, mock_db) -> "GmailObserver":
        from src.observer import GmailObserver
        return GmailObserver(
            gmail_client=mock_gmail,
            ingester=mock_ingester,
            account_id="test_account",
        )

    def test_ensure_ia_review_label_creates_if_missing(
        self, observer: "GmailObserver", mock_gmail: MagicMock,
    ) -> None:
        """Si IA-Review n'existe pas, on le cree et on stocke son ID."""
        # Mock du service pour le create
        mock_service = MagicMock()
        mock_labels = mock_service.users().labels()
        mock_labels.create.return_value.execute.return_value = {
            "id": "Label_42", "name": "IA-Review",
        }
        mock_gmail._get_service.return_value = mock_service
        # list_labels ne retourne rien (label absent)
        mock_gmail.list_labels.return_value = [
            {"id": "INBOX", "name": "INBOX"},
        ]
        label_id = observer.ensure_ia_review_label()
        assert label_id == "Label_42"
        mock_labels.create.assert_called_once()
        # Verifie le scope du create
        call_kwargs = mock_labels.create.call_args.kwargs
        assert call_kwargs["userId"] == "me"
        assert call_kwargs["body"]["name"] == "IA-Review"

    def test_ensure_ia_review_label_uses_existing(
        self, observer: "GmailObserver", mock_gmail: MagicMock,
    ) -> None:
        """Si IA-Review existe deja, on utilise son ID (pas de create)."""
        mock_gmail.list_labels.return_value = [
            {"id": "INBOX", "name": "INBOX"},
            {"id": "Label_99", "name": "IA-Review"},
        ]
        label_id = observer.ensure_ia_review_label()
        assert label_id == "Label_99"
        # Pas d'appel create
        mock_gmail._get_service.assert_not_called()

    def test_get_label_id_returns_stored(
        self, observer: "GmailObserver", mock_gmail: MagicMock,
    ) -> None:
        """get_label_id ne fait PAS d'appel API, lit depuis la DB."""
        # Le mock de _get_label_id va chercher dans la DB (via get_connection)
        # Pour ce test, on verifie juste que l'API n'est pas appelée
        observer.get_label_id("INBOX")
        mock_gmail.list_labels.assert_not_called()

    def test_sync_full_consumes_all_pages(
        self, observer: "GmailObserver", mock_gmail: MagicMock, mock_db: MagicMock,
    ) -> None:
        """sync_full doit consommer TOUS les nextPageToken jusqu'a exhaustion."""
        # Mock : 3 pages, chacune avec un nextPageToken, puis None
        mock_gmail.list_messages.side_effect = [
            ([{"id": "msg_1"}], "page_token_2"),
            ([{"id": "msg_2"}, {"id": "msg_3"}], "page_token_3"),
            ([{"id": "msg_4"}], None),  # Derniere page
        ]
        # Mock _ingest_one pour eviter le parsing
        observer._ingest_one = MagicMock(return_value=True)

        ingested = observer.sync_full(max_results=10)
        assert ingested == 4
        # 3 appels a list_messages (3 pages)
        assert mock_gmail.list_messages.call_count == 3
        # Premier appel sans page_token, ensuite avec
        first_call_kwargs = mock_gmail.list_messages.call_args_list[0].kwargs
        assert "page_token" not in first_call_kwargs or first_call_kwargs["page_token"] is None
        second_call_kwargs = mock_gmail.list_messages.call_args_list[1].kwargs
        assert second_call_kwargs.get("page_token") == "page_token_2"

    def test_sync_full_terminates_on_no_next_token(
        self, observer: "GmailObserver", mock_gmail: MagicMock, mock_db: MagicMock,
    ) -> None:
        """sync_full se termine apres la premiere page si pas de nextPageToken."""
        mock_gmail.list_messages.return_value = ([{"id": "msg_1"}], None)
        observer._ingest_one = MagicMock(return_value=True)

        ingested = observer.sync_full(max_results=10)
        assert ingested == 1
        assert mock_gmail.list_messages.call_count == 1

    def test_sync_full_safety_max_pages(
        self, observer: "GmailObserver", mock_gmail: MagicMock, mock_db: MagicMock,
    ) -> None:
        """sync_full s'arrete au safety_max_pages pour eviter boucle infinie."""
        # Simuler 200 appels qui retournent toujours un token
        mock_gmail.list_messages.return_value = ([{"id": "msg_x"}], "next")
        observer._ingest_one = MagicMock(return_value=True)

        ingested = observer.sync_full(max_results=10)
        # Max 100 pages (safety)
        assert mock_gmail.list_messages.call_count <= 100
        # Warning dans les logs, mais on n'a pas plante


class TestGmailObserverHealth:
    """Tests du healthcheck (utilise par le dashboard)."""

    def test_health_empty_when_no_sync_state(
        self, mock_db, mock_gmail, mock_ingester,
    ) -> None:
        """Si pas de sync_state, health ne crash pas."""
        from src.observer import GmailObserver
        obs = GmailObserver(
            gmail_client=mock_gmail,
            ingester=mock_ingester,
            account_id="test_account",
        )
        h = obs.health()
        assert h["sync_state"] is None
        assert "circuit_breaker" in h
        assert "quota_used_today" in h["circuit_breaker"]


class TestGmailObserverForbiddenMethods:
    """L'observer ne doit JAMAIS appeler une methode Gmail interdite."""

    def test_observer_does_not_call_forbidden_methods_directly(self) -> None:
        """Scanne le code source de l'observer.

        Toute reference directe a users.messages.delete,
        users.messages.send, etc. doit etre absente.
        """
        import re
        from pathlib import Path

        src = Path("src/observer.py")
        content = src.read_text(encoding="utf-8")

        forbidden = [
            r"\.messages\(\)\.delete\(",
            r"\.threads\(\)\.delete\(",
            r"\.messages\(\)\.send\(",
            r"\.drafts\(\)\.send\(",
            r"\.drafts\(\)\.create\(",
            r"\.drafts\(\)\.update\(",
            r"\.drafts\(\)\.delete\(",
        ]
        violations: list[str] = []
        for pattern in forbidden:
            for match in re.finditer(pattern, content):
                line_start = content.rfind("\n", 0, match.start()) + 1
                line_end = content.find("\n", match.end())
                line = content[line_start:line_end if line_end > 0 else None].strip()
                violations.append(f"  {line}")

        assert not violations, (
            "Observer references forbidden Gmail methods:\n"
            + "\n".join(violations)
        )

    def test_observer_uses_gmail_client_wrapper(self) -> None:
        """L'observer doit passer par gmail_client.* et non par l'API directe.

        Toute reference a `service = build(...)` ou `from googleapiclient`
        dans observer.py doit etre absente.
        """
        from pathlib import Path
        content = Path("src/observer.py").read_text(encoding="utf-8")
        assert "googleapiclient" not in content
        assert "build(" not in content
