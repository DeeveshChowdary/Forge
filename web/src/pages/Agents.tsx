import { useState } from "react";
import { AuthBadge, Badge, Card, Empty, ErrorBox, Field, MockBadge, ModelStatus, Spinner } from "../components/ui";
import { api, q } from "../lib/api";
import { ago, duration } from "../lib/format";
import { useApi, useLiveReload } from "../lib/hooks";

export default function Agents() {
  const { data, error, loading, reload } = useApi<any>("/api/providers");
  const { data: settings, reload: reloadSettings } = useApi<any>("/api/settings");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [newModel, setNewModel] = useState({ provider: "claude_code", model_id: "" });
  useLiveReload(reload, (e) => e.type === "model.probe" || e.type === "providers.refreshed");

  const run = async (key: string, fn: () => Promise<any>) => {
    setBusy(key); setErr(null);
    try { await fn(); await reload(); } catch (e: any) { setErr(e.message); } finally { setBusy(null); }
  };
  const patchModel = (m: any, body: any) => run(`patch-${m.provider}-${m.model_id}`, () => api.patch(`/api/models${q({ provider: m.provider, model_id: m.model_id })}`, body));
  const saveSetting = (patch: any) => run("settings", async () => { await api.put("/api/settings", patch); reloadSettings(); });

  if (loading && !data) return <Spinner label="Detecting providers" />;
  const s = settings?.settings;
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">Agent control center</h1>
        <div className="flex gap-2">
          <button className="btn" disabled={!!busy} onClick={() => run("refresh", () => api.post("/api/providers/refresh"))}>{busy === "refresh" ? "Refreshing…" : "Re-detect providers"}</button>
          <button className="btn btn-primary" disabled={!!busy} onClick={() => run("probe-all", () => api.post("/api/models/probe-all?only_unverified=true"))}
            title="Sends one tiny prompt per unverified enabled model using your existing CLI logins">{busy === "probe-all" ? "Probing…" : "Verify unverified models"}</button>
        </div>
      </div>
      <ErrorBox error={error ?? err} onRetry={reload} />
      <p className="text-sm muted">Models are only routable once <b>verified</b> by a probe or a successful run. Discovered = listed by the provider's own catalog; configured = an entry you supplied (verified by probing). Nothing is assumed available.</p>

      {data?.providers.map((p: any) => (
        <Card key={p.provider} title={<span className="flex flex-wrap items-center gap-2">{p.display_name}{p.is_mock && <MockBadge />}
          {p.installed ? <AuthBadge state={p.auth_state} /> : <Badge tone="red">not installed</Badge>}<Badge tone={p.billing === "api" ? "orange" : "zinc"}>billing: {p.billing}</Badge></span>}
          bodyClass="p-0">
          <div className="grid gap-4 border-b border-zinc-200 p-4 text-sm md:grid-cols-2 dark:border-zinc-800">
            <dl className="space-y-1">
              <div><dt className="inline muted">Version: </dt><dd className="inline font-mono text-xs">{p.version ?? "—"}</dd></div>
              <div><dt className="inline muted">Path: </dt><dd className="inline break-all font-mono text-xs">{p.path ?? "—"}</dd></div>
              <div><dt className="inline muted">Auth: </dt><dd className="inline">{p.auth_detail || "—"}</dd></div>
              <div><dt className="inline muted">Checked: </dt><dd className="inline">{ago(p.checked_at)}</dd></div>
              {p.last_probe_error && <div className="text-red-600 dark:text-red-400">Last probe error: {p.last_probe_error}</div>}
              {p.auth_state === "expired" && <div className="rounded bg-yellow-50 p-2 text-xs dark:bg-yellow-950/40">Credentials expired. Re-authenticate in a terminal yourself (for Claude: run <code>claude</code> then <code>/login</code>); Forge never modifies logins. Then click “Verify unverified models”.</div>}
              {p.notes?.map((n: string, i: number) => <div key={i} className="text-xs muted">{n}</div>)}
            </dl>
            <div>
              <div className="mb-1 text-xs font-medium muted">Capabilities</div>
              <ul className="space-y-0.5 text-xs">{Object.entries(p.capabilities ?? {}).map(([k, v]) => <li key={k}><span className="muted">{k}:</span> {Array.isArray(v) ? v.join(", ") : String(v)}</li>)}</ul>
            </div>
          </div>
          {!p.models.length ? <Empty>No models.</Empty> : (
            <div className="overflow-x-auto"><table className="tbl">
              <thead><tr><th>Model</th><th>Source</th><th>Status</th><th>Exact id</th><th>Tier</th><th>Efforts</th><th>Preference</th><th>Enabled</th><th /></tr></thead>
              <tbody>{p.models.map((m: any) => (
                <tr key={m.model_id} className={m.enabled ? "" : "opacity-60"}>
                  <td><div className="font-mono text-xs">{m.model_id}</div><div className="text-[11px] muted">{m.display_name}</div></td>
                  <td className="text-xs">{m.source}</td>
                  <td><ModelStatus status={m.status} />{m.detail && <div className="mt-1 max-w-[16rem] text-[11px] muted">{m.detail.slice(0, 140)}</div>}</td>
                  <td className="font-mono text-[11px]">{m.resolved_id ?? "—"}</td>
                  <td>
                    <select className="input py-1 text-xs" value={m.profile.tier} aria-label={`Tier for ${m.model_id}`} onChange={(e) => patchModel(m, { profile: { tier: e.target.value, cost_index: { fast: 0.4, standard: 1, strong: 2.5 }[e.target.value as "fast"] } })}>
                      {["fast", "standard", "strong"].map((t) => <option key={t}>{t}</option>)}
                    </select>
                    <div className="mt-0.5 text-[10px] muted" title={m.profile.source}>{m.profile.source === "user" ? "set by you" : "default guess"}</div>
                  </td>
                  <td className="text-[11px]">{(m.capabilities.reasoning_levels ?? []).join(", ") || "—"}</td>
                  <td><input type="range" min={-1} max={1} step={0.25} value={m.preference} aria-label={`Preference for ${m.model_id}`}
                    onChange={(e) => patchModel(m, { preference: Number(e.target.value) })} /><span className="ml-1 text-xs">{m.preference}</span></td>
                  <td><input type="checkbox" checked={m.enabled} aria-label={`Enable ${m.model_id}`} onChange={(e) => patchModel(m, { enabled: e.target.checked })} /></td>
                  <td><button className="btn btn-sm" disabled={!!busy} onClick={() => run(`probe-${m.model_id}`, () => api.post(`/api/models/probe${q({ provider: p.provider, model_id: m.model_id })}`))}>
                    {busy === `probe-${m.model_id}` ? "…" : "Probe"}</button></td>
                </tr>))}</tbody>
            </table></div>
          )}
        </Card>
      ))}

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Router configuration">
          {!s ? <Spinner /> : (
            <div className="space-y-3">
              <Field label="Default routing mode (new projects inherit their own default)">
                <select className="input" value={s.routing_mode} onChange={(e) => saveSetting({ routing_mode: e.target.value })}>
                  {["balanced", "max_quality", "economy"].map((m) => <option key={m}>{m}</option>)}
                </select>
              </Field>
              <Field label="Max global concurrent tasks"><input type="number" min={1} max={10} className="input" value={s.max_global_concurrency} onChange={(e) => saveSetting({ max_global_concurrency: Number(e.target.value) })} /></Field>
              <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={s.allow_unverified_models} onChange={(e) => saveSetting({ allow_unverified_models: e.target.checked })} />Allow routing to unverified models</label>
              <p className="text-xs muted">Policy: <b>heuristic-v1</b> — explainable scoring of tier fit, quality, cost, latency and recorded success (with sample size), independence, and your preferences. The interface supports swapping in a learned (e.g. contextual-bandit) policy later; no performance data is assumed before it is recorded.</p>
            </div>
          )}
        </Card>
        <Card title="Add a configured model">
          <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); run("add", () => api.post("/api/models", newModel)).then(() => setNewModel({ ...newModel, model_id: "" })); }}>
            <Field label="Provider"><select className="input" value={newModel.provider} onChange={(e) => setNewModel({ ...newModel, provider: e.target.value })}>{data?.providers.map((p: any) => <option key={p.provider} value={p.provider}>{p.display_name}</option>)}</select></Field>
            <Field label="Model id or alias" hint="Added as 'unverified'; probe it before routing uses it."><input className="input font-mono" required value={newModel.model_id} onChange={(e) => setNewModel({ ...newModel, model_id: e.target.value })} placeholder="e.g. claude-opus-5-5" /></Field>
            <button className="btn">Add model</button>
          </form>
        </Card>
      </div>

      <Card title="Per-model performance history (recorded by Forge)" bodyClass="p-0">
        {!data?.stats.length ? <Empty>No runs recorded yet — routing uses priors only until runs accumulate.</Empty> : (
          <div className="overflow-x-auto"><table className="tbl">
            <thead><tr><th>Provider / model</th><th>Role</th><th>Runs</th><th>Success</th><th>Avg duration</th></tr></thead>
            <tbody>{data.stats.map((r: any, i: number) => (
              <tr key={i}><td className="font-mono text-xs">{r.provider}/{r.model} {r.provider === "mock" && <MockBadge />}</td><td>{r.role}</td><td>{r.n}</td>
                <td>{r.n ? `${Math.round((100 * r.ok) / r.n)}%` : "—"}</td><td>{duration(r.avg_s)}</td></tr>))}</tbody>
          </table></div>
        )}
      </Card>
    </div>
  );
}
