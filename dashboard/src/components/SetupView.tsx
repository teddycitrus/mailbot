import { useState } from "react";
import type { SetupState, StepStatus } from "../types";

/** First-run console.
 *
 *  Everything personal lives here rather than in the shipped build: identity,
 *  credentials, resume and the claims the bot is allowed to make. A downloaded
 *  copy therefore starts empty and belongs to whoever installed it.
 *
 *  Each step states plainly why it is needed, because "paste an app password"
 *  with no explanation is how people end up pasting their real password.
 */

interface Props {
  state: SetupState;
  onSave: (values: Record<string, string>) => Promise<void>;
  onUploadResume: (file: File) => Promise<void>;
  saving: boolean;
  error: string;
}

function StatusDot({ status }: { status: StepStatus }) {
  const tone =
    status === "ok"
      ? "bg-positive"
      : status === "warn"
        ? "bg-accent"
        : "bg-negative";
  const label = status === "ok" ? "complete" : status === "warn" ? "optional" : "required";
  return (
    <span className="flex items-center gap-2">
      <span aria-hidden="true" className={`h-2 w-2 shrink-0 rounded-full ${tone}`} />
      <span className="sr-only">{label}:</span>
    </span>
  );
}

function Field({
  id,
  label,
  hint,
  type = "text",
  value,
  onChange,
  placeholder,
  multiline,
}: {
  id: string;
  label: string;
  hint?: string;
  type?: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  multiline?: boolean;
}) {
  const hintId = hint ? `${id}-hint` : undefined;
  const shared =
    "mt-1 w-full rounded-md border border-border bg-bg px-3 py-2 text-sm placeholder:text-muted";
  return (
    <div className="mb-4">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      {hint && (
        <p id={hintId} className="mt-0.5 text-xs text-muted">
          {hint}
        </p>
      )}
      {multiline ? (
        <textarea
          id={id}
          aria-describedby={hintId}
          value={value}
          rows={10}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          className={`${shared} font-mono text-xs`}
        />
      ) : (
        <input
          id={id}
          type={type}
          aria-describedby={hintId}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          autoComplete={type === "password" ? "new-password" : "off"}
          className={shared}
        />
      )}
    </div>
  );
}

function Step({
  n,
  title,
  status,
  blurb,
  children,
}: {
  n: number;
  title: string;
  status: StepStatus;
  blurb: string;
  children: React.ReactNode;
}) {
  const id = `step-${n}`;
  return (
    <section
      aria-labelledby={`${id}-title`}
      className="rounded-xl border border-border bg-surface p-5"
    >
      <div className="flex items-start gap-3">
        <StatusDot status={status} />
        <div className="min-w-0 flex-1">
          <h2 id={`${id}-title`} className="text-sm font-semibold">
            {n}. {title}
          </h2>
          <p className="mt-1 text-xs text-muted">{blurb}</p>
          <div className="mt-4">{children}</div>
        </div>
      </div>
    </section>
  );
}

