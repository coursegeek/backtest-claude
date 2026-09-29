import sys
from pathlib import Path

WORK = Path(__file__).resolve().parents[1]
if str(WORK) not in sys.path:
    sys.path.insert(0, str(WORK))
sys.path.insert(0, str(Path(__file__).resolve().parent))
