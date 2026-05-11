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

  if (prefix === "agent") return `Agent: ${rest}`;

  let name = rest
    .replace(/^us\.\w+\./, "")
    .replace(/-v\d+:\d+$/, "")
    .replace(/-\d{8}$/, "");

  const provider = providers[prefix] || prefix;
  return `${provider}: ${name}`;
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

// Continuous red -> yellow -> green gradient for a 0-1 score.
function scoreColor(score: number): string {
  const clamped = Math.max(0, Math.min(1, score));
  const hue = Math.round(clamped * 120);
  return `hsl(${hue}, 70%, 55%)`;
}

function scoreBgColor(score: number): string {
  const clamped = Math.max(0, Math.min(1, score));
  const hue = Math.round(clamped * 120);
  // Low saturation + very low lightness so tinted cells stay legible on dark bg.
  return `hsla(${hue}, 55%, 30%, 0.25)`;
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
  prompts,
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
    <div className="overflow-x-auto rounded-lg border border-claude-border">
      <table className="w-full border-collapse">
        <thead>
          <tr className="bg-claude-surface">
            <th className="sticky left-0 z-10 bg-claude-surface border-b border-r border-claude-border px-4 py-3 text-left w-[350px] max-w-[350px]">
              <span className="text-xs font-medium uppercase tracking-wider text-claude-muted">
                Question / Expected Answer
              </span>
              <span className="ml-2 text-xs font-normal normal-case text-claude-muted">
                ({samples.length} samples)
              </span>
            </th>
            {models.map((model) => {
              const promptIdx = getPromptIndex(model);
              const modelName = getModelFromKey(model);
              return (
                <th
                  key={model}
                  className="border-b border-claude-border px-4 py-3 text-center min-w-[100px]"
                >
                  <div className="text-xs font-medium text-claude-text">
                    {formatModelName(modelName)}
                  </div>
                  {promptIdx !== null && (
                    <div className="mt-1 text-[10px] font-normal normal-case text-claude-accent">
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
            return (
              <tr
                key={sample.id}
                className={idx % 2 === 0 ? "bg-claude-bg" : "bg-claude-surface/30"}
              >
                <td
                  className="sticky left-0 z-10 border-r border-claude-border px-4 py-3 text-sm text-claude-text align-top w-[350px] max-w-[350px]"
                  style={{ backgroundColor: idx % 2 === 0 ? "#1a1a1a" : "#232323" }}
                >
                  <div
                    className="cursor-pointer"
                    onClick={() => toggleRow(sample.id)}
                  >
                    <div className="flex items-start gap-2">
                      <span className="mt-0.5 text-claude-muted text-xs select-none flex-shrink-0">
                        {isExpanded ? "▼" : "▶"}
                      </span>
                      <div className="min-w-0 overflow-hidden">
                        <div className={`text-claude-text ${isExpanded ? "" : "truncate"}`}>
                          {sample.input}
                        </div>
                        {isExpanded && (
                          <div className="mt-2 rounded bg-claude-surface/50 p-2 text-xs text-claude-muted">
                            <span className="font-medium">Expected: </span>
                            {sample.target}
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
                        className="border-claude-border px-4 py-3 text-center text-sm text-claude-muted align-top"
                      >
                        &mdash;
                      </td>
                    );
                  }
                  return (
                    <td
                      key={model}
                      onClick={() => onCellClick(sample.id, model)}
                      className={`cursor-pointer border-claude-border px-4 py-3 text-center align-top transition-colors ${
                        isSelected
                          ? "ring-2 ring-inset ring-claude-accent"
                          : "hover:bg-claude-surface"
                      }`}
                      style={{ backgroundColor: scoreBgColor(result.score) }}
                    >
                      <div className="flex items-center justify-center">
                        <span className="text-sm font-medium" style={{ color: scoreColor(result.score) }}>
                          {(result.score * 100).toFixed(0)}%
                        </span>
                      </div>
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
