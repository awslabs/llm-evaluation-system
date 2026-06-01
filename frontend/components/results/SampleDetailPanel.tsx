"use client";

import { useState } from "react";
import type { Sample } from "./ComparisonView";
import { scorerInfo } from "./scorers";

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
  const name = rest
    .replace(/^us\.\w+\./, "")
    .replace(/-v\d+:\d+$/, "")
    .replace(/-\d{8}$/, "");
  const provider = providers[prefix] || prefix;
  return `${provider} · ${name}`;
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
  const sections = parseExplanation(explanation);
  // Per-sample scorer breakdown. Surfaced whenever the sample carries
  // non-jury scorers — either composed alongside the jury (e.g.
  // ["jury", "f1"]) or run alone (["f1"]). For jury-only samples we
  // hide it since the rubric breakdown already covers that case.
  const scorersByName = result.scoresByScorer || {};
  const scorerEntries = Object.entries(scorersByName);
  const isJuryOnly =
    scorerEntries.length === 1 && scorerEntries[0][0] === "jury_scorer";
  const showPerScorerSection = scorerEntries.length > 0 && !isJuryOnly;
  // "Rubric score" implies criteria-based jury grading. Use a neutral
  // label when the primary score comes from a non-jury scorer so
  // readers don't think "F1 = 75%" is a rubric pass-rate.
  const headlineLabel =
    scorerEntries.length === 0 ||
    scorerEntries.some(([name]) => name === "jury_scorer")
      ? "Rubric score"
      : "Sample score";

  return (
    <aside className="w-[420px] flex-shrink-0 overflow-y-auto border-l border-rule bg-ink-elev">
      <div className="sticky top-0 z-10 flex items-baseline justify-between gap-3 border-b border-rule bg-ink-elev px-5 py-4">
        <div className="min-w-0">
          <p className="eyebrow">Sample № {sample.id}</p>
          <p className="mt-1 truncate font-mono text-[12px] text-bone-dim">
            {formatModel(model)}
          </p>
        </div>
        <button
          onClick={onClose}
          aria-label="Close detail panel"
          className="flex h-7 w-7 flex-shrink-0 items-center justify-center border border-rule text-bone-dim transition-colors hover:border-bone-mute hover:text-bone"
        >
          <svg
            className="h-3.5 w-3.5"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={1.6}
              d="M6 18L18 6M6 6l12 12"
            />
          </svg>
        </button>
      </div>

      <div className="space-y-6 px-5 py-5">
        <div>
          <p className="eyebrow">{headlineLabel}</p>
          <div className="mt-2 flex items-baseline gap-3">
            <span
              className="font-display text-6xl leading-none tabular-nums"
              style={{ color: scoreColor(result.score) }}
            >
              {(result.score * 100).toFixed(0)}
            </span>
            <span className="font-mono text-sm text-bone-mute">/ 100</span>
          </div>
          <div
            className="mt-3 h-1 w-full bg-rule-soft"
            aria-hidden
          >
            <div
              className="h-full transition-all"
              style={{
                width: `${Math.max(0, Math.min(1, result.score)) * 100}%`,
                backgroundColor: scoreColor(result.score),
              }}
            />
          </div>
        </div>

        {showPerScorerSection && (
          <div>
            <p className="eyebrow mb-3">Per scorer</p>
            <dl className="border-y border-rule-soft">
              {scorerEntries.map(([name, value]) => {
                const info = scorerInfo(name);
                return (
                  <div
                    key={name}
                    className="border-t border-rule-soft py-2.5 first:border-t-0"
                  >
                    <div className="flex items-baseline justify-between gap-3">
                      <dt className="text-[13px] text-bone">{info.label}</dt>
                      <dd>
                        <span
                          className="font-sans text-sm font-medium tabular-nums"
                          style={{ color: scoreColor(value) }}
                        >
                          {(value * 100).toFixed(0)}%
                        </span>
                      </dd>
                    </div>
                    <p className="mt-1 text-xs text-bone-dim">
                      {info.description}
                    </p>
                  </div>
                );
              })}
            </dl>
          </div>
        )}

        {criteriaResults.length > 0 && (
          <div>
            <p className="eyebrow mb-3">Per-criterion</p>
            <dl className="border-y border-rule-soft">
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
                    className="flex items-baseline justify-between gap-3 border-t border-rule-soft py-2.5 first:border-t-0"
                  >
                    <dt className="text-[13px] capitalize text-bone">
                      {cr.name.replace(/_/g, " ")}
                    </dt>
                    <dd className="flex items-baseline gap-2">
                      <span
                        className="font-sans text-sm font-medium tabular-nums"
                        style={{ color: scoreColor(critScore) }}
                      >
                        {(critScore * 100).toFixed(0)}%
                      </span>
                      <span className="font-mono text-[10px] tabular-nums text-bone-mute">
                        {cr.votes_for}/{cr.total} judges
                      </span>
                    </dd>
                  </div>
                );
              })}
            </dl>
          </div>
        )}

        {sections.judges.length > 0 && (
          <div>
            <p className="eyebrow mb-3">Individual judges</p>
            <div className="space-y-2">
              {sections.judges.map((judge, i) => (
                <pre
                  key={i}
                  className="whitespace-pre-wrap break-words border-l border-ember-deep bg-ink-raised px-3 py-2 font-mono text-[11px] leading-relaxed text-bone-dim"
                >
                  {judge}
                </pre>
              ))}
            </div>
          </div>
        )}

        <ExpandableSection title="Question" content={sample.input} />
        {sample.retrievalContext && sample.retrievalContext.length > 0 && (
          <RetrievedContextSection chunks={sample.retrievalContext} />
        )}
        <ExpandableSection title="Expected answer" content={sample.target} />
        <ExpandableSection title="Model response" content={result.output} />
      </div>
    </aside>
  );
}