export default function SetupView({
  state,
  onSave,
  onUploadResume,
  saving,
  error,
}: Props) {
  const [form, setForm] = useState<Record<string, string>>(state.values);
  const set = (k: string) => (v: string) => setForm((f) => ({ ...f, [k]: v }));

  return (
    <div className="mx-auto max-w-3xl">
      <header className="mb-6">
        <h1 className="text-xl font-semibold">Set up your outreach</h1>
        <p className="mt-1 text-sm text-muted">
          Nothing here ships with the app. Everything you enter stays in a local{" "}
          <code className="font-mono text-xs">.env</code> file on this machine and is
          never sent anywhere except the mail server you configure.
        </p>
      </header>

      {error && (
        <div
          role="alert"
          className="mb-4 rounded-lg border border-negative/40 bg-negative/10 p-3 text-sm
                     text-negative"
        >
          {error}
        </div>
      )}

      <div className="grid gap-4">
        <Step
          n={1}
          title="Who you are"
          status={state.steps.identity}
          blurb="Used as the From name and address, and as the reply-to and unsubscribe contact."
        >
          <Field
            id="from_name"
            label="Full name"
            value={form.FROM_NAME ?? ""}
            onChange={set("FROM_NAME")}
            placeholder="Ada Lovelace"
          />
          <Field
            id="from_email"
            label="Your email address"
            type="email"
            hint="The account you will send from. Replies and unsubscribes come back here."
            value={form.FROM_EMAIL ?? ""}
            onChange={set("FROM_EMAIL")}
            placeholder="you@gmail.com"
          />
        </Step>

        <Step
          n={2}
          title="Mail access"
          status={state.steps.credentials}
          blurb="An app password, not your account password. Gmail requires two-factor authentication first."
        >
          <Field
            id="smtp_pass"
            label="App password"
            type="password"
            hint="Google Account, Security, 2-Step Verification, then App passwords. It looks like 16 letters. Used for both sending and reading bounces."
            value={form.SMTP_PASS ?? ""}
            onChange={set("SMTP_PASS")}
            placeholder="xxxx xxxx xxxx xxxx"
          />
          <p className="text-xs text-muted">
            Using a provider other than Gmail? Set the host in Advanced below.
          </p>
        </Step>

        <Step
          n={3}
          title="Your resume"
          status={state.steps.resume}
          blurb="Attached to every email. Stored locally; it is never uploaded anywhere."
        >
          <label htmlFor="resume" className="text-sm font-medium">
            PDF file
          </label>
          <input
            id="resume"
            type="file"
            accept="application/pdf"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) void onUploadResume(file);
            }}
            className="mt-2 block w-full text-sm file:mr-3 file:rounded-md file:border-0
                       file:bg-accent file:px-3 file:py-2 file:text-white"
          />
          {state.resumeName && (
            <p className="mt-2 text-xs text-positive">Loaded: {state.resumeName}</p>
          )}
        </Step>

        <Step
          n={4}
          title="What you have actually built"
          status={state.steps.aspects}
          blurb="One short phrase per line. The bot may only claim these, so it can never invent experience you do not have."
        >
          <Field
            id="aspects"
            label="Your work, one per line"
            multiline
            hint="Keep each under six words, e.g. 'real-time risk monitoring'. These are matched against what a company does; if none fit, that sentence is left out."
            value={form.ASPECTS ?? ""}
            onChange={set("ASPECTS")}
            placeholder={"computer-vision pipelines\nspeech-driven interfaces\nshipping full-stack features quickly"}
          />
        </Step>

        <Step
          n={5}
          title="Your email"
          status={state.steps.template}
          blurb="The message itself. Placeholders in braces are filled per recipient."
        >
          <Field
            id="template"
            label="Template"
            multiline
            hint="Available: {first_name} {company} {personal_note} {sender_name} {sender_email} {unsubscribe}. The first line must start with 'Subject:'."
            value={form.TEMPLATE ?? ""}
            onChange={set("TEMPLATE")}
          />
        </Step>

        <Step
          n={6}
          title="Optional keys"
          status={state.steps.optional}
          blurb="Both free. Without them the bot still runs, just with less personalisation and slower discovery."
        >
          <Field
            id="groq"
            label="Groq API key"
            type="password"
            hint="console.groq.com/keys. Writes one tailored sentence per email. Without it that sentence is omitted rather than faked."
            value={form.GROQ_API_KEY ?? ""}
            onChange={set("GROQ_API_KEY")}
          />
          <Field
            id="github"
            label="GitHub token"
            type="password"
            hint="github.com/settings/tokens, classic, no scopes ticked. Raises the API limit from 60 to 5000 per hour."
            value={form.GITHUB_TOKEN ?? ""}
            onChange={set("GITHUB_TOKEN")}
          />
        </Step>

        <Step
          n={7}
          title="Targeting and pace"
          status={state.steps.targeting}
          blurb="Start small. A new sending pattern that jumps straight to high volume is what gets accounts filtered."
        >
          <Field
            id="locations"
            label="Cities"
            hint="Comma separated."
            value={form.TARGET_LOCATIONS ?? ""}
            onChange={set("TARGET_LOCATIONS")}
            placeholder="San Francisco, New York, Toronto"
          />
          <Field
            id="limit"
            label="Emails per day"
            type="number"
            hint="Ten is a sensible ceiling for a personal address."
            value={form.DAILY_SEND_LIMIT ?? ""}
            onChange={set("DAILY_SEND_LIMIT")}
            placeholder="10"
          />
        </Step>
      </div>

      <div className="sticky bottom-0 mt-6 flex items-center gap-3 border-t border-border
                      bg-bg/90 py-4 backdrop-blur">
        <button
          type="button"
          onClick={() => void onSave(form)}
          disabled={saving}
          className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-white
                     disabled:opacity-60"
        >
          {saving ? "Saving..." : "Save configuration"}
        </button>
        <p className="text-xs text-muted">
          Written to <code className="font-mono">.env</code> beside the app. Sending stays
          off until you turn it on.
        </p>
      </div>
    </div>
  );
}
