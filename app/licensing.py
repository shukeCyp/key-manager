"""One product can authorize every program in a suite."""
import datetime
from zoneinfo import ZoneInfo
from app.models import Application, Key, DeviceBinding

DEFAULT_DAYS = {"day": 1, "week": 7, "month": 30, "quarter": 90, "year": 365}


def beijing_now():
    return datetime.datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)


def authorize(db, req, ip):
    # Serialize check + activation + device-slot allocation across workers.
    if db.bind.dialect.name == "sqlite":
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    app = db.query(Application).filter_by(app_code=req.app_code).first()
    key = db.query(Key).filter_by(key_code=req.key_code).with_for_update().first()
    if not app or not key or key.app_id != app.id:
        db.rollback()
        return 403, {"status": "error", "message": "卡密或应用编号无效"}
    if key.status == "disabled":
        db.rollback()
        return 403, {"status": "error", "message": "卡密已被禁用"}
    now = beijing_now()
    if key.status == "expired" or (key.expire_time is not None and now >= key.expire_time):
        key.status = "expired"
        db.commit()
        return 403, {"status": "error", "message": "卡密已过期"}
    if key.status not in {"active", "used"}:
        db.rollback()
        return 403, {"status": "error", "message": "卡密状态无效"}
    binding = db.query(DeviceBinding).filter_by(key_id=key.id, machine_code=req.machine_code).first()
    if binding is None:
        count = db.query(DeviceBinding).filter_by(key_id=key.id).count()
        if count >= key.device_limit:
            db.rollback()
            return 403, {"status": "error", "message": "设备数量已达上限，请联系管理员解绑"}
        binding = DeviceBinding(key_id=key.id, machine_code=req.machine_code, first_seen=now)
        db.add(binding)
    if key.start_time is None:
        days = key.duration_days or DEFAULT_DAYS.get(key.key_type)
        if key.expire_time is None and key.key_type != "permanent" and not days:
            db.rollback()
            return 403, {"status": "error", "message": "卡密有效期未配置，请联系管理员"}
        key.start_time = now
        if key.expire_time is None and days:
            key.expire_time = now + datetime.timedelta(days=days)
    binding.last_heartbeat = now
    binding.ip_address = ip  # Never trust a client-supplied IP address.
    binding.client_name = getattr(req, "client_name", "")
    key.status = "used"
    db.commit()
    used = db.query(DeviceBinding).filter_by(key_id=key.id).count()
    info = {"app_name": app.name, "key_type": key.key_type,
            "start_time": key.start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "expire_time": key.expire_time.strftime("%Y-%m-%d %H:%M:%S") if key.expire_time else "",
            "device_limit": key.device_limit, "used_count": used}
    return 200, {"status": "success", "message": "授权成功", "key_info": info}
