import type { Metric } from "../types";

/** A single KPI tile with a trend indicator.
 *
 *  The colour rule is not "up is green". Bounces rising is bad and replies
 *  rising is good, so direction is compared against `higherIsBetter`. Colour is
 *  never the only carrier of that meaning: the arrow glyph and the visually
 *  hidden text say it too, for colour-blind and screen-reader users.
 */

function Arrow({ trend }: { trend: Metric["trend"] }) {
  if (trend === "flat") {
    return (
      <svg aria-hidden="true" viewBox="0 0 16 16" className="h-3.5 w-3.5" fill="currentColor">
        <rect x="3" y="7" width="10" height="2" rx="1" />
      </svg>
    );
  }
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 16 16"
      className={`h-3.5 w-3.5 ${trend === "down" ? "rotate-180" : ""}`}
      fill="currentColor"
    >
      <path d="M8 3l5 6H9.5v4h-3V9H3l5-6z" />
    </svg>
  );
}

export default function MetricCard({ metric }: { metric: Metric }) {
  const { label, value, changePct, trend, higherIsBetter, hint } = metric;

  const good = trend === "flat" ? null : (trend === "up") === higherIsBetter;
  const tone =
    good === null ? "text-muted" : good ? "text-positive" : "text-negative";

  const direction = trend === "up" ? "increased" : trend === "down" ? "decreased" : "unchanged";
  const spoken =
    changePct === null
      ? "no comparison available"
      : `${direction} ${Math.abs(changePct).toFixed(0)} percent versus the previous period, which is ${
          good === null ? "neutral" : good ? "good" : "a concern"
        }`;

  return (
    <article
      className="rounded-xl border border-border bg-surface p-4 shadow-sm"
      aria-labelledby={`metric-${metric.id}-label`}
    >
      <h3
        id={`metric-${metric.id}-label`}
        className="text-xs font-medium uppercase tracking-wide text-muted"
      >
        {label}
      </h3>

      <div className="mt-2 flex items-baseline gap-2">
        <p className="text-2xl font-semibold tabular-nums">{value}</p>
        {changePct !== null && (
          <span className={`flex items-center gap-0.5 text-sm font-medium ${tone}`}>
            <Arrow trend={trend} />
            <span aria-hidden="true">{Math.abs(changePct).toFixed(0)}%</span>
          </span>
        )}
      </div>

      <span className="sr-only">{spoken}</span>
      {hint && <p className="mt-1 text-xs text-muted">{hint}</p>}
    </article>
  );
}
