"""Client IMAP Gmail (mot de passe d'application) — backend mail unique.

Ce module est le **seul point de contact avec la boîte mail**. Il
utilise une connexion IMAP4 SSL authentifiée par **mot de passe
d'application** Google. Décision produit du 2026-07-17 (choix
utilisateur validé) : l'ancien backend OAuth / API Gmail a été
supprimé (quotas, console GCP, flow navigateur trop lourds) — un mot
de passe d'application IMAP se configure en 2 minutes et suffit pour
tout ce que fait agent-mail (lire, lister, modifier des labels —
jamais envoyer, jamais supprimer).

**Interface historiquement alignée sur l'API Gmail v1** : mêmes
méthodes publiques et mêmes formats de retour que l'ancien client
API, pour que le pipeline complet (parser, ingester, recommender,
action_worker, observer) fonctionne sans modification.

Sécurité — gardes-fous (équivalents de l'ancienne allowlist API) :
  1. Allowlist explicite des commandes IMAP (`ALLOWED_COMMANDS`) :
     APPEND (= envoi), EXPUNGE / DELETE (suppression) en sont
     **absentes**, comme `messages().send` / `messages().delete`.
  2. `validate_call()` vérifié avant chaque commande réseau.
  3. Le mot de passe d'application n'est jamais loggé.
  4. Aucune suppression définitive : `archive` = retrait du label
     `\\Inbox` (le mail reste dans « All Mail »).

Configuration : `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` lus depuis
l'environnement, `.env` (racine) ou `configs/.env`. Vérification :
`python -m src.main setup-imap`.

Correspondances d'identifiants :
  - `emails.id` ↔ `X-GM-MSGID` (stable, même valeur que l'ID API Gmail)
  - thread      ↔ `X-GM-THRID`
  - `historyId` ↔ UID IMAP. Les UID n'expirent jamais → fin des
    erreurs « 404 historyId expired » de l'API (REVIEW §1.4 résolu
    par construction côté IMAP).

Limites connues (documentées, acceptées) :
  - Dossier de sync : « All Mail », résolu via le flag spécial `\\All`
    (RFC 6154) — indépendant de la langue du compte (couvre INBOX +
    archivés, hors spam/corbeille — comme la query `-label:spam`).
  - La recherche est déléguée à l'extension Gmail `X-GM-RAW`
    (mêmes opérateurs que la recherche Gmail, pas de parsing local).
"""
from __future__ import annotations

import base64
import imaplib
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# === Constantes ===

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
IMAP_TIMEOUT_SECONDS = 60

# Dossier de sync (fallback — en production il est résolu via
# l'attribut spécial \All du LIST, indépendant de la langue du
# compte : « [Gmail]/All Mail » en EN, « [Gmail]/Tous les messages »
# en FR, etc.)
ALL_MAIL_FOLDER = '"[Gmail]/All Mail"'

# Borne défensive : messages max traités par sync delta
MAX_DELTA_MESSAGES = 500

# Allowlist stricte : seules ces commandes IMAP sont autorisées.
# Toute autre commande lève IMAPForbiddenCall.
ALLOWED_COMMANDS: frozenset[str] = frozenset({
    "CAPABILITY",
    "LOGIN",
    "SELECT",
    "EXAMINE",
    "LIST",
    "CREATE",
    "SEARCH",
    "UID SEARCH",
    "FETCH",
    "UID FETCH",
    "STORE",
    "UID STORE",
    "NOOP",
    "LOGOUT",
    # Délibérément absents (interdits) :
    # - APPEND                 (= envoyer un message → comme messages.send)
    # - EXPUNGE / UID EXPUNGE / CLOSE (suppression définitive)
    # - DELETE / RENAME mailbox       (destructeur)
    # - COPY / MOVE                   (inutiles : labels via X-GM-LABELS)
})

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# === Erreurs ===

class IMAPClientError(Exception):
    """Erreur de base du client IMAP."""


class IMAPAuthError(IMAPClientError):
    """Échec d'authentification IMAP (app password / IMAP désactivé)."""


class IMAPForbiddenCall(IMAPClientError):
    """Tentative d'exécution d'une commande IMAP interdite.

    Levée **avant tout appel réseau**. Ne doit jamais être catchée
    silencieusement.
    """


# === Helpers de module ===

