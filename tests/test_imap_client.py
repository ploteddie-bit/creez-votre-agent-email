"""Tests du backend IMAP (mot de passe d'application).

Couvre :
  - l'allowlist des commandes IMAP (APPEND/EXPUNGE/DELETE interdites) ;
  - `list_messages` (format API Gmail, pagination par offset) ;
  - `get_message` (adaptation RFC822 → payload Gmail API v1) et son
    intégration avec `parser.parse_raw_message` (pipeline inchangé) ;
  - `modify_labels` (mapping UNREAD/INBOX/STARRED/labels custom) ;
  - `list_history` (delta sync par UID — sans expiration) ;
  - `list_labels` / `create_label` ;
  - la reconnexion automatique après perte de connexion ;
  - la factory `create_mail_client` (IMAP vs OAuth).

Tout passe par `FakeIMAP4_SSL` : aucun réseau, aucun credential réel.
"""
from __future__ import annotations

import imaplib
from datetime import datetime, timezone
from typing import Optional

import pytest

import src.imap_client as imap_module
from src.imap_client import (
    ALLOWED_COMMANDS,
    IMAPClient,
    IMAPForbiddenCall,
    create_mail_client,
)


# ============================================================
# Double de test : imaplib.IMAP4_SSL
# ============================================================

class FakeIMAP4_SSL:
    """Simule le serveur IMAP Gmail en mémoire.

    Les labels/flags sont stockés comme tokens de réponse IMAP bruts
    (ex. ``\\Inbox``, ``"IA-Review"``, ``\\Seen``).
    """

    seed_messages: list[dict] = []
    instances: list["FakeIMAP4_SSL"] = []
    list_lines: Optional[list] = None  # override pour tests localisés

    def __init__(self, host: str, port: int, timeout=None) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.store: dict[int, dict] = {}
        self.uid_calls: list[tuple] = []
        self.created: list[str] = []
        self.selected: Optional[str] = None
        for m in self.seed_messages:
            self.store[m["uid"]] = {
                "msgid": m["msgid"],
                "thrid": m.get("thrid", m["msgid"]),
                "flags": set(m.get("flags", ())),
                "labels": set(m.get("labels", ())),
                "raw": m.get("raw", b""),
                "internaldate": m.get(
                    "internaldate", "01-Jul-2026 10:00:00 +0000"),
            }
        FakeIMAP4_SSL.instances.append(self)

    # --- connexion ---
    def login(self, user, password):
        assert user and password  # jamais vides
        return "OK", [b"LOGIN completed"]

    def select(self, mailbox="INBOX", readonly=False):
        self.selected = mailbox
        return "OK", [b"1"]

    def close(self):
        return "OK", [b"closed"]

    def logout(self):
        return "OK", [b"bye"]

    # --- commandes ---
    def uid(self, command, *args):
        self.uid_calls.append((command, args))
        handler = getattr(self, f"_uid_{command.lower()}")
        return handler(*args)

    def _uid_search(self, *args):
        args = list(args)
        if args and str(args[0]).upper() == "CHARSET":
            args = args[2:]
        key = str(args[0]).upper()
        if key in ("ALL", "X-GM-RAW"):
            return "OK", [self._uid_list(sorted(self.store))]
        if key == "X-GM-MSGID":
            want = str(args[1])
            return "OK", [self._uid_list(
                [u for u in sorted(self.store)
                 if self.store[u]["msgid"] == want])]
        if key == "UID":
            lo = int(str(args[1]).split(":", 1)[0])
            matching = [u for u in sorted(self.store) if u >= lo]
            if not matching and self.store:
                # RFC 3501 : « n:* » inclut le dernier message même si
                # n > max UID — le client doit filtrer.
                matching = [max(self.store)]
            return "OK", [self._uid_list(matching)]
        raise AssertionError(f"unsupported SEARCH: {args}")

    @staticmethod
    def _uid_list(uids: list[int]) -> bytes:
        return b" ".join(str(u).encode() for u in uids)

    def _uid_fetch(self, uid_set, items):
        uids: list[int] = []
        for part in str(uid_set).split(","):
            if ":" in part:
                lo = int(part.split(":", 1)[0])
                uids.extend(u for u in sorted(self.store) if u >= lo)
            elif part:
                uids.append(int(part))
        out: list = []
        for uid in uids:
            rec = self.store[uid]
            flags = " ".join(sorted(rec["flags"]))
            labels = " ".join(sorted(rec["labels"]))
            if "RFC822" in items:
                header = (
                    f'{uid} (UID {uid} X-GM-MSGID {rec["msgid"]} '
                    f'X-GM-THRID {rec["thrid"]} FLAGS ({flags}) '
                    f'X-GM-LABELS ({labels}) '
                    f'INTERNALDATE "{rec["internaldate"]}" '
                    f'RFC822 {{{len(rec["raw"])}}}'
                ).encode()
                out.append((header, rec["raw"]))
                out.append(b")")  # parenthèse fermante après le littéral
            elif "X-GM-LABELS" in items:
                out.append(
                    f"{uid} (UID {uid} FLAGS ({flags}) "
                    f"X-GM-LABELS ({labels}))".encode())
            else:
                out.append(
                    f'{uid} (UID {uid} X-GM-MSGID {rec["msgid"]} '
                    f'X-GM-THRID {rec["thrid"]})'.encode())
        return "OK", out

    def _uid_store(self, uid, cmd, value):
        rec = self.store[int(uid)]
        tokens = str(value).strip()[1:-1].split()  # contenu des ( )
        target = rec["flags"] if "FLAGS" in str(cmd) else rec["labels"]
        if str(cmd).startswith("+"):
            target.update(tokens)
        else:
            target.difference_update(tokens)
        return "OK", [b"STORE completed"]

    def list(self, *args):
        if self.list_lines is not None:
            return "OK", list(self.list_lines)
        return "OK", [
            b'(\\HasNoChildren) "/" INBOX',
            b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
            b'(\\HasNoChildren) "/" "IA-Review"',
        ]

    def create(self, mailbox):
        self.created.append(mailbox)
        return "OK", [b"CREATE completed"]


