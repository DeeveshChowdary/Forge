import { ApprovalCard } from "../components/shared";
import { Card, Empty, ErrorBox, Spinner } from "../components/ui";
import type { Approval } from "../lib/api";
import { useApi, useLiveReload } from "../lib/hooks";

export default function Approvals() {
  const pending = useApi<Approval[]>("/api/approvals?status=pending");
  const decided = useApi<Approval[]>("/api/approvals?status=");
  useLiveReload(() => { pending.reload(); decided.reload(); }, (e) => e.type.startsWith("approval."));
  const done = (decided.data ?? []).filter((a) => a.status !== "pending").slice(0, 30);
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Approvals</h1>
      <p className="text-sm muted">Forge pauses for your decision before merging into your working tree, exceeding spending limits, accepting unresolved review issues, or (if configured) running each phase.</p>
      <ErrorBox error={pending.error} onRetry={pending.reload} />
      <Card title={`Pending (${pending.data?.length ?? 0})`}>
        {!pending.data ? <Spinner /> : !pending.data.length ? <Empty>Nothing needs your approval.</Empty> :
          <div className="space-y-3">{pending.data.map((a) => <ApprovalCard key={a.id} a={a} onDone={() => { pending.reload(); decided.reload(); }} />)}</div>}
      </Card>
      <Card title="Recent decisions">
        {!done.length ? <Empty>No decisions yet.</Empty> : <div className="space-y-3">{done.map((a) => <ApprovalCard key={a.id} a={a} />)}</div>}
      </Card>
    </div>
  );
}
