import { useState } from "react";

/** Nested navigation.
 *
 *  Accessibility notes that are easy to get wrong here:
 *  - the tree is a real <nav> with a list structure, so a screen reader
 *    announces item counts and nesting depth;
 *  - each group toggle carries aria-expanded and aria-controls, and the
 *    submenu it owns is the element that id points at;
 *  - the current page is marked aria-current="page", not just coloured.
 */

export interface NavItem {
  id: string;
  label: string;
  badge?: number;
  children?: NavItem[];
}

interface Props {
  items: NavItem[];
  activeId: string;
  onSelect: (id: string) => void;
  open: boolean;
  onClose: () => void;
}

function Chevron({ expanded }: { expanded: boolean }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 20 20"
      className={`h-4 w-4 shrink-0 transition-transform ${expanded ? "rotate-90" : ""}`}
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
    >
      <path d="M7 4l6 6-6 6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function Leaf({
  item,
  active,
  onSelect,
  nested,
}: {
  item: NavItem;
  active: boolean;
  onSelect: (id: string) => void;
  nested?: boolean;
}) {
  return (
    <li>
      <button
        type="button"
        onClick={() => onSelect(item.id)}
        aria-current={active ? "page" : undefined}
        className={[
          "flex w-full items-center justify-between rounded-md px-3 py-2 text-left text-sm",
          nested ? "pl-9" : "",
          active
            ? "bg-accent/10 font-medium text-accent"
            : "text-muted hover:bg-raised hover:text-text",
        ].join(" ")}
      >
        <span className="truncate">{item.label}</span>
        {item.badge !== undefined && (
          <span
            className="ml-2 rounded-full bg-raised px-2 py-0.5 text-xs tabular-nums text-muted"
            aria-label={`${item.badge} items`}
          >
            {item.badge}
          </span>
        )}
      </button>
    </li>
  );
}

function Group({
  item,
  activeId,
  onSelect,
}: {
  item: NavItem;
  activeId: string;
  onSelect: (id: string) => void;
}) {
  const holdsActive = item.children!.some((c) => c.id === activeId);
  const [expanded, setExpanded] = useState(holdsActive);
  const panelId = `nav-group-${item.id}`;

  return (
    <li>
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        aria-controls={panelId}
        className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm
                   text-muted hover:bg-raised hover:text-text"
      >
        <Chevron expanded={expanded} />
        <span className="flex-1 truncate">{item.label}</span>
      </button>
      <ul id={panelId} hidden={!expanded} className="mt-0.5 space-y-0.5">
        {item.children!.map((child) => (
          <Leaf
            key={child.id}
            item={child}
            active={child.id === activeId}
            onSelect={onSelect}
            nested
          />
        ))}
      </ul>
    </li>
  );
}

export default function Sidebar({ items, activeId, onSelect, open, onClose }: Props) {
  return (
    <>
      {/* Scrim only exists on small screens, where the sidebar overlays. */}
      {open && (
        <div
          className="fixed inset-0 z-20 bg-black/40 lg:hidden"
          onClick={onClose}
          aria-hidden="true"
        />
      )}
      <nav
        aria-label="Main"
        className={[
          "fixed inset-y-0 left-0 z-30 w-64 shrink-0 overflow-y-auto border-r border-border",
          "bg-surface px-3 py-4 transition-transform lg:static lg:translate-x-0",
          open ? "translate-x-0" : "-translate-x-full",
        ].join(" ")}
      >
        <div className="mb-5 flex items-center gap-2 px-2">
          <span
            aria-hidden="true"
            className="grid h-8 w-8 place-items-center rounded-lg bg-accent text-sm
                       font-bold text-white"
          >
            M
          </span>
          <div className="leading-tight">
            <p className="text-sm font-semibold">Mailbot</p>
            <p className="text-xs text-muted">outreach console</p>
          </div>
        </div>

        <ul className="space-y-0.5">
          {items.map((item) =>
            item.children?.length ? (
              <Group key={item.id} item={item} activeId={activeId} onSelect={onSelect} />
            ) : (
              <Leaf
                key={item.id}
                item={item}
                active={item.id === activeId}
                onSelect={onSelect}
              />
            ),
          )}
        </ul>
      </nav>
    </>
  );
}
