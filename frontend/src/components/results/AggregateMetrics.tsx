import { useState } from "react";
import { scorerInfo } from "./scorers";

function formatModelName(model: string): string {
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

  if (prefix === "agent") return `Agent · ${rest}`;

  const name = rest
    .replace(/^us\.\w+\./, "")
    .replace(/-v\d+:\d+$/, "")
    .replace(/-\d{8}$/, "");

  const provider = providers[prefix] || prefix;
  return `${provider} · ${name}`;
}

function getPromptIndex(columnKey: string): number | null {
  if (columnKey.startsWith("eval_")) {
    const sep = columnKey.indexOf("/");
    if (sep !== -1) {
      const num = parseInt(columnKey.slice(5, sep), 10);
      if (!isNaN(num)) return num - 1;
    }
  }
  return null;
}

function getModelFromKey(columnKey: string): string {
  if (columnKey.startsWith("eval_")) {
    const sep = columnKey.indexOf("/");
    if (sep !== -1) return columnKey.slice(sep + 1);
  }
  return columnKey;
}

function formatCriterion(name: string): string {
  return name.replace(/_/g, " ");
}

function scoreColor(score: number): string {
  const s = Math.max(0, Math.min(1, score));
  if (s < 0.5) {
    const t = s * 2;
    const h = 5 + t * 40;
    const sat = 50 + t * 10;
    return `hsl(${h}, ${sat}%, 55%)`;
  }
  const t = (s - 0.5) * 2;
  const h = 45 + t * 30;
  const sat = 60 - t * 15;
  return `hsl(${h}, ${sat}%, 55%)`;
}

interface PipelineStage {
  name: string;
  displayName: string;
  order: number;
  scorerType: "deterministic" | "llm_judge";
  criteria?: string[];
}

interface ModelUsage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost: number | null;
}

interface ModelStats {
  cost?: number;
  latencySeconds?: number;
  total_tokens?: number;
  tokensPerSecond?: number;
  modelUsage?: Record<string, ModelUsage>;
  [key: string]: unknown;
}

interface AggregateMetricsProps {
  models: string[];
  aggregate: Record<
    string,
    {
      overall: number;
      byCriterion: Record<string, number>;
      byStage?: Record<string, number>;
      byScorer?: Record<string, number>;
    }
  >;
  criteria: string[];
  criteriaDescriptions?: Record<string, string>;
  stats: Record<string, ModelStats>;
  sampleCount: number;
  pipeline?: PipelineStage[];
  prompts?: string[];
}


// Distinctive but harmonious swatches — used as accents on model panels.
const MODEL_SWATCHES = ["#d87858", "#9bb556", "#d4a72c", "#9b87b5", "#a39a87"];

