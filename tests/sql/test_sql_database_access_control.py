"""Tests for Step E6: Database access control and admin endpoints.

Mirrors tests/api/test_alpha_auth_and_isolation.py for database resources:
1. Enforce allowed_users from databases/<id>/db.yaml (admins always allowed).
2. Hide databases the user cannot access from lists, routing, and query results.
3. Admin-only endpoints:
   - List databases
   - Save/test connection
   - Edit allowed_users
4. Document access behavior must remain intact and unchanged.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from src.main import app
from src.models.schemas import QueryResult
from src.sql.context import clear_context_cache
from src.sql.registry import (
    can_access_database,
    get_database_access,
    list_databases,
    update_database_access,
)
from src.stages.s12b_sql_retrieval import SQLRetriever


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def temp_databases(tmp_path):
    """Create isolated test databases directory with db_a and db_b configs."""
    dbs_dir = tmp_path / "databases"
    dbs_dir.mkdir(parents=True, exist_ok=True)

    # db_a: only admin
    db_a_dir = dbs_dir / "db_a"
    db_a_dir.mkdir(parents=True, exist_ok=True)
    db_a_yaml = {
        "id": "db_a",
        "display_name": "Database Alpha",
        "description": "Sensitive alpha database",
        "engine": "sqlite",
        "enabled": True,
        "allowed_users": ["admin"],
    }
    (db_a_dir / "db.yaml").write_text(yaml.safe_dump(db_a_yaml), encoding="utf-8")

    # db_b: admin and rahul
    db_b_dir = dbs_dir / "db_b"
    db_b_dir.mkdir(parents=True, exist_ok=True)
    db_b_yaml = {
        "id": "db_b",
        "display_name": "Database Beta",
        "description": "Team beta database",
        "engine": "sqlite",
        "enabled": True,
        "allowed_users": ["admin", "rahul"],
    }
    (db_b_dir / "db.yaml").write_text(yaml.safe_dump(db_b_yaml), encoding="utf-8")

    clear_context_cache()
    yield dbs_dir
    clear_context_cache()


# ---------------------------------------------------------------------------
# 1. Access Control Helpers & Context Tests
# ---------------------------------------------------------------------------

def test_can_access_database_rules(temp_databases):
    # Admin is always allowed
    assert can_access_database("db_a", "admin", databases_dir=temp_databases) is True
    assert can_access_database("db_b", "admin", databases_dir=temp_databases) is True

    # Rahul is only in db_b
    assert can_access_database("db_a", "rahul", databases_dir=temp_databases) is False
    assert can_access_database("db_b", "rahul", databases_dir=temp_databases) is True

    # Priya is in neither
    assert can_access_database("db_a", "priya", databases_dir=temp_databases) is False
    assert can_access_database("db_b", "priya", databases_dir=temp_databases) is False

    # Anonymous / None has no access
    assert can_access_database("db_a", None, databases_dir=temp_databases) is False
    assert can_access_database("db_a", "anonymous", databases_dir=temp_databases) is False


def test_list_databases_filtering(temp_databases):
    # Unfiltered (or admin) returns all
    all_dbs = list_databases(databases_dir=temp_databases)
    ids_all = [d["id"] for d in all_dbs]
    assert "db_a" in ids_all and "db_b" in ids_all

    # Filtered by rahul returns only db_b
    rahul_dbs = list_databases(databases_dir=temp_databases, user="rahul")
    ids_rahul = [d["id"] for d in rahul_dbs]
    assert ids_rahul == ["db_b"]

    # Filtered by priya returns empty list
    priya_dbs = list_databases(databases_dir=temp_databases, user="priya")
    assert priya_dbs == []


def test_update_database_access_preserves_admin(temp_databases):
    # Update db_a to include rahul
    ok = update_database_access("db_a", ["rahul"], databases_dir=temp_databases)
    assert ok is True

    allowed = get_database_access("db_a", databases_dir=temp_databases)
    assert "admin" in allowed
    assert "rahul" in allowed

    # Now rahul can access db_a
    assert can_access_database("db_a", "rahul", databases_dir=temp_databases) is True


# ---------------------------------------------------------------------------
# 2. API Endpoints: Non-Admin Forbidden (Admin-Only Enforcement)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_admin_cannot_manage_database_access_or_connections(temp_databases):
    with patch("src.sql.registry.DATABASES_DIR", temp_databases):
        async with _client() as client:
            # 1. Non-admin cannot view access
            res_get_access = await client.get(
                "/api/databases/db_a/access",
                headers={"X-User-Id": "rahul"},
            )
            assert res_get_access.status_code == 403
            assert "Admin access required" in res_get_access.json()["detail"]

            # 2. Non-admin cannot modify access
            res_post_access = await client.post(
                "/api/databases/db_a/access",
                json={"allowed_users": ["rahul"]},
                headers={"X-User-Id": "rahul"},
            )
            assert res_post_access.status_code == 403
            assert "Admin access required" in res_post_access.json()["detail"]

            # 3. Non-admin cannot test connection
            res_test_conn = await client.post(
                "/api/databases/db_a/test",
                json={"engine": "sqlite", "database": ":memory:"},
                headers={"X-User-Id": "rahul"},
            )
            assert res_test_conn.status_code == 403
            assert "Admin access required" in res_test_conn.json()["detail"]

            # 4. Non-admin cannot save connection
            res_save_conn = await client.post(
                "/api/databases/db_a/connection",
                json={"engine": "sqlite", "database": ":memory:"},
                headers={"X-User-Id": "rahul"},
            )
            assert res_save_conn.status_code == 403
            assert "Admin access required" in res_save_conn.json()["detail"]


# ---------------------------------------------------------------------------
# 3. Database List Hiding via API
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_database_listing_hides_unauthorized_databases(temp_databases):
    with patch("src.sql.registry.DATABASES_DIR", temp_databases):
        async with _client() as client:
            # 1. Admin sees both db_a and db_b
            res_admin = await client.get("/api/databases", headers={"X-User-Id": "admin"})
            assert res_admin.status_code == 200
            admin_ids = [d["id"] for d in res_admin.json()]
            assert "db_a" in admin_ids
            assert "db_b" in admin_ids

            # 2. Rahul sees only db_b; db_a is hidden
            res_rahul = await client.get("/api/databases", headers={"X-User-Id": "rahul"})
            assert res_rahul.status_code == 200
            rahul_ids = [d["id"] for d in res_rahul.json()]
            assert "db_b" in rahul_ids
            assert "db_a" not in rahul_ids

            # 3. Priya sees neither
            res_priya = await client.get("/api/databases", headers={"X-User-Id": "priya"})
            assert res_priya.status_code == 200
            priya_ids = [d["id"] for d in res_priya.json()]
            assert "db_a" not in priya_ids
            assert "db_b" not in priya_ids

            # 4. Rahul attempting to get db_a metadata -> 403 Forbidden
            res_rahul_get = await client.get("/api/databases/db_a", headers={"X-User-Id": "rahul"})
            assert res_rahul_get.status_code == 403
            assert "Access denied" in res_rahul_get.json()["detail"]

            # 5. Rahul getting db_b metadata -> 200 OK
            res_rahul_get_b = await client.get("/api/databases/db_b", headers={"X-User-Id": "rahul"})
            assert res_rahul_get_b.status_code == 200
            assert res_rahul_get_b.json()["id"] == "db_b"


# ---------------------------------------------------------------------------
# 4. Admin Grants Access Flow (Mirroring Document Access Flow)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_admin_grants_database_access_lifecycle(temp_databases):
    with patch("src.sql.registry.DATABASES_DIR", temp_databases):
        async with _client() as client:
            # 1. Initially rahul cannot see db_a
            res_init = await client.get("/api/databases", headers={"X-User-Id": "rahul"})
            assert "db_a" not in [d["id"] for d in res_init.json()]

            # 2. Admin grants rahul access to db_a
            res_grant = await client.post(
                "/api/databases/db_a/access",
                json={"allowed_users": ["rahul"]},
                headers={"X-User-Id": "admin"},
            )
            assert res_grant.status_code == 200
            assert "rahul" in res_grant.json()["allowed_users"]
            assert "admin" in res_grant.json()["allowed_users"]

            # 3. Now rahul has access and sees db_a in the list
            res_after = await client.get("/api/databases", headers={"X-User-Id": "rahul"})
            assert "db_a" in [d["id"] for d in res_after.json()]

            # 4. Priya was not granted access -> still cannot see it
            res_priya = await client.get("/api/databases", headers={"X-User-Id": "priya"})
            assert "db_a" not in [d["id"] for d in res_priya.json()]


# ---------------------------------------------------------------------------
# 5. Query Endpoint Enforces Database Access
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_query_endpoint_enforces_database_access(temp_databases, monkeypatch):
    class FakePipeline:
        def __init__(self, *args, **kwargs):
            pass

        async def query(self, question, filters=None, db_id=None, **_):
            return QueryResult(query=question, answer=f"answer for {db_id}", model_used="test")

    monkeypatch.setattr("src.api.query.QueryPipeline", FakePipeline)

    with patch("src.sql.registry.DATABASES_DIR", temp_databases):
        async with _client() as client:
            # 1. Rahul querying db_b (allowed) -> 200 OK
            res_b = await client.post(
                "/api/query",
                json={"question": "Show sales orders", "db_id": "db_b"},
                headers={"X-User-Id": "rahul"},
            )
            assert res_b.status_code == 200

            # 2. Rahul querying db_a (not allowed) -> 403 Forbidden
            res_a = await client.post(
                "/api/query",
                json={"question": "Show confidential orders", "db_id": "db_a"},
                headers={"X-User-Id": "rahul"},
            )
            assert res_a.status_code == 403
            assert "Access denied" in res_a.json()["detail"]

            # 3. Admin querying db_a (allowed) -> 200 OK
            res_admin = await client.post(
                "/api/query",
                json={"question": "Show confidential orders", "db_id": "db_a"},
                headers={"X-User-Id": "admin"},
            )
            assert res_admin.status_code == 200


# ---------------------------------------------------------------------------
# 6. SQLRetriever Hides Unauthorized Query Results
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sql_retriever_hides_unauthorized_database_results(temp_databases):
    with patch("src.sql.registry.DATABASES_DIR", temp_databases):
        # Create retriever for db_a (only admin allowed)
        retriever_a = SQLRetriever(router=None, vector_store=None, embedding_service=None, db_id="db_a")

        # When rahul (unauthorized) retrieves from db_a, empty list is returned immediately
        chunks = await retriever_a.retrieve("SELECT * FROM alpha_table", user_id="rahul")
        assert chunks == []

        # When priya (unauthorized) retrieves from db_a, empty list is returned
        chunks_priya = await retriever_a.retrieve("SELECT * FROM alpha_table", user_id="priya")
        assert chunks_priya == []


# ---------------------------------------------------------------------------
# 7. Admin Test & Save Connection Endpoints
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_admin_test_and_save_database_connection(temp_databases, tmp_path):
    conn_json = tmp_path / "test_connections.json"

    sqlite_db_file = tmp_path / "test_app.db"
    import sqlite3
    conn = sqlite3.connect(str(sqlite_db_file))
    conn.execute("CREATE TABLE sample (id INT);")
    conn.commit()
    conn.close()

    with patch("src.sql.registry.DATABASES_DIR", temp_databases), \
         patch("src.sql.registry.connections_path", return_value=conn_json):

        async with _client() as client:
            # 1. Admin tests connection with valid sqlite path -> 200 OK
            res_test = await client.post(
                "/api/databases/db_a/test",
                json={"engine": "sqlite", "path": str(sqlite_db_file)},
                headers={"X-User-Id": "admin"},
            )
            assert res_test.status_code == 200
            assert res_test.json() == {"ok": True, "db_id": "db_a"}

            # 2. Admin tests with unsupported engine -> 422
            res_invalid = await client.post(
                "/api/databases/db_a/test",
                json={"engine": "invalid_engine", "path": "none"},
                headers={"X-User-Id": "admin"},
            )
            assert res_invalid.status_code == 422

            # 3. Admin saves connection -> 200 OK
            res_save = await client.post(
                "/api/databases/db_a/connection",
                json={"engine": "sqlite", "path": str(sqlite_db_file)},
                headers={"X-User-Id": "admin"},
            )
            assert res_save.status_code == 200
            assert res_save.json()["status"] == "saved"
            assert res_save.json()["db_id"] == "db_a"

            # 4. Verify saved connection exists in connections.json
            saved_data = json.loads(conn_json.read_text(encoding="utf-8"))
            assert "db_a" in saved_data
            assert saved_data["db_a"]["path"] == str(sqlite_db_file)
