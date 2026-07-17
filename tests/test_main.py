"""Tests pour src.main — wiring du daemon (E4 : services persistants).

Avant E4, chaque cycle recreait GmailObserver/Embedder/ActionWorker :
le CircuitBreaker (quota Gmail) etait remis a zero toutes les 60s.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class TestBuildDaemonServices:
    """_build_daemon_services instancie chaque service une seule fois."""

    def test_instantiates_each_service_once(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        created: list[str] = []
        import src.action_worker as aw_mod
        import src.embedder as emb_mod
        import src.observer as obs_mod

        def _make(name: str):
            def _factory(*a, **k):
                created.append(name)
                return MagicMock(name=name)
            return _factory

        monkeypatch.setattr(obs_mod, "GmailObserver", _make("observer"))
        monkeypatch.setattr(emb_mod, "Embedder", _make("embedder"))
        monkeypatch.setattr(aw_mod, "ActionWorker", _make("worker"))

        from src.main import _build_daemon_services
        services = _build_daemon_services()

        assert set(services.keys()) == {"observer", "embedder", "worker"}
        assert created == ["observer", "embedder", "worker"]


class TestRunOneCycleReusesServices:
    """_run_one_cycle consomme les instances fournies sans les recreer."""

    def _services(self) -> dict:
        obs = MagicMock()
        obs.get_sync_state.return_value = {"last_history_id": "999"}
        obs.sync_delta.return_value = 3
        emb = MagicMock()
        emb.embed_unprocessed.return_value = 2
        aw = MagicMock()
        aw.run.return_value = 1
        return {"observer": obs, "embedder": emb, "worker": aw}

    def test_two_cycles_reuse_same_instances(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import src.recommender as rec_mod
        monkeypatch.setattr(
            rec_mod, "process_new_emails", MagicMock(return_value=0),
        )

        from src.main import _run_one_cycle
        services = self._services()
        c1 = _run_one_cycle(services)
        c2 = _run_one_cycle(services)

        expected = {"synced": 3, "processed": 0, "embedded": 2, "executed": 1}
        assert c1 == expected
        assert c2 == expected
        # Les MEMES instances ont servi les 2 cycles (E4)
        assert services["observer"].sync_delta.call_count == 2
        assert services["embedder"].embed_unprocessed.call_count == 2
        assert services["worker"].run.call_count == 2

    def test_full_sync_when_no_history_id(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import src.recommender as rec_mod
        monkeypatch.setattr(
            rec_mod, "process_new_emails", MagicMock(return_value=0),
        )
        from src.main import _run_one_cycle
        services = self._services()
        services["observer"].get_sync_state.return_value = None
        services["observer"].sync_full.return_value = 7
        counters = _run_one_cycle(services)
        assert counters["synced"] == 7
        services["observer"].sync_full.assert_called_once()
        services["observer"].sync_delta.assert_not_called()

    def test_cycle_survives_service_failure(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Un service en panne ne casse pas les autres etapes du cycle."""
        import src.recommender as rec_mod
        monkeypatch.setattr(
            rec_mod, "process_new_emails", MagicMock(return_value=0),
        )
        from src.main import _run_one_cycle
        services = self._services()
        services["observer"].sync_delta.side_effect = RuntimeError("gmail down")
        counters = _run_one_cycle(services)
        assert counters["synced"] == 0
        assert counters["embedded"] == 2
        assert counters["executed"] == 1


class TestDaemonStartup:
    """cmd_daemon echoue proprement si les services ne s'initialisent pas."""

    def test_daemon_returns_error_if_services_fail(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.config import reset_settings
        reset_settings()
        import src.main as main_mod
        monkeypatch.setattr(
            main_mod, "_build_daemon_services",
            MagicMock(side_effect=RuntimeError("boom")),
        )
        rc = main_mod.cmd_daemon(MagicMock())
        assert rc == 1
