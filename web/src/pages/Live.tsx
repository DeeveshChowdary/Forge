import { useState } from "react";
import { Link } from "react-router-dom";
import { TaskList } from "../components/shared";
import { Card, Elapsed, Empty, ErrorBox, MockBadge, Spinner } from "../components/ui";
import type { ForgeEvent } from "../lib/api";
import { clock } from "../lib/format";
import { useApi, useEvents, useLiveReload } from "../lib/hooks";

export default function Live() {
  const { data, error, reload } = useApi<any>("/api/overview");
  const { data: initial } = useApi<ForgeEvent[]>("/api/events?limit=150");
  const [events, setEvents] = useState<ForgeEvent[]>([]);
  useEvents((e) => setEvents((p) => [...p.slice(-400), e]));
  useLiveReload(reload, (e) => e.type.startsWith("task.") || e.type.startsWith("run."));
  const all = [...(initial ?? []).filter((e) => !events.some((x) => x.id === e.id)), ...events].slice(-400).reverse();
  if (!data) return error ? <ErrorBox error={error} onRetry={reload} /> : <Spinner />;
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Live execution</h1>
      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Running agents" bodyClass="p-0">
          {!data.running_runs.length ? <Empty>No agents running. Engine {data.engine.lock?.alive ? "is idle" : "is not running — start `forge serve`"}.</Empty> : (
            <ul className="divide-y divide-zinc-100 dark:divide-zinc-800">
              {data.running_runs.map((r: any) => (
                <li key={r.id}><Link to={`/tasks/${r.task_id}`} className="block px-4 py-3 hover:bg-zinc-50 dark:hover:bg-zinc-800/50">
                  <div className="flex items-center justify-between gap-2"><span className="font-medium">{r.title}</span><Elapsed from={r.started_at} /></div>
                  <div className="text-xs muted">{r.phase} · {r.role} · <span className="font-mono">{r.provider ? `${r.provider}/${r.model}` : "test runner"}</span> {r.provider === "mock" && <MockBadge />}</div>
                </Link></li>
              ))}
            </ul>
          )}
        </Card>
        <Card title="Active & waiting tasks" bodyClass="p-0"><TaskList tasks={[...data.active, ...data.queued]} empty="Nothing active." /></Card>
      </div>
      <Card title="Global event stream" bodyClass="p-0">
        <div className="max-h-[36rem] overflow-y-auto font-mono text-xs">
          {!all.length ? <Empty>No events.</Empty> : all.map((e) => (
            <div key={e.id} className="flex gap-3 border-b border-zinc-100 px-4 py-1 dark:border-zinc-800/60">
              <span className="shrink-0 muted">{clock(e.ts)}</span>
              <span className={`w-40 shrink-0 truncate ${e.level === "error" ? "text-red-600" : e.level === "warn" ? "text-yellow-600" : "muted"}`}>{e.type}</span>
              <span className="min-w-0 break-words">{e.task_id ? <Link className="underline" to={`/tasks/${e.task_id}`}>{e.message}</Link> : e.message}</span>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
