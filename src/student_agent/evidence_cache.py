"""Per-case evidence cache bound to one competition run.

Evidence refs are only valid for the run that produced them, so the cache directory is keyed by
the run expiry configured in DAY09_RUN_EXPIRES_AT and is ignored once that run has expired.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .mcp_gateway import EvidenceGateway

NO_DATA_MARKER = "Error executing tool"
TRANSIENT_RETRIES = 1


class ToolNoData(RuntimeError):
    """The MCP tool answered with an error for this scope (e.g. no refund events exist)."""


def run_cache_dir(root: Path) -> Path | None:
    expires = os.getenv("DAY09_RUN_EXPIRES_AT", "").strip()
    if not expires:
        return None
    try:
        moment = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment <= datetime.now(UTC):
        return None
    return root / ".cache" / "evidence" / re.sub(r"[^0-9A-Za-z]", "", expires)


class CachedGateway:
    def __init__(self, gateway: EvidenceGateway | None, cache_dir: Path | None) -> None:
        self._gateway = gateway
        self._cache_dir = cache_dir
        self.network_calls = 0
        self.cache_hits = 0
        self.calls_by_case: dict[str, int] = {}

    async def list_tools(self) -> list[str]:
        if self._gateway is None:
            return []
        return await self._gateway.list_tools()

    def _file(self, case_id: str) -> Path | None:
        return None if self._cache_dir is None else self._cache_dir / f"{case_id}.json"

    def _load(self, case_id: str) -> dict[str, Any]:
        path = self._file(case_id)
        if path is None or not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _store(self, case_id: str, key: str, value: dict[str, Any]) -> None:
        path = self._file(case_id)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        entries = self._load(case_id)
        entries[key] = value
        path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        key = json.dumps([tool_name, sorted(arguments.items())], separators=(",", ":"))
        self.calls_by_case[case_id] = self.calls_by_case.get(case_id, 0) + 1
        cached = self._load(case_id).get(key)
        if cached is not None:
            self.cache_hits += 1
            if "error" in cached:
                raise ToolNoData(cached["error"])
            return cached["evidence"]
        if self._gateway is None:
            raise RuntimeError(f"offline mode: no cached evidence for {tool_name} in {case_id}")
        attempt = 0
        while True:
            self.network_calls += 1
            try:
                evidence = await self._gateway.call(tool_name, case_id=case_id, **arguments)
            except RuntimeError as exc:
                if NO_DATA_MARKER in str(exc):
                    self._store(case_id, key, {"error": str(exc)})
                    raise ToolNoData(str(exc)) from exc
                raise
            except (TimeoutError, OSError):
                if attempt >= TRANSIENT_RETRIES:
                    raise
                attempt += 1
                await asyncio.sleep(1.0)
                continue
            self._store(case_id, key, {"evidence": evidence})
            return evidence
