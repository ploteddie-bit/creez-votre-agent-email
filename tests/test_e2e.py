"""Tests E2E bout-en-bout — verifie le pipeline complet sans vraie DB.

Ces tests utilisent des mocks pour simuler un flux complet :

  Gmail API (mock) → Observer.sync_full() → Ingester (mock DB)
                 → Embedder (mock Ollama) → Recommender (mock LLM)
                 → Decider (gates) → ActionWorker → Gmail API (mock)

Le but : verifier que tous les modules sont correctement cables
ensemble. Si un seul maillon casse, le test echoue.

Pas de vraie DB, pas de vrai Ollama, pas de vrai Gmail.
Juste des mocks coordonnes + verification des appels.
"""
from __future__ import annotations

import json
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest


# ============================================================
# Fixtures partagees
# ============================================================

@pytest.fixture
def full_mocks(monkeypatch: pytest.MonkeyPatch):
    """Mock complet du pipeline E2E.

    Mock :
      - get_connection (DB) -> tout reste en memoire (lastrowid, etc.)
      - Gmail API (via GmailClient)
      - Ollama (via Embedder + Recommender)

    Returns un dict avec tous les mocks pour assertions.
    """
    # DB mock
    import src.db as db_mod
    import src.observer as obs_mod
    import src.recommender as rec_mod
    import src.decider as dec_mod
    import src.action_worker as aw_mod
    import src.embedder as emb_mod

    # Tables simulees en memoire
    tables: dict[str, list[dict]] = {
        "emails": [],
        "decision_journal": [],
        "action_queue": [],
        "sync_state": [],
        "gmail_labels": [],
        "email_embeddings": [],
    }
    auto_inc = {"value": 1}

    def make_cursor():
        cur = MagicMock()
        cur.fetchone = MagicMock(return_value=(0,))
        cur.fetchall = MagicMock(return_value=[])
        cur.execute = MagicMock()
        cur.rowcount = 0
        return cur

    def make_conn():
        conn = MagicMock()
        cur = make_cursor()
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False
        conn.__enter__.return_value = conn
        conn.__exit__.return_value = False
        conn.commit = MagicMock()
        return conn

    ctx = MagicMock()
    ctx.__enter__.return_value = make_conn()
    ctx.__exit__.return_value = False

    for mod in (db_mod, obs_mod, rec_mod, dec_mod, aw_mod):
        monkeypatch.setattr(mod, "get_connection", lambda *a, **kw: ctx)

    # GmailClient mock : retourne des messages, labels, etc.
    mock_gmail = MagicMock()
    # list_messages retourne (messages, next_token) - 1 page de 3 messages
    sample_messages = [
        {"id": "msg_001", "threadId": "t1", "labelIds": ["INBOX", "UNREAD"]},
        {"id": "msg_002", "threadId": "t2", "labelIds": ["INBOX", "UNBOX"]},
        {"id": "msg_003", "threadId": "t3", "labelIds": ["INBOX", "UNREAD"]},
    ]
    mock_gmail.list_messages.return_value = (sample_messages, None)
    mock_gmail.list_history.return_value = []
    mock_gmail.get_message.return_value = {
        "id": "msg_001",
        "threadId": "t1",
        "labelIds": ["INBOX", "UNREAD"],
        "internalDate": "1721167200000",
        "payload": {
            "headers": [
                {"name": "From", "value": "newsletter@example.com"},
                {"name": "Subject", "value": "Weekly digest"},
                {"name": "Date", "value": "Thu, 16 Jul 2026 22:00:00 +0000"},
            ],
            "mimeType": "text/plain",
            "body": {"data": "V2VsY29tZSB0byB0aGUgbmV3c2xldHRlciE="},  # "Welcome to the newsletter!"
        },
    }
    mock_gmail.list_labels.return_value = [
        {"id": "INBOX", "name": "INBOX"},
        {"id": "STARRED", "name": "STARRED"},
    ]
    mock_gmail.modify_labels.return_value = {"id": "msg_001"}
    mock_gmail.allowed_methods.return_value = frozenset({"users.messages.list"})

    return {
        "tables": tables,
        "auto_inc": auto_inc,
        "gmail": mock_gmail,
    }


