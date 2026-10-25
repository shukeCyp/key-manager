"""Fresh, request-bound license responses verified with a pinned public key."""
import hashlib
import time
import datetime as dt
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.config import settings
from app.database import get_db
from app.dependencies import check_rate_limit
from app.licensing import authorize
from app.schemas import ClientAuthRequest, SignedVerifyRequest
from app.signing import public_key_info, signed_envelope
from app.dependencies import create_token
from app.models import Key

router = APIRouter(prefix="/client", tags=["client-auth"])


def request_limits(req, request):
    ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(f"license-ip:{ip}", settings.CLIENT_IP_REQUESTS_PER_MINUTE, 60):
        return ip, False
    identity = hashlib.sha256(f"{req.app_code}:{req.key_code}:{req.machine_code}".encode()).hexdigest()
    return ip, check_rate_limit(f"license-device:{identity}", settings.CLIENT_DEVICE_REQUESTS_PER_MINUTE, 60)


@router.get("/public-key")
def public_key():
    return public_key_info()


@router.post("/verify")
def verify(req: SignedVerifyRequest, request: Request, db: Session = Depends(get_db)):
    ip, allowed = request_limits(req, request)
    if allowed:
        status, result = authorize(db, req, ip)
    else:
        status, result = 429, {"status": "error", "message": "请求过于频繁，请稍后再试"}
    now = int(time.time())
    lease_until = now + settings.LICENSE_LEASE_SECONDS if status == 200 else now
    expiry = result.get("key_info", {}).get("expire_time")
    if expiry:
        expiry_epoch = int(datetime.strptime(expiry, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
        lease_until = min(lease_until, expiry_epoch)
    if status == 200:
        key = db.query(Key).filter_by(key_code=req.key_code).one()
        result['feedback_token'] = create_token(dict(type='feedback', key_id=key.id,
            app_code=key.application.app_code, machine_code=req.machine_code, client_name=req.client_name),
            dt.timedelta(seconds=max(1, lease_until-now)))
        if settings.SUBTITLE_DATABASE_URL:
            result['subtitle_token'] = create_token(dict(type='subtitle_query', key_id=key.id,
                app_code=key.application.app_code, machine_code=req.machine_code, client_name=req.client_name),
                dt.timedelta(seconds=max(1, lease_until-now)))
    payload = {**result, "issued_at": now, "lease_until": lease_until, "nonce": req.nonce,
               "app_code": req.app_code, "machine_code": req.machine_code, "client_name": req.client_name,
               "key_fingerprint": hashlib.sha256(req.key_code.encode()).hexdigest()}
    return JSONResponse(signed_envelope(payload), status_code=status, headers={"Cache-Control": "no-store"})


@router.post("/auth")
def legacy_auth(req: ClientAuthRequest, request: Request, db: Session = Depends(get_db)):
    if not settings.ENABLE_LEGACY_AUTH:
        return JSONResponse({"status": "error", "message": "请使用新版 SDK 的 /api/client/verify 签名验证"}, status_code=410)
    ip, allowed = request_limits(req, request)
    if not allowed:
        return JSONResponse({"status": "error", "message": "请求过于频繁"}, status_code=429)
    status, result = authorize(db, req, ip)
    return JSONResponse(result, status_code=status, headers={"Cache-Control": "no-store"})