class FlakyIMAP4_SSL(FakeIMAP4_SSL):
    """Lâche la connexion une fois, puis fonctionne (reconnexion)."""
    fail_armed = True

    def _uid_search(self, *args):
        if FlakyIMAP4_SSL.fail_armed:
            FlakyIMAP4_SSL.fail_armed = False
            raise imaplib.IMAP4.abort("connection reset by peer")
        return super()._uid_search(*args)


# Message RFC822 de test : sujet accentué (RFC2047) + multipart.
RAW_MESSAGE = (
    b"From: =?UTF-8?Q?C=C3=A9dric_Dupont?= <cedric@example.com>\r\n"
    b"To: moi@example.com\r\n"
    b"Subject: =?UTF-8?B?UsOpdW5pb24gZGUgbHVuZGk=?=\r\n"  # "Réunion de lundi"
    b"Date: Tue, 01 Jul 2026 10:00:00 +0000\r\n"
    b"Message-ID: <abc123@example.com>\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/alternative; boundary="xyz"\r\n'
    b"\r\n"
    b"--xyz\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"Content-Transfer-Encoding: base64\r\n"
    b"\r\n"
    b"Qm9uam91ciwgY2EgbWFyY2hlID8=\r\n"  # "Bonjour, ca marche ?"
    b"--xyz\r\n"
    b"Content-Type: text/html; charset=utf-8\r\n"
    b"\r\n"
    b"<p>Bonjour</p>\r\n"
    b"--xyz--\r\n"
)


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def fake_imap(monkeypatch):
    FakeIMAP4_SSL.instances = []
    FakeIMAP4_SSL.seed_messages = []
    FakeIMAP4_SSL.list_lines = None
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP4_SSL)
    yield FakeIMAP4_SSL
    FakeIMAP4_SSL.seed_messages = []
    FakeIMAP4_SSL.list_lines = None