function RetrievedContextSection({ chunks }: { chunks: string[] }) {
  const [expanded, setExpanded] = useState<Record<number, boolean>>({});
  return (
    <div>
      <p className="eyebrow mb-2">
        Retrieved context{" "}
        <span className="ml-1 font-mono text-[10px] text-bone-mute">
          ({chunks.length} chunk{chunks.length === 1 ? "" : "s"}, retriever rank)
        </span>
      </p>
      <ol className="space-y-2">
        {chunks.map((chunk, i) => {
          const isLong = chunk.length > 240;
          const isOpen = expanded[i] || !isLong;
          const visible = isOpen
            ? chunk
            : chunk.slice(0, chunk.lastIndexOf(" ", 240)) + "…";
          return (
            <li
              key={i}
              className="border-l border-rule bg-ink-raised/40 px-3 py-2"
            >
              <div className="flex items-baseline gap-2">
                <span className="flex-shrink-0 font-mono text-[10px] uppercase tracking-eyebrow text-ember">
                  chunk {i + 1}
                </span>
                <span className="whitespace-pre-wrap text-[13px] leading-relaxed text-bone">
                  {visible}
                </span>
              </div>
              {isLong && (
                <button
                  onClick={() =>
                    setExpanded((s) => ({ ...s, [i]: !s[i] }))
                  }
                  className="mt-1.5 font-mono text-[10px] uppercase tracking-eyebrow text-ember hover:text-ember-deep"
                >
                  {isOpen ? "Show less ▴" : "Show more ▾"}
                </button>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function ExpandableSection({
  title,
  content,
}: {
  title: string;
  content: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const isLong = content.length > 200;
  const truncated = isLong
    ? content.slice(0, content.lastIndexOf(" ", 200)) + "…"
    : content;

  return (
    <div>
      <p className="eyebrow mb-2">{title}</p>
      <div className="whitespace-pre-wrap border-l border-rule bg-ink-raised/40 px-3 py-2.5 text-[13px] leading-relaxed text-bone">
        {isLong && !expanded ? truncated : content}
      </div>
      {isLong && (
        <button
          onClick={() => setExpanded(!expanded)}
          className="mt-1.5 font-mono text-[10px] uppercase tracking-eyebrow text-ember hover:text-ember-deep"
        >
          {expanded ? "Show less ▴" : "Show more ▾"}
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
