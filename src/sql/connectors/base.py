"""Database connector protocol and base types."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Connector(Protocol):
    """Protocol for engine-specific database connectors."""

    async def test(self, cfg: dict[str, Any] | None = None) -> None:
        """Test database connection health and credentials.

        Raises an exception if connection fails.
        """
        ...

    async def run_readonly(
        self,
        sql: str,
        params: dict[str, Any] | tuple[Any, ...] | list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a read-only query and return rows as dictionaries."""
        ...
