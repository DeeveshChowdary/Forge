import { useState } from "react";
import { Badge, Card, ErrorBox, Field, MockBadge, Spinner } from "../components/ui";
import { api } from "../lib/api";
import { useApi } from "../lib/hooks";

export default function Settings() {
  const { data, error, reload } = useApi<any>("/api/settings");
  const { data: health } = useApi<any>("/api/health");
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [claudeModels, setClaudeModels] = useState<string | null>(null);
  if (!data) return error ? <ErrorBox error={error} onRetry={reload} /> : <Spinner />;
  const s = data.settings;
  const save = async (patch: any, confirmText?: string) => {
    if (confirmText && !confirm(confirmText)) return;
    setErr(null); setMsg(null);
    try { await api.put("/api/settings", patch); setMsg("Saved"); reload(); } catch (e: any) { setErr(e.message); }
  };
  const lim = s.default_limits;
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Settings</h1>
      <ErrorBox error={err} />{msg && <div className="text-sm text-emerald-600">{msg}</div>}
      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Providers">
          <div className="space-y-3">
            <Field label="Claude model entries (comma-separated aliases or full ids)" hint="Claude Code has no model-listing command; these are probed to confirm availability and resolve exact ids.">
              <div className="flex gap-2">
                <input className="input font-mono" value={claudeModels ?? s.claude_models.join(", ")} onChange={(e) => setClaudeModels(e.target.value)} />
                <button className="btn" onClick={() => save({ claude_models: (claudeModels ?? "").split(",").map((x) => x.trim()).filter(Boolean) })}>Save</button>
              </div>
            </Field>
            <Field label="Codex binary override" hint="Empty = auto-detect (PATH, then editor extension bundles)">
              <input className="input font-mono" defaultValue={s.codex_path} onBlur={(e) => e.target.value !== s.codex_path && save({ codex_path: e.target.value })} />
            </Field>
            <Field label="Claude binary override"><input className="input font-mono" defaultValue={s.claude_path} onBlur={(e) => e.target.value !== s.claude_path && save({ claude_path: e.target.value })} /></Field>
            <label className="flex items-start gap-2 text-sm"><input type="checkbox" className="mt-1" checked={s.codex_ignore_user_config} onChange={(e) => save({ codex_ignore_user_config: e.target.checked })} />
              <span>Run Codex with <code>--ignore-user-config</code><span className="block text-xs muted">Reproducible, isolated runs; authentication still comes from your Codex login.</span></span></label>
            <label className="flex items-start gap-2 text-sm"><input type="checkbox" className="mt-1" checked={s.enable_mock_provider} onChange={(e) => save({ enable_mock_provider: e.target.checked })} />
              <span>Enable the deterministic mock provider <MockBadge /><span className="block text-xs muted">For demos and tests. All mock activity is labelled MOCK everywhere.</span></span></label>
          </div>
        </Card>
        <Card title="Spending & paid APIs">
          <div className="space-y-3 text-sm">
            <p>Forge runs agents through your existing subscription CLIs. Child processes never inherit <code>ANTHROPIC_API_KEY</code> / <code>OPENAI_API_KEY</code>, so a CLI cannot silently fall back to billed API usage.</p>
            <label className="flex items-start gap-2"><input type="checkbox" className="mt-1" checked={s.paid_api_globally_allowed}
              onChange={(e) => save({ paid_api_globally_allowed: e.target.checked }, e.target.checked ? "Allow providers that bill per API call? Each project must also opt in, and the known-spend limit still applies." : undefined)} />
              <span>Allow directly billed API providers globally {s.paid_api_globally_allowed ? <Badge tone="orange">on</Badge> : <Badge tone="green">off</Badge>}<span className="block text-xs muted">No paid-API adapter ships in this version; this switch gates any future one.</span></span></label>
          </div>
        </Card>
        <Card title="Default execution limits (new projects)">
          <div className="grid grid-cols-2 gap-3">
            {Object.entries(lim).map(([k, v]) => (
              <Field key={k} label={k.replaceAll("_", " ")}>
                <input type="number" min={0} step={k.includes("usd") ? 0.5 : 1} className="input" defaultValue={v as number}
                  onBlur={(e) => Number(e.target.value) !== v && save({ default_limits: { ...lim, [k]: Number(e.target.value) } })} />
              </Field>
            ))}
          </div>
          <p className="mt-2 text-xs muted">Applied to projects added from now on. Existing projects keep their own limits (Project → Settings).</p>
        </Card>
        <Card title="System">
          <dl className="space-y-1 text-sm">
            <div><dt className="inline muted">Version: </dt><dd className="inline">{health?.version}</dd></div>
            <div><dt className="inline muted">Data directory: </dt><dd className="inline font-mono text-xs">{health?.home}</dd></div>
            <div><dt className="inline muted">Engine: </dt><dd className="inline">{health?.engine?.alive ? `running (pid ${health.engine.pid})` : "not running"}</dd></div>
          </dl>
          <h3 className="mb-1 mt-4 text-sm font-medium">Workflows</h3>
          <ul className="space-y-1 text-xs">{data.workflows.map((w: any) => <li key={w.key}><b>{w.name}</b> <span className="muted">— {w.phases.map((p: any) => p.key).join(" → ")}</span>{w.read_only_ok && <Badge tone="blue">read-only ok</Badge>}</li>)}</ul>
        </Card>
      </div>
    </div>
  );
}
