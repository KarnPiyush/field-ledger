import sys
from pathlib import Path

TEST_ROOT = str(Path(__file__).parent)
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)
