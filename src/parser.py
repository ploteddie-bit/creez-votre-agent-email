"""Parser d'emails Gmail + sanitization anti-injection.

Toute la logique de transformation d'un message Gmail brut en
`EmailInDB` propre, résistant aux injections de prompt.

Pipeline :
    1. Décodage base64url du body (format Gmail)
    2. Extraction des headers (Subject, From, To, Date)
    3. Si HTML → sanitization nh3 → texte pur
    4. Détection des patterns d'injection connus
    5. Normalisation des caractères Unicode invisibles
    6. Génération du `body_snippet` (500 chars)
    7. Retourne un dict compatible `EmailInDB`

Aucun HTML brut n'est jamais retourné pour affichage — uniquement
archivé. Le texte pur est la seule sortie utilisable.
"""
from __future__ import annotations

import base64
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ============================================================
# Sanitization de chaînes
# ============================================================

# Caractères Unicode invisibles à neutraliser
_INVISIBLE_CHARS = re.compile(
    "[-‍﻿‎‏‪-‮⁠﻿‏﻿‎‪‫‬‭‮⁦-⁩﻿]"
)

# Zero-width et assimilés
_ZERO_WIDTH = re.compile("[\u200B-\u200F\u2028-\u202F\u205F-\u206F\uFEFF]")

# Tags HTML à supprimer entièrement (avec leur contenu)
_DANGEROUS_TAGS = ("script", "style", "iframe", "object", "embed",
                   "form", "input", "button", "link", "meta", "base")

# Attributs HTML considérés comme "trackers" ou "handlers"
_DANGEROUS_ATTRS = re.compile(
    r'\s+(on\w+|style|src|href|data-(?![a-z]+-ignore))="[^"]*"',
    flags=re.IGNORECASE,
)


def sanitize_html(html: str) -> str:
    """Convertit du HTML en texte pur via nh3 (binding Rust, maintenu).

    Paramètres :
    - tag_set: ensemble vide → supprime TOUS les tags
    - attributes: ensemble vide → supprime TOUS les attributs
    - link_rel: force noopener/noreferrer

    Retourne du texte brut SANS aucune balise ni attribut.
    """
    if not html:
        return ""
    import nh3  # lazy import (binding natif)
    return nh3.clean(
        html,
        tags=set(),          # aucune balise conservée → strip complet
        attributes={},       # aucun attribut conservé (dict vide obligatoire)
        link_rel=None,
    )


def strip_invisible_chars(text: str) -> str:
    """Supprime les caractères Unicode invisibles (zero-width, etc.)."""
    if not text:
        return ""
    text = _INVISIBLE_CHARS.sub("", text)
    text = _ZERO_WIDTH.sub("", text)
    return text


