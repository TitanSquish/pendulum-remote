import sys
from pathlib import Path

# The bridge modules live next to bridge.py, not in a package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
