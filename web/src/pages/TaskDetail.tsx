import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApprovalCard } from "../components/shared";
import { FailureAndRecovery, ProgressLimits, RoutePlan } from "../components/TaskInsights";
import { Badge, Card, DiffView, Elapsed, Empty, ErrorBox, MockBadge, Modal, RunBadge, Spinner, StateBadge, StructuredView, Tabs } from "../components/ui";
import { api, type Artifact, type ForgeEvent, type Run } from "../lib/api";
import { clock, duration, num } from "../lib/format";
import { useApi, useEvents, useLiveReload } from "../lib/hooks";

const ACTIVE = ["PLANNING", "EXECUTING", "TESTING", "REVIEWING"];

export default function TaskDetail() {
  const { tid = "" } = useParams();
  const { data: t, error, loading, reload } = useApi<any>(`/api/tasks/${tid}`, [tid]);
  const { data: settings } = useApi<any>("/api/settings");
  const [events, setEvents] = useState<ForgeEvent[]>([]);
  const [tab, setTab] = useState("live");
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [logRun, setLogRun] = useState<Run | null>(null);

  useEffect(() => { if (t?.events) setEvents(t.events); }, [t?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  useEvents((e) => setEvents((prev) => (prev.some((x) => x.id === e.id) ? prev : [...prev.slice(-1500), e])), (e) => e.task_id === tid);
  useLiveReload(reload, (e) => e.task_id === tid && !e.type.startsWith("agent."), 300);

  if (loading && !t) return <Spinner label="Loading task" />;
  if (!t) return <ErrorBox error={error ?? "Task not found"} onRetry={reload} />;

  const act = async (a: string) => {
    setActionErr(null);
    try { await api.post(`/api/tasks/${t.id}/${a}`); reload(); } catch (e: any) { setActionErr(e.message); }
  };
  const wf = settings?.workflows?.find((w: any) => w.key === t.workflow);
  const running: Run | undefined = t.runs.find((r: Run) => r.state === "running");
  const pending = t.approvals.filter((a: any) => a.status === "pending");
  const isMock = t.runs.some((r: Run) => r.is_mock);
  const proposals = t.artifacts.filter((a: Artifact) => a.kind === "proposal");
  const comparison = [...t.artifacts].reverse().find((a: Artifact) => a.kind === "comparison");

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <div className="text-xs muted"><Link to={`/projects/${t.project.id}`} className="underline">{t.project.name}</Link> / {t.id}</div>
          <h1 className="flex flex-wrap items-center gap-2 text-2xl font-semibold">{t.title} <StateBadge state={t.state} />{isMock && <MockBadge />}</h1>
          <div className="flex flex-wrap gap-2 text-xs muted">
            <span>{wf?.name ?? t.workflow}</span>·<span>routing: {t.routing_mode}</span>·<span>review: {t.review_depth}</span>·
            <span>approvals: {t.approval_policy}</span>·<span>cycle {t.review_cycle}/{t.budget.max_review_cycles}</span>·
            <span>elapsed <Elapsed from={t.started_at} to={t.finished_at} /></span>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          {(ACTIVE.includes(t.state) || ["READY", "AWAITING_APPROVAL", "BLOCKED"].includes(t.state)) && !t.pause_requested &&
            <button className="btn" onClick={() => act("pause")}>Pause</button>}
          {t.state === "PAUSED" && <button className="btn btn-primary" onClick={() => act("resume")}>Resume</button>}
          {(t.state === "BLOCKED" || (t.state === "FAILED" && t.analysis?.last_failure?.classification !== "limit_exhausted")) && t.phase !== "__await_children__" && <button className="btn btn-primary" onClick={() => act("retry")}>Retry</button>}
          {!["COMPLETED", "FAILED", "CANCELLED"].includes(t.state) && !t.cancel_requested &&
            <button className="btn btn-danger" onClick={() => { if (confirm("Stop and cancel this task?")) act("cancel"); }}>Stop</button>}
        </div>
      </div>
      <ErrorBox error={actionErr} />
      {(t.pause_requested || t.cancel_requested) ? <div className="text-sm text-yellow-700 dark:text-yellow-400">{t.cancel_requested ? "Cancellation" : "Pause"} requested — the running agent is being stopped…</div> : null}
      {t.error && <ErrorBox error={`Error: ${t.error}`} />}
      {t.disposition && <div className="rounded-lg bg-zinc-100 p-3 text-sm dark:bg-zinc-900"><span className="font-medium">Disposition:</span> {t.disposition}</div>}
      {pending.map((a: any) => <ApprovalCard key={a.id} a={a} onDone={reload} />)}

      <PhaseTimeline t={t} wf={wf} />
      <FailureAndRecovery t={t} onChange={reload} />
      <ProgressLimits t={t} events={events} />
      <RoutePlan t={t} />

      <div className="grid gap-4 md:grid-cols-3">
        <Card title="Current agent">
          {running ? (
            <div className="space-y-1 text-sm">
              <div className="font-medium">{running.phase} · {running.role}</div>
              <div className="font-mono text-xs">{running.provider ? `${running.provider}/${running.model}` : "test runner"}{running.effort ? ` · effort ${running.effort}` : ""} {running.is_mock && <MockBadge />}</div>
              <div className="muted">running for <Elapsed from={running.started_at} /> · attempt {running.attempt}</div>
              <button className="btn btn-sm mt-2" onClick={() => setLogRun(running)}>Raw log</button>
            </div>
          ) : <div className="text-sm muted">No agent running{t.assigned_model ? ` (last: ${t.assigned_provider}/${t.assigned_model})` : ""}.</div>}
        </Card>
        <Card title="Objective" className="md:col-span-2">
          <p className="whitespace-pre-wrap text-sm">{t.objective}</p>
          {t.acceptance_criteria.length > 0 && <ul className="mt-2 list-disc pl-5 text-sm muted">{t.acceptance_criteria.map((c: string, i: number) => <li key={i}>{c}</li>)}</ul>}
          {t.branch && <div className="mt-2 font-mono text-xs muted">worktree {t.worktree_path} · branch {t.branch} · base {t.base_commit?.slice(0, 10)}</div>}
        </Card>
      </div>

      <Tabs active={tab} onChange={setTab} tabs={[
        { key: "live", label: `Live log (${events.length})` },
        { key: "runs", label: `Runs (${t.runs.length})` },
        { key: "artifacts", label: `Handoffs & artifacts (${t.artifacts.length})` },
        ...(t.base_commit ? [{ key: "diff", label: "Diff" }] : []),
        ...(proposals.length ? [{ key: "proposals", label: `Proposals (${proposals.length})` }] : []),
        { key: "routing", label: "Routing decisions" },
        ...(t.children.length ? [{ key: "subtasks", label: `Subtasks (${t.children.length})` }] : []),
      ]} />
      {tab === "live" && <EventLog events={events} active={ACTIVE.includes(t.state)} />}
      {tab === "runs" && <Runs runs={t.runs} onLog={setLogRun} />}
      {tab === "artifacts" && <ArtifactList items={t.artifacts.filter((a: Artifact) => a.kind !== "comparison")} />}
      {tab === "diff" && <TaskDiff tid={t.id} />}
      {tab === "proposals" && <Proposals proposals={proposals} comparison={comparison?.data} />}
      {tab === "routing" && <Routing decisions={t.routing} />}
      {tab === "subtasks" && (
        <Card bodyClass="p-0"><ul className="divide-y divide-zinc-100 dark:divide-zinc-800">{t.children.map((c: any) => (
          <li key={c.id}><Link to={`/tasks/${c.id}`} className="flex items-center gap-3 px-4 py-2 hover:bg-zinc-50 dark:hover:bg-zinc-800"><StateBadge state={c.state} /><span className="text-sm">{c.title}</span>
            {c.depends_on.length > 0 && <span className="text-xs muted">after {c.depends_on.length} task(s)</span>}</Link></li>))}</ul></Card>
      )}
      <LogModal run={logRun} onClose={() => setLogRun(null)} />
    </div>
  );
}

function PhaseTimeline({ t, wf }: { t: any; wf: any }) {
  if (!wf) return null;
  const phases: any[] = [...wf.phases];
  const ran = new Set(t.runs.map((r: Run) => r.phase));
  const doneAll = t.phase === "__finish__" || t.state === "COMPLETED";
  return (
    <ol className="flex flex-wrap items-center gap-2" aria-label="Workflow phases">
      {phases.map((p, i) => {
        const runsFor = t.runs.filter((r: Run) => r.phase === p.key);
        const last = runsFor[runsFor.length - 1];
        const current = t.phase === p.key && !["COMPLETED", "FAILED", "CANCELLED"].includes(t.state);
        const computed = p.kind === "compare" && t.artifacts.some((a: Artifact) => a.kind === "comparison");
        const status = current ? (last?.state === "running" ? "running" : "current") : last ? last.state
          : computed ? "succeeded" : doneAll ? "skipped" : "pending";
        const cls = status === "running" || status === "current" ? "border-orange-500 bg-orange-50 dark:bg-orange-950/40"
          : status === "succeeded" ? "border-emerald-400 bg-emerald-50 dark:bg-emerald-950/30"
          : status === "failed" ? "border-red-400 bg-red-50 dark:bg-red-950/30" : "border-zinc-200 dark:border-zinc-800";
        return (
          <li key={p.key} className="flex items-center gap-2">
            <div className={`rounded-lg border px-3 py-1.5 text-xs ${cls}`}>
              <div className="font-medium">{p.key}</div>
              <div className="muted">{p.state.toLowerCase()} · {status}{runsFor.length > 1 ? ` ×${runsFor.length}` : ""}</div>
            </div>
            {i < phases.length - 1 && <span className="muted" aria-hidden>→</span>}
          </li>
        );
      })}
      {(ran.has("fix") || wf.has_review_loop) && (
        <li className={`rounded-lg border border-dashed px-3 py-1.5 text-xs ${ran.has("fix") ? "border-orange-400" : "border-zinc-300 dark:border-zinc-700"}`}>
          fix loop · {t.runs.filter((r: Run) => r.phase === "fix").length}/{t.budget.max_review_cycles}
        </li>
      )}
    </ol>
  );
}

const LEVEL: Record<string, string> = { error: "text-red-600 dark:text-red-400", warn: "text-yellow-700 dark:text-yellow-400" };

function EventLog({ events, active }: { events: ForgeEvent[]; active: boolean }) {
  const [filter, setFilter] = useState("all");
  const [follow, setFollow] = useState(true);
  const ref = useRef<HTMLDivElement>(null);
  const shown = useMemo(() => events.filter((e) => filter === "all" || (filter === "agent" ? e.type.startsWith("agent.") : !e.type.startsWith("agent."))), [events, filter]);
  useEffect(() => { if (follow && ref.current) ref.current.scrollTop = ref.current.scrollHeight; }, [shown.length, follow]);
  return (
    <Card bodyClass="p-0" title={<span className="flex items-center gap-2">Events {active && <span className="size-2 animate-pulse rounded-full bg-orange-500" aria-label="live" />}</span>}
      actions={<>
        <select className="input py-1 text-xs" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter events">
          <option value="all">all</option><option value="agent">agent output</option><option value="system">orchestration</option>
        </select>
        <label className="flex items-center gap-1 text-xs"><input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />follow</label>
      </>}>
      <div ref={ref} className="max-h-[32rem] overflow-y-auto font-mono text-xs" data-testid="event-log">
        {!shown.length ? <Empty>No events yet.</Empty> : shown.map((e) => (
          <div key={e.id} className="flex gap-3 border-b border-zinc-100 px-4 py-1 dark:border-zinc-800/60">
            <span className="shrink-0 muted">{clock(e.ts)}</span>
            <span className={`w-36 shrink-0 truncate ${LEVEL[e.level] ?? "text-zinc-500"}`}>{e.type}</span>
            <span className={`min-w-0 whitespace-pre-wrap break-words ${LEVEL[e.level] ?? ""}`}>{e.message}{e.data?.mock ? " " : ""}</span>
          </div>
        ))}
      </div>
    </Card>
  );
}

function Runs({ runs, onLog }: { runs: Run[]; onLog: (r: Run) => void }) {
  if (!runs.length) return <Card><Empty>No runs yet.</Empty></Card>;
  return (
    <Card bodyClass="p-0"><div className="overflow-x-auto"><table className="tbl">
      <thead><tr><th>Phase</th><th>Agent / exact model</th><th>State</th><th>Duration</th><th>Tokens in/out</th><th>Cost</th><th /></tr></thead>
      <tbody>{runs.map((r) => (
        <tr key={r.id}>
          <td>{r.phase} <span className="muted">· {r.role}</span><div className="text-[11px] muted">attempt {r.attempt}{r.review_cycle ? ` · cycle ${r.review_cycle}` : ""}</div></td>
          <td className="font-mono text-xs">{r.kind === "test" ? "test runner" : <>{r.provider}/{r.model}{r.resolved_model && r.resolved_model !== r.model ? <div className="muted">→ {r.resolved_model}</div> : null}{r.effort && <div className="muted">effort {r.effort}</div>}</>} {r.is_mock && <MockBadge />}</td>
          <td><RunBadge state={r.state} />{r.error && <div className="mt-1 max-w-xs text-[11px] text-red-600">{r.error.slice(0, 200)}</div>}</td>
          <td>{r.state === "running" ? <Elapsed from={r.started_at} /> : duration(r.ended_at ? r.ended_at - r.started_at : null)}</td>
          <td className="text-xs">{r.usage?.input_tokens != null ? `${num(r.usage.input_tokens)} / ${num(r.usage.output_tokens)}` : <span className="muted">not reported</span>}</td>
          <td className="text-xs">{r.usage?.cost_usd != null ? <span title={r.usage.cost_kind}>${r.usage.cost_usd.toFixed(3)}{r.usage.cost_kind === "subscription_equivalent" && <span className="muted"> (subscription est., not billed)</span>}</span> : <span className="muted">—</span>}</td>
          <td><button className="btn btn-sm" onClick={() => onLog(r)}>Log</button></td>
        </tr>))}</tbody>
    </table></div></Card>
  );
}

function LogModal({ run, onClose }: { run: Run | null; onClose: () => void }) {
  const { data } = useApi<any>(run ? `/api/runs/${run.id}/log` : null, [run?.id]);
  const [showPrompt, setShowPrompt] = useState(false);
  return (
    <Modal open={!!run} onClose={onClose} title={run ? `Run ${run.id} — ${run.phase}` : ""} wide>
      {!data ? <Spinner /> : (
        <div className="space-y-3">
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={showPrompt} onChange={(e) => setShowPrompt(e.target.checked)} />Show the exact prompt sent</label>
          {showPrompt && <pre className="code max-h-80 whitespace-pre-wrap">{data.prompt}</pre>}
          <pre className="code max-h-[28rem] whitespace-pre-wrap">{data.lines.length ? data.lines.join("\n") : "(empty log)"}</pre>
        </div>
      )}
    </Modal>
  );
}

function ArtifactList({ items }: { items: Artifact[] }) {
  if (!items.length) return <Card><Empty>No artifacts yet.</Empty></Card>;
  return (
    <div className="space-y-3">
      {items.map((a, i) => (
        <details key={a.id} className="card" open={i === items.length - 1}>
          <summary className="flex cursor-pointer flex-wrap items-center gap-2 px-4 py-2.5 text-sm">
            <Badge tone={a.kind === "test_report" ? (a.data?.passed ? "green" : a.data?.passed === false ? "red" : "zinc") : "violet"}>{a.kind}</Badge>
            <span className="font-medium">{a.title}</span><span className="ml-auto text-xs muted">{clock(a.created_at)}</span>
          </summary>
          <div className="border-t border-zinc-200 p-4 dark:border-zinc-800">
            {a.kind === "diff" ? <DiffView diff={a.content} /> : a.data ? <StructuredView kind={a.kind} data={a.data} /> : <pre className="code max-h-96 whitespace-pre-wrap">{a.content}</pre>}
          </div>
        </details>
      ))}
    </div>
  );
}

function TaskDiff({ tid }: { tid: string }) {
  const { data, error, reload } = useApi<string>(`/api/tasks/${tid}/diff`, [tid]);
  useLiveReload(reload, (e) => e.task_id === tid && e.type === "worktree.commit");
  if (error) return <ErrorBox error={error} />;
  return <Card title="Cumulative diff (base..branch)" bodyClass="p-2">{data == null ? <Spinner /> : <DiffView diff={data} maxHeight="48rem" />}</Card>;
}

function Routing({ decisions }: { decisions: any[] }) {
  if (!decisions.length) return <Card><Empty>No routing decisions yet.</Empty></Card>;
  return (
    <div className="space-y-3">
      {decisions.map((d) => (
        <Card key={d.id} title={<span className="flex flex-wrap items-center gap-2"><Badge>{d.phase}</Badge><span className="font-mono text-xs">{d.provider}/{d.model}{d.effort ? ` · ${d.effort}` : ""}</span><Badge tone="blue">{d.mode}</Badge>
          {d.outcome && <Badge tone={d.outcome.ok ? "green" : "red"}>{d.outcome.ok ? "succeeded" : d.outcome.error_kind ?? "failed"}</Badge>}</span>}>
          <ul className="list-disc space-y-0.5 pl-5 text-sm">{d.reasons.map((r: string, i: number) => <li key={i}>{r}</li>)}</ul>
          {d.candidates.length > 0 && (
            <details className="mt-2"><summary className="cursor-pointer text-xs muted">All candidates ({d.candidates.length})</summary>
              <div className="overflow-x-auto"><table className="tbl mt-2"><thead><tr><th>Model</th><th>Tier</th><th>Score</th><th>Breakdown / exclusion</th></tr></thead>
                <tbody>{d.candidates.map((c: any) => (
                  <tr key={`${c.provider}/${c.model}`} className={c.excluded ? "opacity-60" : ""}>
                    <td className="font-mono text-xs">{c.provider}/{c.model}</td><td>{c.tier}</td><td>{c.excluded ? "—" : c.score}</td>
                    <td className="text-xs">{c.excluded ?? Object.entries(c.breakdown).map(([k, v]) => `${k} ${v}`).join(", ")}</td>
                  </tr>))}</tbody></table></div>
            </details>
          )}
          <div className="mt-2 text-[11px] muted">policy {d.policy} · {clock(d.created_at)}</div>
        </Card>
      ))}
    </div>
  );
}

/** Side-by-side view of independent proposals and their computed differences. */
export function Proposals({ proposals, comparison }: { proposals: Artifact[]; comparison: any }) {
  const pair = comparison?.pairs?.[0];
  const meta = (runId: string | null) => comparison?.proposals?.find((p: any) => p.run_id === runId);
  return (
    <div className="space-y-4">
      <div className={`grid gap-4 ${proposals.length > 1 ? "lg:grid-cols-2" : ""}`}>
        {proposals.map((p) => {
          const m = meta(p.run_id);
          return (
            <Card key={p.id} title={<span className="flex flex-wrap items-center gap-2">{p.title}{m?.provider === "mock" && <MockBadge />}</span>}>
              {m && <div className="mb-2 font-mono text-[11px] muted">{m.provider}/{m.resolved_model ?? m.model}{m.effort ? ` · effort ${m.effort}` : ""}</div>}
              <StructuredView kind="proposal" data={p.data} />
            </Card>
          );
        })}
      </div>
      {pair && (
        <Card title="Differences (computed by Forge: deterministic word overlap, not a judgement of quality)">
          <div className="mb-3 flex flex-wrap gap-2 text-xs"><Badge>title similarity {pair.title_similarity}</Badge><Badge>summary similarity {pair.summary_similarity}</Badge><Badge tone="green">{pair.agreeing_steps.length} agreeing step(s)</Badge></div>
          <div className="grid gap-4 md:grid-cols-2">
            <div><h3 className="mb-1 text-sm font-medium">Only in proposal A</h3><ul className="list-disc pl-5 text-sm">{pair.only_a_steps.map((s: string, i: number) => <li key={i}>{s}</li>)}{!pair.only_a_steps.length && <li className="muted">none</li>}</ul>
              {pair.only_a_risks.length > 0 && <><h4 className="mt-2 text-xs font-medium muted">Risks only A raised</h4><ul className="list-disc pl-5 text-sm">{pair.only_a_risks.map((s: string, i: number) => <li key={i}>{s}</li>)}</ul></>}</div>
            <div><h3 className="mb-1 text-sm font-medium">Only in proposal B</h3><ul className="list-disc pl-5 text-sm">{pair.only_b_steps.map((s: string, i: number) => <li key={i}>{s}</li>)}{!pair.only_b_steps.length && <li className="muted">none</li>}</ul>
              {pair.only_b_risks.length > 0 && <><h4 className="mt-2 text-xs font-medium muted">Risks only B raised</h4><ul className="list-disc pl-5 text-sm">{pair.only_b_risks.map((s: string, i: number) => <li key={i}>{s}</li>)}</ul></>}</div>
          </div>
          {pair.agreeing_steps.length > 0 && (
            <><h3 className="mb-1 mt-4 text-sm font-medium">Where they agree</h3>
              <ul className="space-y-1 text-sm">{pair.agreeing_steps.map((s: any, i: number) => <li key={i} className="rounded border border-zinc-200 p-2 dark:border-zinc-800"><div>A: {s.a}</div><div className="muted">B: {s.b}</div></li>)}</ul></>
          )}
          <div className="mt-3 text-xs muted">Shared evidence: {pair.shared_evidence.join(", ") || "none"} · only A: {pair.only_a_evidence.join(", ") || "none"} · only B: {pair.only_b_evidence.join(", ") || "none"}</div>
        </Card>
      )}
    </div>
  );
}
