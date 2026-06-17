import { useEffect, useMemo, useState } from "react";

interface EvalGroup {
  id: string;
  task: string;
  configName: string;
  created: string;
  models: string[];
  sampleCount: number;
  status: string;
  scores: Record<string, Record<string, number>>;
}

interface Props {
  selectedId: string | null;
  onSelect: (id: string) => void;
}

function relativeTime(iso: string): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const diff = Date.now() - then;
  const m = Math.floor(diff / 60_000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d ago`;
  return new Date(iso).toLocaleDateString(undefined, {
    month: "short",
    day: "2-digit",
  });
}

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
  if (status === "success") {
    return <span aria-label="success" className="inline-block h-2 w-2 rounded-full bg-sage" />;
  }
  if (status === "error" || status === "failed") {
    return <span aria-label="error" className="inline-block h-2 w-2 rounded-full bg-oxide" />;
  }
  return <span aria-label={status} className="inline-block h-2 w-2 rounded-full border border-ember" />;
}

function bestScore(group: EvalGroup): number | undefined {
  let best: number | undefined;
  for (const model of Object.keys(group.scores ?? {})) {
    const s = group.scores[model] || {};
    const v = s.accuracy ?? s.jury_score;
    if (typeof v === "number" && (best === undefined || v > best)) {
      best = v;
    }
  }
  return best;
}

export default function RunRail({ selectedId, onSelect }: Props) {
  const [groups, setGroups] = useState<EvalGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  useEffect(() => {
    const load = () => {
      fetch("/api/compare/groups")
        .then((res) => {
          if (!res.ok) throw new Error(`Failed to load: ${res.status}`);
          return res.json();
        })
        .then((data) => {
          setGroups(data.groups || []);
          setLoading(false);
        })
        .catch((err) => {
          setError(err.message);
          setLoading(false);
        });
    };
    load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, []);

  const filtered = useMemo(() => {
    if (!search.trim()) return groups;
    const q = search.toLowerCase();
    return groups.filter(
      (g) =>
        g.configName?.toLowerCase().includes(q) ||
        g.task?.toLowerCase().includes(q) ||
        g.models?.some((m) => m.toLowerCase().includes(q)),
    );
  }, [groups, search]);

  const summary = useMemo(() => {
    const running = groups.filter((g) => ["running", "pending"].includes(g.status)).length;
    return { running };
  }, [groups]);

  return (
    <aside className="flex w-96 flex-col border-r border-rule bg-ink-elev">
      <div className="flex items-baseline justify-between border-b border-rule-soft px-5 py-4">
        <p className="eyebrow">Evaluations</p>
        <span className="font-mono text-[10px] tabular-nums text-bone-mute">
          {groups.length.toString().padStart(3, "0")} runs
          {summary.running > 0 && (
            <span className="ml-2 text-ember">
              · {summary.running} active
            </span>
          )}
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
              {groups.length === 0 ? "No runs yet." : "No matches."}
            </span>
            <br />
            {groups.length === 0
              ? "Kick off an evaluation in chat and it will appear here."
              : "Try a different search term."}
          </p>
        ) : (
          <ul>
            {filtered.map((group, idx) => {
              const active = selectedId === group.id;
              const num = (filtered.length - idx).toString().padStart(3, "0");
              const best = bestScore(group);
              const uniqueModels = [...new Set(group.models)];
              const title = group.configName || group.task || "Untitled run";
              return (
                <li key={group.id}>
                  <button
                    onClick={() => onSelect(group.id)}
                    className={`group flex w-full flex-col gap-2 border-b border-rule-soft border-l-2 px-4 py-3.5 text-left transition-colors ${
                      active
                        ? "border-l-ember bg-ink-raised"
                        : "border-l-transparent hover:border-l-rule hover:bg-ink-raised/40"
                    }`}
                  >
                    <div className="flex items-baseline gap-3">
                      <StatusGlyph status={group.status} />
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
                        {title}
                      </span>
                      {typeof best === "number" && (
                        <span
                          className="font-sans text-lg tabular-nums"
                          style={{ color: scoreColor(best) }}
                        >
                          {(best * 100).toFixed(0)}
                          <span className="text-bone-mute text-xs">%</span>
                        </span>
                      )}
                    </div>
                    <div className="flex items-baseline gap-2 font-mono text-[10px] uppercase tracking-eyebrow text-bone-mute">
                      <span className="tabular-nums">
                        {group.sampleCount.toString().padStart(3, "0")} samples
                      </span>
                      <span aria-hidden>·</span>
                      <span>
                        {uniqueModels.length} model{uniqueModels.length === 1 ? "" : "s"}
                      </span>
                      <span aria-hidden>·</span>
                      <span>{relativeTime(group.created)}</span>
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
