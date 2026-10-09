"""Test-session setup.

The admin-saved DB connection (config/db_connection.json) intentionally wins over
env/.env at runtime. Point it at a file that does not exist for the whole test
session so a connection saved on a developer machine can never leak into tests
(CI sets DB_ENGINE=sqlite and has no such file). Tests that exercise the file
itself set DB_CONFIG_FILE explicitly.
"""

from __future__ import annotations

import os
import tempfile

os.environ["ALLOW_HEADER_AUTH"] = "1"  # tests authenticate with the X-User-Id header
os.environ["SESSION_STORE_FILE"] = os.path.join(tempfile.gettempdir(), "antarkosh-tests-sessions.json")
os.environ["DB_CONFIG_FILE"] = os.path.join(tempfile.gettempdir(), "antarkosh-tests-no-db-config.json")
