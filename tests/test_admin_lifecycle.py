import datetime
from app.models import Key
from app.licensing import beijing_now
from test_licensing import env, generate, admin_headers, raw_verify


def test_renewal_keeps_one_key_and_preserves_disabled_state(env):
    client,factory=env
    card=generate(env,key_type="custom",expire_days=7)
    with factory() as db: kid=db.query(Key).filter_by(key_code=card).one().id
    h=admin_headers(client)
    assert client.post(f"/api/keys/{kid}/extend",headers=h,json={"days":3}).status_code==200
    with factory() as db:
        key=db.get(Key,kid); assert key.duration_days==10 and key.expire_time is None
    assert raw_verify(client,card).status_code==200
    with factory() as db:
        key=db.get(Key,kid); key.expire_time=beijing_now()-datetime.timedelta(days=1); key.status="expired"; db.commit()
    assert client.post(f"/api/keys/{kid}/extend",headers=h,json={"days":3}).status_code==200
    assert raw_verify(client,card).status_code==200
    with factory() as db:
        key=db.get(Key,kid); key.status="disabled"; db.commit()
    assert client.post(f"/api/keys/{kid}/extend",headers=h,json={"days":3}).status_code==200
    assert raw_verify(client,card).status_code==403


def test_invalid_admin_prefix_cannot_break_public_routes(env):
    client,_=env
    h=admin_headers(client)
    for prefix in ["/", "/api/client", "/static", "/integration"]:
        assert client.put("/api/settings",headers=h,json={"api_prefix":prefix}).status_code==400
    assert client.put("/api/settings",headers=h,json={"api_prefix":"/manage-api"}).status_code==200
    assert client.get("/manage-api/applications",headers=h).status_code==200
    assert client.get("/api/client/public-key").status_code==200


def test_recycle_restore_preserves_unactivated_duration(env):
    client,factory=env
    card=generate(env,key_type="custom",expire_days=17)
    h=admin_headers(client)
    with factory() as db: kid=db.query(Key).filter_by(key_code=card).one().id
    deletion=client.delete(f"/api/keys/{kid}",headers=h)
    assert deletion.status_code==200
    batch=deletion.json()["batch_id"]
    restored=client.post(f"/api/recycle/{batch}/restore",headers=h)
    assert restored.status_code==200,restored.text
    with factory() as db:
        key=db.query(Key).filter_by(key_code=card).one()
        assert key.duration_days==17 and key.expire_time is None