export default function AggregateMetrics({
  models,
  aggregate,
  criteria,
  criteriaDescriptions,
  stats,
  sampleCount,
  pipeline,
  prompts,
}: AggregateMetricsProps) {
  const [expandedCriteria, setExpandedCriteria] = useState<Set<string>>(
    new Set(),
  );

  const toggleCriterion = (name: string) => {
    setExpandedCriteria((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  return (
    <div className="mb-6 space-y-6">
      <div
        className="grid gap-px overflow-hidden border border-rule bg-rule"
        style={{ gridTemplateColumns: `repeat(${models.length}, minmax(0, 1fr))` }}
      >
        {models.map((model, i) => {
          const overall = aggregate[model]?.overall ?? 0;
          const swatch = MODEL_SWATCHES[i % MODEL_SWATCHES.length];
          const totalTokens = Number(stats[model]?.total_tokens || 0);
          const tokens =
            sampleCount > 0
              ? Math.round(totalTokens / sampleCount)
              : totalTokens;
          return (
            <div
              key={model}
              className="relative flex flex-col gap-3 bg-ink-elev px-5 py-5"
            >
              <span
                aria-hidden
                className="absolute left-0 top-0 h-full w-0.5"
                style={{ backgroundColor: swatch }}
              />
              <div className="flex items-baseline gap-2">
                <span
                  className="font-mono text-[10px] uppercase tracking-eyebrow text-bone break-words"
                  // Allow the name to wrap on word boundaries AND break
                  // within long hyphen-separated identifiers (e.g.
                  // "claude-sonnet-4-6") so it never gets cut.
                  style={{ overflowWrap: "anywhere", wordBreak: "break-word" }}
                >
                  {pipeline
                    ? "Agent evaluation"
                    : formatModelName(getModelFromKey(model))}
                </span>
                {prompts && getPromptIndex(model) !== null && (
                  <span className="font-mono text-[10px] flex-shrink-0 text-ember">
                    P{getPromptIndex(model)! + 1}
                  </span>
                )}
              </div>
              <div className="flex items-baseline gap-3">
                <span
                  className="font-display text-5xl leading-none tabular-nums"
                  style={{ color: scoreColor(overall) }}
                >
                  {(overall * 100).toFixed(0)}
                </span>
                <span className="font-mono text-xs uppercase tracking-eyebrow text-bone-mute">
                  / 100
                </span>
              </div>
              {(() => {
                // Make the scorer methodology explicit in three cases:
                //   - jury only: skip (it's the project default, headline
                //     reflects criteria-based jury scoring already).
                //   - single non-jury (e.g. f1 alone): inline subtitle
                //     under the headline — "F1 · token overlap" — so the
                //     reader knows what the big number represents.
                //   - multi-scorer composition: chip row breaking down
                //     each scorer's mean.
                const byScorer = aggregate[model]?.byScorer;
                if (!byScorer) return null;
                const entries = Object.entries(byScorer);
                if (entries.length === 0) return null;
                const isJuryOnly =
                  entries.length === 1 && entries[0][0] === "jury_scorer";
                if (isJuryOnly) return null;
                if (entries.length === 1) {
                  const [name] = entries[0];
                  const info = scorerInfo(name);
                  return (
                    <p
                      className="font-mono text-[11px] text-bone-mute"
                      title={info.description}
                    >
                      <span className="uppercase tracking-eyebrow text-bone">
                        {info.label}
                      </span>{" "}
                      · {info.short}
                    </p>
                  );
                }
                return (
                  <div className="border-t border-rule-soft pt-3">
                    <p className="eyebrow mb-2">Scorers</p>
                    <div className="flex flex-col gap-1.5">
                      {entries.map(([name, value]) => {
                        const info = scorerInfo(name);
                        return (
                          <div
                            key={name}
                            className="flex items-baseline gap-2 border bg-ink px-2 py-1"
                            style={{ borderColor: scoreColor(value) }}
                            title={info.description}
                          >
                            <span className="font-mono text-[10px] uppercase tracking-eyebrow text-bone">
                              {info.label}
                            </span>
                            <span className="font-mono text-[10px] text-bone-mute">
                              {info.short}
                            </span>
                            <span
                              className="ml-auto font-sans text-xs font-medium tabular-nums"
                              style={{ color: scoreColor(value) }}
                            >
                              {(value * 100).toFixed(0)}%
                            </span>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                );
              })()}
              <dl className="mt-1 grid grid-cols-2 gap-x-4 gap-y-1.5 border-t border-rule-soft pt-3">
                {stats[model]?.cost != null && (
                  <Row label="Cost" value={`$${Number(stats[model].cost).toFixed(4)}`} />
                )}
                {stats[model]?.latencySeconds != null && (
                  <Row
                    label="Latency"
                    value={`${Number(stats[model].latencySeconds).toFixed(1)}s`}
                  />
                )}
                {tokens > 0 && (
                  <Row label="Avg tokens" value={tokens.toLocaleString()} />
                )}
                {stats[model]?.tokensPerSecond != null && (
                  <Row
                    label="Speed"
                    value={`${Number(stats[model].tokensPerSecond).toFixed(0)} tok/s`}
                  />
                )}
              </dl>
              {pipeline && stats[model]?.modelUsage && (
                <div className="mt-1 border-t border-rule-soft pt-3">
                  <p className="eyebrow mb-1.5">Models used</p>
                  {Object.entries(stats[model].modelUsage!).map(
                    ([modelName, usage]) => (
                      <div
                        key={modelName}
                        className="flex items-baseline justify-between gap-3 py-0.5 text-[11px]"
                      >
                        <span
                          className="text-bone break-words"
                          style={{ overflowWrap: "anywhere", wordBreak: "break-word" }}
                        >
                          {formatModelName(modelName)}
                        </span>
                        <span className="ml-2 whitespace-nowrap font-mono text-bone-mute tabular-nums">
                          {usage.total_tokens?.toLocaleString()} tok
                          {usage.cost != null &&
                            ` · $${usage.cost.toFixed(4)}`}
                        </span>
                      </div>
                    ),
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {pipeline && pipeline.length > 0 && (
        <div className="border border-rule bg-ink-elev px-5 py-4">
          <p className="eyebrow mb-3">Pipeline stages</p>
          <div className="flex items-center gap-2 overflow-x-auto pb-1">
            {pipeline
              .sort((a, b) => a.order - b.order)
              .map((stage, i) => {
                const passRate =
                  aggregate[models[0]]?.byStage?.[stage.name] ?? 0;
                const color = scoreColor(passRate);
                return (
                  <div
                    key={stage.name}
                    className="flex flex-shrink-0 items-center gap-2"
                  >
                    {i > 0 && (
                      <span className="font-mono text-bone-mute">→</span>
                    )}
                    <div
                      className="border bg-ink px-3 py-2"
                      style={{ borderColor: color }}
                    >
                      <div className="font-mono text-[10px] uppercase tracking-eyebrow text-bone">
                        {stage.displayName}
                      </div>
                      <div className="mt-1 flex items-baseline gap-2">
                        <span
                          className="font-sans text-lg font-medium tabular-nums"
                          style={{ color }}
                        >
                          {(passRate * 100).toFixed(0)}
                          <span className="text-bone-mute">%</span>
                        </span>
                        <span className="font-mono text-[10px] uppercase tracking-eyebrow text-bone-mute">
                          {stage.scorerType === "deterministic"
                            ? "auto"
                            : "judge"}
                        </span>
                      </div>
                    </div>
                  </div>
                );
              })}
          </div>
        </div>
      )}

      {pipeline && pipeline.length > 0 ? (
        <div className="space-y-3">
          {pipeline
            .sort((a, b) => a.order - b.order)
            .map((stage) => {
              const stagePassRate =
                aggregate[models[0]]?.byStage?.[stage.name] ?? 0;
              const stageColor = scoreColor(stagePassRate);
              return (
                <div
                  key={stage.name}
                  className="border border-rule bg-ink-elev px-5 py-4"
                >
                  <div className="mb-3 flex items-baseline justify-between">
                    <div className="flex items-baseline gap-3">
                      <span className="font-display text-lg text-bone">
                        {stage.displayName}
                      </span>
                      <span className="font-mono text-[10px] uppercase tracking-eyebrow text-bone-mute">
                        {stage.scorerType === "deterministic"
                          ? "auto"
                          : "judge"}
                      </span>
                    </div>
                    <span
                      className="font-sans text-base font-medium tabular-nums"
                      style={{ color: stageColor }}
                    >
                      {(stagePassRate * 100).toFixed(0)}%
                    </span>
                  </div>
                  {stage.scorerType === "deterministic" ? (
                    <p className="text-xs text-bone-dim">
                      Checks whether the correct tools were called for each sample.
                    </p>
                  ) : (
                    <table className="w-full">
                      <tbody>
                        {(stage.criteria && stage.criteria.length > 0
                          ? stage.criteria
                          : criteria
                        ).map((criterion) => {
                          const value =
                            aggregate[models[0]]?.byCriterion?.[criterion] ?? 0;
                          const color = scoreColor(value);
                          const description = criteriaDescriptions?.[criterion];
                          const isExpanded = expandedCriteria.has(criterion);
                          return (
                            <tr
                              key={criterion}
                              className="border-t border-rule-soft"
                            >
                              <td className="py-2">
                                <button
                                  className={`flex items-center gap-1.5 text-left text-[13px] capitalize text-bone ${
                                    description ? "cursor-pointer hover:text-ember" : ""
                                  }`}
                                  onClick={() =>
                                    description && toggleCriterion(criterion)
                                  }
                                >
                                  {description && (
                                    <span className="select-none font-mono text-[10px] text-bone-mute">
                                      {isExpanded ? "▾" : "▸"}
                                    </span>
                                  )}
                                  {formatCriterion(criterion)}
                                </button>
                                {isExpanded && description && (
                                  <div className="ml-4 mt-1 text-xs text-bone-dim">
                                    {description}
                                  </div>
                                )}
                              </td>
                              <td className="py-2 text-right align-top">
                                <span
                                  className="font-sans text-sm font-medium tabular-nums"
                                  style={{ color }}
                                >
                                  {(value * 100).toFixed(0)}%
                                </span>
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
        <div className="border border-rule bg-ink-elev px-5 py-4">
          <p className="eyebrow mb-3">Per-criterion comparison</p>
          <table className="w-full">
            <thead>
              <tr>
                <th className="pb-2 text-left">
                  <span className="eyebrow">Criterion</span>
                </th>
                {models.map((model, i) => (
                  <th key={model} className="pb-2 text-right">
                    <span className="inline-flex items-center justify-end gap-1.5">
                      <span
                        className="inline-block h-2 w-2 rounded-full"
                        style={{
                          backgroundColor:
                            MODEL_SWATCHES[i % MODEL_SWATCHES.length],
                        }}
                      />
                      <span
                        className="font-mono text-[10px] uppercase tracking-eyebrow text-bone break-words"
                        style={{ overflowWrap: "anywhere", wordBreak: "break-word" }}
                      >
                        {formatModelName(getModelFromKey(model))}
                      </span>
                      {getPromptIndex(model) !== null && (
                        <span className="font-mono text-[10px] text-ember">
                          P{getPromptIndex(model)! + 1}
                        </span>
                      )}
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
                  <tr
                    key={criterion}
                    className="border-t border-rule-soft"
                  >
                    <td className="py-2">
                      <button
                        className={`flex items-center gap-1.5 text-left text-[13px] capitalize text-bone ${
                          description ? "cursor-pointer hover:text-ember" : ""
                        }`}
                        onClick={() =>
                          description && toggleCriterion(criterion)
                        }
                      >
                        {description && (
                          <span className="select-none font-mono text-[10px] text-bone-mute">
                            {isExpanded ? "▾" : "▸"}
                          </span>
                        )}
                        {formatCriterion(criterion)}
                      </button>
                      {isExpanded && description && (
                        <div className="ml-4 mt-1 text-xs text-bone-dim">
                          {description}
                        </div>
                      )}
                    </td>
                    {models.map((model) => {
                      const value =
                        aggregate[model]?.byCriterion?.[criterion] ?? 0;
                      return (
                        <td key={model} className="py-2 text-right">
                          <span
                            className="font-sans text-sm font-medium tabular-nums"
                            style={{ color: scoreColor(value) }}
                          >
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

function Row({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="font-mono text-[10px] uppercase tracking-eyebrow text-bone-mute">
        {label}
      </dt>
      <dd className="text-right font-sans text-xs font-medium tabular-nums text-bone">
        {value}
      </dd>
    </>
  );
}
