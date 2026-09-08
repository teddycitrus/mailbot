import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { SeriesPoint, SourceSlice } from "../types";

/** Charts read their colours from the same CSS custom properties as everything
 *  else, resolved at render time, so they follow the theme without a second
 *  palette to keep in sync. Each chart is wrapped in a labelled region with a
 *  text summary, because an SVG on its own tells a screen reader nothing.
 */

function cssVar(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return raw ? `rgb(${raw})` : fallback;
}

function Panel({
  title,
  summary,
  children,
}: {
  title: string;
  summary: string;
  children: React.ReactNode;
}) {
  const id = title.toLowerCase().replace(/\s+/g, "-");
  return (
    <section
      className="rounded-xl border border-border bg-surface p-4"
      aria-labelledby={`${id}-title`}
    >
      <h2 id={`${id}-title`} className="text-sm font-semibold">
        {title}
      </h2>
      <p className="sr-only">{summary}</p>
      <div className="mt-3 h-56" role="img" aria-label={summary}>
        {children}
      </div>
    </section>
  );
}

export function ActivityChart({ data }: { data: SeriesPoint[] }) {
  const accent = cssVar("--accent", "rgb(37 99 235)");
  const positive = cssVar("--positive", "rgb(22 163 74)");
  const border = cssVar("--border", "rgb(226 232 240)");
  const muted = cssVar("--muted", "rgb(107 114 128)");
  const surface = cssVar("--surface", "#fff");

  const totalSent = data.reduce((n, d) => n + d.sent, 0);
  const totalReplies = data.reduce((n, d) => n + d.replies, 0);

  return (
    <Panel
      title="Sends and replies"
      summary={`${totalSent} emails sent and ${totalReplies} replies over the last ${data.length} days.`}
    >
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
          <defs>
            <linearGradient id="fillSent" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={accent} stopOpacity={0.35} />
              <stop offset="100%" stopColor={accent} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke={border} vertical={false} />
          <XAxis
            dataKey="date"
            tick={{ fill: muted, fontSize: 11 }}
            tickFormatter={(d: string) => d.slice(5)}
            stroke={border}
          />
          <YAxis tick={{ fill: muted, fontSize: 11 }} stroke={border} allowDecimals={false} />
          <Tooltip
            contentStyle={{
              background: surface,
              border: `1px solid ${border}`,
              borderRadius: 8,
              fontSize: 12,
            }}
          />
          <Area type="monotone" dataKey="sent" stroke={accent} fill="url(#fillSent)" strokeWidth={2} />
          <Area type="monotone" dataKey="replies" stroke={positive} fill="none" strokeWidth={2} />
        </AreaChart>
      </ResponsiveContainer>
    </Panel>
  );
}

export function SourceChart({ data }: { data: SourceSlice[] }) {
  const accent = cssVar("--accent", "rgb(37 99 235)");
  const border = cssVar("--border", "rgb(226 232 240)");
  const muted = cssVar("--muted", "rgb(107 114 128)");
  const surface = cssVar("--surface", "#fff");

  const summary = data.length
    ? `Contacts by discovery source: ${data.map((d) => `${d.source} ${d.count}`).join(", ")}.`
    : "No contacts discovered yet.";

  return (
    <Panel title="Where contacts came from" summary={summary}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} layout="vertical" margin={{ top: 4, right: 8, left: 8, bottom: 0 }}>
          <CartesianGrid stroke={border} horizontal={false} />
          <XAxis type="number" tick={{ fill: muted, fontSize: 11 }} stroke={border} allowDecimals={false} />
          <YAxis
            type="category"
            dataKey="source"
            width={90}
            tick={{ fill: muted, fontSize: 11 }}
            stroke={border}
          />
          <Tooltip
            cursor={{ fill: "transparent" }}
            contentStyle={{
              background: surface,
              border: `1px solid ${border}`,
              borderRadius: 8,
              fontSize: 12,
            }}
          />
          <Bar dataKey="count" radius={[0, 4, 4, 0]}>
            {data.map((entry) => (
              <Cell key={entry.source} fill={accent} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </Panel>
  );
}
