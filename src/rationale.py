"""Explicabilite deterministe des decisions de l'IA.

Quand l'IA decide d'archiver un mail, l'utilisateur veut comprendre
POURQUOI. On genere une explication en francais a partir des donnees
disponibles (rule_name, classification, sender, similar count, etc.).

Approche deterministe (pas d'appel LLM pour expliquer) :
  - Plus rapide (pas de latence)
  - Plus testable (sortie fixe pour des inputs fixes)
  - Plus fiable (pas d'hallucination dans l'explication)

Format des templates : "Action car pattern declenche sur X mails
similaires, regle Y, confiance Z".
"""
from __future__ import annotations

from typing import Optional

from src.models import MailDecision


def build_rationale(
    decision: MailDecision,
    *,
    rule_name: Optional[str] = None,
    similar_count: int = 0,
    similar_action: Optional[str] = None,
    sender_domain: Optional[str] = None,
    llm_call_skipped: bool = False,
    llm_error: Optional[str] = None,
) -> str:
    """Construit une explication en francais de la decision.

    Args:
        decision: la MailDecision finale
        rule_name: nom de la regle statique qui a matche (si applicable)
        similar_count: nombre de mails similaires trouves
        similar_action: action la plus frequente parmi les similaires
        sender_domain: domaine de l'expediteur
        llm_call_skipped: True si on a court-circuite le LLM
        llm_error: nom de l'erreur LLM si applicable

    Returns:
        String en francais, lisible, qui explique la decision.
    """
    op = decision.executable_operation
    cls = decision.classification
    conf = decision.confidence
    rec = decision.recommended_user_action

    # 1. Cas special : regle statique (LLM non appele)
    if llm_call_skipped and rule_name:
        return _rationale_from_rule(rule_name, decision)

    # 2. Cas special : erreur LLM
    if llm_error:
        return (
            f"Defaut sur l'IA (erreur {llm_error}). "
            f"Action proposee : {op}. "
            f"Recommandation : {rec}."
        )

    # 3. Cas normal : classification par LLM
    parts: list[str] = []

    # 3a. Explication de la classification
    if cls == "newsletter":
        parts.append("identifie comme newsletter")
    elif cls == "receipt":
        parts.append("identifie comme recu/facture")
    elif cls == "security_alert":
        parts.append("detecte comme alerte de securite")
    elif cls == "needs_reply":
        parts.append("detecte comme necessitant une reponse")
    elif cls == "personal":
        parts.append("identifie comme mail personnel")
    else:
        parts.append("classification incertaine")

    # 3b. Explication de l'action
    if op == "archive":
        parts.append("proposition d'archivage")
    elif op == "mark_read":
        parts.append("proposition de marquage comme lu")
    elif op == "star":
        parts.append("proposition d'etoilage")
    elif op == "move_ia_review":
        parts.append("transfert vers IA-Review pour validation manuelle")
    elif op == "none":
        parts.append("aucune action automatique recommandee")

    # 3c. Justification heuristique (le contexte RAG)
    if similar_count > 0 and similar_action:
        parts.append(
            f"{similar_count} mail(s) similaire(s) dans l'historique, "
            f"action la plus frequente : {similar_action}"
        )
    elif similar_count == 0:
        parts.append("aucun mail similaire dans l'historique (cold start)")

    # 3d. Confiance
    if conf >= 0.9:
        parts.append(f"confiance elevee ({conf:.0%})")
    elif conf >= 0.7:
        parts.append(f"confiance moyenne ({conf:.0%})")
    else:
        parts.append(f"confiance faible ({conf:.0%})")

    # 3e. Recommandation utilisateur
    if rec == "reply_manually":
        parts.append("reponse manuelle recommandee")
    elif rec == "check_manually":
        parts.append("verification manuelle recommandee")

    # 3f. Sender (si connu)
    if sender_domain:
        parts.append(f"expediteur : {sender_domain}")

    return "Action : " + ", ".join(parts) + "."


def _rationale_from_rule(rule_name: str, decision: MailDecision) -> str:
    """Explication specifique aux court-circuits du Rules Engine."""
    op = decision.executable_operation
    reason = decision.reason

    if rule_name == "critical_keyword":
        return (
            f"Regle de securite : un mot-cle critique (facture, paiement, "
            f"banque, etc.) a ete detecte dans le mail. "
            f"Action : {op} pour validation manuelle obligatoire."
        )
    if rule_name == "noreply_with_critical":
        return (
            f"Regle noreply + mot-cle critique : mail automatique d'un "
            f"expediteur noreply contenant un mot-cle sensible. "
            f"Action : {op} vers IA-Review."
        )
    if rule_name == "spam_label":
        return (
            f"Regle anti-spam : le mail est deja labelle SPAM par Gmail. "
            f"Action : {op} (marquage comme lu, archivage ulterieur)."
        )
    if rule_name == "noreply_low_priority_domain":
        return (
            f"Regle noreply + domaine de confiance : expediteur automatique "
            f"connu pour envoyer des newsletters low-priority. "
            f"Action : {op}."
        )
    if rule_name == "noreply_unknown_domain":
        return (
            f"Regle noreply domaine inconnu : mail automatique d'un "
            f"expediteur jamais vu. Action proposee : {op} (validation "
            f"humaine recommandee)."
        )
    if rule_name == "default":
        return (
            f"Aucune regle declenchee, fallback P1. "
            f"Recommandation : {op} (validation manuelle via dashboard)."
        )
    return f"Regle '{rule_name}' : {reason}"
