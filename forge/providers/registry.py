"""Provider registry: detection, model discovery, verification probes, profiles.

A model is only routable when its status is `verified` (a probe or a real run
succeeded), unless the user explicitly allows unverified models.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from ..db import dumps, loads, now
from .base import ProviderAdapter, ProviderStatus
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .mock import MockAdapter

if TYPE_CHECKING:
    from ..app import Forge

TIER_COST = {"fast": 0.4, "standard": 1.0, "strong": 2.5}
_STRONG = re.compile(r"opus|astra|\bpro\b|-max\b|ultra", re.I)
_FAST = re.compile(r"haiku|luna|mini|nano|flash|lite|fast", re.I)


def default_profile(provider: str, model_id: str, caps: dict) -> dict:
    """Initial, explainable guess. Stored per model and editable in the UI/API."""
    if provider == "mock":
        tier = caps.get("tier", "standard")
    elif _STRONG.search(model_id):
        tier = "strong"
    elif _FAST.search(model_id):
        tier = "fast"
    else:
        tier = "standard"
    return {"tier": tier, "cost_index": TIER_COST[tier], "strengths": [],
            "source": "name-based default; edit in Agent Control Center"}


class ProviderRegistry:
    def __init__(self, forge: "Forge"):
        self.forge = forge
        self.db = forge.db
        self._build()

    def _build(self) -> None:
        s = self.forge.settings()
        adapters: list[ProviderAdapter] = [
            ClaudeCodeAdapter(s.claude_path, s.claude_models),
            CodexAdapter(s.codex_path, s.codex_ignore_user_config),
        ]
        if s.enable_mock_provider:
            adapters.append(MockAdapter())
        self.adapters: dict[str, ProviderAdapter] = {
            a.name: a for a in adapters if a.name not in s.disabled_providers}

    def reload(self) -> None:
        self._build()

    def get(self, name: str) -> ProviderAdapter:
        if name not in self.adapters:
            raise KeyError(f"provider {name!r} is not enabled")
        return self.adapters[name]

    # --- detection & discovery ----------------------------------------------------
    def refresh(self, *, probe: bool = False) -> list[dict]:
        self._build()
        for name, a in self.adapters.items():
            try:
                st = a.detect()
            except Exception as e:  # noqa: BLE001
                st = ProviderStatus(provider=name, display_name=a.display_name, installed=False,
                                    notes=[f"detection error: {e}"])
            prev = loads((self.db.one("SELECT data FROM provider_status WHERE provider=?", (name,)) or {}).get("data"), {})
            data = asdict(st)
            data["last_probe_error"] = prev.get("last_probe_error")
            self.db.execute(
                "INSERT INTO provider_status(provider,data,checked_at) VALUES (?,?,?) "
                "ON CONFLICT(provider) DO UPDATE SET data=excluded.data, checked_at=excluded.checked_at",
                (name, dumps(data), now()))
            if st.installed:
                try:
                    models = a.discover_models()
                except Exception:  # noqa: BLE001
                    models = []
                with self.db.tx() as c:
                    for m in models:
                        row = c.execute("SELECT profile FROM models WHERE provider=? AND model_id=?",
                                        (name, m.model_id)).fetchone()
                        if row is None:
                            c.execute(
                                "INSERT INTO models(provider,model_id,display_name,source,status,capabilities,profile,"
                                "enabled,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                (name, m.model_id, m.display_name, m.source, "unverified", dumps(m.capabilities),
                                 dumps(default_profile(name, m.model_id, m.capabilities)), 0 if m.hidden else 1, now()))
                        else:
                            c.execute("UPDATE models SET display_name=?, source=?, capabilities=?, updated_at=? "
                                      "WHERE provider=? AND model_id=?",
                                      (m.display_name, m.source, dumps(m.capabilities), now(), name, m.model_id))
        if probe:
            asyncio.run(self.probe_all())
        self.forge.bus.emit("providers.refreshed", "Provider detection refreshed",
                            data={"providers": list(self.adapters)})
        return self.statuses()

    async def probe_all(self, *, only_unverified: bool = False) -> list[dict]:
        out = []
        for m in self.models(enabled_only=True):
            if only_unverified and m["status"] == "verified":
                continue
            out.append(await self.probe(m["provider"], m["model_id"]))
        return out

    async def probe(self, provider: str, model_id: str) -> dict:
        a = self.get(provider)
        res = await a.probe(model_id)
        status = "verified" if res.ok else "failed"
        self.db.execute(
            "UPDATE models SET status=?, resolved_id=COALESCE(?, resolved_id), detail=?, verified_at=?, updated_at=? "
            "WHERE provider=? AND model_id=?",
            (status, res.resolved_id, res.detail, now() if res.ok else None, now(), provider, model_id))
        self._note_probe(provider, res.ok, res.error_kind, res.detail)
        self.forge.bus.emit("model.probe", f"Probe {provider}/{model_id}: {status} — {res.detail}",
                            level="info" if res.ok else "warn",
                            data={"provider": provider, "model": model_id, "ok": res.ok,
                                  "resolved_id": res.resolved_id, "error_kind": res.error_kind})
        return self.model(provider, model_id)

    def _note_probe(self, provider: str, ok: bool, kind: str | None, detail: str) -> None:
        row = self.db.one("SELECT data FROM provider_status WHERE provider=?", (provider,))
        if not row:
            return
        data = loads(row["data"], {})
        if ok:
            data["auth_state"] = "ok"
            data["last_probe_error"] = None
        elif kind == "auth":
            data["auth_state"] = "expired"
            data["last_probe_error"] = detail
        else:
            data["last_probe_error"] = detail
        self.db.execute("UPDATE provider_status SET data=? WHERE provider=?", (dumps(data), provider))

    def record_run_outcome(self, provider: str, model_id: str, ok: bool, error_kind: str | None,
                           resolved: str | None, detail: str = "") -> None:
        """A real run is stronger evidence than a probe."""
        if ok:
            self.db.execute(
                "UPDATE models SET status='verified', resolved_id=COALESCE(?, resolved_id), verified_at=?, "
                "detail=CASE WHEN status='verified' THEN detail ELSE 'verified by a successful run' END "
                "WHERE provider=? AND model_id=?", (resolved, now(), provider, model_id))
            self._note_probe(provider, True, None, "")
        elif error_kind in ("auth", "model_unavailable"):
            self.db.execute("UPDATE models SET status='failed', detail=? WHERE provider=? AND model_id=?",
                            (detail[:500], provider, model_id))
            self._note_probe(provider, False, error_kind, detail)

    # --- reads -----------------------------------------------------------------------
    def _hydrate_model(self, r: dict) -> dict:
        r["capabilities"] = loads(r["capabilities"], {})
        r["profile"] = loads(r["profile"], {})
        r["enabled"] = bool(r["enabled"])
        r["is_mock"] = r["provider"] == "mock"
        return r

    def model(self, provider: str, model_id: str) -> dict:
        r = self.db.one("SELECT * FROM models WHERE provider=? AND model_id=?", (provider, model_id))
        if not r:
            raise KeyError(f"{provider}/{model_id} unknown")
        return self._hydrate_model(r)

    def models(self, *, enabled_only: bool = False, provider: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM models WHERE 1=1", []
        if enabled_only:
            sql += " AND enabled=1"
        if provider:
            sql += " AND provider=?"
            params.append(provider)
        rows = [self._hydrate_model(r) for r in self.db.query(sql + " ORDER BY provider, model_id", params)]
        return [r for r in rows if r["provider"] in self.adapters]

    def update_model(self, provider: str, model_id: str, *, enabled: bool | None = None,
                     preference: float | None = None, profile: dict | None = None) -> dict:
        m = self.model(provider, model_id)
        prof = {**m["profile"], **(profile or {})}
        if profile and "tier" in profile:
            prof.setdefault("cost_index", TIER_COST.get(profile["tier"], 1.0))
            prof["source"] = "user"
        self.db.execute("UPDATE models SET enabled=?, preference=?, profile=?, updated_at=? WHERE provider=? AND model_id=?",
                        (int(enabled if enabled is not None else m["enabled"]),
                         preference if preference is not None else m["preference"], dumps(prof), now(),
                         provider, model_id))
        return self.model(provider, model_id)

    def add_configured_model(self, provider: str, model_id: str, display_name: str = "") -> dict:
        a = self.get(provider)
        caps = {"reasoning_levels": a.supported_efforts(None)}
        self.db.execute(
            "INSERT OR IGNORE INTO models(provider,model_id,display_name,source,status,capabilities,profile,enabled,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (provider, model_id, display_name or model_id, "configured", "unverified", dumps(caps),
             dumps(default_profile(provider, model_id, caps)), 1, now()))
        if provider == "claude_code":
            s = self.forge.settings()
            if model_id not in s.claude_models:
                self.forge.save_settings({"claude_models": [*s.claude_models, model_id]})
        return self.model(provider, model_id)

    def status(self, provider: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM provider_status WHERE provider=?", (provider,))
        data = loads(row["data"], {}) if row else {"provider": provider, "installed": False}
        data["checked_at"] = row["checked_at"] if row else None
        return data

    def statuses(self) -> list[dict]:
        out = []
        for name in self.adapters:
            st = self.status(name)
            st["models"] = self.models(provider=name)
            out.append(st)
        return out

    def available(self, provider: str) -> bool:
        st = self.status(provider)
        return bool(st.get("installed")) and st.get("auth_state") in ("ok", "unverified")
