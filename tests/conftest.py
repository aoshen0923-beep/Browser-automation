import sys
from pathlib import Path

# Lets test modules share fixtures (e.g. the Chromium fixture in test_browser).
sys.path.insert(0, str(Path(__file__).parent))
