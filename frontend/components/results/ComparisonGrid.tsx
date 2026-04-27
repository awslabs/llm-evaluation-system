"use client";

import { useState } from "react";
import type { Sample, SelectedCell } from "./ComparisonView";

function formatModel(model: string): string {
  const parts = model.split("/");
  return parts[parts.length - 1];
}

interface ComparisonGridProps {
  models: string[];
  samples: Sample[];
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
            {models.map((model) => (
              <th
                key={model}
                className="border-b border-claude-border px-4 py-3 text-center text-xs font-medium uppercase tracking-wider text-claude-muted min-w-[100px]"
              >
                {formatModel(model).split(".").pop()}
              </th>
            ))}
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
                      } ${
                        result.passed
                          ? "bg-green-950/20"
                          : "bg-red-950/20"
                      }`}
                    >
                      <div className="flex items-center justify-center gap-1.5">
                        <span className={`text-lg ${result.passed ? "text-green-400" : "text-red-400"}`}>
                          {result.passed ? "✓" : "✗"}
                        </span>
                        <span className="text-xs text-claude-muted">
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
