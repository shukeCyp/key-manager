import os
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    # Admin account (seed on first run)
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin123"

    # JWT
    JWT_SECRET: str = "change-me-to-a-random-secret-in-production"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 120  # 2 hours
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Database
    DATABASE_URL: str = "sqlite:///./data/app.db"

    # Security
    LOGIN_MAX_ATTEMPTS: int = 5
    LOGIN_LOCKOUT_MINUTES: int = 15
    CLIENT_AUTH_RATE_LIMIT: str = "10/minute"  # per IP

    # Default settings (can be changed via admin panel)
    DEFAULT_API_PREFIX: str = "/api"
    DEFAULT_HEARTBEAT_THRESHOLD_MINUTES: int = 5

    ENVIRONMENT: str = "development"
    ENABLE_API_DOCS: bool = False
    ENABLE_LEGACY_AUTH: bool = False
    SIGNING_KEY_PATH: str = "data/signing-key.pem"
    LICENSE_LEASE_SECONDS: int = 300
    CLIENT_IP_REQUESTS_PER_MINUTE: int = 1800
    CLIENT_DEVICE_REQUESTS_PER_MINUTE: int = 120
    SUITE_APP_CODE: str = "SHAOHUA_SUITE"
    SUITE_APP_NAME: str = "韶华软件全家桶"

    def validate_production(self):
        if self.ENVIRONMENT != "production":
            return
        if len(self.JWT_SECRET) < 40 or self.JWT_SECRET.startswith("change-me"):
            raise RuntimeError("Production requires a random JWT_SECRET of at least 40 characters")
        if len(self.ADMIN_PASSWORD) < 16 or self.ADMIN_PASSWORD == "admin123":
            raise RuntimeError("Production requires ADMIN_PASSWORD of at least 16 characters")
        if not 60 <= self.LICENSE_LEASE_SECONDS <= 900:
            raise RuntimeError("LICENSE_LEASE_SECONDS must be between 60 and 900")

settings = Settings()
