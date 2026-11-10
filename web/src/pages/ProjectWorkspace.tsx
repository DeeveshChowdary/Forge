import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Badge, Card, Empty, ErrorBox, Field, MockBadge, RunBadge, Spinner, StateBadge, StructuredView, Tabs } from "../components/ui";
import { api, q, type Project, type Task } from "../lib/api";
import { ago, duration } from "../lib/format";
import { useApi, useLiveReload } from "../lib/hooks";

const TABS = ["overview", "tasks", "sessions", "files", "artifacts", "commits", "memory", "integrations", "settings"];

export default function ProjectWorkspace() {
  const { pid = "", tab = "overview" } = useParams();
  const nav = useNavigate();
  const { data: p, error, loading, reload } = useApi<Project>(`/api/projects/${pid}`, [pid]);
  useLiveReload(reload, (e) => e.project_id === p?.id && (e.type.startsWith("project.") || e.type === "task.state"));
  if (loading && !p) return <Spinner label="Loading project" />;
  if (!p) return <ErrorBox error={error ?? "Project not found"} onRetry={reload} />;

  const toggle = async () => {
    await api.post(`/api/projects/${p.id}/${p.status === "active" ? "pause" : "resume"}`);
    reload();
  };
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-2xl font-semibold">{p.name}</h1>
            {p.config.permissions.mode === "read_only" ? <Badge tone="blue">read-only</Badge> : <Badge>worktree write</Badge>}
            {p.status !== "active" ? <Badge tone="yellow">{p.status}</Badge> : <Badge tone="green">active</Badge>}
          </div>
          <p className="truncate font-mono text-xs muted">{p.root_path}</p>
        </div>
        <div className="flex gap-2">
          <button className="btn" onClick={toggle}>{p.status === "active" ? "Pause project" : "Resume project"}</button>
          <Link className="btn btn-primary" to={`/new?project=${p.id}`}>New task</Link>
        </div>
      </div>
      <Tabs tabs={TABS.map((t) => ({ key: t, label: t[0].toUpperCase() + t.slice(1) }))} active={tab}
        onChange={(k) => nav(`/projects/${p.id}/${k}`)} />
      {tab === "overview" && <Overview p={p} reload={reload} />}
      {tab === "tasks" && <TaskBoard p={p} />}
      {tab === "sessions" && <Sessions p={p} />}
      {tab === "files" && <Files p={p} />}
      {tab === "artifacts" && <Artifacts p={p} />}
      {tab === "commits" && <Commits p={p} />}
      {tab === "memory" && <Memory p={p} />}
      {tab === "integrations" && <Integrations p={p} />}
      {tab === "settings" && <ProjectSettings p={p} reload={reload} />}
    </div>
  );
}

