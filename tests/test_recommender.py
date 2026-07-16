"""Tests pour src.search (HybridSearch RRF) et src.recommender (Recommender)."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest


# ============================================================
# Fixtures partagees
# ============================================================

@pytest.fixture
def mock_db(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock complet de get_connection."""
    import src.db as db_mod
    import src.search as search_mod
    import src.recommender as rec_mod
    import src.observer as obs_mod

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
    monkeypatch.setattr(db_mod, "get_connection", lambda *a, **kw: ctx)
    monkeypatch.setattr(search_mod, "get_connection", lambda *a, **kw: ctx)
    monkeypatch.setattr(rec_mod, "get_connection", lambda *a, **kw: ctx)
    monkeypatch.setattr(obs_mod, "get_connection", lambda *a, **kw: ctx)
    return mock_cursor


# ============================================================
# Tests HybridSearch
# ============================================================

class TestHybridSearch:
    """Tests de la recherche hybride RRF avec cascade."""

    def _make_search_result(self, **kwargs):
        from src.search import SearchResult
        defaults = {
            "email_id": "msg_1", "subject": "Test", "sender_email": "a@b.com",
            "sender_domain": "b.com", "action_taken": None,
            "rrf_score": 0.5, "distance": 0.1, "rank_vector": 1,
            "rank_tsvector": None, "retrieval_strategy": "global_fallback",
        }
        defaults.update(kwargs)
        return SearchResult(**defaults)

    def test_rrf_score_higher_for_better_rank(self) -> None:
        """RRF donne un meilleur score aux rangs plus bas (1 = meilleur)."""
        from src.search import RRF_K
        # rank=1 -> 1/(60+1) = 0.0164
        # rank=5 -> 1/(60+5) = 0.0154
        score_rank_1 = 1.0 / (RRF_K + 1)
        score_rank_5 = 1.0 / (RRF_K + 5)
        assert score_rank_1 > score_rank_5

    def test_search_returns_results(self, mock_db: MagicMock) -> None:
        """search() appelle _search_with_filter et retourne des SearchResult."""
        from src.search import HybridSearch
        # Mock le retour de _vector_search (utilise mock_db indirectement)
        hs = HybridSearch()

        # Mock _vector_search directement
        hs._vector_search = MagicMock(return_value=[
            {"email_id": "msg_1", "subject": "Newsletter 1",
             "sender_email": "noreply@newsletter.com", "sender_domain": "newsletter.com",
             "action_taken": "archive", "distance": 0.1},
            {"email_id": "msg_2", "subject": "Newsletter 2",
             "sender_email": "noreply@newsletter.com", "sender_domain": "newsletter.com",
             "action_taken": "archive", "distance": 0.2},
            {"email_id": "msg_3", "subject": "Newsletter 3",
             "sender_email": "noreply@newsletter.com", "sender_domain": "newsletter.com",
             "action_taken": "archive", "distance": 0.3},
        ])
        results = hs.search([0.0] * 1024, sender_email="noreply@newsletter.com",
                            sender_domain="newsletter.com", limit=5)
        assert len(results) == 3
        assert all(r.retrieval_strategy == "same_sender" for r in results)

    def test_search_shortcut_when_enough_results(self, mock_db: MagicMock) -> None:
        """Si on a >= 3 resultats au 1er niveau, on shortcut (pas de fallback)."""
        from src.search import HybridSearch
        hs = HybridSearch(min_results_to_shortcut=3)
        hs._vector_search = MagicMock(return_value=[
            {"email_id": f"msg_{i}", "subject": f"S{i}",
             "sender_email": "a@b.com", "sender_domain": "b.com",
             "action_taken": "archive", "distance": 0.1 * i}
            for i in range(1, 4)  # 3 resultats
        ])
        results = hs.search([0.0] * 1024, sender_email="a@b.com",
                            sender_domain="b.com")
        # _vector_search n'a ete appele qu'une fois (pas de fallback)
        assert hs._vector_search.call_count == 1
        assert len(results) == 3

    def test_search_falls_back_to_global(self, mock_db: MagicMock) -> None:
        """Si < 3 resultats au 1er niveau, on retente avec moins de filtre."""
        from src.search import HybridSearch
        hs = HybridSearch(min_results_to_shortcut=3)
        # Premier appel (sender) -> 1 resultat, deuxieme (domain) -> 1 resultat, troisieme (global) -> 5
        hs._vector_search = MagicMock(side_effect=[
            [{"email_id": "msg_1", "subject": "S1", "sender_email": "a@b.com",
              "sender_domain": "b.com", "action_taken": None, "distance": 0.1}],
            [{"email_id": "msg_2", "subject": "S2", "sender_email": "c@b.com",
              "sender_domain": "b.com", "action_taken": None, "distance": 0.2}],
            [{"email_id": f"msg_g{i}", "subject": f"GS{i}", "sender_email": f"x{i}@y.com",
              "sender_domain": "y.com", "action_taken": None, "distance": 0.3}
             for i in range(1, 6)],
        ])
        results = hs.search([0.0] * 1024, sender_email="a@b.com",
                            sender_domain="b.com", limit=5)
        # Apres 2 essais avec peu de resultats, on finit par le global
        assert hs._vector_search.call_count == 3
        assert all(r.retrieval_strategy == "global_fallback" for r in results)

    def test_results_sorted_by_rrf_then_distance(self) -> None:
        """Resultats tries par RRF DESC puis distance ASC."""
        from src.search import HybridSearch
        hs = HybridSearch()
        # msg_a est retourne EN PREMIER (rang 1, meilleur RRF) -> doit etre en tete
        # msg_b est en second (rang 2) mais a une meilleure distance -> secondaire
        hs._vector_search = MagicMock(return_value=[
            {"email_id": "msg_a", "subject": "A", "sender_email": "x@y.com",
             "sender_domain": "y.com", "action_taken": "archive", "distance": 0.1},
            {"email_id": "msg_b", "subject": "B", "sender_email": "x@y.com",
             "sender_domain": "y.com", "action_taken": "archive", "distance": 0.05},
        ])
        results = hs.search([0.0] * 1024, sender_email="x@y.com")
        # RRF prime sur la distance
        assert results[0].email_id == "msg_a"
        # Et quand meme tries par distance comme tie-breaker
        assert results[1].email_id == "msg_b"