def _client() -> IMAPClient:
    return IMAPClient(address="user@example.com", app_password="app-secret")


def _seed(uid: int, msgid: str, **kw) -> dict:
    return {"uid": uid, "msgid": msgid, **kw}


# ============================================================
# Allowlist
# ============================================================

class TestAllowlist:
    @pytest.mark.parametrize("cmd", [
        "APPEND", "EXPUNGE", "UID EXPUNGE", "CLOSE",
        "DELETE", "RENAME", "COPY", "UID COPY", "MOVE", "UID MOVE",
        "SUBSCRIBE", "STARTTLS",
    ])
    def test_forbidden_commands_raise(self, cmd):
        with pytest.raises(IMAPForbiddenCall):
            _client().validate_call(cmd)

    @pytest.mark.parametrize("cmd", sorted(ALLOWED_COMMANDS))
    def test_allowed_commands_pass(self, cmd):
        _client().validate_call(cmd)  # ne lève pas

    def test_send_and_delete_equivalents_absent(self):
        # Équivalents IMAP de messages().send / messages().delete
        assert "APPEND" not in ALLOWED_COMMANDS
        assert "EXPUNGE" not in ALLOWED_COMMANDS
        assert "DELETE" not in ALLOWED_COMMANDS

    def test_normalization(self):
        client = _client()
        client.validate_call("  uid   fetch ")  # espaces normalisés


# ============================================================
# list_messages
# ============================================================

class TestListMessages:
    def test_format_and_order(self, fake_imap):
        FakeIMAP4_SSL.seed_messages = [
            _seed(1, "900", thrid="800"),
            _seed(2, "901", thrid="801"),
            _seed(3, "902", thrid="802"),
        ]
        messages, next_token = _client().list_messages(
            query="newer_than:6m -label:spam", max_results=10)
        assert next_token is None
        assert [m["id"] for m in messages] == ["902", "901", "900"]
        assert messages[0]["threadId"] == "802"

    def test_pagination_by_offset(self, fake_imap):
        FakeIMAP4_SSL.seed_messages = [
            _seed(u, str(900 + u)) for u in range(1, 6)
        ]
        client = _client()
        page1, tok1 = client.list_messages(max_results=2)
        page2, tok2 = client.list_messages(max_results=2, page_token=tok1)
        page3, tok3 = client.list_messages(max_results=2, page_token=tok2)
        assert [m["id"] for m in page1] == ["905", "904"]
        assert [m["id"] for m in page2] == ["903", "902"]
        assert [m["id"] for m in page3] == ["901"]
        assert tok3 is None

    def test_empty_mailbox(self, fake_imap):
        messages, next_token = _client().list_messages()
        assert messages == [] and next_token is None


# ============================================================
# get_message + intégration parser
# ============================================================

