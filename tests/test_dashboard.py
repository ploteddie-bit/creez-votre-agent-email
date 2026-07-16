"""Tests pour src.dashboard - FastAPI app + endpoints + WebSocket."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def mock_db(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock complet de get_connection (pas de vraie DB)."""
    import src.db as db_mod
    import src.observer as obs_mod
    import src.decider as dec_mod
    import src.search as search_mod
    import src.recommender as rec_mod
    import src.action_worker as aw_mod

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (0,)
    mock_cursor.fetchall.return_value = []
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.__exit__.return_value = False
    mock_conn.commit.return_value = None

    ctx = MagicMock()
    ctx.__enter__.return_value = mock_conn
    ctx.__exit__.return_value = False
    for mod in (db_mod, obs_mod, dec_mod, search_mod, rec_mod, aw_mod):
        monkeypatch.setattr(mod, "get_connection", lambda *a, **kw: ctx)
    return mock_cursor


@pytest.fixture
def client(mock_db: MagicMock) -> TestClient:
    """Client de test FastAPI."""
    from src.dashboard import app
    return TestClient(app)


# ============================================================
# Tests : health
# ============================================================

class TestHealthEndpoint:
    def test_health_returns_200(self, client: TestClient) -> None:
        r = client.get("/api/health")
        assert r.status_code == 200
        data = r.json()
        assert "status" in data
        assert "timestamp" in data
        assert "checks" in data

    def test_health_includes_postgresql(self, client: TestClient) -> None:
        r = client.get("/api/health")
        data = r.json()
        assert "postgresql" in data["checks"]

    def test_health_includes_ollama(self, client: TestClient) -> None:
        r = client.get("/api/health")
        data = r.json()
        assert "ollama" in data["checks"]

    def test_health_includes_p2_kill_switch(self, client: TestClient) -> None:
        r = client.get("/api/health")
        data = r.json()
        assert "p2_enabled" in data
        assert "kill_switch" in data
        # Par defaut P2 desactive, kill_switch active (etat sur)
        assert data["p2_enabled"] is False
        assert data["kill_switch"] is True

    def test_health_csp_header(self, client: TestClient) -> None:
        r = client.get("/api/health")
        assert "Content-Security-Policy" in r.headers
        assert "default-src 'self'" in r.headers["Content-Security-Policy"]

    def test_health_x_frame_options(self, client: TestClient) -> None:
        r = client.get("/api/health")
        assert r.headers.get("X-Frame-Options") == "DENY"


# ============================================================
# Tests : emails
# ============================================================

class TestEmailsEndpoint:
    def test_list_emails_empty(self, client: TestClient, mock_db: MagicMock) -> None:
        mock_db.fetchone.return_value = (0,)
        r = client.get("/api/emails")
        assert r.status_code == 200
        data = r.json()
        assert "items" in data
        assert "total" in data
        assert "limit" in data
        assert "offset" in data

    def test_list_emails_pagination(self, client: TestClient, mock_db: MagicMock) -> None:
        r = client.get("/api/emails?limit=10&offset=20")
        assert r.status_code == 200
        data = r.json()
        assert data["limit"] == 10
        assert data["offset"] == 20

    def test_list_emails_limit_validation(self, client: TestClient) -> None:
        """Limit > 500 doit etre refuse."""
        r = client.get("/api/emails?limit=1000")
        assert r.status_code == 422

    def test_list_emails_negative_offset(self, client: TestClient) -> None:
        r = client.get("/api/emails?offset=-1")
        assert r.status_code == 422

    def test_get_email_404(self, client: TestClient, mock_db: MagicMock) -> None:
        mock_db.fetchone.return_value = None
        r = client.get("/api/emails/nonexistent")
        assert r.status_code == 404


# ============================================================
# Tests : decisions
# ============================================================