function Overview({ p, reload }: { p: Project; reload: () => void }) {
  const [title, setTitle] = useState("");
  const addMilestone = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title) return;
    await api.post(`/api/projects/${p.id}/milestones`, { title });
    setTitle("");
    reload();
  };
  const trust = async (path: string, trusted: boolean) => {
    await api.post(`/api/projects/${p.id}/instructions/trust${q({ path, trusted })}`);
    reload();
  };
  return (
    <div className="grid gap-6 lg:grid-cols-3">
      <div className="space-y-6 lg:col-span-2">
        <Card title="Overview">
          <p className="text-sm">{p.description || <span className="muted">No description.</span>}</p>
          <dl className="mt-4 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            <div><dt className="text-xs muted">Origin</dt><dd>{p.origin.replace("_", " ")}</dd></div>
            <div><dt className="text-xs muted">Branch</dt><dd className="font-mono text-xs">{p.git?.branch ?? "—"}</dd></div>
            <div><dt className="text-xs muted">HEAD</dt><dd className="font-mono text-xs">{p.git?.head ?? "no commits"}</dd></div>
            <div><dt className="text-xs muted">Uncommitted</dt><dd>{p.git?.uncommitted ?? "—"}</dd></div>
            <div><dt className="text-xs muted">Test command</dt><dd className="font-mono text-xs">{p.config.test_command || "none"}</dd></div>
            <div><dt className="text-xs muted">Default routing</dt><dd>{p.config.default_routing_mode}</dd></div>
            <div><dt className="text-xs muted">Max review cycles</dt><dd>{p.config.limits.max_review_cycles}</dd></div>
            <div><dt className="text-xs muted">Known API budget</dt><dd>${p.config.limits.max_known_api_usd}</dd></div>
          </dl>
        </Card>
        <Card title="Goals & milestones">
          {!p.milestones?.length ? <Empty>No milestones yet. Tasks created “as milestone” and decomposed subtasks appear here.</Empty> : (
            <ol className="space-y-2">
              {p.milestones.map((m: any) => (
                <li key={m.id} className="flex items-center gap-2 text-sm">
                  <span className={`grid size-5 place-items-center rounded-full text-[10px] ${m.status === "done" ? "bg-emerald-500 text-white" : "border border-zinc-400"}`}>{m.status === "done" ? "✓" : ""}</span>
                  <span className={m.status === "done" ? "line-through muted" : ""}>{m.title}</span>
                  {m.task_id && <Link className="text-xs underline muted" to={`/tasks/${m.task_id}`}>task</Link>}
                </li>
              ))}
            </ol>
          )}
          <form onSubmit={addMilestone} className="mt-3 flex gap-2">
            <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Add a milestone" aria-label="New milestone" />
            <button className="btn">Add</button>
          </form>
        </Card>
      </div>
      <div className="space-y-6">
        <Card title="Repository instructions">
          {!p.instruction_files?.length ? <Empty>No AGENTS.md / CLAUDE.md found.</Empty> : (
            <ul className="space-y-2 text-sm">
              {p.instruction_files.map((f: any) => (
                <li key={f.path} className="flex items-center justify-between gap-2">
                  <span className="font-mono text-xs">{f.path}</span>
                  {f.trusted ? (
                    <span className="flex items-center gap-2"><Badge tone="green">trusted</Badge><button className="btn btn-sm" onClick={() => trust(f.path, false)}>Revoke</button></span>
                  ) : (
                    <span className="flex items-center gap-2"><Badge tone="yellow">untrusted</Badge><button className="btn btn-sm" onClick={() => trust(f.path, true)}>Trust</button></span>
                  )}
                </li>
              ))}
            </ul>
          )}
          <p className="mt-3 text-xs muted">Repository-provided instructions are treated as data until you trust them. Trust is pinned to the file's hash and revoked automatically if it changes. Note: the Claude/Codex CLIs may still read these files natively as context.</p>
        </Card>
        <Card title="Usage">
          {!p.usage?.per_provider?.length ? <Empty>No runs yet.</Empty> : (
            <ul className="space-y-1 text-sm">
              {p.usage.per_provider.map((u: any) => (
                <li key={u.provider} className="flex justify-between"><span>{u.provider}</span><span className="muted">{u.runs} runs · ${u.billed_usd.toFixed(2)} billed</span></li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </div>
  );
}

const COLUMNS: [string, string[]][] = [
  ["Queued", ["CREATED", "READY"]],
  ["In progress", ["PLANNING", "EXECUTING", "TESTING", "REVIEWING"]],
  ["Needs you", ["AWAITING_APPROVAL", "BLOCKED", "PAUSED"]],
  ["Done", ["COMPLETED"]],
  ["Failed / cancelled", ["FAILED", "CANCELLED"]],
];

function TaskBoard({ p }: { p: Project }) {
  const { data, error, reload } = useApi<Task[]>(`/api/tasks${q({ project_id: p.id })}`, [p.id]);
  useLiveReload(reload, (e) => e.project_id === p.id && e.type === "task.state");
  if (!data) return error ? <ErrorBox error={error} onRetry={reload} /> : <Spinner />;
  const title = Object.fromEntries(data.map((t) => [t.id, t.title]));
  return (
    <div className="grid gap-4 md:grid-cols-3 xl:grid-cols-5">
      {COLUMNS.map(([name, states]) => {
        const items = data.filter((t) => states.includes(t.state));
        return (
          <div key={name} className="rounded-xl bg-zinc-100/70 p-2 dark:bg-zinc-900/60">
            <div className="mb-2 flex justify-between px-1 text-xs font-semibold uppercase tracking-wide muted"><span>{name}</span><span>{items.length}</span></div>
            <div className="space-y-2">
              {items.map((t) => (
                <Link key={t.id} to={`/tasks/${t.id}`} className="card block p-3 hover:border-orange-400">
                  <div className="mb-1 flex items-center justify-between gap-2"><StateBadge state={t.state} /><span className="text-[11px] muted">{ago(t.updated_at)}</span></div>
                  <div className="text-sm font-medium">{t.title}</div>
                  <div className="mt-1 text-xs muted">{t.workflow.replaceAll("_", " ")}{t.phase && !t.phase.startsWith("__") ? ` · ${t.phase}` : ""}</div>
                  {t.depends_on.length > 0 && (
                    <div className="mt-1 text-[11px] muted">depends on: {t.depends_on.map((d) => title[d] ?? d).join(", ")}</div>
                  )}
                  {t.parent_id && <div className="text-[11px] muted">subtask of {title[t.parent_id] ?? t.parent_id}</div>}
                </Link>
              ))}
              {!items.length && <div className="p-3 text-center text-xs muted">—</div>}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function Sessions({ p }: { p: Project }) {
  const { data, error, reload } = useApi<any[]>(`/api/projects/${p.id}/runs`, [p.id]);
  useLiveReload(reload, (e) => e.project_id === p.id && e.type.startsWith("run."));
  if (!data) return error ? <ErrorBox error={error} /> : <Spinner />;
  return (
    <Card bodyClass="p-0">
      {!data.length ? <Empty>No agent sessions yet.</Empty> : (
        <div className="overflow-x-auto"><table className="tbl">
          <thead><tr><th>Task</th><th>Phase</th><th>Agent</th><th>State</th><th>Session</th><th>Duration</th><th>Started</th></tr></thead>
          <tbody>{data.map((r) => (
            <tr key={r.id}>
              <td><Link to={`/tasks/${r.task_id}`} className="hover:underline">{r.title}</Link></td>
              <td>{r.phase} <span className="muted">· {r.role}</span>{r.review_cycle ? <span className="muted"> · cycle {r.review_cycle}</span> : null}</td>
              <td className="font-mono text-xs">{r.provider ? `${r.provider}/${r.resolved_model ?? r.model}` : "test runner"} {r.is_mock && <MockBadge />}</td>
              <td><RunBadge state={r.state} /></td>
              <td className="font-mono text-[11px] muted">{r.session_id ? r.session_id.slice(0, 13) : "—"}</td>
              <td>{duration(r.ended_at ? r.ended_at - r.started_at : null)}</td>
              <td className="text-xs muted">{ago(r.started_at)}</td>
            </tr>))}</tbody>
        </table></div>
      )}
    </Card>
  );
}

function Files({ p }: { p: Project }) {
  const [dir, setDir] = useState("");
  const [file, setFile] = useState<string | null>(null);
  const { data, error } = useApi<any[]>(`/api/projects/${p.id}/files${q({ path: dir })}`, [p.id, dir]);
  const { data: content, error: fErr } = useApi<any>(file ? `/api/projects/${p.id}/file${q({ path: file })}` : null, [file]);
  const crumbs = dir ? dir.split("/") : [];
  return (
    <div className="grid gap-4 lg:grid-cols-[18rem_1fr]">
      <Card bodyClass="p-0">
        <div className="flex flex-wrap gap-1 border-b border-zinc-200 px-3 py-2 text-xs dark:border-zinc-800">
          <button className="underline" onClick={() => setDir("")}>root</button>
          {crumbs.map((c, i) => <span key={i}>/ <button className="underline" onClick={() => setDir(crumbs.slice(0, i + 1).join("/"))}>{c}</button></span>)}
        </div>
        <ErrorBox error={error} />
        <ul className="max-h-[32rem] overflow-y-auto text-sm">
          {data?.map((f) => (
            <li key={f.path}>
              <button className={`flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-zinc-50 dark:hover:bg-zinc-800 ${file === f.path ? "bg-orange-50 dark:bg-orange-950/40" : ""}`}
                onClick={() => (f.dir ? setDir(f.path) : setFile(f.path))}>
                <span aria-hidden>{f.dir ? "📁" : "📄"}</span><span className="truncate">{f.name}</span>
              </button>
            </li>
          ))}
        </ul>
      </Card>
      <Card title={file ?? "Select a file"} bodyClass="p-0">
        <ErrorBox error={fErr} />
        {content ? (content.binary ? <Empty>Binary file ({content.size} bytes)</Empty> :
          <pre className="code max-h-[36rem] rounded-none">{content.content}{content.truncated ? "\n… (truncated)" : ""}</pre>)
          : <Empty>Read-only file browser. Paths are confined to the project root.</Empty>}
      </Card>
    </div>
  );
}

function Artifacts({ p }: { p: Project }) {
  const { data, error } = useApi<any[]>(`/api/projects/${p.id}/artifacts`, [p.id]);
  if (!data) return error ? <ErrorBox error={error} /> : <Spinner />;
  if (!data.length) return <Card><Empty>No artifacts yet. Plans, handoffs, reviews, test reports and diffs appear here.</Empty></Card>;
  return (
    <div className="space-y-3">
      {data.map((a) => (
        <details key={a.id} className="card">
          <summary className="flex cursor-pointer flex-wrap items-center gap-2 px-4 py-2.5 text-sm">
            <Badge tone="violet">{a.kind}</Badge><span className="font-medium">{a.title}</span>
            <Link to={`/tasks/${a.task_id}`} className="text-xs underline muted">task</Link>
            <span className="ml-auto text-xs muted">{ago(a.created_at)}</span>
          </summary>
          <div className="border-t border-zinc-200 p-4 dark:border-zinc-800">
            {a.data ? <StructuredView kind={a.kind} data={a.data} /> : <pre className="code max-h-96 whitespace-pre-wrap">{a.content}</pre>}
          </div>
        </details>
      ))}
    </div>
  );
}

function Commits({ p }: { p: Project }) {
  const { data, error } = useApi<any[]>(`/api/projects/${p.id}/commits`, [p.id]);
  if (!data) return error ? <ErrorBox error={error} /> : <Spinner />;
  return (
    <Card bodyClass="p-0">
      {!data.length ? <Empty>No commits.</Empty> : (
        <ul className="divide-y divide-zinc-100 dark:divide-zinc-800">
          {data.map((c) => (
            <li key={c.sha} className="flex flex-wrap items-center gap-3 px-4 py-2 text-sm">
              <code className="text-xs text-orange-700 dark:text-orange-400">{c.short}</code>
              <span className="min-w-0 flex-1 truncate">{c.subject}</span>
              <span className="text-xs muted">{c.author} · {ago(c.time)}</span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

const MEM_KINDS = ["requirement", "decision", "hypothesis", "milestone", "failure", "test_result", "attempt", "problem", "user_decision", "note", "research"];

function Memory({ p }: { p: Project }) {
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const { data, error, reload } = useApi<any[]>(`/api/projects/${p.id}/memory${q({ q: query })}`, [p.id, query]);
  useLiveReload(reload, (e) => e.project_id === p.id && e.type === "memory.added");
  const [form, setForm] = useState({ kind: "requirement", title: "", content: "" });
  const [err, setErr] = useState<string | null>(null);
  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await api.post(`/api/projects/${p.id}/memory`, form);
      setForm({ ...form, title: "", content: "" });
      reload();
    } catch (e: any) { setErr(e.message); }
  };
  const del = async (id: string) => { await api.del(`/api/memory/${id}`); reload(); };
  return (
    <div className="grid gap-6 lg:grid-cols-3">
      <Card title="Project memory" className="lg:col-span-2" bodyClass="p-0">
        <form className="flex gap-2 border-b border-zinc-200 p-3 dark:border-zinc-800" onSubmit={(e) => { e.preventDefault(); setQuery(search); }}>
          <input className="input" placeholder="Search memory (relevance + recency retrieval, as agents see it)" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="Search memory" />
          <button className="btn">Search</button>
          {query && <button type="button" className="btn" onClick={() => { setSearch(""); setQuery(""); }}>Clear</button>}
        </form>
        <ErrorBox error={error} />
        {!data?.length ? <Empty>No memory entries.</Empty> : (
          <ul className="divide-y divide-zinc-100 dark:divide-zinc-800">
            {data.map((m) => (
              <li key={m.id} className="px-4 py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone="violet">{m.kind}</Badge><span className="text-sm font-medium">{m.title}</span>
                  {m.score != null && <span className="text-[11px] muted">score {m.score}</span>}
                  <span className="ml-auto text-xs muted">{ago(m.created_at)}</span>
                  <button className="btn btn-sm" onClick={() => del(m.id)} aria-label={`Delete memory ${m.title}`}>Delete</button>
                </div>
                <p className="mt-1 whitespace-pre-wrap text-sm muted">{m.content}</p>
                <div className="mt-1 text-[11px] muted">source: {m.source_task_id ? <Link className="underline" to={`/tasks/${m.source_task_id}`}>{m.source_task_id}</Link> : m.source}</div>
              </li>
            ))}
          </ul>
        )}
      </Card>
      <Card title="Add memory">
        <form onSubmit={add} className="space-y-3">
          <Field label="Kind"><select className="input" value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}>{MEM_KINDS.map((k) => <option key={k}>{k}</option>)}</select></Field>
          <Field label="Title"><input className="input" required value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} /></Field>
          <Field label="Content"><textarea className="input min-h-24" required value={form.content} onChange={(e) => setForm({ ...form, content: e.target.value })} /></Field>
          <ErrorBox error={err} />
          <button className="btn btn-primary w-full">Save to memory</button>
        </form>
      </Card>
    </div>
  );
}

function Integrations({ p }: { p: Project }) {
  const { data, error, reload } = useApi<any[]>(`/api/projects/${p.id}/integrations`, [p.id]);
  const [err, setErr] = useState<string | null>(null);
  const [zq, setZq] = useState("");
  const [zres, setZres] = useState<any>(null);
  if (!data) return error ? <ErrorBox error={error} /> : <Spinner />;
  const save = async (i: any, enabled: boolean, config = i.config) => {
    setErr(null);
    try { await api.put(`/api/projects/${p.id}/integrations/${i.type}`, { enabled, config }); reload(); }
    catch (e: any) { setErr(e.message); }
  };
  const zsearch = async () => {
    setErr(null);
    try { setZres(await api.post(`/api/projects/${p.id}/integrations/zotero/actions/search`, { q: zq })); }
    catch (e: any) { setErr(e.message); }
  };
  return (
    <div className="space-y-4">
      <ErrorBox error={err} />
      <div className="grid gap-4 md:grid-cols-2">
        {data.map((i) => (
          <Card key={i.type} title={<span className="flex items-center gap-2">{i.name}{i.enabled ? (i.status?.ok ? <Badge tone="green">ok</Badge> : <Badge tone="yellow">needs attention</Badge>) : <Badge>off</Badge>}</span>}
            actions={<button className="btn btn-sm" onClick={() => save(i, !i.enabled)}>{i.enabled ? "Disable" : "Enable"}</button>}>
            <p className="text-sm muted">{i.description}</p>
            {i.config_fields?.length > 0 && (
              <form className="mt-3 space-y-2" onSubmit={(e) => {
                e.preventDefault();
                const fd = new FormData(e.currentTarget);
                save(i, i.enabled, Object.fromEntries(i.config_fields.map((f: any) => [f.key, String(fd.get(f.key) ?? "")])));
              }}>
                {i.config_fields.map((f: any) => (
                  <Field key={f.key} label={f.label}>
                    {f.type === "select" ? (
                      <select name={f.key} className="input" defaultValue={i.config[f.key] ?? f.options[0]}>{f.options.map((o: string) => <option key={o}>{o}</option>)}</select>
                    ) : <input name={f.key} className="input font-mono" defaultValue={i.config[f.key] ?? f.default ?? ""} />}
                  </Field>
                ))}
                <button className="btn btn-sm">Save config</button>
              </form>
            )}
            {i.enabled && i.status && (
              <pre className="code mt-3 whitespace-pre-wrap">{Object.entries(i.status).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join("\n")}</pre>
            )}
            {i.type === "zotero" && i.enabled && (
              <div className="mt-3 space-y-2">
                <div className="flex gap-2"><input className="input" placeholder="Search Zotero (read-only)" value={zq} onChange={(e) => setZq(e.target.value)} aria-label="Zotero query" /><button className="btn" onClick={zsearch}>Search</button></div>
                {zres && <pre className="code max-h-60">{JSON.stringify(zres, null, 1)}</pre>}
              </div>
            )}
          </Card>
        ))}
      </div>
    </div>
  );
}

function ProjectSettings({ p, reload }: { p: Project; reload: () => void }) {
  const [cfg, setCfg] = useState<any>(p.config);
  const [instructions, setInstructions] = useState(p.instructions);
  const [description, setDescription] = useState(p.description);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const set = (path: string[], v: any) => {
    const next = structuredClone(cfg);
    let o = next;
    for (const k of path.slice(0, -1)) o = o[k];
    o[path[path.length - 1]] = v;
    setCfg(next);
  };
  const save = async () => {
    setErr(null); setMsg(null);
    try { await api.patch(`/api/projects/${p.id}`, { config: cfg, instructions, description }); setMsg("Saved."); reload(); }
    catch (e: any) { setErr(e.message); }
  };
  const remove = async () => {
    if (!confirm(`Unregister ${p.name}? Files on disk are NOT deleted.`)) return;
    try { await api.del(`/api/projects/${p.id}`); location.href = "/projects"; } catch (e: any) { setErr(e.message); }
  };
  const lim = cfg.limits, perm = cfg.permissions;
  const numField = (label: string, key: string, step = 1) => (
    <Field label={label}><input type="number" step={step} min={0} className="input" value={lim[key]} onChange={(e) => set(["limits", key], Number(e.target.value))} /></Field>
  );
  const check = (label: string, key: string, hint?: string) => (
    <label className="flex items-start gap-2 text-sm"><input type="checkbox" className="mt-1" checked={!!perm[key]} onChange={(e) => set(["permissions", key], e.target.checked)} /><span>{label}{hint && <span className="block text-xs muted">{hint}</span>}</span></label>
  );
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Card title="General">
        <div className="space-y-3">
          <Field label="Description"><input className="input" value={description} onChange={(e) => setDescription(e.target.value)} /></Field>
          <Field label="Project instructions (trusted; sent to every agent)"><textarea className="input min-h-28" value={instructions} onChange={(e) => setInstructions(e.target.value)} /></Field>
          <Field label="Test command" hint="Argv only (no shell). {python} = Forge's Python."><input className="input font-mono" value={cfg.test_command} onChange={(e) => set(["test_command"], e.target.value)} /></Field>
          <Field label="Default routing mode"><select className="input" value={cfg.default_routing_mode} onChange={(e) => set(["default_routing_mode"], e.target.value)}>{["balanced", "max_quality", "economy"].map((m) => <option key={m}>{m}</option>)}</select></Field>
          <Field label="Base branch for worktrees" hint="Empty = current HEAD"><input className="input font-mono" value={cfg.base_branch} onChange={(e) => set(["base_branch"], e.target.value)} /></Field>
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={cfg.share_memory} onChange={(e) => set(["share_memory"], e.target.checked)} />Read shared memory from other projects</label>
        </div>
      </Card>
      <Card title="Permissions">
        <div className="space-y-3">
          <Field label="Mode"><select className="input" value={perm.mode} onChange={(e) => set(["permissions", "mode"], e.target.value)}><option value="worktree_write">worktree write (agents edit isolated worktrees)</option><option value="read_only">read-only</option></select></Field>
          {check("Agents may run shell commands (scoped allowlist)", "allow_agent_shell")}
          {check("Agents may use network tools (web search/fetch)", "allow_network_tools")}
          {check("Allow directly billed API usage", "allow_paid_api", "Also requires the global switch in Settings. Off = subscription CLIs only.")}
          {check("Auto-merge approved results without asking", "auto_merge", "Only effective if 'merge' is removed from approval requirements.")}
          <div className="text-xs muted">Always requires approval: {perm.require_approval_for.join(", ")}</div>
        </div>
      </Card>
      <Card title="Execution limits">
        <div className="grid grid-cols-2 gap-3">
          {numField("Max runtime per agent run (s)", "max_runtime_s")}
          {numField("Max task runtime (s)", "max_task_runtime_s")}
          {numField("Max agent turns/steps per run", "max_turns")}
          {numField("Max review/fix cycles", "max_review_cycles")}
          {numField("Max retries per phase", "max_retries")}
          {numField("Max concurrent tasks", "max_concurrency")}
          {numField("Known API spend limit (USD)", "max_known_api_usd", 0.5)}
          {numField("Test timeout (s)", "test_timeout_s")}
        </div>
      </Card>
      <Card title="Save">
        <div className="flex flex-wrap items-center gap-2">
          <button className="btn btn-primary" onClick={save}>Save settings</button>
          <button className="btn btn-danger" onClick={remove}>Unregister project</button>
          {msg && <span className="text-sm text-emerald-600">{msg}</span>}
        </div>
        <div className="mt-3"><ErrorBox error={err} /></div>
      </Card>
    </div>
  );
}
