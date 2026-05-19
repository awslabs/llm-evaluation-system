"use client";

import { useEffect, useMemo, useState } from "react";

interface OptimizationRow {
  id: string;
  dataset: string;
  judge: string;
  providers: string[];
  winner_iter: number | null;
  winner_test_score: number | null;
  iterations_run: number;
  status: string;
  created_at: number;
}

interface Props {
  selectedId: string | null;
  onSelect: (id: string) => void;
}

function relativeTime(ms: number): string {
  if (!ms) return "";
  const diff = Date.now() - ms;
  const m = Math.floor(diff / 60_000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d ago`;
  return new Date(ms).toLocaleDateString(undefined, { month: "short", day: "2-digit" });
}

// Same color ramp as RunRail.tsx so the visual language stays consistent
// between the Results and Optimizations tabs.
function scoreColor(score: number): string {
  const s = Math.max(0, Math.min(1, score));
  if (s < 0.5) {
    const t = s * 2;
    const h = 5 + t * 40;
    const sat = 50 + t * 10;
    const lt = 53 + t * 2;
    return `hsl(${h}, ${sat}%, ${lt}%)`;
  }
  const t = (s - 0.5) * 2;
  const h = 45 + t * 30;
  const sat = 60 - t * 15;
  return `hsl(${h}, ${sat}%, 55%)`;
}

function StatusGlyph({ status }: { status: string }) {
  if (status === "complete" || status === "converged" || status === "converged_initial") {
    return <span aria-label={status} className="inline-block h-2 w-2 rounded-full bg-sage" />;
  }
  if (status?.startsWith("error") || status?.startsWith("partial")) {
    return <span aria-label={status} className="inline-block h-2 w-2 rounded-full bg-oxide" />;
  }
  return <span aria-label={status} className="inline-block h-2 w-2 rounded-full border border-ember" />;
}

export default function OptimizationRail({ selectedId, onSelect }: Props) {
  const [rows, setRows] = useState<OptimizationRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  useEffect(() => {
    const load = () => {
      fetch("/api/optimizations/list")
        .then((res) => {
          if (!res.ok) throw new Error(`Failed to load: ${res.status}`);
          return res.json();
        })
        .then((data) => {
          setRows(data.optimizations || []);
          setLoading(false);
        })
        .catch((err) => {
          setError(err.message);
          setLoading(false);
        });
    };
    load();
    // Same 10s poll cadence as the Results rail — optimizations can
    // take minutes, so users will glance at the list periodically.
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, []);

  const filtered = useMemo(() => {
    if (!search.trim()) return rows;
    const q = search.toLowerCase();
    return rows.filter(
      (r) =>
        r.dataset?.toLowerCase().includes(q) ||
        r.judge?.toLowerCase().includes(q) ||
        r.id?.toLowerCase().includes(q),
    );
  }, [rows, search]);

  return (
    <aside className="flex w-96 flex-col border-r border-rule bg-ink-elev">
      <div className="flex items-baseline justify-between border-b border-rule-soft px-5 py-4">
        <p className="eyebrow">Optimizations</p>
        <span className="font-mono text-[10px] tabular-nums text-bone-mute">
          {rows.length.toString().padStart(3, "0")} runs
        </span>
      </div>

      <div className="border-b border-rule-soft px-5 py-3">
        <input
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search runs…"
          className="w-full border-b border-rule bg-transparent py-1.5 font-mono text-[12px] text-bone placeholder:text-bone-mute focus:border-bone-mute focus:outline-none"
        />
      </div>

      <div className="flex-1 overflow-y-auto">
        {loading ? (
          <p className="px-5 py-4 eyebrow">
            Reading
            <span className="cursor-block ml-2 align-baseline" />
          </p>
        ) : error ? (
          <div className="px-5 py-4">
            <p className="eyebrow text-oxide">Read error</p>
            <p className="mt-2 font-mono text-[11px] text-bone-dim">{error}</p>
          </div>
        ) : filtered.length === 0 ? (
          <p className="px-5 py-6 text-sm italic leading-relaxed text-bone-mute">
            <span className="font-display not-italic text-bone">
              {rows.length === 0 ? "No runs yet." : "No matches."}
            </span>
            <br />
            {rows.length === 0
              ? "Ask the chat agent to optimize_prompt for a dataset; the run will appear here."
              : "Try a different search term."}
          </p>
        ) : (
          <ul>
            {filtered.map((r, idx) => {
              const active = selectedId === r.id;
              const num = (filtered.length - idx).toString().padStart(3, "0");
              const score = r.winner_test_score;
              return (
                <li key={r.id}>
                  <button
                    onClick={() => onSelect(r.id)}
                    className={`group flex w-full flex-col gap-2 border-b border-rule-soft border-l-2 px-4 py-3.5 text-left transition-colors ${
                      active
                        ? "border-l-ember bg-ink-raised"
                        : "border-l-transparent hover:border-l-rule hover:bg-ink-raised/40"
                    }`}
                  >
                    <div className="flex items-baseline gap-3">
                      <StatusGlyph status={r.status} />
                      <span
                        className={`font-mono text-[10px] tabular-nums ${
                          active ? "text-ember" : "text-bone-mute"
                        }`}
                      >
                        {num}
                      </span>
                      <span
                        className={`min-w-0 flex-1 truncate text-[14px] leading-snug ${
                          active ? "text-bone" : "text-bone-dim"
                        }`}
                      >
                        {r.dataset || "Untitled"}
                      </span>
                      {typeof score === "number" && (
                        <span
                          className="font-sans text-lg tabular-nums"
                          style={{ color: scoreColor(score) }}
                        >
                          {(score * 100).toFixed(0)}
                          <span className="text-bone-mute text-xs">%</span>
                        </span>
                      )}
                    </div>
                    <div className="flex items-baseline gap-2 font-mono text-[10px] uppercase tracking-eyebrow text-bone-mute">
                      <span className="tabular-nums">
                        {r.iterations_run.toString().padStart(2, "0")} iters
                      </span>
                      <span aria-hidden>·</span>
                      <span>winner #{r.winner_iter ?? "—"}</span>
                      <span aria-hidden>·</span>
                      <span>{relativeTime(r.created_at)}</span>
                    </div>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </aside>
  );
}