def _read_env_var(name: str) -> Optional[str]:
    """Lit une variable : environnement réel, puis `.env` racine,
    puis `configs/.env` (cohérent avec `src.db._read_env_var`,
    étendu au `.env` racine où l'utilisateur stocke ses credentials).
    """
    if (val := os.environ.get(name)) is not None:
        return val
    for candidate in (_PROJECT_ROOT / ".env",
                      _PROJECT_ROOT / "configs" / ".env"):
        if not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == name:
                return v.strip().strip('"').strip("'")
    return None


_ATTR_NUM_RE = re.compile(r"(UID|X-GM-MSGID|X-GM-THRID)\s+(\d+)")
_FLAGS_RE = re.compile(r"FLAGS\s+\(([^)]*)\)")
# Regex propre au projet : l'attribut `imaplib.Internaldate` a été
# renommé en Python 3.14 — ne pas dépendre des internals d'imaplib.
_INTERNALDATE_RE = re.compile(
    rb'INTERNALDATE "(?P<day>[ 0123][0-9])-(?P<mon>[A-Z][a-z][a-z])-'
    rb'(?P<year>[0-9]{4}) (?P<hour>[0-9]{2}):(?P<min>[0-9]{2}):'
    rb'(?P<sec>[0-9]{2}) (?P<zonen>[-+])(?P<zoneh>[0-9]{2})'
    rb'(?P<zonem>[0-9]{2})"'
)
_MONTH_TO_NUM = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
# Le `)` fermant de X-GM-LABELS est identifié par lookahead (suivi de
# `)` d'enveloppe, d'un autre attribut, ou de fin) — robuste aux labels
# contenant des parenthèses, ex. X-GM-LABELS ("Foo (bar)").
_LABELS_RE = re.compile(
    r"X-GM-LABELS\s+\((.*?)\)(?=\s*\)|\s+(?:RFC822|INTERNALDATE|FLAGS|X-GM-)|\s*$)",
    re.DOTALL,
)
_LABEL_TOKEN_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|(\S+)')
_LIST_LINE_RE = re.compile(
    r'\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+'
    r'(?P<name>"?(?:[^"\\]|\\.)*"?)\s*$'
)


def _parse_label_list(text: str) -> list[str]:
    """Tokenise le contenu d'un `X-GM-LABELS (...)` (atoms + quoted)."""
    labels: list[str] = []
    for m in _LABEL_TOKEN_RE.finditer(text):
        token = m.group(1) if m.group(1) is not None else m.group(2)
        labels.append(token.replace('\\"', '"').replace("\\\\", "\\"))
    return labels


def _quote_mailbox(name: str) -> str:
    """Quote un nom de dossier IMAP (imaplib ne quote pas ses args)."""
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _parse_list_line(text: str) -> Optional[tuple[list[str], str]]:
    """Parse une ligne LIST → (flags, nom de dossier déquoté)."""
    m = _LIST_LINE_RE.match(text)
    if not m:
        return None
    name = m.group("name").strip()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1]
    name = name.replace('\\"', '"').replace("\\\\", "\\")
    if not name:
        return None
    return m.group("flags").split(), name


def _derive_label_ids(flags: list[str], xlabels: list[str]) -> list[str]:
    """Convertit FLAGS + X-GM-LABELS IMAP en `labelIds` format API Gmail."""
    upper = {f.upper() for f in flags}
    label_ids: list[str] = []
    if "\\SEEN" not in upper:
        label_ids.append("UNREAD")
    if "\\FLAGGED" in upper:
        label_ids.append("STARRED")
    for lbl in xlabels:
        if lbl == "\\Inbox":
            label_ids.append("INBOX")
        elif lbl.startswith("\\"):
            continue  # \Sent, \Draft, \Important... : non utilisés
        else:
            label_ids.append(lbl)
    return label_ids


def _headers_to_list(msg: Any) -> list[dict[str, str]]:
    """Convertit les headers d'un `email.message.Message` en liste
    `{"name", "value"}` format API Gmail (valeurs décodées RFC2047)."""
    headers: list[dict[str, str]] = []
    for name in msg.keys():
        try:
            value = str(msg[name])  # policy.default décode les mots encodés
        except Exception:
            try:
                value = str(make_header(decode_header(msg.get(name, ""))))
            except Exception:
                value = ""
        headers.append({"name": name, "value": value})
    return headers


