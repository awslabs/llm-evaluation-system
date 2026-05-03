"use client";

import { useState } from "react";

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

function formatCriterion(name: string): string {
  return name.replace(/_/g, " ");
}

interface PipelineStage {
  name: string;
  displayName: string;
  order: number;
  scorerType: "deterministic" | "llm_judge";
  criteria?: string[];
}

interface AggregateMetricsProps {
  models: string[];
  aggregate: Record<string, { overall: number; byCriterion: Record<string, number>; byStage?: Record<string, number> }>;
  criteria: string[];
  criteriaDescriptions?: Record<string, string>;
  stats: Record<string, Record<string, unknown>>;
  sampleCount: number;
  pipeline?: PipelineStage[];
}

const MODEL_COLORS = [
  { bar: "bg-indigo-500", text: "text-indigo-400" },
  { bar: "bg-amber-500", text: "text-amber-400" },
  { bar: "bg-emerald-500", text: "text-emerald-400" },
  { bar: "bg-rose-500", text: "text-rose-400" },
];

export default function AggregateMetrics({
  models,
  aggregate,
  criteria,
  criteriaDescriptions,
  stats,
  sampleCount,
  pipeline,
}: AggregateMetricsProps) {
  const [expandedCriteria, setExpandedCriteria] = useState<Set<string>>(new Set());

  const toggleCriterion = (name: string) => {
    setExpandedCriteria((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  return (
    <div className="mb-6 space-y-4">
      {/* Model overall scores */}
      <div className="grid gap-4" style={{ gridTemplateColumns: `repeat(${models.length}, 1fr)` }}>
        {models.map((model, i) => {
          const overall = aggregate[model]?.overall ?? 0;
          const color = MODEL_COLORS[i % MODEL_COLORS.length];
          const totalTokens = Number(stats[model]?.total_tokens || 0);
          const tokens = sampleCount > 0 ? Math.round(totalTokens / sampleCount) : totalTokens;
          return (
            <div key={model} className="rounded-lg border border-claude-border bg-claude-surface p-4">
              <div className="flex items-center gap-2">
                <div className={`h-3 w-3 rounded-full ${color.bar}`} />
                <span className="text-sm font-medium text-claude-text truncate">
                  {pipeline ? "Agent Evaluation" : formatModel(model)}
                </span>
              </div>
              <div className="mt-2 flex items-end gap-2">
                <span className={`text-3xl font-bold ${overall >= 0.7 ? "text-green-400" : overall >= 0.4 ? "text-yellow-400" : "text-red-400"}`}>
                  {(overall * 100).toFixed(0)}%
                </span>
                <span className="mb-1 text-xs text-claude-muted">score</span>
              </div>
              <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-claude-muted">
                {stats[model]?.cost != null && (
                  <>
                    <span>Total cost</span>
                    <span className="text-claude-text font-medium">
                      ${Number(stats[model].cost).toFixed(4)}
                    </span>
                  </>
                )}
                {stats[model]?.latencySeconds != null && (
                  <>
                    <span>Avg latency</span>
                    <span className="text-claude-text font-medium">
                      {Number(stats[model].latencySeconds).toFixed(1)}s
                    </span>
                  </>
                )}
                {tokens > 0 && (
                  <>
                    <span>Avg tokens</span>
                    <span className="text-claude-text font-medium">
                      {tokens.toLocaleString()}
                    </span>
                  </>
                )}
                {stats[model]?.tokensPerSecond != null && (
                  <>
                    <span>Speed</span>
                    <span className="text-claude-text font-medium">
                      {Number(stats[model].tokensPerSecond).toFixed(0)} tok/s
                    </span>
                  </>
                )}
              </div>
              {/* Per-model usage breakdown for pipeline evals */}
              {pipeline && (stats[model] as Record<string, unknown>)?.modelUsage && (
                <div className="mt-3 border-t border-claude-border/50 pt-2">
                  <div className="text-xs text-claude-muted mb-1">Models used</div>
                  {Object.entries((stats[model] as Record<string, unknown>).modelUsage as Record<string, Record<string, number>>).map(([modelName, usage]) => (
                    <div key={modelName} className="flex justify-between text-xs py-0.5">
                      <span className="text-claude-text truncate">{formatModel(modelName)}</span>
                      <span className="text-claude-muted ml-2 whitespace-nowrap">
                        {usage.total_tokens?.toLocaleString()} tok {usage.cost != null && `· $${usage.cost.toFixed(4)}`}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Pipeline stages overview */}
      {pipeline && pipeline.length > 0 && (
        <div className="rounded-lg border border-claude-border bg-claude-surface p-4">
          <div className="mb-3 text-xs font-medium uppercase tracking-wider text-claude-muted">
            Pipeline Stages
          </div>
          <div className="flex items-center gap-2 overflow-x-auto">
            {pipeline.sort((a, b) => a.order - b.order).map((stage, i) => {
              const passRate = aggregate[models[0]]?.byStage?.[stage.name] ?? 0;
              const color = passRate >= 0.7 ? "border-green-500 text-green-400" : passRate >= 0.4 ? "border-yellow-500 text-yellow-400" : "border-red-500 text-red-400";
              return (
                <div key={stage.name} className="flex items-center gap-2">
                  {i > 0 && <span className="text-claude-muted">→</span>}
                  <div className={`rounded-md border px-3 py-2 ${color}`}>
                    <div className="text-sm font-medium">{stage.displayName}</div>
                    <div className="flex items-center gap-2 mt-0.5">
                      <span className="text-lg font-bold">{(passRate * 100).toFixed(0)}%</span>
                      <span className="text-xs text-claude-muted">{stage.scorerType === "deterministic" ? "auto" : "judge"}</span>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Per-stage criteria (pipeline mode) */}
      {pipeline && pipeline.length > 0 ? (
        <div className="space-y-3">
          {pipeline.sort((a, b) => a.order - b.order).map((stage) => {
            const stagePassRate = aggregate[models[0]]?.byStage?.[stage.name] ?? 0;
            const stageColor = stagePassRate >= 0.7 ? "text-green-400" : stagePassRate >= 0.4 ? "text-yellow-400" : "text-red-400";
            return (
              <div key={stage.name} className="rounded-lg border border-claude-border bg-claude-surface p-4">
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-claude-text">{stage.displayName}</span>
                    <span className="text-xs px-1.5 py-0.5 rounded bg-claude-border/50 text-claude-muted">
                      {stage.scorerType === "deterministic" ? "auto" : "judge"}
                    </span>
                  </div>
                  <span className={`text-sm font-bold ${stageColor}`}>
                    {(stagePassRate * 100).toFixed(0)}%
                  </span>
                </div>
                {stage.scorerType === "deterministic" ? (
                  <div className="text-xs text-claude-muted">
                    Checks if the correct tools were called for each sample
                  </div>
                ) : (
                  <table className="w-full mt-2">
                    <tbody>
                      {(stage.criteria && stage.criteria.length > 0 ? stage.criteria : criteria).map((criterion) => {
                        const value = aggregate[models[0]]?.byCriterion?.[criterion] ?? 0;
                        const color = value >= 0.7 ? "text-green-400" : value >= 0.4 ? "text-yellow-400" : "text-red-400";
                        return (
                          <tr key={criterion} className="border-t border-claude-border/30">
                            <td className="py-1.5">
                              <span className="text-sm text-claude-text capitalize">{formatCriterion(criterion)}</span>
                            </td>
                            <td className="py-1.5 text-right">
                              <span className={`text-sm font-medium ${color}`}>{(value * 100).toFixed(0)}%</span>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                )}
              </div>
            );
          })}
        </div>
      ) : criteria.length > 0 ? (
        <div className="rounded-lg border border-claude-border bg-claude-surface p-4">
          <table className="w-full">
            <thead>
              <tr>
                <th className="pb-2 text-left text-xs font-medium uppercase tracking-wider text-claude-muted">
                  Criterion
                </th>
                {models.map((model, i) => (
                  <th key={model} className="pb-2 text-right text-xs font-medium uppercase tracking-wider text-claude-muted">
                    <span className="flex items-center justify-end gap-1.5">
                      <span className="truncate">{formatModel(model)}</span>
                      <span className={`inline-block h-2 w-2 rounded-full ${MODEL_COLORS[i % MODEL_COLORS.length].bar}`} />
                    </span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {criteria.map((criterion) => {
                const isExpanded = expandedCriteria.has(criterion);
                const description = criteriaDescriptions?.[criterion];
                return (
                  <tr key={criterion} className="border-t border-claude-border/50">
                    <td className="py-2">
                      <div
                        className={`text-sm capitalize text-claude-text ${description ? "cursor-pointer hover:text-claude-accent" : ""}`}
                        onClick={() => description && toggleCriterion(criterion)}
                      >
                        <span className="flex items-center gap-1.5">
                          {description && (
                            <span className="text-xs text-claude-muted select-none">
                              {isExpanded ? "▼" : "▶"}
                            </span>
                          )}
                          {formatCriterion(criterion)}
                        </span>
                      </div>
                      {isExpanded && description && (
                        <div className="mt-1 ml-4 text-xs text-claude-muted">
                          {description}
                        </div>
                      )}
                    </td>
                    {models.map((model) => {
                      const value = aggregate[model]?.byCriterion?.[criterion] ?? 0;
                      return (
                        <td key={model} className="py-2 text-right">
                          <span className={`text-sm font-medium ${value >= 0.7 ? "text-green-400" : value >= 0.4 ? "text-yellow-400" : "text-red-400"}`}>
                            {(value * 100).toFixed(0)}%
                          </span>
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}
