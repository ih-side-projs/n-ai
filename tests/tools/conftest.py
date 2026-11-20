"""Pytest configuration for WP6 tests."""
import sys
from pathlib import Path

# Add the WP6 package to the path
wp6_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(wp6_root))

# Also add WP1 for nai.core dependencies
wp1_root = wp6_root.parent / "WP1" / "extracted" / "nai-wp1"
if wp1_root.exists():
    sys.path.insert(0, str(wp1_root))