import { useMemo, useState } from "react";
import type { ContactRow } from "../types";

/** Sortable contact table.
 *
 *  Accessibility specifics: the sort state lives on the <th> as aria-sort so
 *  assistive tech announces it, the control itself is a real <button> inside
 *  the header so it is reachable by keyboard, and every sort change is
 *  announced through a polite live region rather than silently reordering.
 */

type Key = keyof Pick<
  ContactRow,
  "email" | "company" | "location" | "confidence" | "status" | "source" | "createdAt"
>;

const COLUMNS: { key: Key; label: string; numeric?: boolean }[] = [
  { key: "email", label: "Contact" },
  { key: "company", label: "Company" },
  { key: "location", label: "Location" },
  { key: "confidence", label: "Confidence", numeric: true },
  { key: "source", label: "Source" },
  { key: "status", label: "Status" },
  { key: "createdAt", label: "Added" },
];

const STATUS_TONE: Record<string, string> = {
  replied: "bg-positive/15 text-positive",
  sent: "bg-accent/15 text-accent",
  queued: "bg-raised text-text",
  pending: "bg-raised text-muted",
  bounced: "bg-negative/15 text-negative",
  suppressed: "bg-negative/10 text-negative",
};

export default function DataTable({ rows }: { rows: ContactRow[] }) {
  const [sortKey, setSortKey] = useState<Key>("confidence");
  const [asc, setAsc] = useState(false);
  const [query, setQuery] = useState("");

  const sorted = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const filtered = needle
      ? rows.filter((r) =>
          [r.email, r.company, r.location, r.status].some((f) =>
            f.toLowerCase().includes(needle),
          ),
        )
      : rows;
    return [...filtered].sort((a, b) => {
      const x = a[sortKey];
      const y = b[sortKey];
      const cmp =
        typeof x === "number" && typeof y === "number"
          ? x - y
          : String(x).localeCompare(String(y));
      return asc ? cmp : -cmp;
    });
  }, [rows, sortKey, asc, query]);

  function toggle(key: Key) {
    if (key === sortKey) setAsc((v) => !v);
    else {
      setSortKey(key);
      setAsc(key !== "confidence"); // numbers read best highest-first
    }
  }

  const activeLabel = COLUMNS.find((c) => c.key === sortKey)?.label ?? "";

  return (
    <section
      className="rounded-xl border border-border bg-surface"
      aria-labelledby="contacts-heading"
    >
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border p-4">
        <h2 id="contacts-heading" className="text-sm font-semibold">
          Contacts
          <span className="ml-2 font-normal text-muted">{sorted.length}</span>
        </h2>
        <div>
          <label htmlFor="contact-filter" className="sr-only">
            Filter contacts
          </label>
          <input
            id="contact-filter"
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter by email, company, status"
            className="w-64 rounded-md border border-border bg-bg px-3 py-1.5 text-sm
                       placeholder:text-muted"
          />
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-sm">
          <caption className="sr-only">
            Discovered contacts, sortable by column. Currently sorted by {activeLabel},{" "}
            {asc ? "ascending" : "descending"}.
          </caption>
          <thead>
            <tr className="border-b border-border">
              {COLUMNS.map((col) => {
                const active = col.key === sortKey;
                return (
                  <th
                    key={col.key}
                    scope="col"
                    aria-sort={active ? (asc ? "ascending" : "descending") : "none"}
                    className={`whitespace-nowrap px-4 py-2 text-left font-medium text-muted ${
                      col.numeric ? "text-right" : ""
                    }`}
                  >
                    <button
                      type="button"
                      onClick={() => toggle(col.key)}
                      className="inline-flex items-center gap-1 hover:text-text"
                    >
                      {col.label}
                      <span aria-hidden="true" className={active ? "" : "opacity-0"}>
                        {asc ? "↑" : "↓"}
                      </span>
                    </button>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {sorted.map((row) => (
              <tr key={row.id} className="border-b border-border last:border-0 hover:bg-raised/60">
                <td className="px-4 py-2">
                  <span className="block font-medium">{row.name || row.email.split("@")[0]}</span>
                  <span className="block truncate font-mono text-xs text-muted">{row.email}</span>
                </td>
                <td className="px-4 py-2">{row.company}</td>
                <td className="whitespace-nowrap px-4 py-2 text-muted">{row.location}</td>
                <td className="px-4 py-2 text-right tabular-nums">{row.confidence}</td>
                <td className="whitespace-nowrap px-4 py-2 text-muted">{row.source}</td>
                <td className="px-4 py-2">
                  <span
                    className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                      STATUS_TONE[row.status] ?? "bg-raised text-muted"
                    }`}
                  >
                    {row.status}
                  </span>
                </td>
                <td className="whitespace-nowrap px-4 py-2 tabular-nums text-muted">
                  {row.createdAt.slice(0, 10)}
                </td>
              </tr>
            ))}
            {sorted.length === 0 && (
              <tr>
                <td colSpan={COLUMNS.length} className="px-4 py-10 text-center text-muted">
                  Nothing matches that filter yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <p aria-live="polite" className="sr-only">
        Sorted by {activeLabel}, {asc ? "ascending" : "descending"}. {sorted.length} rows.
      </p>
    </section>
  );
}
