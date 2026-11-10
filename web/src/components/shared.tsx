import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type Approval, type Task } from "../lib/api";
import { ago } from "../lib/format";
import { Badge, Elapsed, Empty, ErrorBox, StateBadge } from "./ui";

export function TaskList({ tasks, empty = "No tasks.", showProject = true }: { tasks: Task[]; empty?: string; showProject?: boolean }) {
  if (!tasks.length) return <Empty>{empty}</Empty>;
  return (
    <ul className="divide-y divide-zinc-100 dark:divide-zinc-800">
      {tasks.map((t) => (
        <li key={t.id}>
          <Link to={`/tasks/${t.id}`} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5 hover:bg-zinc-50 dark:hover:bg-zinc-800/50">
            <StateBadge state={t.state} />
            <span className="min-w-0 flex-1 truncate text-sm font-medium">{t.title}</span>
            {showProject && t.project_name && <span className="text-xs muted">{t.project_name}</span>}
            {t.phase && !t.phase.startsWith("__") && <Badge>{t.phase}</Badge>}
            {t.assigned_model && <span className="font-mono text-xs muted">{t.assigned_provider}/{t.assigned_model}</span>}
            <span className="text-xs muted">
              {t.finished_at ? ago(t.finished_at) : t.started_at ? <Elapsed from={t.started_at} /> : ago(t.created_at)}
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

export function ApprovalCard({ a, onDone }: { a: Approval; onDone?: () => void }) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const decide = async (approve: boolean) => {
    setBusy(true);
    setErr(null);
    try {
      await api.post(`/api/approvals/${a.id}`, { approve, note });
      onDone?.();
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };
  const risky = ["merge", "spending", "destructive", "publish"].includes(a.kind);
  return (
    <div className="space-y-2 rounded-lg border border-yellow-300 bg-yellow-50/60 p-3 dark:border-yellow-900 dark:bg-yellow-950/30" data-testid="approval">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={risky ? "orange" : "yellow"}>{a.kind.replace("_", " ")}</Badge>
        <span className="text-sm font-medium">{a.title}</span>
        {a.task_id && <Link className="text-xs text-orange-700 underline dark:text-orange-400" to={`/tasks/${a.task_id}`}>task</Link>}
        <span className="ml-auto text-xs muted">{ago(a.created_at)}</span>
      </div>
      {a.detail && <pre className="code max-h-48 whitespace-pre-wrap">{a.detail}</pre>}
      {a.status === "pending" ? (
        <div className="flex flex-wrap items-center gap-2">
          <input className="input max-w-xs flex-1" placeholder="Optional note (recorded in project memory)" value={note}
            onChange={(e) => setNote(e.target.value)} aria-label="Decision note" />
          <button className="btn btn-primary" disabled={busy} onClick={() => decide(true)}>Approve</button>
          <button className="btn btn-danger" disabled={busy} onClick={() => decide(false)}>Reject</button>
        </div>
      ) : (
        <div className="text-xs muted">{a.status} {a.decided_at ? ago(a.decided_at) : ""} {a.decision_note ? `— ${a.decision_note}` : ""}</div>
      )}
      <ErrorBox error={err} />
    </div>
  );
}