def _part_to_payload(part: Any) -> dict[str, Any]:
    """Adaptateur récursif : part MIME → payload format API Gmail v1."""
    node: dict[str, Any] = {
        "mimeType": part.get_content_type(),
        "headers": _headers_to_list(part),
    }
    filename = part.get_filename()
    if filename:
        node["filename"] = filename
    if part.is_multipart():
        node["body"] = {"size": 0}
        node["parts"] = [_part_to_payload(p) for p in part.iter_parts()]
    else:
        try:
            content = part.get_payload(decode=True) or b""
        except Exception:
            content = b""
        node["body"] = {
            "data": base64.urlsafe_b64encode(content).decode("ascii"),
            "size": len(content),
        }
    return node


def _internal_date_ms(header: bytes) -> int:
    """Extrait `INTERNALDATE "01-Jul-2026 10:00:00 +0000"` → epoch ms."""
    try:
        mo = _INTERNALDATE_RE.search(header)
        if mo:
            offset_min = (int(mo.group("zoneh")) * 60
                          + int(mo.group("zonem")))
            if mo.group("zonen") == b"-":
                offset_min = -offset_min
            dt = datetime(
                int(mo.group("year")),
                _MONTH_TO_NUM[mo.group("mon").decode("ascii")],
                int(mo.group("day")),
                int(mo.group("hour")),
                int(mo.group("min")),
                int(mo.group("sec")),
                tzinfo=timezone(timedelta(minutes=offset_min)),
            )
            return int(dt.timestamp() * 1000)
    except Exception as e:
        logger.debug("INTERNALDATE parse failed: %s", e)
    return int(datetime.now(timezone.utc).timestamp() * 1000)


# === Client ===

