"use client";

import { useEffect, useState } from "react";
import AggregateMetrics from "./AggregateMetrics";
import ComparisonGrid from "./ComparisonGrid";
import SampleDetailPanel from "./SampleDetailPanel";

interface CriteriaResult {
  name: string;
  votes_for: number;
  total: number;
  // Old logs emit `passed` (majority-vote boolean); new logs emit `score`
  // (raw judge fraction). Readers should prefer `score` when present and
  // fall back to `votes_for/total` or `passed` for backwards compatibility.
  score?: number;
  passed?: boolean;
  note?: string;
}

interface StageResult {
  passed: boolean;
  explanation?: string;
  stage_order?: number;
  tools_called?: string[];
  tools_expected?: string[];
  criteriaResults?: CriteriaResult[];
}

interface SampleResult {
  passed: boolean;
  score: number;
  output: string;
  explanation?: string;
  criteriaResults?: CriteriaResult[];
  // When the eval ran more than one scorer (e.g. ["jury", "f1"]), each
  // scorer's per-sample value lands here keyed by scorer name. The
  // primary `score` above keeps backward compat (jury_scorer preferred);
  // this dict is the full picture.
  scoresByScorer?: Record<string, number>;
  stages?: Record<string, StageResult>;
}

interface Sample {
  id: string;
  input: string;
  target: string;
  results: Record<string, SampleResult>;
}

interface PipelineStage {
  name: string;
  displayName: string;
  order: number;
  scorerType: "deterministic" | "llm_judge";
  criteria?: string[];
}

interface ComparisonData {
  groupId: string;
  task: string;
  models: string[];
  criteria: string[];
  criteriaDescriptions: Record<string, string>;
  aggregate: Record<
    string,
    {
      overall: number;
      byCriterion: Record<string, number>;
      byStage?: Record<string, number>;
      byScorer?: Record<string, number>;
    }
  >;
  samples: Sample[];
  stats: Record<string, Record<string, unknown>>;
  pipeline?: PipelineStage[];
  prompts?: string[];
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
        <span className="eyebrow">
          Loading comparison
          <span className="cursor-block ml-2 align-baseline" />
        </span>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="border border-oxide bg-ink-elev px-6 py-4">
          <p className="eyebrow text-oxide">Read error</p>
          <p className="mt-2 font-mono text-sm text-bone-dim">
            {error || "No data returned for this evaluation."}
          </p>
        </div>
      </div>
    );
  }

  const selectedSample = selectedCell
    ? data.samples.find((s) => s.id === selectedCell.sampleId)
    : null;

  return (
    <div className="flex h-full">
      <div
        className={`flex-1 overflow-y-auto overflow-x-hidden p-6 ${selectedCell ? "pr-0" : ""}`}
      >
        <AggregateMetrics
          models={data.models}
          aggregate={data.aggregate}
          criteria={data.criteria}
          criteriaDescriptions={data.criteriaDescriptions}
          stats={data.stats}
          sampleCount={data.samples.length}
          pipeline={data.pipeline}
          prompts={data.prompts}
        />
        {data.prompts && data.prompts.length > 0 && (
          <details className="group mb-4 border border-rule bg-ink-elev">
            <summary className="flex cursor-pointer list-none select-none items-center gap-2 px-3 py-2 eyebrow">
              <span className="font-mono text-[10px] transition-transform group-open:rotate-90">
                ▶
              </span>
              {data.prompts.length === 1
                ? "Prompt template — click to expand"
                : `Prompt templates (${data.prompts.length}) — click to expand`}
            </summary>
            <div className="space-y-2 border-t border-rule-soft px-3 pb-3 pt-2">
              {data.prompts.map((prompt, i) => (
                <div key={i} className="flex items-start gap-3">
                  <span className="mt-0.5 flex-shrink-0 font-mono text-xs text-ember">
                    P{i + 1}
                  </span>
                  <span className="whitespace-pre-wrap break-all font-mono text-[11px] text-bone">
                    {prompt}
                  </span>
                </div>
              ))}
            </div>
          </details>
        )}
        <ComparisonGrid
          models={data.models}
          samples={data.samples}
          prompts={data.prompts}
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

export type { CriteriaResult, StageResult, SampleResult, Sample, ComparisonData, PipelineStage, SelectedCell };
