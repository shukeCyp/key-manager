import base64
from concurrent.futures import ThreadPoolExecutor
import datetime
import hashlib
import json
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import app.database as database
import app.main as main
import app.routers.pages as pages
from app.config import settings
from app.dependencies import _rate_store
from app.models import Admin, Application, DeviceBinding, Key
from app.signing import private_key, public_key_info, signed_envelope
from app.licensing import beijing_now

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
from shaohua_license import LicenseClient, LicenseError


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False, "timeout": 30})
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main, "SessionLocal", factory)
    monkeypatch.setattr(pages, "SessionLocal", factory)
    monkeypatch.setattr(settings, "SIGNING_KEY_PATH", str(tmp_path / "signing.pem"))
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "OnlyForTests-Strong-123")
    monkeypatch.setattr(settings, "JWT_SECRET", "test-secret-" + "a" * 50)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    _rate_store.clear()
    private_key.cache_clear()
    with TestClient(main.app) as client:
        yield client, factory
    private_key.cache_clear()
    engine.dispose()


def admin_headers(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": settings.ADMIN_PASSWORD,
                                            "captcha_code": "", "captcha_key": ""})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def generate(env, **values):
    client, factory = env
    with factory() as db:
        app_id = db.query(Application).filter_by(app_code=settings.SUITE_APP_CODE).one().id
    r = client.post("/api/keys/generate", headers=admin_headers(client),
                    json={"app_id": app_id, "key_type": "month", **values})
    assert r.status_code == 200, r.text
    return r.json()["keys"][0]["key_code"]


def raw_verify(client, card, **values):
    return client.post("/api/client/verify", json={"key_code": card, "app_code": settings.SUITE_APP_CODE,
                       "machine_code": "a" * 64, "nonce": secrets.token_urlsafe(24), "client_name": "软件A", **values})


def sdk_for(client, name="软件A"):
    sdk = LicenseClient("https://licenses.test", settings.SUITE_APP_CODE, public_key_info()["public_key"], name, machine="a" * 64)
    class Transport:
        def post(self, url, **kwargs):
            kwargs.pop("timeout", None)
            return client.post(urlparse(url).path, **kwargs)
        def close(self): pass
    sdk._session = Transport()
    return sdk


def test_suite_apps_share_one_device_and_activation(env):
    card = generate(env)
    a = sdk_for(env[0]); b = sdk_for(env[0], "软件B")
    result_a = a.verify(card); result_b = b.verify(card)
    assert result_a["start_time"] == result_b["start_time"]
    assert result_b["used_count"] == 1
    a.require_valid(); b.require_valid()
    with env[1]() as db:
        assert db.query(DeviceBinding).count() == 1
        assert db.query(DeviceBinding).one().client_name == "软件B"


def test_custom_duration_starts_at_activation(env):
    card = generate(env, key_type="custom", expire_days=7)
    with env[1]() as db:
        key = db.query(Key).filter_by(key_code=card).one()
        assert key.duration_days == 7 and key.start_time is None and key.expire_time is None
        key.created_at = beijing_now() - datetime.timedelta(days=20); db.commit()
    assert raw_verify(env[0], card).status_code == 200
    with env[1]() as db:
        key = db.query(Key).filter_by(key_code=card).one()
        assert key.expire_time - key.start_time == datetime.timedelta(days=7)
        assert key.start_time > key.created_at + datetime.timedelta(days=19)


def test_other_products_and_excess_devices_are_rejected(env):
    card = generate(env)
    with env[1]() as db:
        db.add(Application(name="Other", app_code="OTHER")); db.commit()
    assert raw_verify(env[0], card, app_code="OTHER").status_code == 403
    assert raw_verify(env[0], card).status_code == 200
    assert raw_verify(env[0], card, machine_code="b" * 64).status_code == 403


@pytest.mark.parametrize("state", ["disabled", "expired", "past_expiry"])
def test_rejection_invalidates_cached_sdk_authorization(env, state):
    card = generate(env)
    sdk = sdk_for(env[0]); sdk.verify(card)
    with env[1]() as db:
        key = db.query(Key).filter_by(key_code=card).one()
        if state == "past_expiry": key.expire_time = beijing_now() - datetime.timedelta(seconds=1)
        else: key.status = state
        db.commit()
    with pytest.raises(LicenseError): sdk.verify()
    with pytest.raises(LicenseError): sdk.require_valid()