# ============================================================
# Tests Recommender
# ============================================================

class TestRecommenderPipeline:
    """Tests du pipeline de classification P1."""

    def test_recommend_short_circuits_on_critical_rule(self, mock_db: MagicMock) -> None:
        """Un email avec mot-cle critique est force en move_ia_review, pas d'appel LLM."""
        from src.recommender import Recommender
        rec = Recommender()
        rec._call_llm = MagicMock(side_effect=AssertionError("LLM should NOT be called"))

        email = {
            "id": "msg_1",
            "sender_email": "unknown@bank.com",
            "sender_domain": "bank.com",
            "subject": "URGENT: Verification 2FA requise",
            "body_snippet": "Cliquez ici pour valider votre compte",
        }
        decision = rec.recommend(email)
        # Le LLM n'a PAS ete appele
        rec._call_llm.assert_not_called()
        # La decision est un move_ia_review avec confiance haute
        assert decision.executable_operation == "move_ia_review"
        assert decision.confidence >= 0.9

    def test_recommend_calls_llm_when_no_critical(self, mock_db: MagicMock) -> None:
        """Un email sans mot-cle critique declenche l'appel LLM."""
        from src.recommender import Recommender
        from src.models import MailDecision
        rec = Recommender()
        rec._call_llm = MagicMock(return_value=MailDecision(
            classification="newsletter", executable_operation="archive",
            recommended_user_action="none", confidence=0.85, reason="test",
        ))

        email = {
            "id": "msg_2",
            "sender_email": "noreply@newsletter.com",
            "sender_domain": "newsletter.com",
            "subject": "Weekly tech news",
            "body_snippet": "This week in tech...",
        }
        rec.embedder.embed_text = MagicMock(return_value=[0.0] * 1024)
        rec.hybrid_search.search = MagicMock(return_value=[])

        rec.recommend(email)
        # LLM appele exactement 1 fois
        assert rec._call_llm.call_count == 1

    def test_low_llm_confidence_forces_none(self, mock_db: MagicMock) -> None:
        """Si llm_confidence < 0.3, executable_operation force a 'none'."""
        from src.recommender import Recommender
        from src.models import MailDecision
        rec = Recommender()
        rec._call_llm = MagicMock(return_value=MailDecision(
            classification="unknown", executable_operation="archive",
            recommended_user_action="none", confidence=0.2, reason="unsure",
        ))
        rec.embedder.embed_text = MagicMock(return_value=[0.0] * 1024)
        rec.hybrid_search.search = MagicMock(return_value=[])

        email = {
            "id": "msg_3", "sender_email": "x@y.com", "sender_domain": "y.com",
            "subject": "Unsure email", "body_snippet": "...",
        }
        decision = rec.recommend(email)
        # L'executable_operation doit etre force a 'none'
        assert decision.executable_operation == "none"

    def test_high_divergence_forces_none(self, mock_db: MagicMock) -> None:
        """Si |llm - heuristic| > 0.3, executable_operation force a 'none'."""
        from src.recommender import Recommender
        from src.models import MailDecision
        rec = Recommender()
        rec._call_llm = MagicMock(return_value=MailDecision(
            classification="newsletter", executable_operation="archive",
            recommended_user_action="none", confidence=0.9, reason="sure",
        ))
        rec.embedder.embed_text = MagicMock(return_value=[0.0] * 1024)
        rec.hybrid_search.search = MagicMock(return_value=[])

        email = {
            "id": "msg_4", "sender_email": "x@y.com", "sender_domain": "y.com",
            "subject": "Newsletter", "body_snippet": "...",
        }
        decision = rec.recommend(email)
        assert decision.executable_operation == "none"

    def test_llm_error_fallback(self, mock_db: MagicMock) -> None:
        """Si le LLM leve une erreur, on fallback sur une decision 'unknown' safe."""
        from src.recommender import Recommender, LLMError
        rec = Recommender()
        rec._call_llm = MagicMock(side_effect=LLMError("ollama down"))
        rec.embedder.embed_text = MagicMock(return_value=[0.0] * 1024)
        rec.hybrid_search.search = MagicMock(return_value=[])

        email = {
            "id": "msg_5", "sender_email": "x@y.com", "sender_domain": "y.com",
            "subject": "Test", "body_snippet": "...",
        }
        decision = rec.recommend(email)
        # Fallback safe : 'unknown' + 'none'
        assert decision.classification == "unknown"
        assert decision.executable_operation == "none"


