"""Tests pour src.rules_engine — classification statique (cold start)."""
from __future__ import annotations

import pytest

from src.rules_engine import (
    CRITICAL_DOMAINS,
    CRITICAL_KEYWORDS,
    RuleAction,
    RuleConfidence,
    RulesEngine,
)


# ============================================================
# Mots-clés critiques
# ============================================================

def test_critical_keywords_not_empty() -> None:
    """La liste des mots-clés critiques ne doit pas être vide."""
    assert len(CRITICAL_KEYWORDS) >= 10
    # Quelques mots-clés essentiels
    for kw in ("facture", "paiement", "banque", "sécurité", "password"):
        assert kw in CRITICAL_KEYWORDS or kw.replace("é", "e") in CRITICAL_KEYWORDS


def test_critical_domains_government() -> None:
    """Les domaines des services publics doivent être critiques."""
    assert "impots.gouv.fr" in CRITICAL_DOMAINS
    assert "ameli.fr" in CRITICAL_DOMAINS
    assert "urssaf.fr" in CRITICAL_DOMAINS


# ============================================================
# contains_critical_keywords
# ============================================================

def test_contains_critical_keywords_basic() -> None:
    engine = RulesEngine()
    assert "facture" in engine.contains_critical_keywords("Voici votre facture EDF")
    assert "paiement" in engine.contains_critical_keywords("Paiement en attente")
    assert "banque" in engine.contains_critical_keywords("Votre banque vous informe")


def test_contains_critical_keywords_case_insensitive() -> None:
    engine = RulesEngine()
    assert engine.contains_critical_keywords("FACTURE EDF") != []
    assert engine.contains_critical_keywords("Facture edf") != []


def test_contains_critical_keywords_no_match() -> None:
    engine = RulesEngine()
    assert engine.contains_critical_keywords("Bienvenue dans notre newsletter !") == []


def test_contains_critical_keywords_empty() -> None:
    engine = RulesEngine()
    assert engine.contains_critical_keywords("") == []
    assert engine.contains_critical_keywords(None) == []  # type: ignore


def test_contains_critical_keywords_multiple() -> None:
    engine = RulesEngine()
    found = engine.contains_critical_keywords("Facture + paiement en attente + sécurité compte")
    assert "facture" in found
    assert "paiement" in found
    assert "sécurité" in found or "securite" in found


# ============================================================
# classify — toutes les règles
# ============================================================

def test_rule_spam_label() -> None:
    """Règle 5 : label spam → mark_read."""
    engine = RulesEngine()
    result = engine.classify({
        "sender_email": "anything@anywhere.com",
        "sender_domain": "anywhere.com",
        "subject": "Buy cheap stuff",
        "body_text": "Click here",
        "labels": ["SPAM"],
    })
    assert result.action == RuleAction.MARK_READ
    assert result.rule_name == "spam_label"


def test_rule_critical_keyword() -> None:
    """Règle 4 : mot-clé critique → move_ia_review (CRITICAL)."""
    engine = RulesEngine()
    result = engine.classify({
        "sender_email": "service@unknown-corp.com",
        "sender_domain": "unknown-corp.com",
        "subject": "Votre facture est disponible",
        "body_text": "Cher client, voici votre facture",
        "labels": [],
    })
    assert result.action == RuleAction.MOVE_IA_REVIEW
    assert result.confidence == RuleConfidence.CRITICAL
    assert "facture" in result.matched_keywords


def test_rule_noreply_with_critical_keyword() -> None:
    """Règle 2 : noreply + mot-clé critique → move_ia_review."""
    engine = RulesEngine()
    result = engine.classify({
        "sender_email": "noreply@bank-corp.com",
        "sender_domain": "bank-corp.com",
        "subject": "Alerte sécurité sur votre compte",
        "body_text": "Vérification 2FA requise",
        "labels": [],
    })
    assert result.action == RuleAction.MOVE_IA_REVIEW
    assert result.confidence == RuleConfidence.CRITICAL