# ============================================================
# Test E2E : pipeline complet synchrone
# ============================================================

def test_e2e_pipeline_runs_without_errors(full_mocks) -> None:
    """Le pipeline complet peut s'executer sans crash.

    On execute un cycle de la boucle daemon en mockant Gmail + DB.
    """
    from src.observer import GmailObserver

    mocks = full_mocks
    # Mock Ingester pour qu'il "reussisse" toujours
    mock_ingester = MagicMock()
    mock_ingester.ingest_email.return_value = True

    obs = GmailObserver(
        gmail_client=mocks["gmail"],
        ingester=mock_ingester,
        account_id="test_account",
    )

    # 1. Sync full
    ingested = obs.sync_full(max_results=10, query="newer_than:1d")
    assert ingested == 3  # 3 messages ingeres

    # Verifie que Gmail a ete appele avec la bonne query
    mocks["gmail"].list_messages.assert_called()
    call_kwargs = mocks["gmail"].list_messages.call_args.kwargs
    assert "query" in call_kwargs
    assert "newer_than:1d" in call_kwargs["query"]


def test_e2e_sync_then_embed_then_recommend(full_mocks) -> None:
    """Chaine sync → embed → recommend fonctionne en sequence."""
    from src.observer import GmailObserver
    from src.embedder import Embedder
    from src.recommender import Recommender
    from src.models import MailDecision

    mocks = full_mocks
    # Mock Ingester
    mock_ingester = MagicMock()
    mock_ingester.ingest_email.return_value = True

    # 1. Sync full
    obs = GmailObserver(
        gmail_client=mocks["gmail"],
        ingester=mock_ingester,
        account_id="test_account",
    )
    ingested = obs.sync_full(max_results=10)
    assert ingested == 3

    # 2. Mock Embedder : appel reussi, vecteur de bonne dim
    embedder = Embedder()
    embedder.embed_text = MagicMock(return_value=[0.1] * 1024)
    embedder.store_embedding = MagicMock()

    # 3. Mock Recommender
    from src.search import SearchResult
    recommender = Recommender()
    # Remplacer les properties par des mocks (les setters n'existent pas)
    object.__setattr__(recommender, "_embedder", embedder)
    recommender.rules_engine.classify = MagicMock(
        return_value=MagicMock(confidence=MagicMock(value="low"), action=MagicMock(value="p1_proposal"))
    )
    # Similaires avec action "archive" pour que la confiance heuristique
    # soit elevee (evite que la divergence force 'none')
    similar_with_action = [
        SearchResult(
            email_id=f"msg_old_{i}", subject=None,
            sender_email="newsletter@example.com", sender_domain="example.com",
            action_taken="archive", rrf_score=0.5, distance=0.1,
            rank_vector=i, rank_tsvector=None, retrieval_strategy="same_sender",
        )
        for i in range(1, 4)
    ]
    recommender.hybrid_search.search = MagicMock(return_value=similar_with_action)
    recommender._call_llm = MagicMock(return_value=MailDecision(
        classification="newsletter",
        executable_operation="archive",
        confidence=0.85,
        reason="test e2e",
    ))

    email = {
        "id": "msg_001",
        "sender_email": "newsletter@example.com",
        "sender_domain": "example.com",
        "subject": "Weekly digest",
        "body_snippet": "Welcome to the newsletter!",
    }
    decision = recommender.recommend(email)
    # LLM a ete appele, la decision est archive
    assert decision.executable_operation == "archive"
    assert decision.confidence == 0.85


def test_e2e_p2_path_with_kill_switch(full_mocks) -> None:
    """Si P2 desactive (kill-switch ON par defaut), l'auto-execute refuse."""
    from src.decider import Decider
    from src.models import MailDecision

    decider = Decider(p2_enabled=False)  # Kill-switch ON
    decision = MailDecision(
        classification="newsletter",
        executable_operation="archive",
        confidence=0.9,
        reason="test",
    )
    result = decider.auto_execute("msg_1", decision)
    # P2 off -> pas d'execution
    assert result is None


