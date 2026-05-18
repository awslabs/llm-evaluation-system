"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

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

function formatModel(model: string): string {
  const providers: Record<string, string> = {
    bedrock: "Bedrock",
    openai: "OpenAI",
    anthropic: "Anthropic",
    google: "Google",
    groq: "Groq",
    mistral: "Mistral",
    azure: "Azure",
  };
  const slashIdx = model.indexOf("/");
  if (slashIdx === -1) return model;
  const prefix = model.slice(0, slashIdx);
  const rest = model.slice(slashIdx + 1);
  const name = rest
    .replace(/^us\.\w+\./, "")
    .replace(/-v\d+:\d+$/, "")
    .replace(/-\d{8}$/, "");
  const provider = providers[prefix] || prefix;
  return `${provider}: ${name}`;
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
    return (
      <span
        aria-label="success"
        className="inline-block h-2 w-2 rounded-full bg-sage"
      />
    );
  }
  if (status === "error" || status === "failed") {
    return (
      <span
        aria-label="error"
        className="inline-block h-2 w-2 rounded-full bg-oxide"
      />
    );
  }
  return (
    <span
      aria-label={status}
      className="inline-block h-2 w-2 rounded-full border border-ember"
    />
  );
}

export default function ComparisonGroupList() {
  const [groups, setGroups] = useState<EvalGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const router = useRouter();

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
    const interval = setInterval(load, 10000);
    return () => clearInterval(interval);
  }, []);

  const summary = useMemo(() => {
    const running = groups.filter((g) =>
      ["running", "pending"].includes(g.status),
    ).length;
    const lastCompleted = groups
      .filter((g) => g.status === "success")
      .map((g) => g.created)
      .sort()
      .pop();
    return { running, lastCompleted };
  }, [groups]);

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <span className="eyebrow">
          Reading archive
          <span className="cursor-block ml-2 align-baseline" />
        </span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="border border-oxide bg-ink-elev px-6 py-4">
          <p className="eyebrow text-oxide">Read error</p>
          <p className="mt-2 font-mono text-sm text-bone-dim">{error}</p>
        </div>
      </div>
    );
  }

  if (groups.length === 0) {
    return (
      <div className="mx-auto max-w-3xl px-6 pt-20">
        <p className="eyebrow">Evaluation index</p>
        <h2 className="font-display mt-3 text-5xl leading-tight text-bone">
          <em className="text-ember">No runs</em> on record yet.
        </h2>
        <p className="mt-5 max-w-md text-sm leading-relaxed text-bone-dim">
          Open a chat session and ask the instrument to compare some models, or
          drop a CSV of test cases to begin recording.
        </p>
        <button
          onClick={() => router.push("/chat")}
          className="mt-8 inline-flex items-center gap-3 border border-bone px-5 py-2.5 font-mono text-[11px] uppercase tracking-eyebrow transition-colors hover:bg-bone hover:text-ink"
        >
          Open conversational driver
          <span>→</span>
        </button>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-6xl px-6 py-12">
      <div className="reveal flex items-end justify-between border-b border-rule pb-6">
        <div>
          <p className="eyebrow">Evaluation index</p>
          <h1 className="font-display mt-2 text-5xl leading-none text-bone">
            {groups.length}{" "}
            <span className="text-bone-mute">
              {groups.length === 1 ? "run" : "runs"}
            </span>
          </h1>
        </div>
        <dl className="flex items-end gap-8 text-right">
          <div>
            <dt className="eyebrow">Active</dt>
            <dd className="mt-1 font-sans text-2xl tabular-nums text-bone">
              {summary.running.toString().padStart(2, "0")}
            </dd>
          </div>
          <div>
            <dt className="eyebrow">Last completed</dt>
            <dd className="mt-1 font-sans text-2xl tabular-nums text-bone">
              {summary.lastCompleted ? relativeTime(summary.lastCompleted) : "—"}
            </dd>
          </div>
        </dl>
      </div>

      <ul className="reveal stagger-1">
        {groups.map((group, idx) => {
          const num = (groups.length - idx).toString().padStart(3, "0");
          const uniqueModels = [...new Set(group.models)];
          return (
            <li key={group.id}>
              <button
                onClick={() =>
                  router.push(`/results?group=${encodeURIComponent(group.id)}`)
                }
                className="group grid w-full grid-cols-12 items-baseline gap-6 border-b border-rule-soft px-1 py-5 text-left transition-colors hover:bg-ink-elev"
              >
                <div className="col-span-1 flex items-center gap-3">
                  <StatusGlyph status={group.status} />
                  <span className="font-mono text-[11px] tabular-nums text-bone-mute">
                    {num}
                  </span>
                </div>

                <div className="col-span-6">
                  <h3 className="font-display text-xl leading-tight text-bone transition-colors group-hover:text-ember">
                    {group.configName || group.task}
                  </h3>
                  <p className="mt-1 font-mono text-[10px] uppercase tracking-eyebrow text-bone-mute">
                    {group.sampleCount} samples · {group.task} ·{" "}
                    {relativeTime(group.created)}
                  </p>
                </div>

                <div className="col-span-5 flex flex-wrap justify-end gap-x-5 gap-y-2">
                  {uniqueModels.slice(0, 4).map((model) => {
                    const modelScores = group.scores[model] || {};
                    const accuracy =
                      modelScores.accuracy ?? modelScores.jury_score;
                    return (
                      <div key={model} className="flex items-baseline gap-2">
                        <span className="font-mono text-[10px] uppercase tracking-eyebrow text-bone-dim">
                          {formatModel(model)}
                        </span>
                        {accuracy !== undefined && (
                          <span
                            className="font-sans text-base font-medium tabular-nums"
                            style={{ color: scoreColor(accuracy) }}
                          >
                            {(accuracy * 100).toFixed(0)}
                            <span className="text-bone-mute">%</span>
                          </span>
                        )}
                      </div>
                    );
                  })}
                  {uniqueModels.length > 4 && (
                    <span className="font-mono text-[10px] uppercase tracking-eyebrow text-bone-mute">
                      +{uniqueModels.length - 4} more
                    </span>
                  )}
                </div>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