def strip_html_comments(text: str) -> str:
    """Supprime les commentaires HTML `<!-- ... -->`."""
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def strip_dangerous_tags(text: str) -> str:
    """Supprime les balises dangereuses et leur contenu."""
    for tag in _DANGEROUS_TAGS:
        text = re.sub(
            rf"<{tag}\b[^>]*>.*?</{tag}>",
            "",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        # Auto-fermantes
        text = re.sub(
            rf"<{tag}\b[^>]*/?>",
            "",
            text,
            flags=re.IGNORECASE,
        )
    return text


def strip_dangerous_attrs(text: str) -> str:
    """Supprime les attributs HTML dangereux (onclick, style, src, ...)."""
    return _DANGEROUS_ATTRS.sub("", text)


# ============================================================
# Détection d'injection (7 patterns de la SPEC)
# ============================================================

INJECTION_PATTERNS: dict[str, re.Pattern[str]] = {
    # 1. CSS display:none / visibility:hidden
    "css_hidden": re.compile(
        r"display\s*:\s*none|visibility\s*:\s*hidden",
        re.IGNORECASE,
    ),
    # 2. Commentaire HTML (caché mais présent)
    "html_comment": re.compile(r"<!--.*?(?:ignore|instruction|system|prompt).*?-->",
                               re.IGNORECASE | re.DOTALL),
    # 3. Texte blanc sur fond blanc
    "white_text": re.compile(
        r"color\s*:\s*(?:#fff|white|#ffffff|rgba?\(\s*255\s*,\s*255\s*,\s*255)",
        re.IGNORECASE,
    ),
    # 4. Zero-width / Unicode invisible (le compteur est dans normalize, ici on flag)
    "zero_width": _ZERO_WIDTH,
    # 5. Homoglyphes courants (lettres cyrilliques qui remplacent latines)
    "homoglyph": re.compile("[ΑΒΕΖΗΙΚΜΝΟΡΤΧаеорсух]"),
    # 6. Sujet contenant un verbe d'instruction
    "subject_injection": re.compile(
        r"^\s*(?:ignore|disregard|forget|override|system|assistant|new instructions)",
        re.IGNORECASE,
    ),
    # 7. Ancien mail RAG contenant "ignore les règles" / "ignore previous"
    "rag_injection": re.compile(
        r"(?:ignore|disregard|forget|override)\s+(?:les\s+règles|previous|all|the|mes|tous)",
        re.IGNORECASE,
    ),
    # Bonus : base64 massif
    "base64_massive": re.compile(r"[A-Za-z0-9+/]{200,}={0,2}"),
    # Bonus : JSON qui ressemble à une instruction système
    "fake_system": re.compile(
        r"\{\s*[\"']?(?:role|system|instruction)[\"']?\s*:\s*[\"'](?:system|assistant|user)[\"']",
        re.IGNORECASE,
    ),
}


def check_injection_patterns(text: str) -> list[str]:
    """Retourne la liste des noms de patterns d'injection détectés.

    Cette fonction ne bloque rien : elle **signale**. C'est au caller
    de décider (logger, alerter sandbox, refuser l'embedding, etc.).
    """
    if not text:
        return []
    found: list[str] = []
    for name, pattern in INJECTION_PATTERNS.items():
        if pattern.search(text):
            found.append(name)
    return found


# ============================================================
# Décodage du format Gmail brut
# ============================================================

def decode_base64url(data: str) -> str:
    """Décode une chaîne base64url (format Gmail) en texte.

    Gmail utilise le base64 URL-safe avec padding optionnel.
    En cas d'erreur de décodage base64, retourne une chaîne vide.
    Le décodage bytes → texte est délégué à `decode_bytes_smart`
    (gestion des vieux encodages ISO-8859-1 / Windows-1252).
    """
    if not data:
        return ""
    try:
        # Ajouter le padding manquant
        padded = data + "=" * (-len(data) % 4)
        # URL-safe alphabet
        decoded = base64.urlsafe_b64decode(padded)
    except Exception as e:
        logger.debug("base64url decode failed: %s", e)
        return ""
    return decode_bytes_smart(decoded)


def decode_bytes_smart(raw: bytes) -> str:
    """Décode des bytes en texte avec détection d'encodage (REVIEW §1.3).

    Stratégie :
      1. UTF-8 strict — le cas nominal moderne.
      2. `chardet.detect()` si UTF-8 échoue et confiance >= 0.7
         (vieux mails français en ISO-8859-1 / Windows-1252).
      3. Fallback UTF-8 avec remplacement — jamais d'exception,
         jamais de crash du pipeline sur un mail mal encodé.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        import chardet  # lazy import

        guess = chardet.detect(raw)
        encoding = guess.get("encoding")
        confidence = guess.get("confidence") or 0.0
        if encoding and confidence >= 0.7:
            logger.debug("encoding detected: %s (%.2f)", encoding, confidence)
            return raw.decode(encoding, errors="replace")
    except Exception as e:
        logger.debug("chardet detection failed: %s", e)
    return raw.decode("utf-8", errors="replace")


def extract_text_from_payload(payload: dict) -> tuple[str, str]:
    """Extrait le texte et le HTML d'un payload Gmail.

    Retourne (text, html). Si le mail est en text/plain, html est vide.
    Si le mail est en text/html, text est la version sanitizée.
    Si multipart, on prend la partie text/plain prioritairement, sinon text/html.
    """
    text = ""
    html = ""
    mime = payload.get("mimeType", "")

    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        text = decode_base64url(data)
    elif mime == "text/html":
        data = payload.get("body", {}).get("data", "")
        html = decode_base64url(data)
        text = sanitize_html(html)
    elif mime.startswith("multipart/"):
        for part in payload.get("parts", []):
            t, h = extract_text_from_payload(part)
            if t and not text:
                text = t
            if h and not html:
                html = h
            if text and html:
                break

    return text, html


def extract_headers(payload: dict) -> dict[str, str]:
    """Extrait les headers standards d'un payload Gmail en dict.

    Retourne un dict vide si headers absents ou non-standard.
    """
    headers: dict[str, str] = {}
    for h in payload.get("headers", []):
        name = h.get("name", "").lower()
        value = h.get("value", "")
        if name and value:
            headers[name] = value
    return headers


def parse_email_address(value: str) -> tuple[str, str, str]:
    """Parse un header From/To en (display_name, email, domain).

    Gère les formats :
        "John Doe" <john@example.com>
        john@example.com
        John Doe <john@example.com>
    """
    if not value:
        return "", "", ""
    import email.utils
    display, addr = email.utils.parseaddr(value)
    if not addr:
        return value.strip(), "", ""
    domain = addr.split("@", 1)[1] if "@" in addr else ""
    return display.strip(' "'), addr.lower(), domain.lower()


# ============================================================
# Entry point : parse un message Gmail brut
# ============================================================

def parse_raw_message(raw: dict, *, snippet_max_len: int = 500) -> dict:
    """Parse un message Gmail brut en dict compatible `EmailInDB`.

    Args:
        raw: dict au format Gmail API v1 (id, threadId, payload, labelIds, internalDate)
        snippet_max_len: taille max du body_snippet (défaut 500)

    Returns:
        Dict avec les champs attendus par `EmailInDB`. Champs optionnels
        absents du message = None ou liste vide.

    Side effects:
        Logge les patterns d'injection détectés.
    """
    payload = raw.get("payload", {})
    headers = extract_headers(payload)
    body_text, body_html = extract_text_from_payload(payload)

    # Nettoyage sécurité
    body_text = strip_invisible_chars(body_text)
    body_text = strip_html_comments(body_text)
    body_text = strip_dangerous_tags(body_text)
    body_text = strip_dangerous_attrs(body_text)

    # Snippet tronqué pour le prompt LLM
    body_snippet = body_text[:snippet_max_len] if body_text else None

    # Subject sécurisé
    subject = strip_invisible_chars(headers.get("subject", ""))
    subject = subject[:500] if subject else None  # borne défensive

    # From / sender
    sender_display, sender_email, sender_domain = parse_email_address(
        headers.get("from", "")
    )

    # Recipients
    recipients: list[str] = []
    for header_name in ("to", "cc", "bcc"):
        for value in headers.get(header_name, "").split(","):
            _, addr, _ = parse_email_address(value)
            if addr:
                recipients.append(addr)

    # Date
    date_str = headers.get("date", "")
    try:
        date_received = parsedate_to_datetime(date_str).astimezone(timezone.utc)
    except (TypeError, ValueError):
        # Fallback sur internalDate (ms epoch)
        internal = raw.get("internalDate")
        if internal:
            date_received = datetime.fromtimestamp(int(internal) / 1000, tz=timezone.utc)
        else:
            date_received = datetime.now(timezone.utc)

    # Labels Gmail
    label_ids = raw.get("labelIds", []) or []
    is_read = "UNREAD" not in label_ids
    is_starred = "STARRED" in label_ids
    is_deleted = "TRASH" in label_ids

    # Détection d'injection (log seulement — le sandbox décide)
    patterns = check_injection_patterns(body_text or "")
    if subject and (subj_pat := check_injection_patterns(subject)):
        patterns.extend(f"subject_{p}" for p in subj_pat)
    if patterns:
        logger.warning(
            "injection patterns detected in email %s: %s",
            raw.get("id", "?"),
            patterns,
        )

    return {
        "id": raw.get("id", ""),
        "thread_id": raw.get("threadId"),
        "sender": sender_display or sender_email,
        "sender_email": sender_email,
        "sender_domain": sender_domain or None,
        "recipients": recipients,
        "subject": subject,
        "body_text": body_text or None,
        "body_snippet": body_snippet,
        "body_html": body_html or None,  # archivage seul, jamais affiché
        "has_attachments": bool(payload.get("parts")),
        "attachment_text": None,  # extrait séparément par attachment_parser
        "date_received": date_received,
        "labels": label_ids,
        "is_read": is_read,
        "is_starred": is_starred,
        "is_deleted": is_deleted,
        "is_archived": "INBOX" not in label_ids and "TRASH" not in label_ids,
    }


# ============================================================
# Helpers de test
# ============================================================

def make_minimal_raw(
    *,
    msg_id: str = "msg_1",
    thread_id: Optional[str] = "thread_1",
    from_: str = "John Doe <john@example.com>",
    subject: str = "Hello",
    body_text: str = "Plain text body",
    body_html: Optional[str] = None,
    label_ids: Optional[list[str]] = None,
    internal_date: Optional[int] = None,
) -> dict:
    """Construit un message Gmail minimal pour les tests.

    Utile pour les tests unitaires et l'idempotence du parser.
    """
    import json

    payload: dict[str, Any] = {
        "mimeType": "text/plain" if not body_html else "text/html",
        "headers": [
            {"name": "From", "value": from_},
            {"name": "Subject", "value": subject},
            {"name": "Date", "value": "Thu, 16 Jul 2026 22:00:00 +0000"},
        ],
        "body": {
            "data": base64.urlsafe_b64encode(
                (body_html or body_text).encode("utf-8")
            ).decode("ascii").rstrip("="),
        },
    }
    if label_ids is None:
        label_ids = ["INBOX"]
    return {
        "id": msg_id,
        "threadId": thread_id,
        "payload": payload,
        "labelIds": label_ids,
        "internalDate": str(internal_date or 1721167200000),
        "_raw": json.dumps(payload),  # pour debug si besoin
    }
