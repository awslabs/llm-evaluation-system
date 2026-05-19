"use client";

import { useEffect, useState } from "react";

interface IterationRecord {
  iter: number;
  prompt: string;
  train_pass_rate: number;
  n_train_samples: number;
}

interface OptimizationRecord {
  id: string;
  dataset: string;
  judge: string;
  providers: string[];
  initial_prompt: string;
  winner_prompt: string;
  winner_iter: number;
  winner_test_score: number;
  history: IterationRecord[];
  test_scores_by_iter: Record<string, number>;
  rationales: Record<string, string>;
  train_size: number;
  test_size: number;
  status: string;
  created_at: number;
  max_iterations: number;
  sample_size: number;
}

interface Props {
  optimizationId: string;
}

function fmtPct(n: number | undefined | null): string {
  if (typeof n !== "number" || Number.isNaN(n)) return "—";
  return `${(n * 100).toFixed(0)}%`;
}

// Inline SVG chart: train pass rate per iteration as a polyline, with the
// winner's test score drawn as a horizontal reference line. Kept inline so
// we don't pull in a chart library for one view.
function ScoreChart({ record }: { record: OptimizationRecord }) {
  const W = 720;
  const H = 220;
  const PAD = { top: 20, right: 24, bottom: 36, left: 44 };

  const history = record.history || [];
  if (history.length === 0) return null;

  const iters = history.map((h) => h.iter);
  const trainRates = history.map((h) => h.train_pass_rate);
  const testRates = history
    .map((h) => record.test_scores_by_iter?.[String(h.iter)])
    .filter((v): v is number => typeof v === "number");

  const xMin = Math.min(...iters);
  const xMax = Math.max(...iters, xMin + 1);
  const yMin = 0;
  const yMax = 1;

  const sx = (x: number) =>
    PAD.left + ((x - xMin) / (xMax - xMin || 1)) * (W - PAD.left - PAD.right);
  const sy = (y: number) =>
    H - PAD.bottom - ((y - yMin) / (yMax - yMin)) * (H - PAD.top - PAD.bottom);

  const trainPath = history
    .map((h, i) => `${i === 0 ? "M" : "L"} ${sx(h.iter)} ${sy(h.train_pass_rate)}`)
    .join(" ");

  const winnerTestY = sy(record.winner_test_score ?? 0);

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      className="w-full max-w-3xl"
      preserveAspectRatio="xMidYMid meet"
    >
      {/* Y-axis gridlines at 0/0.5/1 */}
      {[0, 0.5, 1].map((y) => (
        <g key={y}>
          <line
            x1={PAD.left}
            y1={sy(y)}
            x2={W - PAD.right}
            y2={sy(y)}
            stroke="currentColor"
            strokeOpacity="0.15"
            strokeDasharray="2 4"
          />
          <text
            x={PAD.left - 8}
            y={sy(y) + 4}
            textAnchor="end"
            fontSize="10"
            fill="currentColor"
            opacity="0.5"
          >
            {(y * 100).toFixed(0)}%
          </text>
        </g>
      ))}

      {/* X-axis: one tick per iteration */}
      {history.map((h) => (
        <g key={h.iter}>
          <line
            x1={sx(h.iter)}
            y1={H - PAD.bottom}
            x2={sx(h.iter)}
            y2={H - PAD.bottom + 4}
            stroke="currentColor"
            strokeOpacity="0.4"
          />
          <text
            x={sx(h.iter)}
            y={H - PAD.bottom + 18}
            textAnchor="middle"
            fontSize="10"
            fill="currentColor"
            opacity="0.6"
          >
            iter {h.iter}
          </text>
        </g>
      ))}

      {/* Winner's test score as horizontal reference */}
      {testRates.length > 0 && (
        <g>
          <line
            x1={PAD.left}
            y1={winnerTestY}
            x2={W - PAD.right}
            y2={winnerTestY}
            stroke="#d97757"
            strokeOpacity="0.6"
            strokeWidth="1"
            strokeDasharray="6 4"
          />
          <text
            x={W - PAD.right - 6}
            y={winnerTestY - 6}
            textAnchor="end"
            fontSize="10"
            fill="#d97757"
          >
            winner test {fmtPct(record.winner_test_score)}
          </text>
        </g>
      )}

      {/* Train pass-rate polyline */}
      <path
        d={trainPath}
        fill="none"
        stroke="currentColor"
        strokeOpacity="0.85"
        strokeWidth="2"
      />
      {history.map((h) => (
        <circle
          key={h.iter}
          cx={sx(h.iter)}
          cy={sy(h.train_pass_rate)}
          r="4"
          fill={h.iter === record.winner_iter ? "#d97757" : "currentColor"}
          fillOpacity={h.iter === record.winner_iter ? 1 : 0.85}
        />
      ))}
    </svg>
  );
}

function PromptBlock({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="eyebrow mb-2">{label}</p>
      <pre className="whitespace-pre-wrap break-words border border-rule-soft bg-ink-elev/40 p-3 font-mono text-[12px] leading-relaxed text-bone-dim">
        {value || "(empty)"}
      </pre>
    </div>
  );
}