def test_concurrent_activation_cannot_overallocate_device_slots(env):
    card = generate(env)
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda i: raw_verify(env[0], card, machine_code=hashlib.sha256(str(i).encode()).hexdigest()).status_code, range(8)))
    assert codes.count(200) == 1 and codes.count(403) == 7
    with env[1]() as db: assert db.query(DeviceBinding).count() == 1


@pytest.mark.parametrize("attack", ["tamper", "nonce", "stale", "machine", "app", "lease"])
def test_sdk_rejects_modified_replayed_or_mismatched_responses(env, attack):
    card = generate(env)
    sdk = sdk_for(env[0])
    real = sdk._session.post
    class FakeResponse:
        status_code = 200
        def __init__(self, value): self.value = value
        def json(self): return self.value
    def intercepted(url, **kwargs):
        original = real(url, **kwargs).json()
        payload = original["payload"]
        if attack == "tamper":
            payload["key_info"]["device_limit"] = 100
            return FakeResponse(original)
        if attack == "nonce": payload["nonce"] = "old-response-nonce"
        if attack == "stale": payload["issued_at"] -= 1000; payload["lease_until"] -= 1000
        if attack == "machine": payload["machine_code"] = "b" * 64
        if attack == "app": payload["app_code"] = "OTHER"
        if attack == "lease": payload["lease_until"] = payload["issued_at"] + 86400
        return FakeResponse(signed_envelope(payload))
    sdk._session.post = intercepted
    with pytest.raises(LicenseError): sdk.verify(card)
    with pytest.raises(LicenseError): sdk.require_valid()


def test_expired_local_lease_cannot_be_used_offline(env):
    sdk = sdk_for(env[0]); sdk.verify(generate(env))
    sdk._valid_until = time.monotonic() - 1
    with pytest.raises(LicenseError): sdk.require_valid()


def test_legacy_endpoint_is_disabled(env):
    r = env[0].post("/api/client/auth", json={"key_code": "test", "app_code": settings.SUITE_APP_CODE, "machine_code": "a" * 64})
    assert r.status_code == 410


def test_refresh_token_cannot_access_admin_and_password_change_revokes_tokens(env):
    client, _ = env
    r=client.post("/api/auth/login",json={"username":"admin","password":settings.ADMIN_PASSWORD,"captcha_code":"","captcha_key":""})
    tokens=r.json()
    assert client.get("/api/applications",headers={"Authorization":"Bearer "+tokens["refresh_token"]}).status_code==401
    headers={"Authorization":"Bearer "+tokens["access_token"]}
    assert client.get("/api/applications",headers=headers).status_code==200
    assert client.post("/api/auth/change-password",headers=headers,json={"old_password":settings.ADMIN_PASSWORD,"new_password":"Changed-Password-123!"}).status_code==200
    assert client.get("/api/applications",headers=headers).status_code==401
    assert client.post("/api/auth/refresh",json={"refresh_token":tokens["refresh_token"]}).status_code==401


def test_generation_constraints_and_production_configuration(env, monkeypatch):
    client, factory=env
    with factory() as db: app_id=db.query(Application).first().id
    for values in [{"key_type":"custom"},{"device_limit":0},{"expire_days":0},{"prefix":"<script>"}]:
        assert client.post("/api/keys/generate",headers=admin_headers(client),json={"app_id":app_id,**values}).status_code==422
    monkeypatch.setattr(settings,"ENVIRONMENT","production")
    monkeypatch.setattr(settings,"JWT_SECRET","change-me")
    with pytest.raises(RuntimeError): settings.validate_production()


def test_web_pages_and_public_metadata(env):
    client,_=env
    for path in ["/login","/","/keys","/applications","/settings","/integration","/recycle-bin","/healthz","/api/client/public-key"]:
        assert client.get(path).status_code==200, path
    assert client.get("/docs").status_code==404
    assert client.get("/api/applications").status_code==401
    assert client.get("/api/integration",headers=admin_headers(client)).json()["public_key"]==public_key_info()["public_key"]