class TestGetMessage:
    def test_adapts_to_gmail_format(self, fake_imap):
        FakeIMAP4_SSL.seed_messages = [
            _seed(1, "900", thrid="800", labels={r"\Inbox"},
                  raw=RAW_MESSAGE),
        ]
        raw = _client().get_message("900")
        assert raw["id"] == "900"
        assert raw["threadId"] == "800"
        # pas de \Seen → UNREAD ; \Inbox → INBOX
        assert set(raw["labelIds"]) == {"UNREAD", "INBOX"}
        expected_ms = int(
            datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc).timestamp()
            * 1000)
        assert raw["internalDate"] == str(expected_ms)
        assert raw["payload"]["mimeType"] == "multipart/alternative"

    def test_parser_pipeline_unchanged(self, fake_imap):
        """Le dict IMAP passe dans parse_raw_message sans adaptation."""
        from src.parser import parse_raw_message

        FakeIMAP4_SSL.seed_messages = [
            _seed(1, "900", thrid="800", labels={r"\Inbox"},
                  raw=RAW_MESSAGE),
        ]
        raw = _client().get_message("900")
        parsed = parse_raw_message(raw)
        assert parsed["subject"] == "Réunion de lundi"
        assert parsed["sender_email"] == "cedric@example.com"
        assert "Bonjour, ca marche ?" in parsed["body_text"]
        assert parsed["is_read"] is False  # UNREAD présent
        assert parsed["id"] == "900"
        assert parsed["thread_id"] == "800"

    def test_read_message_has_no_unread(self, fake_imap):
        FakeIMAP4_SSL.seed_messages = [
            _seed(1, "900", flags={r"\Seen"}, labels={r"\Inbox"},
                  raw=RAW_MESSAGE),
        ]
        raw = _client().get_message("900")
        assert "UNREAD" not in raw["labelIds"]

    def test_unknown_message_raises_404(self, fake_imap):
        with pytest.raises(Exception, match="404"):
            _client().get_message("999999")


# ============================================================
# modify_labels
# ============================================================

class TestModifyLabels:
    def _seeded_client(self):
        FakeIMAP4_SSL.seed_messages = [
            _seed(1, "900", labels={r"\Inbox"}, raw=RAW_MESSAGE),
        ]
        return _client()

    def test_mark_read_maps_to_seen_flag(self, fake_imap):
        client = self._seeded_client()
        result = client.modify_labels("900", remove=["UNREAD"])
        store = FakeIMAP4_SSL.instances[0].store[1]
        assert r"\Seen" in store["flags"]
        assert "UNREAD" not in result["labelIds"]

    def test_archive_removes_inbox_label(self, fake_imap):
        client = self._seeded_client()
        result = client.modify_labels("900", remove=["INBOX"])
        store = FakeIMAP4_SSL.instances[0].store[1]
        assert r"\Inbox" not in store["labels"]
        assert "INBOX" not in result["labelIds"]

    def test_star_maps_to_flagged(self, fake_imap):
        client = self._seeded_client()
        result = client.modify_labels("900", add=["STARRED"])
        store = FakeIMAP4_SSL.instances[0].store[1]
        assert r"\Flagged" in store["flags"]
        assert "STARRED" in result["labelIds"]

    def test_custom_label_via_xgm_labels(self, fake_imap):
        client = self._seeded_client()
        result = client.modify_labels("900", add=["IA-Review"])
        store = FakeIMAP4_SSL.instances[0].store[1]
        assert '"IA-Review"' in store["labels"]
        assert "IA-Review" in result["labelIds"]

    def test_empty_call_rejected(self, fake_imap):
        with pytest.raises(ValueError):
            _client().modify_labels("900")

    def test_store_command_values(self, fake_imap):
        client = self._seeded_client()
        client.modify_labels("900", add=["STARRED"], remove=["UNREAD"])
        calls = FakeIMAP4_SSL.instances[0].uid_calls
        store_cmds = [args for cmd, args in calls if cmd == "STORE"]
        assert ("1", "+FLAGS", r"(\Seen)") in store_cmds
        assert ("1", "+FLAGS", r"(\Flagged)") in store_cmds


# ============================================================
# list_history (delta sync par UID)
# ============================================================

class TestListHistory:
    def test_returns_new_messages_only(self, fake_imap):
        FakeIMAP4_SSL.seed_messages = [
            _seed(1, "900"), _seed(2, "901"), _seed(3, "902"),
        ]
        history = _client().list_history("1")
        assert len(history) == 1
        assert history[0]["id"] == "3"  # max UID
        assert {m["id"] for m in history[0]["messages"]} == {"901", "902"}

    def test_no_new_messages_returns_empty(self, fake_imap):
        FakeIMAP4_SSL.seed_messages = [_seed(3, "902")]
        # RFC 3501 : « 4:* » renvoie quand même le dernier message →
        # le client doit le filtrer et retourner vide.
        assert _client().list_history("3") == []

    def test_invalid_history_id_rejected(self, fake_imap):
        with pytest.raises(Exception, match="invalid historyId"):
            _client().list_history("not-a-number")


