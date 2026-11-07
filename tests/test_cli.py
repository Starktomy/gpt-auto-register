from __future__ import annotations

import queue

from regix.cli import main as cli


class FakeProvider:
    pooled = True
    display_name = "Outlook"


class FakeDB:
    def __init__(self):
        self.prefs = {"want2fa": True, "wantRefreshToken": False, "otpTimeout": 180}
        self.pool_proxy = "socks5://pool-proxy"

    def get_setting(self, key, default=""):
        return "outlook" if key == "mail_source" else default

    def get_prefs(self):
        return self.prefs

    def claim_next(self, kind=""):
        return {
            "email": "test@example.com",
            "password": "old",
            "client_id": "cid",
            "refresh_token": "refresh",
            "relay_url": "",
            "kind": "outlook",
        }

    def claim_account(self, email):
        return self.claim_next()

    def release_unused(self, email):
        self.released = getattr(self, "released", [])
        self.released.append(email)

    def find_proxy(self, proxy):
        return False

    def pick_proxy(self):
        return self.pool_proxy


class FakeRegistrar:
    def __init__(self):
        self.calls = []

    def start_registration(self, account, options):
        self.calls.append((account, options))
        q = queue.Queue()
        q.put('__EVENT__:{"kind":"done","password":"pw","totp_secret":"SECRET","totp_bound":true}')
        q.put(None)
        self.q = q
        return "run123"

    def get_run_queue(self, run_id):
        return self.q

    def remove_run_queue(self, run_id):
        self.removed = run_id


def test_register_uses_webui_service_and_defaults_to_totp(monkeypatch, capsys):
    fake_db = FakeDB()
    fake_registrar = FakeRegistrar()
    monkeypatch.setattr(cli, "db", fake_db)
    monkeypatch.setattr(cli, "registrar", fake_registrar)
    monkeypatch.setattr(cli, "get_provider_class", lambda kind: FakeProvider)

    rc = cli.main(["register", "--json"])

    out = capsys.readouterr().out
    account, options = fake_registrar.calls[0]
    assert rc == 0
    assert account["email"] == "test@example.com"
    assert options["want_2fa"] is True
    assert options["want_refresh_token"] is False
    assert fake_registrar.removed == "run123"
    assert "SECRET" not in out
    assert '"password": "pw"' not in out
    assert '"totp_bound": true' in out


def test_register_pool_proxy_is_reserved(monkeypatch, capsys):
    fake_db = FakeDB()
    fake_registrar = FakeRegistrar()
    monkeypatch.setattr(cli, "db", fake_db)
    monkeypatch.setattr(cli, "registrar", fake_registrar)
    monkeypatch.setattr(cli, "get_provider_class", lambda kind: FakeProvider)

    rc = cli.main(["register", "--pool-proxy", "--json", "--no-totp"])

    assert rc == 0
    _, options = fake_registrar.calls[0]
    assert options["proxy"] == "socks5://pool-proxy"
    assert options["proxy_reserved"] == 1
    assert options["want_2fa"] is False


def test_register_releases_claim_when_proxy_pool_is_empty(monkeypatch):
    fake_db = FakeDB()
    fake_db.pool_proxy = ""
    fake_registrar = FakeRegistrar()
    monkeypatch.setattr(cli, "db", fake_db)
    monkeypatch.setattr(cli, "registrar", fake_registrar)
    monkeypatch.setattr(cli, "get_provider_class", lambda kind: FakeProvider)

    rc = cli.main(["register", "--pool-proxy", "--json"])

    assert rc == 1
    assert fake_db.released == ["test@example.com"]
    assert fake_registrar.calls == []


def test_cli_log_redacts_generated_password():
    line = "[register] 完成 email=user@example.com pw=Secret123 at=10 st=10"

    assert cli._redact_log(line, show_secrets=False) == (
        "[register] 完成 email=user@example.com pw=<set> at=10 st=10"
    )
