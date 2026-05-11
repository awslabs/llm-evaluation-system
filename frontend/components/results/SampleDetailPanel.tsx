"use client";

import { useState } from "react";
import type { Sample } from "./ComparisonView";

// Continuous red -> yellow -> green gradient for a 0-1 score.
function scoreColor(score: number): string {
  const clamped = Math.max(0, Math.min(1, score));
  const hue = Math.round(clamped * 120);
  return `hsl(${hue}, 70%, 55%)`;
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

interface SampleDetailPanelProps {
  sample: Sample;
  model: string;
  onClose: () => void;
}

export default function SampleDetailPanel({
  sample,
  model,
  onClose,
}: SampleDetailPanelProps) {
  const result = sample.results[model];
  if (!result) return null;

  const criteriaResults = result.criteriaResults || [];
  const explanation = result.explanation || "";

  // Parse judge details from explanation text
  const judgeLines = explanation
    .split("\n")
    .filter((l) => l.startsWith("  ") && l.includes(":"))
    .filter((l) => !l.startsWith("  Errors"));

  // Split explanation into sections
  const sections = parseExplanation(explanation);

  return (
    <div className="w-96 flex-shrink-0 overflow-y-auto border-l border-claude-border bg-claude-surface">
      {/* Header */}
      <div className="sticky top-0 z-10 flex items-center justify-between border-b border-claude-border bg-claude-surface px-4 py-3">
        <div>
          <h3 className="text-sm font-medium text-claude-text">
            Sample #{sample.id}
          </h3>
          <p className="text-xs text-claude-muted">{formatModel(model)}</p>
        </div>
        <button
          onClick={onClose}
          className="rounded p-1 text-claude-muted hover:bg-claude-bg hover:text-claude-text"
        >
          <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>

      <div className="p-4 space-y-4">
        {/* Overall score */}
        <div
          className="rounded-lg p-3 border"
          style={{
            borderColor: scoreColor(result.score),
            backgroundColor: `hsla(${Math.round(result.score * 120)}, 55%, 30%, 0.20)`,
          }}
        >
          <div className="flex items-baseline gap-2">
            <span className="text-2xl font-bold" style={{ color: scoreColor(result.score) }}>
              {(result.score * 100).toFixed(0)}%
            </span>
            <span className="text-sm text-claude-muted">rubric score</span>
          </div>
        </div>

        {/* Criteria breakdown */}
        {criteriaResults.length > 0 && (
          <div>
            <h4 className="mb-2 text-xs font-medium uppercase tracking-wider text-claude-muted">
              Per-Criterion Scores
            </h4>
            <div className="space-y-2">
              {criteriaResults.map((cr) => {
                const critScore =
                  typeof (cr as { score?: number }).score === "number"
                    ? (cr as { score: number }).score
                    : cr.total > 0
                      ? cr.votes_for / cr.total
                      : cr.passed
                        ? 1
                        : 0;
                return (
                  <div
                    key={cr.name}
                    className="flex items-center justify-between rounded bg-claude-bg px-3 py-2"
                  >
                    <span className="text-sm capitalize text-claude-text">
                      {cr.name}
                    </span>
                    <div className="flex items-center gap-2">
                      <span
                        className="text-xs font-medium"
                        style={{ color: scoreColor(critScore) }}
                      >
                        {(critScore * 100).toFixed(0)}%
                      </span>
                      <span className="text-xs text-claude-muted">
                        ({cr.votes_for}/{cr.total} judges)
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* Judge details */}
        {sections.judges.length > 0 && (
          <div>
            <h4 className="mb-2 text-xs font-medium uppercase tracking-wider text-claude-muted">
              Individual Judges
            </h4>
            <div className="space-y-2">
              {sections.judges.map((judge, i) => (
                <div
                  key={i}
                  className="rounded bg-claude-bg px-3 py-2 text-xs text-claude-muted"
                >
                  <pre className="whitespace-pre-wrap font-mono">{judge}</pre>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Question */}
        <ExpandableSection title="Question" content={sample.input} />

        {/* Expected answer */}
        <ExpandableSection title="Expected Answer" content={sample.target} />

        {/* Model response */}
        <ExpandableSection title="Model Response" content={result.output} />

      </div>
    </div>
  );
}

function ExpandableSection({ title, content }: { title: string; content: string }) {
  const [expanded, setExpanded] = useState(false);
  const isLong = content.length > 200;

  const truncated = isLong ? content.slice(0, content.lastIndexOf(" ", 200)) + "..." : content;

  return (
    <div>
      <h4 className="mb-1 text-xs font-medium uppercase tracking-wider text-claude-muted">
        {title}
      </h4>
      <div className="rounded bg-claude-bg p-3 text-sm text-claude-text whitespace-pre-wrap">
        {isLong && !expanded ? truncated : content}
      </div>
      {isLong && (
        <button
          onClick={() => setExpanded(!expanded)}
          className="mt-1 text-xs text-claude-accent hover:text-claude-hover"
        >
          {expanded ? "▲ Show less" : "▼ Show more"}
        </button>
      )}
    </div>
  );
}

function parseExplanation(explanation: string): { judges: string[] } {
  const lines = explanation.split("\n");
  const judges: string[] = [];
  let inJudges = false;
  let current = "";

  for (const line of lines) {
    if (line.trim() === "Judges:") {
      inJudges = true;
      continue;
    }
    if (line.trim() === "Errors:" || line.trim() === "") {
      if (inJudges && current) {
        judges.push(current.trim());
        current = "";
      }
      if (line.trim() === "Errors:") break;
      continue;
    }
    if (inJudges && line.startsWith("  ")) {
      if (current) judges.push(current.trim());
      current = line.trim();
    } else if (inJudges) {
      current += " " + line.trim();
    }
  }
  if (current) judges.push(current.trim());

  return { judges };
}
