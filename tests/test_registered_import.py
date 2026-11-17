"""Import already-registered credentials so payment links can use them."""

from __future__ import annotations

import pytest

from regix.services.account_import import RegisteredImportError, parse_registered_import


def test_parse_dash_format():
    rows = parse_registered_import(
        "a@b.com----pw----JBSWY3DPEHPK3PXP----AT----RT----ST----ID"
    )
    assert rows == [{
        "email": "a@b.com",
        "password": "pw",
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "access_token": "AT",
        "refresh_token": "RT",
        "session_token": "ST",
        "id_token": "ID",
    }]


def test_parse_dash_short_form_keeps_missing_fields_empty():
    rows = parse_registered_import("a@b.com----pw----JBSWY3DPEHPK3PXP----AT----RT")
    assert rows[0]["totp_secret"] == "JBSWY3DPEHPK3PXP"
    assert rows[0]["refresh_token"] == "RT"
    assert rows[0]["session_token"] == ""


def test_parse_two_field_at_is_detected_by_jwt_shape():
    jwt = "eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIn0.sig"
    rows = parse_registered_import(f"a@b.com----{jwt}")
    assert rows[0]["access_token"] == jwt
    assert rows[0]["password"] == ""


def test_parse_json_camel_case_and_nested_tokens():
    rows = parse_registered_import(
        '{"email":"A@B.com","tokens":{"accessToken":"AT","refreshToken":"RT"},"totp":"SECRET"}'
    )
    assert rows[0]["email"] == "a@b.com"
    assert rows[0]["access_token"] == "AT"
    assert rows[0]["refresh_token"] == "RT"
    assert rows[0]["totp_secret"] == "SECRET"


def test_parse_json_array():
    rows = parse_registered_import(
        '[{"email":"a@b.com","refresh_token":"RT"},'
        ' {"email":"c@d.com","access_token":"AT"}]'
    )
    assert [r["email"] for r in rows] == ["a@b.com", "c@d.com"]


def test_parse_rejects_line_without_token():
    with pytest.raises(RegisteredImportError) as excinfo:
        parse_registered_import("a@b.com----pw")
    assert excinfo.value.errors[0]["line"] == 1
    assert "token" in excinfo.value.errors[0]["error"]


def test_parse_rejects_whole_batch_on_one_bad_line():
    text = "a@b.com----pw----JBSWY3DPEHPK3PXP----AT\nnot-an-email----pw----X----AT"
    with pytest.raises(RegisteredImportError) as excinfo:
        parse_registered_import(text)
    assert len(excinfo.value.errors) == 1
    assert excinfo.value.errors[0]["line"] == 2


def test_import_merges_with_existing_row(clean_db):
    clean_db.save_registered(
        {
            "email": "keep@example.com",
            "access_token": "old-at",
            "password": "old-pw",
            "totp_secret": "OLDSECRET",
            "proxy": "socks5://old:1080",
        }
    )
    result = clean_db.import_registered_accounts(
        [{"email": "keep@example.com", "refresh_token": "new-rt"}]
    )
    assert result == {"inserted": 0, "updated": 1}
    row = clean_db.get_registered("keep@example.com")
    assert row["refresh_token"] == "new-rt"
    # empty import fields must not wipe existing credentials
    assert row["password"] == "old-pw"
    assert row["totp_secret"] == "OLDSECRET"
    assert row["access_token"] == "old-at"
    assert row["extra"]["proxy"] == "socks5://old:1080"


def test_import_inserts_new_row(clean_db):
    result = clean_db.import_registered_accounts(
        [{"email": "new@example.com", "access_token": "AT", "refresh_token": "RT"}]
    )
    assert result == {"inserted": 1, "updated": 0}
    row = clean_db.get_registered("new@example.com")
    assert row["refresh_token"] == "RT"


def test_import_api(clean_db, monkeypatch):
    import regix.api.app as app_module
    from fastapi.testclient import TestClient

    monkeypatch.setattr(app_module, "ADMIN_PASSWORD", "")
    app_module._AUTH_FAILED_TIMES.clear()
    client = TestClient(app_module.app)

    ok = client.post(
        "/api/registered/import",
        json={"text": "api@example.com----pw----JBSWY3DPEHPK3PXP----AT----RT"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["inserted"] == 1
    assert clean_db.get_registered("api@example.com")["refresh_token"] == "RT"

    bad = client.post("/api/registered/import", json={"text": "nope"})
    assert bad.status_code == 422
    assert bad.json()["errors"][0]["line"] == 1