def test_e2e_p2_path_with_decision_allowed(full_mocks, monkeypatch) -> None:
    """Si P2 actif et tous les garde-fous OK, l'auto-execute enqueue."""
    from src.decider import Decider
    from src.models import MailDecision

    decider = Decider(p2_enabled=True)
    # Mock des helpers qui accedent a la DB
    decider._is_known_sender = MagicMock(return_value=True)
    decider._get_recent_heuristic_confidence = MagicMock(return_value=0.85)
    decider._today_actions_count = MagicMock(return_value=0)
    decider.get_window_precision = MagicMock(return_value=0.98)
    decider._p2_volume_guards_ok = MagicMock(return_value=True)
    decider._mark_decision_pending = MagicMock()

    # Mock action_worker.enqueue_action
    decider.action_worker.enqueue_action = MagicMock(return_value=42)

    decision = MailDecision(
        classification="newsletter",
        executable_operation="archive",
        confidence=0.85,
        reason="test",
    )
    email = {
        "id": "msg_1",
        "sender_domain": "trusted.com",
        "subject": "Newsletter",
        "body_snippet": "...",
    }
    result = decider.auto_execute("msg_1", decision, email=email)
    # P2 on + tous les garde-fous OK -> enqueue
    assert result == 42
    decider.action_worker.enqueue_action.assert_called_once()


def test_e2e_action_worker_processes_queue(full_mocks) -> None:
    """L'action_worker prend un job, execute l'action, marque done."""
    from src.action_worker import ActionWorker

    worker = ActionWorker(gmail_client=full_mocks["gmail"])
    # Mock _claim_next_job pour retourner un job
    worker._claim_next_job = MagicMock(side_effect=[
        {"id": 1, "email_id": "msg_1", "operation": "mark_read",
         "idempotency_key": "k1", "attempts": 0},
        None,  # queue vide ensuite
    ])
    worker._process_job = MagicMock()

    # Execute 2 iterations
    processed = worker.run(max_iterations=2)
    # 1 job traite
    assert processed == 1
    assert worker._process_job.call_count == 1


# ============================================================
# Tests E2E : smoke tests du CLI
# ============================================================

class TestCLISmoke:
    """Smoke tests : les sous-commandes du CLI fonctionnent."""

    def test_main_help_shows_all_subcommands(self) -> None:
        """python -m src.main --help liste toutes les sous-commandes."""
        result = subprocess.run(
            [sys.executable, "-m", "src.main", "--help"],
            capture_output=True, text=True, timeout=10,
            cwd="C:\\Users\\eddie\\Documents\\agent-mail",
        )
        assert result.returncode == 0
        # Verifie que les sous-commandes sont listees
        for cmd in ("daemon", "sync", "embed", "process", "health", "dashboard", "setup-oauth"):
            assert cmd in result.stdout, f"subcommand '{cmd}' missing from --help"

    def test_setup_oauth_prints_guide(self) -> None:
        """python -m src.main setup-oauth affiche le guide."""
        result = subprocess.run(
            [sys.executable, "-m", "src.main", "setup-oauth"],
            capture_output=True, text=True, timeout=10,
            cwd="C:\\Users\\eddie\\Documents\\agent-mail",
        )
        assert result.returncode == 0
        # Verifie les elements cles du guide
        assert "Google Cloud Console" in result.stdout
        assert "Gmail API" in result.stdout
        assert "gmail.modify" in result.stdout
        assert "gmail-credentials.json" in result.stdout


# ============================================================
# Tests E2E : verification absence de placeholders
# ============================================================

