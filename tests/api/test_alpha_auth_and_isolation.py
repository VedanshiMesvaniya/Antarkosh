"""Unit and integration tests for Alpha Test Authentication and Data Isolation.

Verifies:
1. Authentication:
   - Login with valid/invalid credentials
   - Session cookie creation and verification
   - X-User-Id header fallback
   - Current user introspection (/api/auth/me) and logout
2. Chat Isolation:
   - User A cannot see User B's chats in /api/chats
   - User A cannot read, modify, or delete User B's chats (403 Forbidden)
   - Admin can view all chats
3. Document Isolation & Deduplication:
   - Registry deduplication is user-scoped (User B is not blocked from uploading a file User A uploaded)
   - User A cannot see User B's documents in /api/documents
   - User A cannot delete User B's document (403 Forbidden)
4. Vector Store Qdrant Filtering:
   - _build_filter correctly constructs MatchAny([target_user, "system", "shared"]) + IsEmptyCondition
   - Chunks belonging to other users are excluded
"""

from __future__ import annotations

import pytest
from pathlib import Path
from httpx import ASGITransport, AsyncClient
from unittest.mock import patch

from src.main import app
from src.core.state import state_manager
from src.core.ingestion_registry import IngestionRegistry, RegistryStatus
from src.stages.s11_vector_store import QdrantStore
from qdrant_client.models import IsEmptyCondition


@pytest.fixture
def clean_state(tmp_path):
    """Isolate state manager with a clean temp directory."""
    original_chats_file = state_manager.chats_file
    original_messages_file = state_manager.messages_file
    original_documents_file = state_manager.documents_file

    state_manager.chats_file = tmp_path / "chats.json"
    state_manager.messages_file = tmp_path / "messages.json"
    state_manager.documents_file = tmp_path / "documents.json"

    yield state_manager

    state_manager.chats_file = original_chats_file
    state_manager.messages_file = original_messages_file
    state_manager.documents_file = original_documents_file


# ---------------------------------------------------------------------------
# 1. Authentication Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auth_login_success():
    """Verify login with valid alpha credentials returns 200 and sets session cookie."""
    from src.api.auth import ALPHA_USERS
    mihir_pass = ALPHA_USERS.get("mihir", "mihir123")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/auth/login",
            json={"username": "mihir", "password": mihir_pass},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "logged_in"
        assert data["user_id"] == "mihir"
        assert "alpha_session" in res.cookies


@pytest.mark.asyncio
async def test_auth_login_invalid_password():
    """Verify login with invalid password returns 401."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/auth/login",
            json={"username": "mihir", "password": "wrong_password"},
        )
        assert res.status_code == 401
        assert "Invalid Alpha credentials" in res.json()["detail"]


@pytest.mark.asyncio
async def test_auth_login_unknown_user():
    """Verify login with unknown username returns 401."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/auth/login",
            json={"username": "ghost_user", "password": "password"},
        )
        assert res.status_code == 401


