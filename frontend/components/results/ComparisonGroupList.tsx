"use client";

import { useEffect, useState } from "react";
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

  let name = rest
    .replace(/^us\.\w+\./, "")
    .replace(/-v\d+:\d+$/, "")
    .replace(/-\d{8}$/, "");

  const provider = providers[prefix] || prefix;
  return `${provider}: ${name}`;
}

function formatDate(dateStr: string): string {
  if (!dateStr) return "";
  const d = new Date(dateStr);
  return d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function ComparisonGroupList() {
  const [groups, setGroups] = useState<EvalGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const router = useRouter();

  useEffect(() => {
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
  }, []);

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-claude-muted">Loading evaluations...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-red-400">Error: {error}</div>
      </div>
    );
  }

  if (groups.length === 0) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-4">
        <div className="text-claude-muted text-lg">No evaluations yet</div>
        <p className="text-claude-muted text-sm">
          Run an evaluation from the chat to see results here.
        </p>
        <button
          onClick={() => router.push("/chat")}
          className="rounded-lg bg-claude-accent px-4 py-2 text-sm font-semibold text-white hover:bg-claude-hover"
        >
          Go to Chat
        </button>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-5xl p-6">
      <h2 className="mb-4 text-xl font-semibold text-claude-text">
        Evaluation Results
      </h2>
      <div className="grid gap-4">
        {groups.map((group) => (
          <button
            key={group.id}
            onClick={() => router.push(`/results?group=${encodeURIComponent(group.id)}`)}
            className="rounded-lg border border-claude-border bg-claude-surface p-4 text-left transition-colors hover:border-claude-accent"
          >
            <div className="flex items-start justify-between">
              <div>
                <h3 className="font-medium text-claude-text">
                  {group.configName || group.task}
                </h3>
                <p className="mt-1 text-sm text-claude-muted">
                  {formatDate(group.created)} &middot; {group.sampleCount} samples
                </p>
              </div>
              <span
                className={`rounded px-2 py-0.5 text-xs font-medium ${
                  group.status === "success"
                    ? "bg-green-900/30 text-green-400"
                    : group.status === "error"
                    ? "bg-red-900/30 text-red-400"
                    : "bg-yellow-900/30 text-yellow-400"
                }`}
              >
                {group.status}
              </span>
            </div>
            <div className="mt-3 flex flex-wrap gap-3">
              {group.models.map((model) => {
                const modelScores = group.scores[model] || {};
                const accuracy = modelScores.accuracy ?? modelScores.jury_score;
                return (
                  <div
                    key={model}
                    className="flex items-center gap-2 rounded bg-claude-bg px-3 py-1.5"
                  >
                    <span className="text-sm text-claude-text">
                      {formatModel(model)}
                    </span>
                    {accuracy !== undefined && (
                      <span
                        className={`text-sm font-medium ${
                          accuracy >= 0.7
                            ? "text-green-400"
                            : accuracy >= 0.4
                            ? "text-yellow-400"
                            : "text-red-400"
                        }`}
                      >
                        {(accuracy * 100).toFixed(0)}%
                      </span>
                    )}
                  </div>
                );
              })}
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}
