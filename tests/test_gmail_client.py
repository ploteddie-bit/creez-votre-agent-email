"""Tests pour src.gmail_client — allowlist Gmail (sécurité critique)."""
from __future__ import annotations

import pytest

from src.gmail_client import (
    ALLOWED_METHODS,
    GmailAuthError,
    GmailClient,
    GmailForbiddenCall,
)


# Helper direct pour tester sans instancier
def _check(method: str) -> None:
    """Équivalent de GmailClient().validate_call(method)."""
    GmailClient().validate_call(method)


# ============================================================
# Tests de l'allowlist (sécurité)
# ============================================================

def test_allowed_methods_basic() -> None:
    """Les méthodes de base doivent être autorisées."""
    _check("users.messages.list")
    _check("users.messages.get")
    _check("users.messages.modify")
    _check("users.history.list")
    _check("users.labels.list")


def test_allowed_methods_with_parentheses() -> None:
    """Les méthodes avec parenthèses doivent être acceptées."""
    _check("users.messages.list()")
    _check("users.messages.modify()")
    _check("users.history.list()")


def test_allowed_methods_with_whitespace() -> None:
    """Les espaces en trop doivent être ignorés."""
    _check("users.  messages.  list")
    _check(" users.messages.list ")


def test_delete_method_forbidden() -> None:
    """messages.delete est la méthode la plus dangereuse : INTERDITE."""
    with pytest.raises(GmailForbiddenCall) as exc_info:
        _check("users.messages.delete")
    assert "not in the allowlist" in str(exc_info.value)


def test_send_method_forbidden() -> None:
    """messages.send ne doit JAMAIS être appelable (fuite de données)."""
    with pytest.raises(GmailForbiddenCall):
        _check("users.messages.send")


def test_threads_delete_forbidden() -> None:
    """threads.delete est interdit aussi."""
    with pytest.raises(GmailForbiddenCall):
        _check("users.threads.delete")


def test_drafts_send_forbidden() -> None:
    """drafts.send est interdit (envoi via brouillon)."""
    with pytest.raises(GmailForbiddenCall):
        _check("users.drafts.send")


def test_drafts_create_forbidden() -> None:
    """drafts.create est interdit (pourrait créer un brouillon envoyable)."""
    with pytest.raises(GmailForbiddenCall):
        _check("users.drafts.create")


def test_drafts_update_forbidden() -> None:
    """drafts.update est interdit (modification d'un brouillon existant)."""
    with pytest.raises(GmailForbiddenCall):
        _check("users.drafts.update")


def test_drafts_delete_forbidden() -> None:
    """drafts.delete est interdit."""
    with pytest.raises(GmailForbiddenCall):
        _check("users.drafts.delete")


def test_unknown_method_forbidden() -> None:
    """Toute méthode non explicitement allowlistée est refusée."""
    with pytest.raises(GmailForbiddenCall):
        _check("users.settings.forwardingAddresses.list")  # transfert !


def test_allowlist_is_frozen() -> None:
    """L'allowlist doit être un frozenset (non modifiable à runtime)."""
    assert isinstance(ALLOWED_METHODS, frozenset)


# ============================================================
# Tests du client (sans réseau)
# ============================================================

def test_client_init_does_not_connect() -> None:
    """Le constructeur ne doit pas déclencher d'appel OAuth."""
    client = GmailClient()
    # Pas d'exception, pas d'appel réseau
    assert client._service is None


def test_client_allowed_methods_returns_frozenset() -> None:
    """allowed_methods() doit retourner un frozenset."""
    client = GmailClient()
    methods = client.allowed_methods()
    assert isinstance(methods, frozenset)
    assert "users.messages.list" in methods
    assert "users.messages.delete" not in methods


def test_client_modify_labels_requires_args() -> None:
    """modify_labels doit exiger add OU remove non vide."""
    client = GmailClient()
    # Sans mock complet, on ne peut pas tester l'appel réseau,
    # mais on peut vérifier la validation des arguments en amont
    with pytest.raises(ValueError, match="add or remove"):
        # Le check d'args arrive avant l'appel réseau
        # (l'appel réseau lèverait GmailAuthError)
        try:
            client.modify_labels("msg_1", add=None, remove=None)
        except (ValueError, Exception) as e:
            if "add or remove" in str(e):
                raise ValueError("add or remove must be non-empty")  # re-raise propre
            # Sinon c'est l'auth error, on la passe
            if "OAuth" not in str(e):
                raise


# ============================================================
# Test "anti-régression" : scane le code source
# ============================================================

