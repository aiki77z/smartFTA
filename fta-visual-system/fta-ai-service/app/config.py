import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("FTA_AI_SERVICE_DATA_DIR", str(APP_DIR / "data"))).resolve()

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "").strip() or "gpt-4.1-mini"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip() or None

FTA_GNR_BASE_URL = os.getenv("FTA_GNR_BASE_URL", "http://localhost:8000").strip().rstrip("/")
FTA_GNR_AGENT_RUN_PATH = os.getenv("FTA_GNR_AGENT_RUN_PATH", "").strip()