export default function OptimizationDetail({ optimizationId }: Props) {
  const [record, setRecord] = useState<OptimizationRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedIter, setExpandedIter] = useState<number | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    fetch(`/api/optimizations/detail?id=${encodeURIComponent(optimizationId)}`)
      .then((res) => {
        if (!res.ok) throw new Error(`Failed: ${res.status}`);
        return res.json();
      })
      .then((data) => {
        setRecord(data);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message);
        setLoading(false);
      });
  }, [optimizationId]);

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <span className="eyebrow">
          Loading
          <span className="cursor-block ml-2 align-baseline" />
        </span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="px-8 py-6">
        <p className="eyebrow text-oxide">Read error</p>
        <p className="mt-2 font-mono text-[11px] text-bone-dim">{error}</p>
      </div>
    );
  }

  if (!record) {
    return null;
  }

  return (
    <div className="h-full overflow-y-auto px-8 py-6 text-bone-dim">
      <div className="mb-6 flex items-baseline justify-between border-b border-rule pb-4">
        <div>
          <p className="eyebrow">{record.dataset}</p>
          <h2 className="font-display mt-2 text-2xl leading-tight text-bone">
            {record.id}
          </h2>
          <p className="mt-1 font-mono text-[11px] text-bone-mute">
            Judge: {record.judge} · Status: {record.status} · Train:{" "}
            {record.train_size} · Test: {record.test_size}
          </p>
        </div>
        <div className="text-right">
          <p className="eyebrow">Winner</p>
          <p className="font-display mt-1 text-3xl text-ember">
            {fmtPct(record.winner_test_score)}
          </p>
          <p className="font-mono text-[10px] text-bone-mute">iter #{record.winner_iter}</p>
        </div>
      </div>

      <div className="mb-8">
        <p className="eyebrow mb-3">Train pass rate by iteration</p>
        <div className="text-bone-dim">
          <ScoreChart record={record} />
        </div>
      </div>

      <div className="mb-8 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <PromptBlock label="Initial prompt (iter 0)" value={record.initial_prompt} />
        <PromptBlock label={`Winner prompt (iter ${record.winner_iter})`} value={record.winner_prompt} />
      </div>

      <div className="mb-2">
        <p className="eyebrow">Iteration history</p>
      </div>
      <table className="w-full border-separate border-spacing-0 text-left text-sm">
        <thead>
          <tr className="text-bone-mute">
            <th className="border-b border-rule-soft px-3 py-2 font-mono text-[10px] uppercase tracking-eyebrow">
              Iter
            </th>
            <th className="border-b border-rule-soft px-3 py-2 font-mono text-[10px] uppercase tracking-eyebrow">
              Train
            </th>
            <th className="border-b border-rule-soft px-3 py-2 font-mono text-[10px] uppercase tracking-eyebrow">
              Test
            </th>
            <th className="border-b border-rule-soft px-3 py-2 font-mono text-[10px] uppercase tracking-eyebrow">
              Prompt
            </th>
            <th className="border-b border-rule-soft px-3 py-2" />
          </tr>
        </thead>
        <tbody>
          {(record.history || []).map((h) => {
            const isWinner = h.iter === record.winner_iter;
            const isOpen = expandedIter === h.iter;
            const testScore = record.test_scores_by_iter?.[String(h.iter)];
            const rationale = record.rationales?.[String(h.iter)];
            return (
              <>
                <tr key={`row-${h.iter}`} className={isWinner ? "bg-ink-elev/30" : ""}>
                  <td className="border-b border-rule-soft px-3 py-2 font-mono tabular-nums">
                    {isWinner ? <span className="text-ember">★ {h.iter}</span> : h.iter}
                  </td>
                  <td className="border-b border-rule-soft px-3 py-2 font-mono tabular-nums">
                    {fmtPct(h.train_pass_rate)}
                    <span className="ml-1 text-[10px] text-bone-mute">
                      (n={h.n_train_samples})
                    </span>
                  </td>
                  <td className="border-b border-rule-soft px-3 py-2 font-mono tabular-nums">
                    {fmtPct(testScore)}
                  </td>
                  <td className="border-b border-rule-soft px-3 py-2 font-mono text-[11px] text-bone-mute">
                    {(h.prompt || "").slice(0, 120)}
                    {h.prompt && h.prompt.length > 120 ? "…" : ""}
                  </td>
                  <td className="border-b border-rule-soft px-3 py-2 text-right">
                    <button
                      onClick={() => setExpandedIter(isOpen ? null : h.iter)}
                      className="eyebrow border border-rule px-2 py-1 transition-colors hover:border-bone-mute hover:text-bone-dim"
                    >
                      {isOpen ? "Close" : "Open"}
                    </button>
                  </td>
                </tr>
                {isOpen && (
                  <tr key={`exp-${h.iter}`}>
                    <td colSpan={5} className="border-b border-rule-soft bg-ink-elev/20 px-3 py-3">
                      {rationale && (
                        <div className="mb-3">
                          <p className="eyebrow mb-1">Rationale</p>
                          <p className="text-[12px] text-bone-dim">{rationale}</p>
                        </div>
                      )}
                      <p className="eyebrow mb-1">Full prompt</p>
                      <pre className="whitespace-pre-wrap break-words border border-rule-soft bg-ink-elev/40 p-3 font-mono text-[11px] leading-relaxed text-bone-dim">
                        {h.prompt}
                      </pre>
                    </td>
                  </tr>
                )}
              </>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
