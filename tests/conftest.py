import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Tools call _require_credentials() before touching the network; set fakes so
# tests exercise real logic rather than the missing-credentials guard.
os.environ.setdefault("ICLOUD_EMAIL", "test@icloud.com")
os.environ.setdefault("ICLOUD_APP_PASSWORD", "abcd-efgh-ijkl-mnop")
