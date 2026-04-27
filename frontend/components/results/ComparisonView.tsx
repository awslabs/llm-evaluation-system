"use client";

import { useEffect, useState } from "react";
import AggregateMetrics from "./AggregateMetrics";
import ComparisonGrid from "./ComparisonGrid";
import SampleDetailPanel from "./SampleDetailPanel";

interface CriteriaResult {
  name: string;
  votes_for: number;
  total: number;
  passed: boolean;
  note?: string;
}

interface SampleResult {
  passed: boolean;
  score: number;
  output: string;
  explanation?: string;
  criteriaResults?: CriteriaResult[];
}

interface Sample {
  id: string;
  input: string;
  target: string;
  results: Record<string, SampleResult>;
}

interface ComparisonData {
  groupId: string;
  task: string;
  models: string[];
  criteria: string[];
  aggregate: Record<string, { overall: number; byCriterion: Record<string, number> }>;
  samples: Sample[];
  stats: Record<string, Record<string, unknown>>;
}

interface SelectedCell {
  sampleId: string;
  model: string;
}

export default function ComparisonView({ groupId }: { groupId: string }) {
  const [data, setData] = useState<ComparisonData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedCell, setSelectedCell] = useState<SelectedCell | null>(null);

  useEffect(() => {
    fetch(`/api/compare/detail?group_id=${encodeURIComponent(groupId)}`)
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load: ${res.status}`);
        return res.json();
      })
      .then((d) => {
        setData(d);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message);
        setLoading(false);
      });
  }, [groupId]);

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-claude-muted">Loading comparison data...</div>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-red-400">Error: {error || "No data"}</div>
      </div>
    );
  }

  const selectedSample = selectedCell
    ? data.samples.find((s) => s.id === selectedCell.sampleId)
    : null;

  return (
    <div className="flex h-full">
      <div className={`flex-1 overflow-auto p-6 ${selectedCell ? "pr-0" : ""}`}>
        <AggregateMetrics
          models={data.models}
          aggregate={data.aggregate}
          criteria={data.criteria}
          stats={data.stats}
        />
        <ComparisonGrid
          models={data.models}
          samples={data.samples}
          selectedCell={selectedCell}
          onCellClick={(sampleId, model) =>
            setSelectedCell(
              selectedCell?.sampleId === sampleId && selectedCell?.model === model
                ? null
                : { sampleId, model }
            )
          }
        />
      </div>
      {selectedCell && selectedSample && (
        <SampleDetailPanel
          sample={selectedSample}
          model={selectedCell.model}
          onClose={() => setSelectedCell(null)}
        />
      )}
    </div>
  );
}

export type { CriteriaResult, SampleResult, Sample, ComparisonData, SelectedCell };
