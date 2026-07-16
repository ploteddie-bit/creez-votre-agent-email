"""Rules engine — règles statiques de classification (cold start + fallback).

Implémente les 6 règles de la SPEC §7.1. Ces règles s'appliquent
**avant** le LLM : si une règle matche un cas évident, on n'appelle
même pas Ollama. C'est ce qui rend le système utilisable dès le
premier mail, sans avoir besoin d'attendre un modèle entraîné.

Règles :
  1. noreply + domaine low-priority + pas de mot-clé critique → archive (high)
  2. noreply + mot-clé critique → move_ia_review (critical)
  2b. PJ jamais classifiée par l'utilisateur → move_ia_review (critical, ABSOLUE)
  3. noreply + domaine inconnu → P1 (medium)
  4. Mot-clé critique présent → move_ia_review (critical)
  5. Label spam → mark_read (high)
  6. Défaut → P1 (low)

Mots-clés critiques :
  facture, paiement, impôt, sécurité, 2FA, contrat, banque,
  assurance, médical, juridique, relance, recommandé, échéance,
  password, verification

L'auto-apprentissage des `KNOWN_LOW_PRIORITY_DOMAINS` est implémenté
mais simple : on charge depuis un JSON dans `configs/`. Une version
plus sophistiquée utiliserait un cron + SQL.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# === Mots-clés critiques (toujours vérifier, JAMAIS auto-archiver) ===

CRITICAL_KEYWORDS: tuple[str, ...] = (
    "facture", "paiement", "impôt", "impot", "sécurité", "securite",
    "2fa", "contrat", "banque", "assurance", "médical", "medical",
    "juridique", "relance", "recommandé", "recommande", "échéance",
    "echeance", "password", "verification", "vérification",
    "cb", "carte bancaire", "virement", "rib", "iban",
)

CRITICAL_DOMAINS: frozenset[str] = frozenset({
    "impots.gouv.fr", "ameli.fr", "service-public.fr",
    "urssaf.fr", "gmail.com",  # gmail.com en critical pour safety
    "banque.fr", "caissedepargne.fr", "credit-agricole.fr",
    "bnpparibas.fr", "lcl.fr", "societegenerale.fr",
})


# === Énumérations de sortie ===

class RuleAction(str, Enum):
    """Actions possibles retournées par le rules engine."""
    ARCHIVE = "archive"
    MARK_READ = "mark_read"
    MOVE_IA_REVIEW = "move_ia_review"
    P1_PROPOSAL = "p1_proposal"  # laisse le LLM + humain décider


class RuleConfidence(str, Enum):
    """Niveau de confiance dans la décision."""
    CRITICAL = "critical"   # priorité absolue (mots-clés critiques)
    HIGH = "high"           # confiance haute (règle déterministe)
    MEDIUM = "medium"       # confiance moyenne
    LOW = "low"             # confiance basse (fallback)


@dataclass
class RuleResult:
    """Résultat de l'application d'une règle."""
    action: RuleAction
    confidence: RuleConfidence
    rule_name: str
    matched_keywords: list[str]
    sender_domain: Optional[str] = None


# === Rules engine ===

class RulesEngine:
    """Moteur de règles statiques. Pas d'état persistant complexe."""

    def __init__(self, low_priority_domains_path: Optional[Path] = None) -> None:
        self.known_low_priority_domains: set[str] = self._load_low_priority_domains(
            low_priority_domains_path
        )

    def classify(self, email: dict) -> RuleResult:
        """Classifie un email et retourne la règle applicable.

        L'ordre des vérifications est important : les règles les plus
        critiques (mots-clés) sont testées en premier.
        """
        sender_email = (email.get("sender_email") or "").lower()
        sender_domain = (email.get("sender_domain") or "").lower()
        subject = (email.get("subject") or "").lower()
        body_text = (email.get("body_text") or email.get("body_snippet") or "").lower()
        labels = [l.lower() for l in (email.get("labels") or [])]
        text_to_check = f"{subject} {body_text}"

        matched_keywords = self.contains_critical_keywords(text_to_check)
        has_critical = bool(matched_keywords)
        is_noreply = "noreply" in sender_email or "no-reply" in sender_email
        is_spam = "spam" in labels
        is_critical_domain = sender_domain in CRITICAL_DOMAINS

        # Règle 5 : spam → mark_read
        if is_spam:
            return RuleResult(
                action=RuleAction.MARK_READ,
                confidence=RuleConfidence.HIGH,
                rule_name="spam_label",
                matched_keywords=[],
                sender_domain=sender_domain,
            )

        # Règle 2 & 4 : mot-clé critique → JAMAIS auto-archiver
        if has_critical:
            return RuleResult(
                action=RuleAction.MOVE_IA_REVIEW,
                confidence=RuleConfidence.CRITICAL,
                rule_name="critical_keyword" if not is_noreply else "noreply_with_critical",
                matched_keywords=matched_keywords,
                sender_domain=sender_domain,
            )

        # Règle 2b (REVIEW §1.2 — sécurité, ABSOLUE) : pièce jointe
        # jamais classifiée par l'utilisateur → move_ia_review.
        # Aucune PJ non encore vue ne peut être auto-classifiée :
        # un PDF scanné de facture ne doit JAMAIS finir auto-archivé.
        if email.get("has_attachments") and not email.get("user_classified"):
            return RuleResult(
                action=RuleAction.MOVE_IA_REVIEW,
                confidence=RuleConfidence.CRITICAL,
                rule_name="unclassified_attachment",
                matched_keywords=[],
                sender_domain=sender_domain,
            )

        # Règle 1 : noreply + domaine low-priority connu → archive
        if is_noreply and sender_domain in self.known_low_priority_domains:
            return RuleResult(
                action=RuleAction.ARCHIVE,
                confidence=RuleConfidence.HIGH,
                rule_name="noreply_low_priority_domain",
                matched_keywords=[],
                sender_domain=sender_domain,
            )

        # Règle 3 : noreply + domaine inconnu → P1 (humain décide)
        if is_noreply and not is_critical_domain:
            return RuleResult(
                action=RuleAction.P1_PROPOSAL,
                confidence=RuleConfidence.MEDIUM,
                rule_name="noreply_unknown_domain",
                matched_keywords=[],
                sender_domain=sender_domain,
            )

        # Règle 6 : défaut → P1 (fallback)
        return RuleResult(
            action=RuleAction.P1_PROPOSAL,
            confidence=RuleConfidence.LOW,
            rule_name="default",
            matched_keywords=[],
            sender_domain=sender_domain,
        )

    def contains_critical_keywords(self, text: str) -> list[str]:
        """Retourne la liste des mots-clés critiques trouvés dans le texte.

        La recherche est insensible à la casse et aux accents simples.
        """
        if not text:
            return []
        text_lower = text.lower()
        # Normaliser les accents courants (e/é/è/ê → e, etc.)
        text_normalized = (
            text_lower
            .replace("é", "e").replace("è", "e").replace("ê", "e")
            .replace("à", "a").replace("â", "a")
            .replace("ù", "u").replace("û", "u")
            .replace("ô", "o").replace("ö", "o")
            .replace("î", "i").replace("ï", "i")
        )
        # Mots avec accents
        text_full = f"{text_lower} {text_normalized}"

        found: list[str] = []
        for kw in CRITICAL_KEYWORDS:
            kw_normalized = (
                kw
                .replace("é", "e").replace("è", "e").replace("ê", "e")
                .replace("à", "a").replace("â", "a")
                .replace("ù", "u").replace("û", "u")
            )
            if kw in text_full or kw_normalized in text_full:
                found.append(kw)
        return found

    # ----------------------------------------------------------------
    # Apprentissage des domaines low-priority
    # ----------------------------------------------------------------
    def _load_low_priority_domains(self, path: Optional[Path]) -> set[str]:
        """Charge la liste des domaines low-priority depuis un JSON.

        Format : `{"domains": ["newsletter.com", "promo.example", ...]}`
        Retourne un set vide si le fichier n'existe pas.
        """
        if path is None:
            path = Path("configs/low_priority_domains.json")
        if not path.exists():
            return set()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            domains = data.get("domains", [])
            return {d.lower() for d in domains if isinstance(d, str)}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("failed to load low_priority_domains: %s", e)
            return set()

    def add_low_priority_domain(self, domain: str) -> None:
        """Ajoute un domaine à la liste (utilisé par l'apprentissage)."""
        domain = domain.lower().strip()
        if domain and domain not in self.known_low_priority_domains:
            self.known_low_priority_domains.add(domain)
            logger.info("added low-priority domain: %s", domain)

    def remove_low_priority_domain(self, domain: str) -> None:
        """Retire un domaine (ex: l'utilisateur a répondu/star un email de ce domaine)."""
        domain = domain.lower().strip()
        if domain in self.known_low_priority_domains:
            self.known_low_priority_domains.discard(domain)
            logger.info("removed low-priority domain: %s", domain)
