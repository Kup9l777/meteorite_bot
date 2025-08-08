from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()

def _parse_admins(s: str):
    if not s: 
        return []
    return [int(x.strip()) for x in s.split(',') if x.strip()]

@dataclass
class Settings:
    tg_token: str = os.getenv("TG_TOKEN", "")
    admin_ids: list[int] = None
    ozon_client_id: str = os.getenv("OZON_CLIENT_ID", "")
    ozon_api_key: str = os.getenv("OZON_API_KEY", "")

    def __post_init__(self):
        self.admin_ids = _parse_admins(os.getenv("ADMIN_IDS", ""))

settings = Settings()
