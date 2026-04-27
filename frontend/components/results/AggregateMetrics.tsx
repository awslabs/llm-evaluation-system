"use client";

function formatModel(model: string): string {
  const parts = model.split("/");
  return parts[parts.length - 1];
}

interface AggregateMetricsProps {
  models: string[];
  aggregate: Record<string, { overall: number; byCriterion: Record<string, number> }>;
  criteria: string[];
  stats: Record<string, Record<string, unknown>>;
}

export default function AggregateMetrics({
  models,
  aggregate,
  criteria,
  stats,
}: AggregateMetricsProps) {
  return (
    <div className="mb-6 rounded-lg border border-claude-border bg-claude-surface p-4">
      {/* Overall scores */}
      <div className="mb-4 flex flex-wrap gap-6">
        {models.map((model) => {
          const overall = aggregate[model]?.overall ?? 0;
          return (
            <div key={model} className="flex items-center gap-3">
              <span className="text-sm font-medium text-claude-text">
                {formatModel(model)}
              </span>
              <div className="flex items-center gap-2">
                <div className="h-2 w-24 overflow-hidden rounded-full bg-claude-bg">
                  <div
                    className={`h-full rounded-full transition-all ${
                      overall >= 0.7
                        ? "bg-green-500"
                        : overall >= 0.4
                        ? "bg-yellow-500"
                        : "bg-red-500"
                    }`}
                    style={{ width: `${overall * 100}%` }}
                  />
                </div>
                <span
                  className={`text-sm font-semibold ${
                    overall >= 0.7
                      ? "text-green-400"
                      : overall >= 0.4
                      ? "text-yellow-400"
                      : "text-red-400"
                  }`}
                >
                  {(overall * 100).toFixed(0)}%
                </span>
              </div>
              {/* Token usage */}
              {stats[model] && (
                <span className="text-xs text-claude-muted">
                  {Number(stats[model].total_tokens || 0).toLocaleString()} tokens
                </span>
              )}
            </div>
          );
        })}
      </div>

      {/* Per-criterion breakdown */}
      {criteria.length > 0 && (
        <div className="border-t border-claude-border pt-3">
          <div className="grid gap-2">
            {criteria.map((criterion) => (
              <div key={criterion} className="flex items-center gap-3">
                <span className="w-28 text-xs text-claude-muted capitalize">
                  {criterion}
                </span>
                <div className="flex flex-1 items-center gap-4">
                  {models.map((model) => {
                    const value = aggregate[model]?.byCriterion?.[criterion] ?? 0;
                    return (
                      <div key={model} className="flex items-center gap-1.5">
                        <div className="h-1.5 w-16 overflow-hidden rounded-full bg-claude-bg">
                          <div
                            className={`h-full rounded-full ${
                              value >= 0.7
                                ? "bg-green-500"
                                : value >= 0.4
                                ? "bg-yellow-500"
                                : "bg-red-500"
                            }`}
                            style={{ width: `${value * 100}%` }}
                          />
                        </div>
                        <span className="text-xs text-claude-muted">
                          {(value * 100).toFixed(0)}%
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