@pytest.mark.asyncio
async def test_auth_me_cookie_and_header():
    """Verify /api/auth/me works with cookie, header, and falls back to anonymous."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Anonymous (no auth)
        res_anon = await client.get("/api/auth/me")
        assert res_anon.status_code == 200
        assert res_anon.json()["user_id"] == "anonymous"
        assert res_anon.json()["authenticated"] is False

        # Header fallback (X-User-Id)
        res_header = await client.get(
            "/api/auth/me",
            headers={"X-User-Id": "rahul"},
        )
        assert res_header.status_code == 200
        assert res_header.json()["user_id"] == "rahul"
        assert res_header.json()["authenticated"] is True

        # Cookie authentication
        login_res = await client.post(
            "/api/auth/login",
            json={"username": "priya", "password": "alpha_pass_3"},
        )
        assert login_res.status_code == 200
        res_cookie = await client.get("/api/auth/me")
        assert res_cookie.status_code == 200
        assert res_cookie.json()["user_id"] == "priya"
        assert res_cookie.json()["authenticated"] is True

        # Logout clears cookie
        logout_res = await client.post("/api/auth/logout")
        assert logout_res.status_code == 200
        res_post_logout = await client.get("/api/auth/me")
        assert res_post_logout.json()["user_id"] == "anonymous"


# ---------------------------------------------------------------------------
# 2. Chat Isolation Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_isolation_between_users(clean_state):
    """Verify User A's chats cannot be viewed, edited, or deleted by User B."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Mihir creates a chat
        res_m = await client.post(
            "/api/chats",
            json={"title": "Mihir Confidential Plan"},
            headers={"X-User-Id": "mihir"},
        )
        assert res_m.status_code == 200
        mihir_chat = res_m.json()
        mihir_chat_id = mihir_chat["id"]
        assert mihir_chat["userId"] == "mihir"

        # 2. Rahul creates a chat
        res_r = await client.post(
            "/api/chats",
            json={"title": "Rahul Public Notes"},
            headers={"X-User-Id": "rahul"},
        )
        assert res_r.status_code == 200
        rahul_chat = res_r.json()
        rahul_chat_id = rahul_chat["id"]

        # 3. Mihir lists chats -> sees only Mihir's chat
        res_mihir_list = await client.get("/api/chats", headers={"X-User-Id": "mihir"})
        assert res_mihir_list.status_code == 200
        mihir_chats = res_mihir_list.json()
        assert any(c["id"] == mihir_chat_id for c in mihir_chats)
        assert not any(c["id"] == rahul_chat_id for c in mihir_chats)

        # 4. Rahul lists chats -> sees only Rahul's chat
        res_rahul_list = await client.get("/api/chats", headers={"X-User-Id": "rahul"})
        assert res_rahul_list.status_code == 200
        rahul_chats = res_rahul_list.json()
        assert any(c["id"] == rahul_chat_id for c in rahul_chats)
        assert not any(c["id"] == mihir_chat_id for c in rahul_chats)

        # 5. Rahul attempts to read Mihir's messages -> 403 Forbidden
        res_read = await client.get(
            f"/api/chats/{mihir_chat_id}/messages",
            headers={"X-User-Id": "rahul"},
        )
        assert res_read.status_code == 403
        assert "Access denied" in res_read.json()["detail"]

        # 6. Rahul attempts to update Mihir's chat title -> 403 Forbidden
        res_patch = await client.patch(
            f"/api/chats/{mihir_chat_id}",
            json={"title": "Hacked Title"},
            headers={"X-User-Id": "rahul"},
        )
        assert res_patch.status_code == 403

        # 7. Rahul attempts to delete Mihir's chat -> 403 Forbidden
        res_del = await client.delete(
            f"/api/chats/{mihir_chat_id}",
            headers={"X-User-Id": "rahul"},
        )
        assert res_del.status_code == 403

        # 8. Admin lists chats -> can see all chats
        res_admin_list = await client.get("/api/chats", headers={"X-User-Id": "admin"})
        assert res_admin_list.status_code == 200
        admin_chats = res_admin_list.json()
        assert any(c["id"] == mihir_chat_id for c in admin_chats)
        assert any(c["id"] == rahul_chat_id for c in admin_chats)


# ---------------------------------------------------------------------------
# 3. Document Registry & Ingestion Isolation Tests
# ---------------------------------------------------------------------------

def test_ingestion_registry_user_deduplication(tmp_path):
    """Verify deduplication is user-scoped in IngestionRegistry."""
    registry_file = tmp_path / "test_registry.json"
    registry = IngestionRegistry(registry_path=registry_file)

    dummy_file = tmp_path / "sample_doc.txt"
    dummy_file.write_text("Unique content for alpha testing isolation 2026")

    # 1. Mihir checks file -> NEW_FILE
    check_m1 = registry.check(dummy_file, user_id="mihir")
    assert check_m1.status == RegistryStatus.NEW_FILE

    # 2. Mihir creates version
    entry_m = registry.create_version(
        file_path=dummy_file,
        content_hash=check_m1.sha256,
        total_chunks=5,
        user_id="mihir",
    )
    assert entry_m["user_id"] == "mihir"

    # 3. Mihir checks same file again -> ALREADY_INGESTED
    check_m2 = registry.check(dummy_file, user_id="mihir")
    assert check_m2.status == RegistryStatus.ALREADY_INGESTED

    # 4. Rahul checks the EXACT SAME file -> NEW_FILE (not blocked by Mihir's upload!)
    check_r1 = registry.check(dummy_file, user_id="rahul")
    assert check_r1.status == RegistryStatus.NEW_FILE

    # 5. Rahul creates his version
    entry_r = registry.create_version(
        file_path=dummy_file,
        content_hash=check_r1.sha256,
        total_chunks=5,
        user_id="rahul",
    )
    assert entry_r["user_id"] == "rahul"

    # 6. Active listing isolation
    mihir_docs = registry.get_active(user_id="mihir")
    rahul_docs = registry.get_active(user_id="rahul")

    assert any(d["document_id"] == entry_m["document_id"] for d in mihir_docs)
    assert not any(d["document_id"] == entry_r["document_id"] for d in mihir_docs)

    assert any(d["document_id"] == entry_r["document_id"] for d in rahul_docs)
    assert not any(d["document_id"] == entry_m["document_id"] for d in rahul_docs)


