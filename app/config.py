import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-change-me-in-production")
    DATABASE = os.environ.get(
        "DATABASE_PATH",
        str(BASE_DIR / "instance" / "crawl_lab.db"),
    )
    ADMIN_USER = "admin"
    ADMIN_PASSWORD = "123456"
