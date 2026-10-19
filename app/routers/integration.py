from fastapi import APIRouter, Depends
from app.dependencies import get_current_admin
from app.config import settings
from app.signing import public_key_info

router = APIRouter(prefix="/integration", tags=["integration"])


@router.get("")
def integration(_=Depends(get_current_admin)):
    return {**public_key_info(), "app_code": settings.SUITE_APP_CODE,
            "lease_seconds": settings.LICENSE_LEASE_SECONDS,
            "endpoint": "/api/client/verify", "feedback_endpoint":"/api/client/feedback",
            "feedback_max_archive_bytes":settings.FEEDBACK_MAX_ARCHIVE_BYTES}
