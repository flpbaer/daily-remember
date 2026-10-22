import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "sim")


TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
TZ = ZoneInfo(os.getenv("TIMEZONE", "America/Sao_Paulo"))
DB_PATH = Path(os.getenv("DB_PATH", str(ROOT / "data" / "daily_remember.db")))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))
GRACE_MINUTES = int(os.getenv("GRACE_MINUTES", "30"))
MEMBERS_INTENT = _flag("MEMBERS_INTENT", "true")
