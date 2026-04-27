"use client";

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
  return (
    <div className="overflow-x-auto rounded-lg border border-claude-border">
      <table className="w-full border-collapse">
        <thead>
          <tr className="bg-claude-surface">
            <th className="sticky left-0 z-10 bg-claude-surface border-b border-r border-claude-border px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-claude-muted">
              Sample
            </th>
            {models.map((model) => (
              <th
                key={model}
                className="border-b border-claude-border px-4 py-3 text-center text-xs font-medium uppercase tracking-wider text-claude-muted"
              >
                {formatModel(model)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {samples.map((sample, idx) => (
            <tr
              key={sample.id}
              className={idx % 2 === 0 ? "bg-claude-bg" : "bg-claude-surface/30"}
            >
              <td className="sticky left-0 z-10 border-r border-claude-border px-4 py-3 text-sm text-claude-text max-w-xs truncate"
                style={{ backgroundColor: idx % 2 === 0 ? "#1a1a1a" : "#232323" }}
                title={sample.input}
              >
                <span className="text-claude-muted text-xs mr-2">#{sample.id}</span>
                {sample.input.slice(0, 80)}
                {sample.input.length > 80 && "..."}
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
                      className="border-claude-border px-4 py-3 text-center text-sm text-claude-muted"
                    >
                      &mdash;
                    </td>
                  );
                }
                return (
                  <td
                    key={model}
                    onClick={() => onCellClick(sample.id, model)}
                    className={`cursor-pointer border-claude-border px-4 py-3 text-center transition-colors ${
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
                      <span className={result.passed ? "text-green-400" : "text-red-400"}>
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
          ))}
        </tbody>
      </table>
    </div>
  );
}
