import sys
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[2]
API_ROOT = SRC_ROOT / "transcription-api"

for path in (SRC_ROOT, API_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