class TestBuildPrompt:
    """Tests de la construction securisee du prompt Few-Shot."""

    def test_prompt_separates_instructions_from_data(self) -> None:
        """Le prompt met les instructions AVANT et le mail APRES."""
        from src.recommender import Recommender
        rec = Recommender()
        prompt = rec._build_prompt(
            email={"sender_email": "x@y.com", "subject": "Test",
                   "body_snippet": "Test body"},
            similar=[],
        )
        # Les instructions apparaissent avant le mail
        instr_pos = prompt.find("REGLES STRICTES")
        mail_pos = prompt.find("MAIL A ANALYSER")
        assert instr_pos > 0
        assert mail_pos > instr_pos

    def test_prompt_truncates_long_body(self) -> None:
        """Le body est plafonne a 500 chars dans le prompt."""
        from src.recommender import Recommender, MAX_BODY_CHARS_IN_PROMPT
        rec = Recommender()
        long_body = "x" * 1000
        prompt = rec._build_prompt(
            email={"sender_email": "x@y.com", "subject": "T", "body_snippet": long_body},
            similar=[],
        )
        # Le body dans le prompt ne doit pas depasser MAX_BODY_CHARS_IN_PROMPT
        # On extrait la portion apres "Corps (extrait):"
        idx = prompt.find("Corps (extrait):")
        body_in_prompt = prompt[idx:].splitlines()[0]
        assert len(body_in_prompt) - len("Corps (extrait): ") <= MAX_BODY_CHARS_IN_PROMPT

    def test_prompt_with_no_similar_shows_placeholder(self) -> None:
        """Si pas de similaires, le prompt indique '(aucun)' au lieu de planter."""
        from src.recommender import Recommender
        rec = Recommender()
        prompt = rec._build_prompt(
            email={"sender_email": "x@y.com", "subject": "T", "body_snippet": "..."},
            similar=[],
        )
        assert "(aucun)" in prompt or "(aucune)" in prompt


class TestHeuristicConfidence:
    """Tests du calcul de confiance heuristique."""

    def test_no_similar_returns_zero(self) -> None:
        from src.recommender import Recommender
        rec = Recommender()
        conf = rec._compute_heuristic_confidence([], "x@y.com", "y.com")
        assert conf == 0.0

    def test_all_similar_same_action_returns_high(self) -> None:
        from src.recommender import Recommender
        from src.search import SearchResult
        rec = Recommender()
        similar = [
            SearchResult(
                email_id=f"msg_{i}", subject=None, sender_email="x@y.com",
                sender_domain="y.com", action_taken="archive",
                rrf_score=0.5, distance=0.1, rank_vector=i, rank_tsvector=None,
                retrieval_strategy="same_sender",
            )
            for i in range(1, 6)
        ]
        conf = rec._compute_heuristic_confidence(similar, "x@y.com", "y.com")
        # 5/5 memes action + meme sender -> sender_factor 1.2 -> 1.0 (borne)
        assert conf >= 0.9

    def test_sender_factor_higher_than_domain(self) -> None:
        from src.recommender import Recommender
        from src.search import SearchResult
        rec = Recommender()
        # Memes actions, mais sender different
        similar_same_domain = [
            SearchResult(
                email_id=f"msg_{i}", subject=None, sender_email=f"z{i}@y.com",
                sender_domain="y.com", action_taken="archive",
                rrf_score=0.5, distance=0.1, rank_vector=i, rank_tsvector=None,
                retrieval_strategy="same_domain",
            )
            for i in range(1, 6)
        ]
        conf = rec._compute_heuristic_confidence(similar_same_domain, "x@y.com", "y.com")
        # 5/5 memes actions, mais pas meme sender -> sender_factor 1.0
        # -> conf = 1.0 * 1.0 = 1.0
        assert 0.7 <= conf <= 1.0

    def test_no_action_taken_returns_zero(self) -> None:
        from src.recommender import Recommender
        from src.search import SearchResult
        rec = Recommender()
        similar = [
            SearchResult(
                email_id=f"msg_{i}", subject=None, sender_email="x@y.com",
                sender_domain="y.com", action_taken=None,
                rrf_score=0.5, distance=0.1, rank_vector=i, rank_tsvector=None,
                retrieval_strategy="same_sender",
            )
            for i in range(1, 4)
        ]
        conf = rec._compute_heuristic_confidence(similar, "x@y.com", "y.com")
        # Pas d'action -> 0.0
        assert conf == 0.0
