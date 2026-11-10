import { Link } from "react-router-dom";
import { ApprovalCard, TaskList } from "../components/shared";
import { AuthBadge, Badge, Card, Elapsed, Empty, ErrorBox, MockBadge, Spinner } from "../components/ui";
import { num } from "../lib/format";
import { useApi, useLiveReload } from "../lib/hooks";

export default function Dashboard() {
  const { data, error, loading, reload } = useApi<any>("/api/overview");
  useLiveReload(reload, (e) => e.type.startsWith("task.") || e.type.startsWith("run.") || e.type.startsWith("approval.")
    || e.type.startsWith("project.") || e.type.startsWith("providers."));

  if (loading && !data) return <Spinner label="Loading dashboard" />;
  if (error && !data) return <ErrorBox error={error} onRetry={reload} />;
  const d = data!;
  const noProviders = !d.providers.some((p: any) => p.installed && !p.is_mock);
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Dashboard</h1>
          <p className="text-sm muted">{d.projects.length} project(s) · {d.active.length} active task(s) · {d.queued.length} waiting</p>
        </div>
        <div className="flex gap-2">
          <Link to="/projects" className="btn">Add project</Link>
          <Link to="/new" className="btn btn-primary">New task</Link>
        </div>
      </div>
      <ErrorBox error={error} onRetry={reload} />
      {noProviders && <ErrorBox error="No real agent provider is installed. Install Claude Code or Codex CLI, then refresh on the Agent control page." />}

      {d.approvals.length > 0 && (
        <Card title={`Approval requests (${d.approvals.length})`}>
          <div className="space-y-3">{d.approvals.slice(0, 4).map((a: any) => <ApprovalCard key={a.id} a={a} onDone={reload} />)}</div>
        </Card>
      )}

      <div className="grid gap-6 lg:grid-cols-3">
        <Card title="Active agents" className="lg:col-span-2" bodyClass="">
          {d.running_runs.length === 0 ? <Empty>No agent is running right now.</Empty> : (
            <table className="tbl">
              <thead><tr><th>Task</th><th>Phase</th><th>Agent / model</th><th>Elapsed</th></tr></thead>
              <tbody>
                {d.running_runs.map((r: any) => (
                  <tr key={r.id}>
                    <td><Link className="font-medium hover:underline" to={`/tasks/${r.task_id}`}>{r.title}</Link></td>
                    <td>{r.phase} <span className="muted">({r.role})</span></td>
                    <td className="font-mono text-xs">{r.provider ? `${r.provider}/${r.model}` : "test runner"}{r.effort ? ` · ${r.effort}` : ""}{" "}
                      {r.provider === "mock" && <MockBadge />}</td>
                    <td><Elapsed from={r.started_at} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
        <Card title="Providers" actions={<Link to="/agents" className="text-xs underline">manage</Link>}>
          <ul className="space-y-2 text-sm">
            {d.providers.map((p: any) => (
              <li key={p.provider} className="flex flex-wrap items-center justify-between gap-2">
                <span>{p.display_name} {p.is_mock && <MockBadge />}</span>
                {p.installed ? <AuthBadge state={p.auth_state} /> : <Badge tone="red">not installed</Badge>}
              </li>
            ))}
          </ul>
          <div className="mt-3 text-xs muted">Engine: {d.engine.lock?.alive ? `running (pid ${d.engine.lock.pid})` : "not running"}</div>
        </Card>
      </div>

      <Card title="Projects" bodyClass="p-0">
        {d.projects.length === 0 ? (
          <Empty><Link to="/projects" className="text-orange-700 underline">Create or import your first project</Link></Empty>
        ) : (
          <div className="grid sm:grid-cols-2 lg:grid-cols-3">
            {d.projects.map((p: any) => (
              <Link key={p.id} to={`/projects/${p.id}`} className="min-w-0 border-b border-zinc-100 p-4 hover:bg-zinc-50 sm:border-r dark:border-zinc-800 dark:hover:bg-zinc-800/60">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium">{p.name}</span>
                  <span className="flex gap-1">
                    {p.config.permissions.mode === "read_only" && <Badge tone="blue">read-only</Badge>}
                    {p.status !== "active" && <Badge tone="yellow">{p.status}</Badge>}
                  </span>
                </div>
                <div className="mt-1 truncate font-mono text-xs muted">{p.root_path}</div>
                <div className="mt-2 flex flex-wrap gap-1">
                  {Object.entries(p.task_counts ?? {}).map(([s, n]) => <Badge key={s}>{s.toLowerCase()} {String(n)}</Badge>)}
                  {!Object.keys(p.task_counts ?? {}).length && <span className="text-xs muted">no tasks yet</span>}
                </div>
              </Link>
            ))}
          </div>
        )}
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Active & waiting tasks" bodyClass="">
          <TaskList tasks={[...d.active, ...d.queued]} empty="Nothing in progress." />
        </Card>
        <Card title="Recent history" bodyClass="">
          <TaskList tasks={d.recent} empty="No finished tasks yet." />
        </Card>
      </div>

      <Card title="Usage by provider" bodyClass="">
        {d.usage.per_provider.length === 0 ? <Empty>No agent runs recorded yet.</Empty> : (
          <div className="overflow-x-auto">
            <table className="tbl">
              <thead><tr><th>Provider</th><th>Billing</th><th>Runs</th><th>Input tok</th><th>Output tok</th><th>Billed USD</th><th>Subscription-equiv. USD</th></tr></thead>
              <tbody>
                {d.usage.per_provider.map((u: any) => (
                  <tr key={u.provider}>
                    <td className="font-medium">{u.provider} {u.provider === "mock" && <MockBadge />}</td>
                    <td>{u.billing.join(", ") || "—"}</td>
                    <td>{u.succeeded}/{u.runs}</td>
                    <td>{num(u.input_tokens)}{u.runs_without_token_data ? <span className="muted"> ({u.runs_without_token_data} unreported)</span> : null}</td>
                    <td>{num(u.output_tokens)}</td>
                    <td>${u.billed_usd.toFixed(2)}</td>
                    <td>{u.subscription_equivalent_usd ? `$${u.subscription_equivalent_usd.toFixed(2)}` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="px-4 py-2 text-xs muted">{d.usage.note}</p>
      </Card>
    </div>
  );
}
