"use client";

import { useState } from "react";
import type { Sample, SelectedCell } from "./ComparisonView";

function formatModelName(model: string): string {
  const providers: Record<string, string> = {
    bedrock: "Bedrock",
    openai: "OpenAI",
    anthropic: "Anthropic",
    google: "Google",
    groq: "Groq",
    mistral: "Mistral",
    azure: "Azure",
    agent: "Agent",
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

function scoreBg(score: number): string {
  const s = Math.max(0, Math.min(1, score));
  if (s < 0.5) {
    const t = s * 2;
    const h = 5 + t * 40;
    return `hsla(${h}, 45%, 32%, 0.18)`;
  }
  const t = (s - 0.5) * 2;
  const h = 45 + t * 30;
  return `hsla(${h}, 35%, 30%, 0.16)`;
}

function getModelFromKey(columnKey: string): string {
  if (columnKey.startsWith("eval_")) {
    const sep = columnKey.indexOf("/");
    if (sep !== -1) return columnKey.slice(sep + 1);
  }
  return columnKey;
}

interface ComparisonGridProps {
  models: string[];
  samples: Sample[];
  prompts?: string[];
  selectedCell: SelectedCell | null;
  onCellClick: (sampleId: string, model: string) => void;
}

export default function ComparisonGrid({
  models,
  samples,
  selectedCell,
  onCellClick,
}: ComparisonGridProps) {
  const [expandedRows, setExpandedRows] = useState<Set<string>>(new Set());

  const toggleRow = (id: string) => {
    setExpandedRows((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <div className="w-full overflow-x-auto border border-rule bg-ink-elev">
      <table className="border-collapse">
        <thead>
          <tr>
            <th
              className="sticky left-0 z-10 w-[360px] max-w-[360px] border-b border-rule bg-ink-elev px-4 py-3 text-left"
              scope="col"
            >
              <div className="flex items-baseline justify-between gap-3">
                <span className="eyebrow">Sample · expected</span>
                <span className="font-mono text-[10px] tabular-nums text-bone-mute">
                  n={samples.length}
                </span>
              </div>
            </th>
            {models.map((model) => {
              const promptIdx = getPromptIndex(model);
              const modelName = getModelFromKey(model);
              return (
                <th
                  key={model}
                  scope="col"
                  className="min-w-[140px] max-w-[200px] border-b border-l border-rule-soft bg-ink-elev px-3 py-3 text-center align-bottom"
                >
                  <div
                    className="font-mono text-[10px] uppercase tracking-eyebrow text-bone break-words"
                    style={{ overflowWrap: "anywhere", wordBreak: "break-word" }}
                  >
                    {formatModelName(modelName)}
                  </div>
                  {promptIdx !== null && (
                    <div className="mt-1 font-mono text-[10px] text-ember">
                      P{promptIdx + 1}
                    </div>
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {samples.map((sample, idx) => {
            const isExpanded = expandedRows.has(sample.id);
            const rowBg = idx % 2 === 0 ? "bg-ink" : "bg-ink-elev";
            const rowBgInline = idx % 2 === 0 ? "#0c0a08" : "#15120e";
            return (
              <tr key={sample.id} className={rowBg}>
                <td
                  className="sticky left-0 z-10 w-[360px] max-w-[360px] border-b border-rule-soft px-4 py-3 align-top text-sm text-bone"
                  style={{ backgroundColor: rowBgInline }}
                >
                  <div
                    className="cursor-pointer"
                    onClick={() => toggleRow(sample.id)}
                  >
                    <div className="flex items-start gap-3">
                      <span className="mt-0.5 flex-shrink-0 select-none font-mono text-[10px] tabular-nums text-bone-mute">
                        {(idx + 1).toString().padStart(3, "0")}
                        <span className="ml-1 text-bone-mute">
                          {isExpanded ? "▾" : "▸"}
                        </span>
                      </span>
                      <div className="min-w-0 flex-1 overflow-hidden">
                        <div
                          className={`text-[0.875rem] leading-snug text-bone ${
                            isExpanded ? "" : "truncate"
                          }`}
                        >
                          {sample.input}
                        </div>
                        {isExpanded && (
                          <div className="mt-2 border-l border-rule-soft pl-3 text-xs leading-relaxed text-bone-dim">
                            <span className="eyebrow mr-2">Expected</span>
                            <span className="break-words">{sample.target}</span>
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                </td>
                {models.map((model) => {
                  const result = sample.results[model];
                  const isSelected =
                    selectedCell?.sampleId === sample.id &&
                    selectedCell?.model === model;
                  if (!result) {
                    return (
                      <td
                        key={model}
                        className="border-b border-l border-rule-soft px-3 py-3 text-center align-middle font-mono text-sm text-bone-mute"
                      >
                        ·
                      </td>
                    );
                  }
                  return (
                    <td
                      key={model}
                      onClick={() => onCellClick(sample.id, model)}
                      className={`cursor-pointer border-b border-l border-rule-soft px-3 py-3 text-center align-middle transition-colors ${
                        isSelected ? "ring-1 ring-inset ring-ember" : ""
                      }`}
                      style={{
                        backgroundColor: isSelected
                          ? undefined
                          : scoreBg(result.score),
                      }}
                    >
                      <span
                        className="font-sans text-sm font-medium tabular-nums"
                        style={{ color: scoreColor(result.score) }}
                      >
                        {(result.score * 100).toFixed(0)}
                        <span className="text-bone-mute">%</span>
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
  );
}