@pytest.mark.asyncio
async def test_ui_document_endpoints_isolation(tmp_path):
    """Verify /api/documents respects user ownership and blocks unauthorized deletion."""
    registry_file = tmp_path / "test_reg_ui.json"
    dummy_file_m = tmp_path / "mihir_strategy.pdf"
    dummy_file_m.write_text("Mihir Strategy Document")

    dummy_file_r = tmp_path / "rahul_code.py"
    dummy_file_r.write_text("Rahul Code Snippets")

    test_registry = IngestionRegistry(registry_path=registry_file)
    v_m = test_registry.create_version(dummy_file_m, "hash_m", 10, user_id="mihir")
    v_r = test_registry.create_version(dummy_file_r, "hash_r", 8, user_id="rahul")

    with patch("src.core.ingestion_registry.IngestionRegistry.get_all", return_value=test_registry.get_all()), \
         patch("src.core.ingestion_registry.IngestionRegistry.get_by_document_id", side_effect=test_registry.get_by_document_id):

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Mihir lists docs -> sees only Mihir's doc
            res_m = await client.get("/api/documents", headers={"X-User-Id": "mihir"})
            assert res_m.status_code == 200
            m_names = [d["name"] for d in res_m.json()]
            assert "mihir_strategy.pdf" in m_names
            assert "rahul_code.py" not in m_names

            # Rahul lists docs -> sees only Rahul's doc
            res_r = await client.get("/api/documents", headers={"X-User-Id": "rahul"})
            assert res_r.status_code == 200
            r_names = [d["name"] for d in res_r.json()]
            assert "rahul_code.py" in r_names
            assert "mihir_strategy.pdf" not in r_names

            # Rahul attempts to delete Mihir's doc -> 403 Forbidden
            res_del = await client.delete(
                f"/api/documents/{v_m['document_id']}",
                headers={"X-User-Id": "rahul"},
            )
            assert res_del.status_code == 403
            assert "Access denied" in res_del.json()["detail"]


@pytest.mark.asyncio
async def test_only_admin_can_upload_update_delete_documents(tmp_path):
    """Verify non-admin cannot upload, update, or delete documents, but can view admin documents."""
    registry_file = tmp_path / "test_reg_admin_only.json"
    dummy_doc = tmp_path / "company_handbook.pdf"
    dummy_doc.write_text("Company Handbook 2026")

    test_registry = IngestionRegistry(registry_path=registry_file)
    v_admin = test_registry.create_version(dummy_doc, "hash_admin", 12, user_id="admin")

    with patch("src.core.ingestion_registry.IngestionRegistry.get_all", side_effect=test_registry.get_all), \
         patch("src.core.ingestion_registry.IngestionRegistry.get_by_document_id", side_effect=test_registry.get_by_document_id), \
         patch("src.core.ingestion_registry.IngestionRegistry.update_document_access", side_effect=test_registry.update_document_access):

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Non-admin (rahul) attempts to upload -> 403 Forbidden
            res_upload = await client.post(
                "/api/upload",
                files={"file": ("test.pdf", b"dummy content", "application/pdf")},
                headers={"X-User-Id": "rahul"},
            )
            assert res_upload.status_code == 403
            assert "Admin access required" in res_upload.json()["detail"]

            # 2. Non-admin (rahul) attempts to replace -> 403 Forbidden
            res_replace = await client.post(
                f"/api/documents/{v_admin['document_id']}/replace",
                files={"file": ("test_v2.pdf", b"new dummy content", "application/pdf")},
                headers={"X-User-Id": "rahul"},
            )
            assert res_replace.status_code == 403
            assert "Admin access required" in res_replace.json()["detail"]

            # 3. Non-admin (rahul) attempts to delete -> 403 Forbidden
            res_delete = await client.delete(
                f"/api/documents/{v_admin['document_id']}",
                headers={"X-User-Id": "rahul"},
            )
            assert res_delete.status_code == 403
            assert "Admin access required" in res_delete.json()["detail"]

            # 4. Initially rahul has no access -> not in document list
            res_list_initial = await client.get("/api/documents", headers={"X-User-Id": "rahul"})
            assert res_list_initial.status_code == 200
            names_initial = [d["name"] for d in res_list_initial.json()]
            assert "company_handbook.pdf" not in names_initial

            # 5. Non-admin cannot modify access -> 403 Forbidden
            res_access_forbidden = await client.post(
                f"/api/documents/{v_admin['document_id']}/access",
                json={"allowed_users": ["rahul"]},
                headers={"X-User-Id": "rahul"},
            )
            assert res_access_forbidden.status_code == 403

            # 6. Admin grants rahul access
            res_access = await client.post(
                f"/api/documents/{v_admin['document_id']}/access",
                json={"allowed_users": ["rahul"]},
                headers={"X-User-Id": "admin"},
            )
            assert res_access.status_code == 200
            assert "rahul" in res_access.json()["allowed_users"]

            # 7. Now rahul has access and sees it
            res_list_after = await client.get("/api/documents", headers={"X-User-Id": "rahul"})
            assert res_list_after.status_code == 200
            names_after = [d["name"] for d in res_list_after.json()]
            assert "company_handbook.pdf" in names_after

            # 8. Priya was not granted access -> still cannot see it
            res_list_priya = await client.get("/api/documents", headers={"X-User-Id": "priya"})
            assert res_list_priya.status_code == 200
            names_priya = [d["name"] for d in res_list_priya.json()]
            assert "company_handbook.pdf" not in names_priya