class TestDecisionsEndpoint:
    def test_list_decisions_empty(self, client: TestClient, mock_db: MagicMock) -> None:
        mock_db.fetchone.return_value = (0,)
        r = client.get("/api/decisions")
        assert r.status_code == 200
        data = r.json()
        assert data["items"] == []

    def test_approve_decision(self, client: TestClient, mock_db: MagicMock) -> None:
        mock_db.fetchone.return_value = ("msg_1", "archive")
        r = client.post("/api/decisions/42/approve")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["decision_id"] == 42
        assert data["executed_operation"] == "archive"

    def test_approve_decision_404(self, client: TestClient, mock_db: MagicMock) -> None:
        mock_db.fetchone.return_value = None
        r = client.post("/api/decisions/999/approve")
        assert r.status_code == 404

    def test_reject_decision(self, client: TestClient, mock_db: MagicMock) -> None:
        mock_db.fetchone.return_value = ("msg_2",)
        r = client.post("/api/decisions/43/reject")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True


# ============================================================
# Tests : config
# ============================================================

class TestConfigEndpoint:
    def test_get_config(self, client: TestClient) -> None:
        r = client.get("/api/config")
        assert r.status_code == 200
        data = r.json()
        assert "p2_enabled" in data
        assert "ollama_llm_model" in data
        assert "polling_interval_seconds" in data

    def test_update_config_p2_enabled(self, client: TestClient) -> None:
        r = client.put("/api/config", json={"p2_enabled": True})
        assert r.status_code == 200
        data = r.json()
        # Le changement est applique
        assert data["p2_enabled"] is True

    def test_update_config_vacation_mode(self, client: TestClient) -> None:
        r = client.put("/api/config", json={"vacation_mode": True})
        assert r.status_code == 200
        data = r.json()
        assert data["vacation_mode"] is True


# ============================================================
# Tests : pages statiques
# ============================================================

class TestStatsEndpoint:
    """Tests de l'endpoint /api/stats."""

    def test_stats_returns_dict(self, client: TestClient) -> None:
        r = client.get("/api/stats")
        assert r.status_code == 200
        data = r.json()
        assert "days" in data
        assert "repartition_actions" in data
        assert "top_senders" in data
        assert "actions_par_jour" in data
        assert "counters" in data

    def test_stats_default_window_30_days(self, client: TestClient) -> None:
        r = client.get("/api/stats")
        assert r.json()["days"] == 30

    def test_stats_custom_window(self, client: TestClient) -> None:
        r = client.get("/api/stats?days=7")
        assert r.json()["days"] == 7

    def test_stats_window_validation(self, client: TestClient) -> None:
        """days > 365 doit etre refuse."""
        r = client.get("/api/stats?days=1000")
        assert r.status_code == 422