# ============================================================
# Labels
# ============================================================

class TestLabels:
    def test_list_labels(self, fake_imap):
        labels = _client().list_labels()
        by_name = {l["name"]: l for l in labels}
        assert "INBOX" in by_name
        assert "[Gmail]/All Mail" in by_name
        assert "IA-Review" in by_name
        assert by_name["IA-Review"]["type"] == "user"
        assert by_name["[Gmail]/All Mail"]["type"] == "system"

    def test_create_label_returns_name_as_id(self, fake_imap):
        label_id = _client().create_label("IA-Review")
        assert label_id == "IA-Review"
        assert FakeIMAP4_SSL.instances[0].created == ['"IA-Review"']


# ============================================================
# Résilience
# ============================================================

class TestResilience:
    def test_reconnects_once_after_drop(self, fake_imap, monkeypatch):
        FlakyIMAP4_SSL.fail_armed = True
        FlakyIMAP4_SSL.seed_messages = [_seed(1, "900")]
        monkeypatch.setattr(imaplib, "IMAP4_SSL", FlakyIMAP4_SSL)
        messages, _ = _client().list_messages()
        assert [m["id"] for m in messages] == ["900"]
        # Deux connexions : celle qui a lâché + la reconnexion
        assert len(FlakyIMAP4_SSL.instances) == 2


# ============================================================
# Résolution du dossier « All Mail » (flag \All, RFC 6154)
# ============================================================

class TestFolderResolution:
    def test_all_mail_resolved_via_special_use_flag(self, fake_imap):
        """Compte Gmail en français : le dossier s'appelle
        « Tous les messages » — résolution par flag `\\All`,
        indépendante de la langue."""
        FakeIMAP4_SSL.list_lines = [
            b'(\\HasNoChildren) "/" INBOX',
            b'(\\HasNoChildren \\All) "/" "[Gmail]/Tous les messages"',
        ]
        FakeIMAP4_SSL.seed_messages = [_seed(1, "900")]
        messages, _ = _client().list_messages()
        assert [m["id"] for m in messages] == ["900"]
        assert (FakeIMAP4_SSL.instances[0].selected
                == '"[Gmail]/Tous les messages"')

    def test_default_english_folder(self, fake_imap):
        FakeIMAP4_SSL.seed_messages = [_seed(1, "900")]
        _client().list_messages()
        assert (FakeIMAP4_SSL.instances[0].selected
                == '"[Gmail]/All Mail"')


# ============================================================
# Factory
# ============================================================

class TestFactory:
    def test_prefers_imap_when_credentials_present(self, monkeypatch):
        monkeypatch.setattr(
            imap_module, "_read_env_var",
            lambda name: {
                "GMAIL_ADDRESS": "user@example.com",
                "GMAIL_APP_PASSWORD": "app-secret",
            }.get(name))
        assert isinstance(create_mail_client(), IMAPClient)

    def test_falls_back_to_gmail_oauth(self, monkeypatch):
        from src.gmail_client import GmailClient
        monkeypatch.setattr(imap_module, "_read_env_var",
                            lambda name: None)
        assert isinstance(create_mail_client(), GmailClient)

    def test_forced_backend(self, monkeypatch):
        from src.gmail_client import GmailClient
        monkeypatch.setattr(imap_module, "_read_env_var",
                            lambda name: None)
        assert isinstance(create_mail_client("imap"), IMAPClient)
        assert isinstance(create_mail_client("gmail"), GmailClient)

    def test_env_backend_variable(self, monkeypatch):
        monkeypatch.setattr(
            imap_module, "_read_env_var",
            lambda name: "imap" if name == "EMAIL_BACKEND" else None)
        assert isinstance(create_mail_client(), IMAPClient)