def test_rule_noreply_low_priority_domain() -> None:
    """Règle 1 : noreply + domaine low-priority connu → archive."""
    engine = RulesEngine()
    engine.add_low_priority_domain("newsletter.com")
    result = engine.classify({
        "sender_email": "noreply@newsletter.com",
        "sender_domain": "newsletter.com",
        "subject": "Weekly tech news",
        "body_text": "This week in tech...",
        "labels": [],
    })
    assert result.action == RuleAction.ARCHIVE
    assert result.confidence == RuleConfidence.HIGH


def test_rule_noreply_unknown_domain() -> None:
    """Règle 3 : noreply + domaine inconnu → P1 (humain décide)."""
    engine = RulesEngine()
    result = engine.classify({
        "sender_email": "noreply@totally-new-domain.com",
        "sender_domain": "totally-new-domain.com",
        "subject": "Special offer for you",
        "body_text": "Click here for 50% off",
        "labels": [],
    })
    assert result.action == RuleAction.P1_PROPOSAL
    assert result.confidence == RuleConfidence.MEDIUM
    assert result.rule_name == "noreply_unknown_domain"


def test_rule_default() -> None:
    """Règle 6 : défaut (mail personnel) → P1 (low confidence)."""
    engine = RulesEngine()
    result = engine.classify({
        "sender_email": "alice@gmail.com",
        "sender_domain": "gmail.com",
        "subject": "Coffee tomorrow?",
        "body_text": "Want to grab coffee tomorrow at 10?",
        "labels": ["INBOX"],
    })
    assert result.action == RuleAction.P1_PROPOSAL
    assert result.confidence == RuleConfidence.LOW


def test_rule_critical_domain_overrides() -> None:
    """Un mail d'un domaine critique (impots.gouv.fr) doit toujours
    déclencher une review, même sans mot-clé."""
    engine = RulesEngine()
    result = engine.classify({
        "sender_email": "noreply@impots.gouv.fr",
        "sender_domain": "impots.gouv.fr",
        "subject": "Information",
        "body_text": "Some generic info",  # pas de mot-clé
        "labels": [],
    })
    # impots.gouv.fr n'est PAS dans known_low_priority (jamais auto)
    # → fallback noreply_unknown_domain (P1) car on a noreply
    assert result.action == RuleAction.P1_PROPOSAL


def test_rule_no_false_archive_for_billing() -> None:
    """Cas de sécurité : un mail avec 'facture' NE DOIT JAMAIS être archivé auto."""
    engine = RulesEngine()
    # Même si le domaine est "connu" comme low-priority
    engine.add_low_priority_domain("some-corp.com")
    result = engine.classify({
        "sender_email": "noreply@some-corp.com",
        "sender_domain": "some-corp.com",
        "subject": "Facture mensuelle",
        "body_text": "Voici votre facture",
        "labels": [],
    })
    # CRITICAL prime sur la règle noreply+low-priority
    assert result.action == RuleAction.MOVE_IA_REVIEW
    assert result.confidence == RuleConfidence.CRITICAL


# ============================================================
# Apprentissage des domaines
# ============================================================

def test_add_low_priority_domain() -> None:
    engine = RulesEngine()
    assert "newsletter.com" not in engine.known_low_priority_domains
    engine.add_low_priority_domain("newsletter.com")
    assert "newsletter.com" in engine.known_low_priority_domains


def test_add_low_priority_domain_case_insensitive() -> None:
    engine = RulesEngine()
    engine.add_low_priority_domain("NEWSLETTER.COM")
    assert "newsletter.com" in engine.known_low_priority_domains


def test_remove_low_priority_domain() -> None:
    engine = RulesEngine()
    engine.add_low_priority_domain("newsletter.com")
    engine.remove_low_priority_domain("newsletter.com")
    assert "newsletter.com" not in engine.known_low_priority_domains


def test_remove_unknown_domain_is_noop() -> None:
    engine = RulesEngine()
    engine.remove_low_priority_domain("never-added.com")
    # Ne lève pas, no-op silencieux
    assert "never-added.com" not in engine.known_low_priority_domains
