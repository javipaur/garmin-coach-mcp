import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("DATA_DIR", str(PROJECT_ROOT / ".test-data"))
os.environ.setdefault("USERS_DB_DIR", str(PROJECT_ROOT / ".test-data" / "users"))
os.environ.setdefault("GARMIN_LANGUAGE", "es")