def test_no_forbidden_method_called_in_source() -> None:
    """Scanne le code source pour s'assurer qu'aucune méthode interdite
    n'est appelée directement (bypass de validate_call).

    Ce test est volontairement strict : on cherche les patterns
    `service.users().messages().delete(`, etc.
    """
    import re
    from pathlib import Path

    src = Path("src")
    forbidden_patterns = [
        r"\.messages\(\)\.delete\(",
        r"\.threads\(\)\.delete\(",
        r"\.messages\(\)\.send\(",
        r"\.drafts\(\)\.send\(",
        r"\.drafts\(\)\.create\(",
        r"\.drafts\(\)\.update\(",
        r"\.drafts\(\)\.delete\(",
    ]

    violations: list[str] = []
    for py_file in src.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        for pattern in forbidden_patterns:
            for match in re.finditer(pattern, content):
                # Récupérer la ligne complète pour le diagnostic
                line_start = content.rfind("\n", 0, match.start()) + 1
                line_end = content.find("\n", match.end())
                line = content[line_start:line_end if line_end > 0 else None].strip()
                violations.append(f"  {py_file}: {line}")

    assert not violations, (
        "Forbidden Gmail API methods called directly:\n" + "\n".join(violations)
        + "\n\nUse GmailClient().validate_call(...) or a wrapper method instead."
    )


# ============================================================
# Tests OAuth : chargement réel du token (E1 — fin du stub)
# ============================================================

class _FakeCreds:
    """Faux credentials OAuth pour les tests (aucun réseau)."""

    def __init__(self, *, valid: bool = True, expired: bool = False,
                 refresh_token: str | None = None) -> None:
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.refresh_called = False

    def refresh(self, request) -> None:  # noqa: ARG002 - signature imposée
        self.refresh_called = True
        self.valid = True
        self.expired = False

    def to_json(self) -> str:
        return '{"token": "refreshed-token"}'


class _FakeCredentialsClass:
    """Simule google.oauth2.credentials.Credentials (injection de classe)."""

    creds: _FakeCreds | None = None

    @classmethod
    def from_authorized_user_file(cls, path, scopes):  # noqa: ARG003
        return cls.creds


class TestOAuthTokenLoading:
    """_load_credentials charge, rafraîchit ou échoue proprement."""

    @pytest.fixture
    def fake_google_auth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Injecte un faux `google.auth.transport.requests` (lazy-import).

        Les libs google ne sont pas installées sur la machine de dev ;
        en production elles viennent de requirements.txt. Le lazy-import
        dans `_load_credentials` lit sys.modules -> le faux module suffit.
        """
        import sys
        from types import ModuleType

        class Request:  # signature imposée par google-auth
            def __init__(self, *args, **kwargs) -> None:
                pass

        chain = [
            "google", "google.auth", "google.auth.transport",
            "google.auth.transport.requests",
        ]
        for name in chain:
            mod = ModuleType(name)
            if name.endswith("requests"):
                mod.Request = Request
            monkeypatch.setitem(sys.modules, name, mod)

    def test_missing_token_file_raises_auth_error(self, tmp_path) -> None:
        client = GmailClient(token_path=str(tmp_path / "absent.json"))
        with pytest.raises(GmailAuthError, match="setup-oauth"):
            client._load_credentials(_FakeCredentialsClass)

    def test_valid_token_returned_as_is(self, tmp_path) -> None:
        token_file = tmp_path / "token.json"
        token_file.write_text("{}", encoding="utf-8")
        _FakeCredentialsClass.creds = _FakeCreds(valid=True)
        client = GmailClient(token_path=str(token_file))
        creds = client._load_credentials(_FakeCredentialsClass)
        assert creds.valid is True
        assert creds.refresh_called is False

    def test_expired_token_is_refreshed_and_saved(
        self, tmp_path, fake_google_auth,
    ) -> None:
        token_file = tmp_path / "token.json"
        token_file.write_text('{"token": "old"}', encoding="utf-8")
        fake = _FakeCreds(valid=False, expired=True, refresh_token="rt-123")
        _FakeCredentialsClass.creds = fake
        client = GmailClient(token_path=str(token_file))
        creds = client._load_credentials(_FakeCredentialsClass)
        assert creds.refresh_called is True
        assert creds.valid is True
        # Le token rafraîchi est réécrit sur disque
        assert "refreshed-token" in token_file.read_text(encoding="utf-8")

    def test_expired_without_refresh_token_raises(self, tmp_path) -> None:
        token_file = tmp_path / "token.json"
        token_file.write_text("{}", encoding="utf-8")
        _FakeCredentialsClass.creds = _FakeCreds(
            valid=False, expired=True, refresh_token=None,
        )
        client = GmailClient(token_path=str(token_file))
        with pytest.raises(GmailAuthError, match="refresh_token"):
            client._load_credentials(_FakeCredentialsClass)

    def test_refresh_failure_raises_auth_error(
        self, tmp_path, fake_google_auth,
    ) -> None:
        token_file = tmp_path / "token.json"
        token_file.write_text("{}", encoding="utf-8")

        class _FailingCreds(_FakeCreds):
            def refresh(self, request) -> None:
                raise RuntimeError("network down")

        _FakeCredentialsClass.creds = _FailingCreds(
            valid=False, expired=True, refresh_token="rt-123",
        )
        client = GmailClient(token_path=str(token_file))
        with pytest.raises(GmailAuthError, match="refresh failed"):
            client._load_credentials(_FakeCredentialsClass)
