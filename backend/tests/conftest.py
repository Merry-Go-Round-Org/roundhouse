"""Keep all backend tests out of developer runtime storage."""

import os
import tempfile
from pathlib import Path

os.environ["ALLOW_LOCAL_DEMO_AUTH"] = "true"
os.environ["ALLOW_LEGACY_REGISTRATION"] = "true"
os.environ["AI_NATIVE_ENABLED"] = "false"  # Existing compatibility tests exercise the legacy flow.

# Set configuration before importing the app; do not touch developer runtime data.
_temporary_storage = tempfile.TemporaryDirectory()
os.environ["STORAGE_DIR"] = _temporary_storage.name
os.environ["DATABASE_URL"] = (
    f"sqlite+aiosqlite:///{Path(_temporary_storage.name) / 'test.db'}"
)