@pytest.mark.asyncio
async def test_user_cannot_get_answer_from_unauthorized_document(tmp_path):
    """Verify retrieval excludes documents a user does not have permission to access."""
    from src.stages.s12_s13_s14_retrieval import Retriever
    from src.models.schemas import Chunk, ChunkType, DocumentType, RetrievedChunk

    registry_file = tmp_path / "test_reg_retrieval.json"
    dummy_secret = tmp_path / "secret_plans.pdf"
    dummy_secret.write_text("Secret Company Plans 2026")
    dummy_public = tmp_path / "public_info.pdf"
    dummy_public.write_text("Public Company Info 2026")

    test_reg = IngestionRegistry(registry_path=registry_file)
    v_secret = test_reg.create_version(dummy_secret, "hash_sec", 5, user_id="admin", allowed_users=["mihir"])
    v_public = test_reg.create_version(dummy_public, "hash_pub", 5, user_id="admin", allowed_users=["rahul"])

    chunk_sec = RetrievedChunk(
        chunk=Chunk(
            chunk_id="c_sec",
            document_id=v_secret["document_id"],
            content="Top secret merger details 2026",
            chunk_type=ChunkType.PROSE,
            document_type=DocumentType.GENERAL,
            source_file="secret_plans.pdf",
            user_id="admin",
        ),
        score=0.95,
        retrieval_method="dense",
    )
    chunk_pub = RetrievedChunk(
        chunk=Chunk(
            chunk_id="c_pub",
            document_id=v_public["document_id"],
            content="Public company holiday schedule",
            chunk_type=ChunkType.PROSE,
            document_type=DocumentType.GENERAL,
            source_file="public_info.pdf",
            user_id="admin",
        ),
        score=0.90,
        retrieval_method="dense",
    )

    from unittest.mock import AsyncMock, MagicMock
    mock_store = MagicMock()
    mock_store.search_hybrid = AsyncMock(return_value=[chunk_sec, chunk_pub])
    mock_embed = MagicMock()
    mock_embed.embed_query = AsyncMock(return_value=([0.1]*10, None))

    retriever = Retriever(store=mock_store, embedding_service=mock_embed)

    with patch("src.core.ingestion_registry.IngestionRegistry.get_active", side_effect=test_reg.get_active):
        # 1. Rahul asks query (only has access to v_public, NOT v_secret)
        rahul_results = await retriever.retrieve("tell me secret plans", filters={"user_id": "rahul"})
        rahul_doc_ids = [r.chunk.document_id for r in rahul_results]
        assert v_secret["document_id"] not in rahul_doc_ids
        assert v_public["document_id"] in rahul_doc_ids

        # 2. User with no document access (priya) gets empty list immediately
        priya_results = await retriever.retrieve("tell me secret plans", filters={"user_id": "priya"})
        assert priya_results == []

        # 3. Mihir has access to v_secret
        mihir_results = await retriever.retrieve("tell me secret plans", filters={"user_id": "mihir"})
        mihir_doc_ids = [r.chunk.document_id for r in mihir_results]
        assert v_secret["document_id"] in mihir_doc_ids

        # 4. Admin has unrestricted access to all documents
        admin_results = await retriever.retrieve("tell me secret plans", filters={"user_id": "admin"})
        admin_doc_ids = [r.chunk.document_id for r in admin_results]
        assert v_secret["document_id"] in admin_doc_ids
        assert v_public["document_id"] in admin_doc_ids




