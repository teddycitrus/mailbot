import { ActivityChart, SourceChart } from "./Charts";
import DataTable from "./DataTable";
import MetricCard from "./MetricCard";
import type { DashboardData, HealthCheck } from "../types";

/** The running state: what the bot has actually done.
 *
 *  Layout is CSS Grid with auto-fit tracks, so the metric row reflows from four
 *  columns to one without any breakpoint bookkeeping.
 */

function HealthStrip({ checks }: { checks: HealthCheck[] }) {
  const broken = checks.filter((c) => c.state === "FAIL");
  if (broken.length === 0) return null;
  return (
    <div
      role="alert"
      className="rounded-xl border border-negative/40 bg-negative/10 p-4 text-sm"
    >
      <h2 className="font-semibold text-negative">
        {broken.length} problem{broken.length > 1 ? "s" : ""} stopping the next run
      </h2>
      <ul className="mt-2 space-y-1">
        {broken.map((c) => (
          <li key={c.name} className="text-negative/90">
            <span className="font-medium">{c.name}:</span> {c.detail}
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function DashboardView({
  data,
  checks,
}: {
  data: DashboardData;
  checks: HealthCheck[];
}) {
  return (
    <div className="grid gap-5">
      <HealthStrip checks={checks} />

      <section aria-labelledby="metrics-heading">
        <h2 id="metrics-heading" className="sr-only">
          Key metrics
        </h2>
        <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(190px,1fr))]">
          {data.metrics.map((m) => (
            <MetricCard key={m.id} metric={m} />
          ))}
        </div>
      </section>

      <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(320px,1fr))]">
        <ActivityChart data={data.series} />
        <SourceChart data={data.sources} />
      </div>

      <DataTable rows={data.contacts} />

      <p className="text-xs text-muted">
        Generated {new Date(data.generatedAt).toLocaleString()}
      </p>
    </div>
  );
}
