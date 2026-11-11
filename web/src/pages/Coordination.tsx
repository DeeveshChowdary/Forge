import { useState } from "react";
import { Link } from "react-router-dom";
import { ApprovalCard } from "../components/shared";
import { Badge, Card, Empty, ErrorBox, Field, Spinner, StateBadge } from "../components/ui";
import { api, type Approval, type Project } from "../lib/api";
import { ago, clock } from "../lib/format";
import { useApi, useLiveReload } from "../lib/hooks";

const LINK_TONE: Record<string, string> = {
  discovered: "zinc", validated: "blue", awaiting_authorization: "yellow", queued: "blue",
  running: "orange", completed: "green", rejected: "red", failed: "red",
};

export function LinkStatus({ status }: { status: string }) {
  return <Badge tone={LINK_TONE[status] ?? "zinc"}>{status.replace("_", " ")}</Badge>;
}

export default function Coordination() {
  const { data: st, error, loading, reload } = useApi<any>("/api/coordination/status");
  const { data: links, reload: reloadLinks } = useApi<any[]>("/api/coordination/links");
  const { data: activity, reload: reloadAct } = useApi<any[]>("/api/coordination/activity?limit=60");
  const { data: approvals, reload: reloadAp } = useApi<Approval[]>("/api/approvals?status=pending");
  const { data: projects } = useApi<Project[]>("/api/projects");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [syncResult, setSyncResult] = useState<any>(null);
  const [newToken, setNewToken] = useState<any>(null);
  const reloadAll = () => { reload(); reloadLinks(); reloadAct(); reloadAp(); };
  useLiveReload(reloadAll, (e) => e.type.startsWith("coordination.") || e.type.startsWith("approval.") || e.type === "task.state");

  if (loading && !st) return <Spinner label="Loading coordination" />;
  if (!st) return <ErrorBox error={error} onRetry={reload} />;
  const cfg = st.config;
  const s = st.state ?? {};
  const external = (approvals ?? []).filter((a) => a.kind === "external_task");

  const save = async (patch: any) => {
    setErr(null);
    try { await api.put("/api/coordination/bridge", patch); reload(); } catch (e: any) { setErr(e.message); }
  };
  const saveRepo = (name: string, patch: any) => save({ repos: { ...cfg.repos, [name]: { ...cfg.repos[name], ...patch } } });
  const syncNow = async () => {
    setBusy(true); setErr(null);
    try { setSyncResult(await api.post("/api/coordination/sync")); reloadAll(); } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  };
  const createToken = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    const scopes = ["read", ...(fd.get("tasks") ? ["tasks:write"] : []), ...(fd.get("feedback") ? ["feedback:write"] : [])];
    try {
      setNewToken(await api.post("/api/coordination/tokens", { name: fd.get("name"), scopes, projects: fd.getAll("projects") }));
      reload();
    } catch (e: any) { setErr(e.message); }
  };
  const workerAlive = st.worker?.alive;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Coordination</h1>
          <p className="text-sm muted">External assistants (e.g. ChatGPT) submit work through GitHub issues or the token-scoped API; Forge executes it with its normal engine.</p>
        </div>
        <div className="flex gap-2">
          <button className="btn" disabled={busy || !cfg.enabled} onClick={syncNow}>{busy ? "Syncing…" : "Sync now"}</button>
          <button className={`btn ${cfg.enabled ? "btn-danger" : "btn-primary"}`} onClick={() => save({ enabled: !cfg.enabled })}>
            {cfg.enabled ? "Disable bridge" : "Enable bridge"}
          </button>
        </div>
      </div>
      <ErrorBox error={err} />

      <div className="grid gap-4 md:grid-cols-4">
        <Card title="GitHub bridge">
          <div className="space-y-1 text-sm">
            <div>{cfg.enabled ? <Badge tone="green">enabled</Badge> : <Badge>disabled</Badge>}{" "}
              {s.last_error ? <Badge tone="red">error</Badge> : s.last_success_at ? <Badge tone="green">last sync ok</Badge> : <Badge>never synced</Badge>}</div>
            <div className="muted">Worker: {st.worker ? (workerAlive ? `active (pid ${st.worker.pid})` : "stale") : "not running"}</div>
            <div className="muted">Token: {st.token_source}</div>
            {s.viewer && <div className="muted">Acting as <span className="font-mono">{s.viewer}</span></div>}
          </div>
        </Card>
        <Card title="Last synchronization">
          <div className="space-y-1 text-sm">
            <div>{s.last_sync_at ? `${ago(s.last_sync_at)} (${clock(s.last_sync_at)})` : "—"}</div>
            <div className="muted">Last success: {s.last_success_at ? ago(s.last_success_at) : "—"}</div>
            {s.backoff_until && s.backoff_until * 1000 > Date.now() && <div className="text-yellow-700 dark:text-yellow-400">Retrying after {clock(s.backoff_until)} (failures: {s.consecutive_failures ?? 0})</div>}
            {s.rate?.remaining != null && <div className="muted">GitHub rate remaining: {s.rate.remaining}/{s.rate.limit}</div>}
          </div>
        </Card>
        <Card title="Issues">
          <div className="flex flex-wrap gap-1">
            {Object.entries(st.link_counts ?? {}).map(([k, v]) => <span key={k}><LinkStatus status={k} /> <span className="text-xs">{String(v)}</span></span>)}
            {!Object.keys(st.link_counts ?? {}).length && <span className="text-sm muted">none yet</span>}
          </div>
          <div className="mt-2 text-xs muted">Outbox: {Object.entries(st.outbox ?? {}).map(([k, v]) => `${k} ${v}`).join(", ") || "empty"}</div>
        </Card>
        <Card title="Last error">
          <div className="text-sm">{s.last_error ? <span className="text-red-600 dark:text-red-400">{s.last_error_kind}: {s.last_error}</span> : <span className="muted">none</span>}</div>
          {syncResult && <pre className="code mt-2 max-h-32 text-[11px]">{JSON.stringify(syncResult, null, 1)}</pre>}
        </Card>
      </div>

      {external.length > 0 && (
        <Card title={`Awaiting your authorization (${external.length})`}>
          <div className="space-y-3">{external.map((a) => <ApprovalCard key={a.id} a={a} onDone={reloadAll} />)}</div>
        </Card>
      )}

      <Card title="Linked GitHub issues" bodyClass="p-0">
        {!links?.length ? <Empty>No issues processed yet.</Empty> : (
          <div className="overflow-x-auto"><table className="tbl">
            <thead><tr><th>Issue</th><th>Author</th><th>Bridge status</th><th>Forge task</th><th>Last published</th><th>Errors / retries</th></tr></thead>
            <tbody>{links.map((l) => (
              <tr key={l.id}>
                <td><a className="underline" href={l.issue_url} target="_blank" rel="noreferrer">{l.repo}#{l.issue_number}</a><div className="max-w-xs truncate text-xs muted">{l.title}</div></td>
                <td className="font-mono text-xs">{l.author}</td>
                <td><LinkStatus status={l.status} />{l.reason && <div className="mt-1 max-w-xs text-[11px] muted">{l.reason}</div>}</td>
                <td>{l.task_id ? <Link className="underline" to={`/tasks/${l.task_id}`}>{l.task_id}</Link> : "—"}{l.task_state && <div className="mt-1"><StateBadge state={l.task_state} /></div>}</td>
                <td className="text-xs">{l.last_published_at ? ago(l.last_published_at) : "—"}</td>
                <td className="text-xs">{l.error ? <span className="text-red-600">{l.error} (retry {l.retries})</span> : <span className="muted">—</span>}</td>
              </tr>))}</tbody>
          </table></div>
        )}
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Bridge configuration">
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-3">
              <Field label="Poll interval (seconds, ≥30)"><input type="number" min={30} className="input" defaultValue={cfg.poll_interval_s} onBlur={(e) => Number(e.target.value) !== cfg.poll_interval_s && save({ poll_interval_s: Number(e.target.value) })} /></Field>
              <Field label="GitHub token source">
                <select className="input" value={cfg.token_source} onChange={(e) => save({ token_source: e.target.value })}>
                  <option value="auto">auto (env → gh → keychain)</option><option value="env">FORGE_GITHUB_TOKEN</option><option value="gh">GitHub CLI</option><option value="keychain">macOS keychain</option>
                </select>
              </Field>
            </div>
            <h3 className="text-sm font-medium">Authorized repositories</h3>
            {Object.entries(cfg.repos).map(([name, rc]: [string, any]) => (
              <div key={name} className="space-y-2 rounded-lg border border-zinc-200 p-3 dark:border-zinc-800">
                <div className="flex items-center justify-between"><span className="font-mono text-sm">{name}</span>
                  <label className="flex items-center gap-1 text-xs"><input type="checkbox" checked={rc.enabled} onChange={(e) => saveRepo(name, { enabled: e.target.checked })} />enabled</label></div>
                {s.repo_checks?.[name] && <div className="text-xs muted">visibility {s.repo_checks[name].visibility} · can comment {String(s.repo_checks[name].can_comment)}{s.repo_checks[name].error ? ` · ${s.repo_checks[name].error}` : ""}</div>}
                <Field label="Authorized issue authors (comma-separated GitHub logins)">
                  <input className="input font-mono" defaultValue={rc.authorized_users.join(", ")} onBlur={(e) => saveRepo(name, { authorized_users: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) })} />
                </Field>
                <Field label="Forge projects this repository may target">
                  <select multiple className="input h-24" value={rc.projects} onChange={(e) => saveRepo(name, { projects: [...e.target.selectedOptions].map((o) => o.value) })}>
                    {(projects ?? []).map((p) => <option key={p.id} value={p.name}>{p.name}</option>)}
                  </select>
                </Field>
                <div className="grid grid-cols-2 gap-2">
                  <Field label="Required issue label"><input className="input font-mono" defaultValue={rc.label} onBlur={(e) => e.target.value && saveRepo(name, { label: e.target.value })} /></Field>
                  <div className="space-y-1 pt-5 text-xs">
                    <label className="flex items-center gap-1"><input type="checkbox" checked={!rc.auto_queue} onChange={(e) => saveRepo(name, { auto_queue: !e.target.checked })} />Require my authorization per task</label>
                    <label className="flex items-center gap-1"><input type="checkbox" checked={rc.require_private} onChange={(e) => saveRepo(name, { require_private: e.target.checked })} />Refuse if repository is public</label>
                  </div>
                </div>
              </div>
            ))}
            <p className="text-xs muted">Only issues with the label, written by an authorized author who is also a repository collaborator, and containing one <code>forge-task</code> JSON block are accepted. Comments act only via <code>/forge revise|feedback|status</code>. Merges, spending and other approvals are never possible from GitHub.</p>
          </div>
        </Card>

        <Card title="API tokens (coordination API / MCP)">
          <ul className="mb-3 space-y-1 text-sm">
            {st.tokens.map((t: any) => (
              <li key={t.id} className="flex flex-wrap items-center justify-between gap-2">
                <span><span className="font-medium">{t.name}</span> <span className="font-mono text-xs muted">{t.prefix}…</span> {t.scopes.map((x: string) => <Badge key={x}>{x}</Badge>)}{t.revoked_at && <Badge tone="red">revoked</Badge>}</span>
                <span className="text-xs muted">{t.last_used_at ? `used ${ago(t.last_used_at)}` : "never used"} {!t.revoked_at && <button className="btn btn-sm btn-danger" onClick={async () => { await api.del(`/api/coordination/tokens/${t.id}`); reload(); }}>Revoke</button>}</span>
              </li>))}
            {!st.tokens.length && <li className="muted">No tokens.</li>}
          </ul>
          <form onSubmit={createToken} className="space-y-2">
            <Field label="Name"><input name="name" required className="input" placeholder="chatgpt" /></Field>
            <Field label="Projects"><select name="projects" multiple required className="input h-20">{(projects ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></Field>
            <div className="flex gap-4 text-sm"><label className="flex items-center gap-1"><input type="checkbox" checked readOnly />read</label>
              <label className="flex items-center gap-1"><input type="checkbox" name="tasks" />tasks:write</label>
              <label className="flex items-center gap-1"><input type="checkbox" name="feedback" />feedback:write</label></div>
            <button className="btn btn-primary">Create token</button>
          </form>
          {newToken && <div className="mt-3 rounded-lg border border-yellow-300 bg-yellow-50 p-2 text-xs dark:bg-yellow-950/40"><div className="mb-1 font-medium">Copy now — shown only once:</div><code className="break-all">{newToken.token}</code></div>}
          <label className="mt-3 flex items-center gap-2 text-sm"><input type="checkbox" checked={st.api_settings.require_authorization}
            onChange={async (e) => { await api.put("/api/coordination/settings", { require_authorization: e.target.checked }); reload(); }} />API/MCP-submitted tasks wait for my authorization</label>
        </Card>
      </div>

      <Card title="External assistant activity" bodyClass="p-0">
        {!activity?.length ? <Empty>No activity yet.</Empty> : (
          <div className="max-h-96 overflow-y-auto font-mono text-xs">{activity.map((e: any) => (
            <div key={e.id} className="flex gap-3 border-b border-zinc-100 px-4 py-1 dark:border-zinc-800/60">
              <span className="shrink-0 muted">{clock(e.ts)}</span><span className="w-52 shrink-0 truncate muted">{e.type}</span>
              <span className={e.level === "warn" || e.level === "error" ? "text-yellow-700 dark:text-yellow-400" : ""}>{e.task_id ? <Link className="underline" to={`/tasks/${e.task_id}`}>{e.message}</Link> : e.message}</span>
            </div>))}</div>
        )}
      </Card>
    </div>
  );
}
