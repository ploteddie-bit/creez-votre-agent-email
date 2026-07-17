"""Tests pour src.decider - moteur P2 avec garde-fous."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_db(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock complet de get_connection."""
    import src.db as db_mod
    import src.decider as dec_mod
    import src.recommender as rec_mod
    import src.action_worker as aw_mod
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
    monkeypatch.setattr(db_mod, "get_connection", lambda *a, **kw: ctx)
    monkeypatch.setattr(dec_mod, "get_connection", lambda *a, **kw: ctx)
    monkeypatch.setattr(rec_mod, "get_connection", lambda *a, **kw: ctx)
    monkeypatch.setattr(aw_mod, "get_connection", lambda *a, **kw: ctx)
    monkeypatch.setattr(obs_mod, "get_connection", lambda *a, **kw: ctx)
    return mock_cursor


@pytest.fixture
def decider(mock_db: MagicMock) -> "Decider":
    """Decider avec mocks pour les dependances."""
    from src.decider import Decider
    from src.models import MailDecision
    d = Decider(
        recommender=MagicMock(),
        action_worker=MagicMock(),
        p2_enabled=True,  # ON pour tester
    )
    d.action_worker.enqueue_action = MagicMock(return_value=42)
    return d


# ============================================================
# Tests : kill-switch et mode Vacances
# ============================================================