class IMAPClient:
    """Client IMAP Gmail — interface mail unique du projet.

    Mêmes méthodes publiques et formats de retour que l'ancien client
    API (supprimé) : observer / action_worker / parser inchangés.
    La connexion n'est ouverte qu'à la demande (lazy) pour permettre
    les tests unitaires sans réseau.
    """

    # Correspondance label API → (commande STORE add, valeur, remove, valeur)
    _SYSTEM_LABEL_OPS: dict[str, tuple[str, str, str, str]] = {
        "UNREAD": ("-FLAGS", r"(\Seen)", "+FLAGS", r"(\Seen)"),
        "STARRED": ("+FLAGS", r"(\Flagged)", "-FLAGS", r"(\Flagged)"),
        "INBOX": ("+X-GM-LABELS", r"(\Inbox)", "-X-GM-LABELS", r"(\Inbox)"),
    }

    def __init__(self, address: Optional[str] = None,
                 app_password: Optional[str] = None,
                 host: str = IMAP_HOST, port: int = IMAP_PORT) -> None:
        self.host = host
        self.port = port
        self._address = address
        self._app_password = app_password
        self._conn: Optional[imaplib.IMAP4_SSL] = None

    # ----------------------------------------------------------------
    # Allowlist
    # ----------------------------------------------------------------
    def allowed_methods(self) -> frozenset[str]:
        """Retourne l'allowlist courante des commandes autorisées."""
        return ALLOWED_COMMANDS

    def validate_call(self, command: str) -> None:
        """Vérifie qu'une commande IMAP est autorisée. Lève sinon.

        Verrou de sécurité : appelé avant chaque commande réseau
        (via `_run`). Ne JAMAIS bypasser.
        """
        clean = " ".join(command.split()).upper()
        if clean not in ALLOWED_COMMANDS:
            logger.error("🚫 FORBIDDEN IMAP command attempted: %s", command)
            raise IMAPForbiddenCall(
                f"Command '{command}' is not in the allowlist. "
                "This is a hard security constraint. "
                "See IMAPClient.ALLOWED_COMMANDS."
            )

    # ----------------------------------------------------------------
    # Connexion (lazy, avec reconnexion unique)
    # ----------------------------------------------------------------
    def _credentials(self) -> tuple[str, str]:
        address = self._address or _read_env_var("GMAIL_ADDRESS")
        password = self._app_password or _read_env_var("GMAIL_APP_PASSWORD")
        if not address or not password:
            raise IMAPAuthError(
                "GMAIL_ADDRESS / GMAIL_APP_PASSWORD manquants. Les définir "
                "dans l'environnement, `.env` (racine) ou `configs/.env`. "
                "Créer un mot de passe d'application : compte Google → "
                "Sécurité → Mots de passe des applications."
            )
        return address, password

    def _connect(self) -> imaplib.IMAP4_SSL:
        if self._conn is not None:
            return self._conn
        address, password = self._credentials()
        try:
            conn = imaplib.IMAP4_SSL(self.host, self.port,
                                     timeout=IMAP_TIMEOUT_SECONDS)
            self.validate_call("LOGIN")
            conn.login(address, password)
            folder = self._find_all_mail_folder(conn)
            self.validate_call("SELECT")
            typ, _ = conn.select(folder)
            if typ != "OK":
                raise IMAPClientError(
                    f"SELECT {folder} failed ({typ}) — "
                    "le dossier « All Mail » est introuvable."
                )
        except imaplib.IMAP4.error as e:
            raise IMAPAuthError(
                f"IMAP login/select failed for {address}: {e}. Vérifier "
                "que l'IMAP est activé (Gmail → Paramètres → Transfert "
                "et POP/IMAP) et que le mot de passe d'application est "
                "correct."
            ) from e
        except OSError as e:
            raise IMAPClientError(f"IMAP connection failed: {e}") from e
        self._conn = conn
        return conn

    def _find_all_mail_folder(self, conn: imaplib.IMAP4_SSL) -> str:
        """Résout le dossier « All Mail » via l'attribut `\\All`.

        RFC 6154 (SPECIAL-USE) : le dossier portant le flag `\\All` est
        le même quel que soit la langue du compte — contrairement à son
        nom (« All Mail » EN, « Tous les messages » FR, ...).
        Fallback sur `ALL_MAIL_FOLDER` si le LIST échoue.
        """
        try:
            self.validate_call("LIST")
            typ, data = conn.list()
            if typ == "OK":
                for line in data or []:
                    if not isinstance(line, bytes):
                        continue
                    parsed = _parse_list_line(
                        line.decode("utf-8", "replace"))
                    if not parsed:
                        continue
                    flags, name = parsed
                    if "\\ALL" in {f.upper() for f in flags}:
                        logger.info("All Mail folder resolved: %s", name)
                        return _quote_mailbox(name)
        except Exception as e:
            logger.warning("LIST special-use failed (%s) — fallback %s",
                           e, ALL_MAIL_FOLDER)
        return ALL_MAIL_FOLDER

    def close(self) -> None:
        """Ferme proprement la connexion (idempotent)."""
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            pass
        try:
            self.validate_call("LOGOUT")
            conn.logout()
        except Exception:
            pass

    def _run(self, command: str, fn: Any) -> list:
        """Exécute une commande IMAP : allowlist + 1 reconnexion max.

        `fn(conn) -> (typ, data)`. Lève IMAPClientError si typ != 'OK'.
        """
        self.validate_call(command)
        last_error: Optional[Exception] = None
        for _attempt in (0, 1):
            conn = self._connect()
            try:
                typ, data = fn(conn)
            except (imaplib.IMAP4.abort, OSError) as e:
                # Connexion tombée (idle timeout Gmail, réseau) :
                # on reconnecte une fois et on réessaie.
                logger.warning("IMAP %s: connection dropped (%s), reconnecting",
                               command, e)
                last_error = e
                self.close()
                continue
            if typ != "OK":
                raise IMAPClientError(
                    f"{command} failed ({typ}): {str(data)[:200]}"
                )
            return data or []
        raise IMAPClientError(f"{command} failed after reconnect: {last_error}")

    # ----------------------------------------------------------------
    # Résolution d'identifiants
    # ----------------------------------------------------------------
    def _uid_for_msgid(self, msg_id: str) -> int:
        """Résout un X-GM-MSGID (id `emails.id`) en UID IMAP courant."""
        data = self._run("UID SEARCH", lambda c: c.uid(
            "SEARCH", "X-GM-MSGID", str(msg_id)))
        uids = data[0].split() if data and data[0] else []
        if not uids:
            raise IMAPClientError(f"404 message not found: {msg_id}")
        return int(uids[0])

    def _fetch_msgids(self, uids: list[int]) -> list[dict]:
        """Batch : UIDs → [{id: X-GM-MSGID, threadId: X-GM-THRID}]."""
        if not uids:
            return []
        uid_set = ",".join(str(u) for u in uids)
        data = self._run("UID FETCH", lambda c: c.uid(
            "FETCH", uid_set, "(X-GM-MSGID X-GM-THRID)"))
        by_uid: dict[int, dict] = {}
        for item in data:
            header = item if isinstance(item, bytes) else (
                item[0] if isinstance(item, tuple) else b"")
            attrs = dict(_ATTR_NUM_RE.findall(
                header.decode("utf-8", "replace")))
            uid, msgid = attrs.get("UID"), attrs.get("X-GM-MSGID")
            if uid and msgid:
                by_uid[int(uid)] = {
                    "id": msgid,
                    "threadId": attrs.get("X-GM-THRID", msgid),
                }
        return [by_uid[u] for u in uids if u in by_uid]

    # ----------------------------------------------------------------
    # Interface publique (utilisée par observer / action_worker)
    # ----------------------------------------------------------------
    def list_messages(
        self, query: str = "", max_results: int = 100,
        page_token: Optional[str] = None,
    ) -> tuple[list[dict], Optional[str]]:
        """Liste les messages (format API : `[{id, threadId}]`).

        La query Gmail (`newer_than:6m -label:spam ...`) est passée
        telle quelle via l'extension Gmail `X-GM-RAW` — mêmes
        opérateurs que la recherche Gmail, aucun parsing local.
        Pagination : `page_token` = offset (la recherche est triée
        par UID décroissant, donc stable).
        """
        offset = int(page_token) if page_token else 0
        clean_query = " ".join(query.split()).replace('"', "")
        if clean_query:
            criteria: tuple = ("CHARSET", "UTF-8",
                               "X-GM-RAW", f'"{clean_query}"')
        else:
            criteria = ("ALL",)
        data = self._run("UID SEARCH", lambda c: c.uid("SEARCH", *criteria))
        uids = data[0].split() if data and data[0] else []
        ordered = sorted((int(u) for u in uids), reverse=True)
        page = ordered[offset:offset + max_results]
        next_token = (str(offset + max_results)
                      if offset + max_results < len(ordered) else None)
        return self._fetch_msgids(page), next_token

    def get_message(self, msg_id: str, format: str = "full") -> dict:
        """Récupère un message complet, adapté au format API Gmail v1."""
        uid = self._uid_for_msgid(msg_id)
        data = self._run("UID FETCH", lambda c: c.uid(
            "FETCH", str(uid),
            "(X-GM-MSGID X-GM-THRID FLAGS X-GM-LABELS INTERNALDATE RFC822)"))
        raw_bytes: Optional[bytes] = None
        header = b""
        for item in data:
            if isinstance(item, tuple) and len(item) == 2:
                header, raw_bytes = item[0], item[1]
                break
        if raw_bytes is None:
            raise IMAPClientError(
                f"empty FETCH response for message {msg_id}")
        attrs_text = header.decode("utf-8", "replace")
        attrs = dict(_ATTR_NUM_RE.findall(attrs_text))
        flags_m = _FLAGS_RE.search(attrs_text)
        labels_m = _LABELS_RE.search(attrs_text)
        label_ids = _derive_label_ids(
            flags_m.group(1).split() if flags_m else [],
            _parse_label_list(labels_m.group(1)) if labels_m else [],
        )
        msg = BytesParser(policy=policy.default).parsebytes(raw_bytes)
        return {
            "id": str(msg_id),
            "threadId": attrs.get("X-GM-THRID", str(msg_id)),
            "labelIds": label_ids,
            "internalDate": str(_internal_date_ms(header)),
            "payload": _part_to_payload(msg),
        }

    def list_history(self, start_history_id: str) -> list[dict]:
        """Delta sync : messages arrivés après `start_history_id` (= UID).

        Retourne un record unique `{"id": <max_uid>, "messages": [...]}`
        — format history attendu par l'observer (sync delta).
        Les UID n'expirent jamais : pas de 404, pas de full resync.
        """
        try:
            start_uid = int(start_history_id)
        except (TypeError, ValueError) as e:
            raise IMAPClientError(
                f"invalid historyId/UID: {start_history_id!r}") from e
        data = self._run("UID SEARCH", lambda c: c.uid(
            "SEARCH", "UID", f"{start_uid + 1}:*"))
        uids = data[0].split() if data and data[0] else []
        # RFC 3501 : « start:* » inclut le dernier message même si
        # start > max UID → filtrage strict côté client.
        new_uids = sorted(int(u) for u in uids if int(u) > start_uid)
        if not new_uids:
            return []
        new_uids = new_uids[:MAX_DELTA_MESSAGES]
        return [{
            "id": str(new_uids[-1]),
            "messages": self._fetch_msgids(new_uids),
        }]

    def modify_labels(self, msg_id: str, *, add: Optional[list[str]] = None,
                      remove: Optional[list[str]] = None) -> dict:
        """Modifie les labels d'un message (STORE FLAGS / X-GM-LABELS).

        Mapping :
          remove UNREAD  → +FLAGS (\\Seen)        (mark_read)
          add    UNREAD  → -FLAGS (\\Seen)
          add    STARRED → +FLAGS (\\Flagged)     (star)
          remove STARRED → -FLAGS (\\Flagged)     (unstar)
          remove INBOX   → -X-GM-LABELS (\\Inbox) (archive — non destructif)
          add    INBOX   → +X-GM-LABELS (\\Inbox)
          autre label    → ±X-GM-LABELS ("nom")   (ex. IA-Review)
        """
        if not add and not remove:
            raise ValueError("add or remove must be non-empty")
        uid = self._uid_for_msgid(msg_id)
        ops: list[tuple[str, str]] = []
        for label in remove or []:
            ops.append(self._label_op(label, adding=False))
        for label in add or []:
            ops.append(self._label_op(label, adding=True))
        for store_cmd, value in ops:
            self._run("UID STORE", lambda c, sc=store_cmd, v=value: c.uid(
                "STORE", str(uid), sc, v))
        # Retour au format API : labels courants du message
        data = self._run("UID FETCH", lambda c: c.uid(
            "FETCH", str(uid), "(FLAGS X-GM-LABELS)"))
        text = " ".join(
            (item if isinstance(item, bytes) else item[0]).decode(
                "utf-8", "replace")
            for item in data
        )
        flags_m = _FLAGS_RE.search(text)
        labels_m = _LABELS_RE.search(text)
        return {
            "id": str(msg_id),
            "labelIds": _derive_label_ids(
                flags_m.group(1).split() if flags_m else [],
                _parse_label_list(labels_m.group(1)) if labels_m else [],
            ),
        }

    def _label_op(self, label: str, *, adding: bool) -> tuple[str, str]:
        if label in self._SYSTEM_LABEL_OPS:
            add_cmd, add_val, rm_cmd, rm_val = self._SYSTEM_LABEL_OPS[label]
            return (add_cmd, add_val) if adding else (rm_cmd, rm_val)
        safe = label.replace("\\", "\\\\").replace('"', '\\"')
        return ("+X-GM-LABELS" if adding else "-X-GM-LABELS",
                f'("{safe}")')

    def list_labels(self) -> list[dict]:
        """Liste les labels IMAP (dossiers) au format API labels."""
        data = self._run("LIST", lambda c: c.list())
        labels: list[dict] = []
        for line in data:
            if not isinstance(line, bytes):
                continue
            parsed = _parse_list_line(line.decode("utf-8", "replace"))
            if not parsed:
                continue
            _flags, name = parsed
            is_system = name.upper() == "INBOX" or name.startswith("[Gmail]/")
            labels.append({
                "id": name,
                "name": name,
                "type": "system" if is_system else "user",
            })
        return labels

    def create_label(self, name: str) -> str:
        """Crée un label IMAP (dossier). Retourne son id (= son nom)."""
        safe = name.replace("\\", "\\\\").replace('"', '\\"')
        self._run("CREATE", lambda c: c.create(f'"{safe}"'))
        return name


# === Factory : backend mail unique ===

def create_mail_client(backend: Optional[str] = None) -> Any:
    """Retourne le client mail du projet : `IMAPClient` (app password).

    L'ancien backend OAuth / API Gmail a été **supprimé** le 2026-07-17
    (quotas, console GCP, flow navigateur trop lourds) : l'IMAP par
    mot de passe d'application est le seul backend supporté.

    `backend` / `EMAIL_BACKEND` n'acceptent plus que "imap" (défaut) —
    toute autre valeur est ignorée avec un avertissement. Gardée comme
    point d'entrée unique pour que `observer` / `action_worker` n'aient
    pas à connaître la classe concrète.
    """
    choice = (backend or _read_env_var("EMAIL_BACKEND") or "imap").lower()
    if choice not in ("imap", "auto"):
        logger.warning(
            "EMAIL_BACKEND=%r ignoré : le backend OAuth/API Gmail a été "
            "supprimé — utilisation de l'IMAP.", choice)
    logger.info("backend mail: IMAP (mot de passe d'application)")
    return IMAPClient()