# ---------------------------------------------------------------------------
# 4. Qdrant Vector Store Filter Tests
# ---------------------------------------------------------------------------

def test_qdrant_store_build_filter_user_isolation():
    """Verify QdrantStore._build_filter constructs correct multi-tenant filters."""
    # When user_id="mihir" is passed
    q_filter = QdrantStore._build_filter({"user_id": "mihir"})
    assert q_filter is not None
    assert q_filter.must is not None

    # Find the user clause
    user_clause = next((cond for cond in q_filter.must if hasattr(cond, "should") and cond.should), None)
    assert user_clause is not None
    should_conditions = user_clause.should

    # Verify MatchAny condition has target user, system, and shared
    match_any_cond = next((c for c in should_conditions if hasattr(c, "match") and hasattr(c.match, "any")), None)
    assert match_any_cond is not None
    assert "mihir" in match_any_cond.match.any
    assert "system" in match_any_cond.match.any
    assert "shared" in match_any_cond.match.any
    assert "admin" in match_any_cond.match.any
    assert "rahul" not in match_any_cond.match.any

    # Verify IsEmptyCondition is included for legacy unassigned chunks
    empty_cond = next((c for c in should_conditions if isinstance(c, IsEmptyCondition)), None)
    assert empty_cond is not None
    assert empty_cond.is_empty.key == "user_id"

    # Always excludes inactive (superseded) chunks
    assert any(c.key == "active" for c in q_filter.must_not)


@pytest.mark.asyncio
async def test_admin_manage_document_user_access_api(tmp_path):
    """Verify admin can list users, retrieve doc access, and assign access, while non-admins are blocked."""
    registry_file = tmp_path / "test_reg_access.json"
    dummy_file = tmp_path / "roadmap.pdf"
    dummy_file.write_text("Company Roadmap")

    test_registry = IngestionRegistry(registry_path=registry_file)
    entry = test_registry.create_version(dummy_file, "hash_roadmap", 12, user_id="admin", allowed_users=["mihir"])
    doc_id = entry["document_id"]

    with patch("src.core.ingestion_registry.IngestionRegistry.get_all", return_value=test_registry.get_all()), \
         patch("src.core.ingestion_registry.IngestionRegistry.get_by_document_id", side_effect=test_registry.get_by_document_id), \
         patch("src.core.ingestion_registry.IngestionRegistry.update_document_access", side_effect=test_registry.update_document_access):

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Non-admin trying to get /api/users -> 403 Forbidden
            res_users_user = await client.get("/api/users", headers={"X-User-Id": "mihir"})
            assert res_users_user.status_code == 403

            # 2. Admin gets /api/users -> 200 OK and list of users
            res_users_admin = await client.get("/api/users", headers={"X-User-Id": "admin"})
            assert res_users_admin.status_code == 200
            users_data = res_users_admin.json()
            assert "mihir" in users_data["users"]
            assert "admin" not in users_data["users"]  # admin excluded from selectable users

            # 3. Non-admin getting doc access -> 403
            res_acc_user = await client.get(f"/api/documents/{doc_id}/access", headers={"X-User-Id": "mihir"})
            assert res_acc_user.status_code == 403

            # 4. Admin getting doc access -> 200
            res_acc_admin = await client.get(f"/api/documents/{doc_id}/access", headers={"X-User-Id": "admin"})
            assert res_acc_admin.status_code == 200
            assert res_acc_admin.json()["allowed_users"] == ["mihir"]

            # 5. Non-admin modifying access -> 403
            res_update_user = await client.post(
                f"/api/documents/{doc_id}/access",
                headers={"X-User-Id": "rahul"},
                json={"allowed_users": ["rahul"]},
            )
            assert res_update_user.status_code == 403

            # 6. Admin modifies access to add vedanshi and remove mihir
            res_update_admin = await client.post(
                f"/api/documents/{doc_id}/access",
                headers={"X-User-Id": "admin"},
                json={"allowed_users": ["vedanshi"]},
            )
            assert res_update_admin.status_code == 200
            assert res_update_admin.json()["allowed_users"] == ["vedanshi"]

            # 7. Verify document entry reflects update
            updated_entry = test_registry.get_by_document_id(doc_id)
            assert updated_entry["allowed_users"] == ["vedanshi"]