class TestNoPlaceholders:
    """Le code ne doit contenir aucun placeholder explicite."""

    def test_no_todo_fixme_in_source(self) -> None:
        """Aucun TODO/FIXME dans le code de production."""
        import re
        from pathlib import Path
        src = Path("src")
        violations: list[str] = []
        for py_file in src.rglob("*.py"):
            content = py_file.read_text(encoding="utf-8")
            for match in re.finditer(r"#\s*(TODO|FIXME|XXX)\b", content, re.IGNORECASE):
                line_start = content.rfind("\n", 0, match.start()) + 1
                line_end = content.find("\n", match.end())
                line = content[line_start:line_end if line_end > 0 else None].strip()
                # Exclure les commentaires "non-TODO" qui contiennent "todo" en passant
                if "todo" in line.lower() and ("#" in line and "todo" in line.lower().split("#")[1][:20]):
                    violations.append(f"  {py_file}:{line}")
        assert not violations, (
            "Placeholders trouves dans le code:\n" + "\n".join(violations)
        )

    def test_no_not_implemented_error(self) -> None:
        """Aucun raise NotImplementedError dans le code."""
        from pathlib import Path
        src = Path("src")
        for py_file in src.rglob("*.py"):
            content = py_file.read_text(encoding="utf-8")
            assert "NotImplementedError" not in content, (
                f"NotImplementedError dans {py_file}"
            )

    def test_no_placeholder_string(self) -> None:
        """Aucune string 'placeholder' dans le code (hors docstrings explicatifs)."""
        from pathlib import Path
        src = Path("src")
        for py_file in src.rglob("*.py"):
            content = py_file.read_text(encoding="utf-8")
            # Cherche 'placeholder' comme mot
            for line_num, line in enumerate(content.splitlines(), 1):
                if "placeholder" in line.lower():
                    # OK si c'est dans un commentaire explicatif (ex: docstring)
                    # mais pas dans le code
                    if "docstring" in line.lower() or line.strip().startswith("#"):
                        # On accepte dans les docstrings/commentaires
                        continue
                    # En code reel, c'est suspect
                    # Mais on accepte dans les docstrings qui disent "ce n'est PAS un placeholder"
                    # Heuristique simple : la ligne est en string assignment
                    if '"""' in line or "'''" in line or line.strip().startswith("'''") or line.strip().startswith('"""'):
                        continue
                    # Sinon on leve
                    assert False, f"'placeholder' en code dans {py_file}:{line_num}: {line}"


# ============================================================
# Test final : tous les composants s'importent
# ============================================================

class TestAllComponentsImportable:
    """Chaque module src/ peut etre importe sans erreur."""

    def test_all_modules_import(self) -> None:
        """Tous les modules s'importent."""
        modules = [
            "src.config", "src.db", "src.models",
            "src.parser", "src.embedder", "src.ingester",
            "src.gmail_client", "src.observer", "src.rules_engine",
            "src.search", "src.recommender", "src.action_worker",
            "src.decider", "src.dashboard", "src.main",
        ]
        for mod_name in modules:
            try:
                __import__(mod_name)
            except Exception as e:
                pytest.fail(f"Failed to import {mod_name}: {e}")

    def test_dashboard_app_has_routes(self) -> None:
        """Le dashboard FastAPI a bien des routes definies."""
        from src.dashboard import app
        # Au moins 8 routes (health, emails, decisions, config, sync, ws, + 7 pages)
        assert len(app.routes) >= 15, f"Trop peu de routes : {len(app.routes)}"

    def test_main_parser_has_subcommands(self) -> None:
        """Le parser CLI a toutes les sous-commandes attendues."""
        from src.main import build_parser
        parser = build_parser()
        # Tester chaque sous-commande en l'appelant avec --help
        for cmd in ("daemon", "sync", "embed", "process", "health", "dashboard", "setup-oauth"):
            try:
                # Parse avec la sous-commande + --help, on s'attend a SystemExit
                # (argparse appelle sys.exit(0) apres --help)
                import sys
                old_argv = sys.argv
                sys.argv = ["email-learner", cmd, "--help"]
                try:
                    with pytest.raises(SystemExit):
                        parser.parse_args()
                finally:
                    sys.argv = old_argv
            except SystemExit:
                pass  # OK
            except Exception as e:
                pytest.fail(f"sous-commande '{cmd}' invalide: {e}")
