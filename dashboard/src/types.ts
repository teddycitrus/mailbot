/** Shapes returned by the local Python API. Kept in one place so the server
 *  contract is visible: if these drift, TypeScript catches it at build time. */

export type Trend = "up" | "down" | "flat";

export interface Metric {
  id: string;
  label: string;
  value: number | string;
  /** Percentage change against the previous period. Null when unknown. */
  changePct: number | null;
  trend: Trend;
  /** Whether a rise is good. Bounces rising is bad; replies rising is good. */
  higherIsBetter: boolean;
  hint?: string;
}

export interface SeriesPoint {
  date: string;
  sent: number;
  replies: number;
  bounces: number;
}

export interface SourceSlice {
  source: string;
  count: number;
}

export type ContactStatus =
  | "pending"
  | "queued"
  | "sent"
  | "replied"
  | "bounced"
  | "suppressed";

export interface ContactRow {
  id: number;
  email: string;
  name: string;
  company: string;
  location: string;
  confidence: number;
  verify: string;
  source: string;
  status: ContactStatus;
  createdAt: string;
}

export interface DashboardData {
  generatedAt: string;
  metrics: Metric[];
  series: SeriesPoint[];
  sources: SourceSlice[];
  contacts: ContactRow[];
}

/* ----------------------------- setup console ---------------------------- */

export type StepStatus = "ok" | "warn" | "todo";

export interface SetupState {
  /** False until the required steps are all satisfied. */
  configured: boolean;
  steps: {
    identity: StepStatus;
    credentials: StepStatus;
    resume: StepStatus;
    aspects: StepStatus;
    template: StepStatus;
    replies: StepStatus;
    optional: StepStatus;
    targeting: StepStatus;
  };
  /** Current values, with secrets returned masked rather than in clear. */
  values: Record<string, string>;
  resumeName: string;
}

export interface HealthCheck {
  name: string;
  state: "ok" | "warn" | "FAIL";
  detail: string;
}
