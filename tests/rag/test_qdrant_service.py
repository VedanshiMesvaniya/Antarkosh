"""Unit tests for cross-platform Qdrant service manager."""

from __future__ import annotations

import platform
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.core.qdrant_service as qs


@pytest.mark.asyncio
async def test_find_qdrant_binary_finds_project_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(qs, "PROJECT_ROOT", tmp_path)
    qdir = tmp_path / "qdrant"
    qdir.mkdir()
    target_name = "qdrant.exe" if platform.system() == "Windows" else "qdrant"
    bin_file = qdir / target_name
    bin_file.write_text("binary-stub")

    found = qs.find_qdrant_binary()
    assert found == bin_file


@pytest.mark.asyncio
async def test_is_qdrant_ready_true_on_200():
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
        ready = await qs.is_qdrant_ready("http://localhost:6333")
        assert ready is True


@pytest.mark.asyncio
async def test_is_qdrant_ready_false_on_connection_error():
    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=Exception("refused"))):
        ready = await qs.is_qdrant_ready("http://localhost:6333")
        assert ready is False


@pytest.mark.asyncio
async def test_ensure_qdrant_running_when_already_ready():
    with patch("src.core.qdrant_service.is_qdrant_ready", new=AsyncMock(return_value=True)):
        ok = await qs.ensure_qdrant_running()
        assert ok is True