class TestKillSwitch:
    def test_p2_disabled_means_no_auto_execute(self, mock_db: MagicMock) -> None:
        from src.decider import Decider
        from src.models import MailDecision
        d = Decider(p2_enabled=False)  # Kill-switch ON
        decision = MailDecision(
            classification="newsletter", executable_operation="archive",
            confidence=0.9, reason="test",
        )
        assert d.should_auto_execute(decision, {"sender_domain": "known.com"}) is False

    def test_p2_enabled_default_is_false(
        self, mock_db: MagicMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Le kill-switch par defaut est ON (P2 desactive) pour la securite."""
        from src.config import reset_settings
        reset_settings()
        from src.decider import Decider
        d = Decider()
        assert d.p2_enabled is False  # settings.p2.enabled = False par defaut


class TestVacationMode:
    def test_vacation_mode_forces_p1(
        self, mock_db: MagicMock, decider,
    ) -> None:
        from src.models import MailDecision
        decider.vacation_mode = True
        decision = MailDecision(
            classification="newsletter", executable_operation="archive",
            confidence=0.9, reason="test",
        )
        assert decider.should_auto_execute(
            decision, {"sender_domain": "known.com"}
        ) is False


# ============================================================
# Tests : mots-cles critiques
# ============================================================

class TestCriticalKeywords:
    def test_critical_keyword_blocks_auto_execute(self, decider, mock_db) -> None:
        from src.models import MailDecision
        decision = MailDecision(
            classification="receipt", executable_operation="archive",
            confidence=0.9, reason="should not be auto-archived",
        )
        email = {
            "sender_domain": "bank.com",
            "subject": "Votre facture EDF",
            "body_snippet": "Cher client, voici votre facture",
        }
        # bank.com sera considere connu car on a mocke fetchone qui retourne None
        # -> _is_known_sender retourne False, et on teste la securite critique
        # Patchons _is_known_sender pour retourner True
        decider._is_known_sender = MagicMock(return_value=True)
        decider._get_recent_heuristic_confidence = MagicMock(return_value=0.5)
        decider._today_actions_count = MagicMock(return_value=0)
        decider.get_window_precision = MagicMock(return_value=1.0)
        # meme avec precision haute, le mot-cle critique doit bloquer
        assert decider.should_auto_execute(decision, email) is False

    def test_no_critical_keyword_allows_proceed(self, decider, mock_db) -> None:
        from src.models import MailDecision
        decision = MailDecision(
            classification="newsletter", executable_operation="archive",
            confidence=0.9, reason="normal newsletter",
        )
        email = {
            "sender_domain": "newsletter.com",
            "subject": "Weekly tech digest",
            "body_snippet": "This week in tech...",
        }
        decider._is_known_sender = MagicMock(return_value=True)
        decider._get_recent_heuristic_confidence = MagicMock(return_value=0.8)
        decider._today_actions_count = MagicMock(return_value=0)
        decider.get_window_precision = MagicMock(return_value=0.98)
        decider._p2_volume_guards_ok = MagicMock(return_value=True)
        # archive threshold = 0.95, precision 0.98 -> OK
        assert decider.should_auto_execute(decision, email) is True


# ============================================================
# Tests : sender inconnu
# ============================================================

class TestUnknownSender:
    def test_unknown_domain_blocks_auto_execute(self, decider) -> None:
        from src.models import MailDecision
        decision = MailDecision(
            classification="newsletter", executable_operation="archive",
            confidence=0.9, reason="test",
        )
        # Sender unknown
        decider._is_known_sender = MagicMock(return_value=False)
        email = {"sender_domain": "first-time.com", "subject": "Hi", "body_snippet": "..."}
        assert decider.should_auto_execute(decision, email) is False


# ============================================================
# Tests : LLM confidence
# ============================================================

class TestLLMConfidence:
    def test_low_confidence_blocks_auto_execute(self, decider) -> None:
        from src.models import MailDecision
        decision = MailDecision(
            classification="unknown", executable_operation="archive",
            confidence=0.2, reason="unsure",  # < 0.3
        )
        decider._is_known_sender = MagicMock(return_value=True)
        decider._get_recent_heuristic_confidence = MagicMock(return_value=0.2)
        decider._today_actions_count = MagicMock(return_value=0)
        decider.get_window_precision = MagicMock(return_value=0.99)
        # LLM conf 0.2 < 0.3 -> bloque
        assert decider.should_auto_execute(decision, {
            "sender_domain": "x.com", "subject": "X", "body_snippet": "X"
        }) is False


# ============================================================
# Tests : divergence LLM/heuristic
# ============================================================

class TestDivergence:
    def test_high_divergence_blocks(self, decider) -> None:
        from src.models import MailDecision
        decision = MailDecision(
            classification="newsletter", executable_operation="archive",
            confidence=0.9, reason="sure",
        )
        decider._is_known_sender = MagicMock(return_value=True)
        decider._get_recent_heuristic_confidence = MagicMock(return_value=0.1)
        decider._today_actions_count = MagicMock(return_value=0)
        decider.get_window_precision = MagicMock(return_value=0.99)
        # |0.9 - 0.1| = 0.8 > 0.3 -> bloque
        assert decider.should_auto_execute(decision, {
            "sender_domain": "x.com", "subject": "X", "body_snippet": "X"
        }) is False


# ============================================================
# Tests : quota quotidien
# ============================================================

class TestDailyQuota:
    def test_quota_reached_blocks(self, decider) -> None:
        from src.models import MailDecision
        decision = MailDecision(
            classification="newsletter", executable_operation="archive",
            confidence=0.9, reason="test",
        )
        decider._is_known_sender = MagicMock(return_value=True)
        decider._get_recent_heuristic_confidence = MagicMock(return_value=0.8)
        decider._today_actions_count = MagicMock(return_value=20)  # = max
        decider.get_window_precision = MagicMock(return_value=0.99)
        # Quota atteint -> bloque
        assert decider.should_auto_execute(decision, {
            "sender_domain": "x.com", "subject": "X", "body_snippet": "X"
        }) is False


# ============================================================
# Tests : precision fenetre glissante
# ============================================================

class TestWindowPrecision:
    def test_low_precision_blocks_action(self, decider) -> None:
        from src.models import MailDecision
        decision = MailDecision(
            classification="newsletter", executable_operation="archive",
            confidence=0.9, reason="test",
        )
        decider._is_known_sender = MagicMock(return_value=True)
        decider._get_recent_heuristic_confidence = MagicMock(return_value=0.8)
        decider._today_actions_count = MagicMock(return_value=0)
        # precision archive < 0.95 (seuil) -> bloque
        decider.get_window_precision = MagicMock(return_value=0.80)
        assert decider.should_auto_execute(decision, {
            "sender_domain": "x.com", "subject": "X", "body_snippet": "X"
        }) is False

    def test_above_threshold_passes(self, decider) -> None:
        from src.models import MailDecision
        decision = MailDecision(
            classification="newsletter", executable_operation="archive",
            confidence=0.9, reason="test",
        )
        decider._is_known_sender = MagicMock(return_value=True)
        decider._get_recent_heuristic_confidence = MagicMock(return_value=0.8)
        decider._today_actions_count = MagicMock(return_value=0)
        # precision 0.98 > 0.95 (seuil archive) -> OK
        decider.get_window_precision = MagicMock(return_value=0.98)
        decider._p2_volume_guards_ok = MagicMock(return_value=True)
        assert decider.should_auto_execute(decision, {
            "sender_domain": "x.com", "subject": "X", "body_snippet": "X"
        }) is True


# ============================================================
# Tests : corrections consecutives
# ============================================================

class TestConsecutiveRejections:
    def test_three_consecutive_rejections_disables(self, decider) -> None:
        from src.models import MailDecision
        decider._is_known_sender = MagicMock(return_value=True)
        decider._get_recent_heuristic_confidence = MagicMock(return_value=0.8)
        decider._today_actions_count = MagicMock(return_value=0)
        decider.get_window_precision = MagicMock(return_value=0.99)
        # 3 corrections consecutives sur 'archive'
        decider._consecutive_rejections["archive"] = 3
        decision = MailDecision(
            classification="newsletter", executable_operation="archive",
            confidence=0.9, reason="test",
        )
        assert decider.should_auto_execute(decision, {
            "sender_domain": "x.com", "subject": "X", "body_snippet": "X"
        }) is False

    def test_approval_resets_consecutive_counter(self, decider) -> None:
        decider._consecutive_rejections["archive"] = 3
        decider.record_user_correction("msg_1", "archive", was_correct=True)
        assert decider._consecutive_rejections["archive"] == 0

    def test_rejection_increments_counter(self, decider) -> None:
        decider._consecutive_rejections["archive"] = 0
        decider.record_user_correction("msg_1", "archive", was_correct=False)
        assert decider._consecutive_rejections["archive"] == 1


# ============================================================
# Tests : auto_execute
# ============================================================

class TestAutoExecute:
    def test_auto_execute_returns_queue_id_when_authorized(self, decider) -> None:
        from src.models import MailDecision
        decider.should_auto_execute = MagicMock(return_value=True)
        decision = MailDecision(
            classification="newsletter", executable_operation="archive",
            confidence=0.9, reason="test",
        )
        result = decider.auto_execute("msg_1", decision)
        assert result == 42  # l'ID retourne par l'enqueue mock
        decider.action_worker.enqueue_action.assert_called_once_with(
            email_id="msg_1", operation="archive",
        )

    def test_auto_execute_returns_none_when_not_authorized(self, decider) -> None:
        from src.models import MailDecision
        decider.should_auto_execute = MagicMock(return_value=False)
        decision = MailDecision(
            classification="newsletter", executable_operation="archive",
            confidence=0.9, reason="test",
        )
        result = decider.auto_execute("msg_1", decision)
        assert result is None
        decider.action_worker.enqueue_action.assert_not_called()

    def test_auto_execute_skips_none_operation(self, decider) -> None:
        from src.models import MailDecision
        decider.should_auto_execute = MagicMock(return_value=True)
        decision = MailDecision(
            classification="personal", executable_operation="none",
            confidence=0.9, reason="test",
        )
        result = decider.auto_execute("msg_1", decision)
        assert result is None


# ============================================================
# Tests : snapshot
# ============================================================

class TestSnapshot:
    def test_snapshot_includes_all_state(self, decider) -> None:
        decider._today_actions_count = MagicMock(return_value=5)
        decider.get_window_stats = MagicMock(return_value={
            "archive": {"precision": 0.98, "threshold": 0.95,
                       "approved": 50, "rejected": 1, "pending": 0,
                       "above_threshold": True, "consecutive_rejections": 0},
        })
        snap = decider.snapshot()
        assert "p2_enabled" in snap
        assert "vacation_mode" in snap
        assert "max_daily_actions" in snap
        assert "today_actions" in snap
        assert "window_size" in snap
        assert "precision_thresholds" in snap
        assert "window_stats" in snap
        assert snap["p2_enabled"] is True
        assert snap["today_actions"] == 5


# ============================================================
# Tests anti-regression
# ============================================================

class TestP2DefaultsAreSafe:
    """Le P2 ne doit JAMAIS etre actif par defaut."""

    def test_p2_disabled_in_default_settings(
        self, mock_db: MagicMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.config import reset_settings
        reset_settings()
        from src.decider import Decider
        d = Decider()
        assert d.p2_enabled is False, "P2 doit etre OFF par defaut"

    def test_p2_disabled_blocks_all_actions(
        self, mock_db: MagicMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Meme avec tous les mocks favorables, P2 off doit bloquer."""
        from src.config import reset_settings
        reset_settings()
        from src.decider import Decider
        from src.models import MailDecision
        d = Decider()
        d._is_known_sender = MagicMock(return_value=True)
        d._get_recent_heuristic_confidence = MagicMock(return_value=0.99)
        d._today_actions_count = MagicMock(return_value=0)
        d.get_window_precision = MagicMock(return_value=1.0)
        decision = MailDecision(
            classification="newsletter", executable_operation="archive",
            confidence=0.99, reason="perfect",
        )
        # P2 off -> blocked
        assert d.should_auto_execute(decision, {
            "sender_domain": "x.com", "subject": "X", "body_snippet": "X"
        }) is False


# ============================================================
# Tests : gardes de volume P2 (SPEC 7.4) — E2
# ============================================================

class TestVolumeGuards:
    """P2 interdit tant que l'historique est insuffisant."""

    def _favorable_decision(self):
        from src.models import MailDecision
        return MailDecision(
            classification="newsletter", executable_operation="archive",
            confidence=0.9, reason="test",
        )

    def _mock_other_guards_ok(self, decider) -> None:
        decider._is_known_sender = MagicMock(return_value=True)
        decider._get_recent_heuristic_confidence = MagicMock(return_value=0.9)
        decider._today_actions_count = MagicMock(return_value=0)
        decider.get_window_precision = MagicMock(return_value=0.99)

    def test_cold_start_blocks_auto_execute(self, decider, mock_db) -> None:
        """0 mail, 0 proposition -> P2 ne s'auto-execute JAMAIS (E2)."""
        # mock_db.fetchone -> None -> comptages a 0 -> garde bloquee
        self._mock_other_guards_ok(decider)
        assert decider.should_auto_execute(
            self._favorable_decision(),
            {"sender_domain": "x.com", "subject": "X", "body_snippet": "X"},
        ) is False

    def test_not_enough_emails_blocks(self, decider, mock_db) -> None:
        """Moins de 2000 emails ingeres -> garde bloquee."""
        mock_db.fetchone.return_value = (100,)  # 100 < 2000
        assert decider._p2_volume_guards_ok() is False

    def test_not_enough_p1_proposals_blocks(self, decider, mock_db) -> None:
        """Emails OK mais moins de 500 propositions P1 -> bloque."""
        mock_db.fetchone.side_effect = [
            (decider.MIN_EMAILS_FOR_P2,),  # emails OK
            (42,),                          # propositions P1 insuffisantes
        ]
        assert decider._p2_volume_guards_ok() is False

    def test_reverted_archive_blocks(self, decider, mock_db) -> None:
        """Un archivage P2 revoque par l'utilisateur -> bloque."""
        mock_db.fetchone.side_effect = [
            (decider.MIN_EMAILS_FOR_P2,),
            (decider.MIN_P1_PROPOSALS_FOR_P2,),
            (2,),  # 2 archivages revoques sur la fenetre
        ]
        assert decider._p2_volume_guards_ok() is False

    def test_all_volumes_ok_passes(self, decider, mock_db) -> None:
        """2000+ emails, 500+ propositions, 0 revoque -> garde OK."""
        mock_db.fetchone.side_effect = [
            (decider.MIN_EMAILS_FOR_P2,),
            (decider.MIN_P1_PROPOSALS_FOR_P2,),
            (0,),
        ]
        assert decider._p2_volume_guards_ok() is True

    def test_queries_are_bounded(self, decider, mock_db) -> None:
        """Les comptages utilisent LIMIT (pas de COUNT sur table entiere)."""
        mock_db.fetchone.side_effect = [
            (decider.MIN_EMAILS_FOR_P2,),
            (decider.MIN_P1_PROPOSALS_FOR_P2,),
            (0,),
        ]
        decider._p2_volume_guards_ok()
        for call in mock_db.execute.call_args_list:
            sql = " ".join(call.args[0].split())
            assert "LIMIT %s" in sql


# ============================================================
# Tests : cold start precision = 0.0 (E2)
# ============================================================

class TestColdStartPrecision:
    """Sans donnees, la precision mesuree est 0.0 (jamais 1.0)."""

    def test_no_data_returns_zero(self, decider, mock_db) -> None:
        mock_db.fetchone.return_value = None
        assert decider.get_window_precision("archive") == 0.0

    def test_empty_window_returns_zero(self, decider, mock_db) -> None:
        mock_db.fetchone.return_value = (0, 0, 0)  # total = 0
        assert decider.get_window_precision("archive") == 0.0

    def test_no_rated_decisions_returns_zero(self, decider, mock_db) -> None:
        mock_db.fetchone.return_value = (0, 0, 7)  # que du pending
        assert decider.get_window_precision("archive") == 0.0

    def test_rated_decisions_compute_ratio(self, decider, mock_db) -> None:
        mock_db.fetchone.return_value = (19, 1, 20)
        assert decider.get_window_precision("archive") == 0.95


# ============================================================
# Tests : SQL du _mark_decision_pending (E3)
# ============================================================

class TestMarkDecisionPendingSql:
    """PostgreSQL refuse ORDER BY/LIMIT dans un UPDATE : sous-requete."""

    def test_update_uses_subquery_on_id(self, decider, mock_db) -> None:
        from src.models import MailDecision
        decision = MailDecision(
            classification="newsletter", executable_operation="archive",
            confidence=0.9, reason="test",
        )
        decider._mark_decision_pending("msg_1", decision)
        sql = " ".join(mock_db.execute.call_args.args[0].split())
        # La partie UPDATE (avant la sous-requete) ne doit contenir
        # ni ORDER BY ni LIMIT
        outer = sql.split("WHERE id =")[0]
        assert "ORDER BY" not in outer
        assert "LIMIT" not in outer
        # La sous-requete cible la cle primaire de la derniere decision
        assert "SELECT id FROM decision_journal" in sql
        assert "WHERE email_id = %s" in sql
        assert mock_db.execute.call_args.args[1] == ("msg_1",)