class TestLearningEndpoint:
    """Tests de l'endpoint /api/learning."""

    def test_learning_returns_dict(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Mock Decider pour eviter les queries SQL
        from src.decider import Decider
        mock_decider = MagicMock()
        mock_decider.get_window_stats.return_value = {
            "archive": {"precision": 0.95, "threshold": 0.95, "approved": 10,
                       "rejected": 1, "pending": 0, "above_threshold": True,
                       "consecutive_rejections": 0},
        }
        mock_decider.p2_enabled = False
        monkeypatch.setattr(Decider, "get_window_stats", mock_decider.get_window_stats)
        r = client.get("/api/learning")
        assert r.status_code == 200
        data = r.json()
        assert "window" in data
        assert "window_stats" in data
        assert "top_domains_appris" in data
        assert "progression_p2" in data

    def test_learning_progression_p2_safe_default(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Par defaut, P2 est off, kill_switch ON."""
        from src.decider import Decider
        mock_decider = MagicMock()
        mock_decider.get_window_stats.return_value = {}
        mock_decider.p2_enabled = False
        monkeypatch.setattr(Decider, "get_window_stats", mock_decider.get_window_stats)
        r = client.get("/api/learning")
        prog = r.json()["progression_p2"]
        assert prog["p2_enabled"] is False


class TestSearchEndpoint:
    """Tests de l'endpoint /api/emails/search."""

    def test_search_requires_query(self, client: TestClient) -> None:
        """Une query vide doit etre refusee (min_length=1)."""
        r = client.get("/api/emails/search")
        assert r.status_code == 422

    def test_search_returns_results(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Mock HybridSearch pour eviter la query SQL
        from src.search import HybridSearch
        mock_hs = MagicMock()
        mock_hs.fulltext_search.return_value = [
            {"email_id": "msg_1", "subject": "Facture EDF",
             "sender_email": "edf@example.com", "rank": 0.5},
        ]
        monkeypatch.setattr(HybridSearch, "fulltext_search", mock_hs.fulltext_search)
        r = client.get("/api/emails/search?q=facture")
        assert r.status_code == 200
        data = r.json()
        assert "query" in data
        assert "results" in data
        assert "count" in data
        assert data["count"] >= 0


class TestRationaleInDecisions:
    """Chaque decision expose un rationale lisible."""

    def test_decision_has_rationale(self, client: TestClient) -> None:
        r = client.get("/api/decisions")
        assert r.status_code == 200
        items = r.json()["items"]
        # Si on a des decisions, chacune doit avoir un rationale
        for item in items:
            assert "rationale" in item, f"missing rationale in {item}"


class TestStaticPages:
    def test_index_page(self, client: TestClient) -> None:
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_mails_page(self, client: TestClient) -> None:
        r = client.get("/mails")
        assert r.status_code == 200

    def test_decisions_page(self, client: TestClient) -> None:
        r = client.get("/decisions")
        assert r.status_code == 200

    def test_config_page(self, client: TestClient) -> None:
        r = client.get("/config")
        assert r.status_code == 200

    def test_cours_page_redirects_when_missing(self, client: TestClient) -> None:
        """Si un fichier statique manque, on redirige vers /."""
        r = client.get("/this-page-does-not-exist-xyz")
        # Le middleware peut renvoyer 404 ou redirect, on accepte les 2
        assert r.status_code in (200, 404)

    def test_prompts_page_serves_real_html(self, client: TestClient) -> None:
        """/prompts sert prompts.html (et non le fallback meta-refresh)."""
        r = client.get("/prompts")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        # Le fichier prompts.html existe et contient du vrai contenu
        # (pas la page de fallback meta-refresh).
        assert "meta http-equiv" not in r.text

    def test_plan_page_serves_real_html(self, client: TestClient) -> None:
        """/plan sert plan.html (et non le fallback meta-refresh)."""
        r = client.get("/plan")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "meta http-equiv" not in r.text


# ============================================================
# Tests : securite
# ============================================================

class TestSecurityChecks:
    def test_bind_host_refuses_0_0_0_0(
        self, monkeypatch: pytest.MonkeyPatch, mock_db: MagicMock,
    ) -> None:
        """Si bind_host=0.0.0.0 dans la config, le lifespan leve une erreur."""
        from src.config import get_settings, reset_settings
        reset_settings()
        s = get_settings()
        s.dashboard.bind_host = "0.0.0.0"

        from src.dashboard import app
        with pytest.raises(RuntimeError, match="0.0.0.0 REFUSE"):
            with TestClient(app):
                pass

    def test_cors_not_enabled(self, client: TestClient) -> None:
        """CORS n'est PAS active (LAN seulement, pas de cross-origin)."""
        r = client.get("/api/health", headers={"Origin": "http://evil.com"})
        # Pas de header Access-Control-Allow-Origin
        assert "Access-Control-Allow-Origin" not in r.headers


# ============================================================
# Tests : WebSocket
# ============================================================

class TestWebSocket:
    def test_websocket_connect(self, client: TestClient) -> None:
        """Une connexion WebSocket reussit et peut recevoir un pong."""
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text("ping")
            data = ws.receive_text()
            assert data == "pong"

    def test_websocket_broadcast(self) -> None:
        """Le WebSocketManager broadcast a toutes les connexions actives."""
        from src.dashboard import WebSocketManager
        import asyncio

        mgr = WebSocketManager()
        ws1 = MagicMock()
        ws2 = MagicMock()
        # simulate async accept
        async def fake_send(msg):
            return None
        ws1.send_text = fake_send
        ws2.send_text = fake_send
        mgr.active.add(ws1)
        mgr.active.add(ws2)

        async def run():
            await mgr.broadcast("test", {"key": "value"})
        asyncio.run(run())

    def test_websocket_manager_disconnect(self) -> None:
        from src.dashboard import WebSocketManager
        mgr = WebSocketManager()
        ws = MagicMock()
        mgr.active.add(ws)
        assert len(mgr.active) == 1
        mgr.disconnect(ws)
        assert len(mgr.active) == 0
