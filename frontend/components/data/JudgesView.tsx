"use client";

import { useEffect, useState } from "react";
import { deleteJudge, getJudge, listJudges } from "@/lib/data-api";
import type { JudgeDetail, JudgeSummary } from "@/lib/data-types";
import { formatTimestamp } from "@/lib/data-types";

export default function JudgesView() {
  const [judges, setJudges] = useState<JudgeSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<JudgeDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    listJudges()
      .then(setJudges)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    setDetail(null);
    setConfirmingDelete(false);
    getJudge(selectedId).then(setDetail).catch((e) => setError(String(e)));
  }, [selectedId]);

  async function confirmDelete() {
    if (!selectedId) return;
    setBusy(true);
    try {
      await deleteJudge(selectedId);
      setJudges(judges.filter((j) => j.id !== selectedId));
      setSelectedId(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  const criteria = detail?.config?.criteria ?? [];

  return (
    <div className="flex flex-1 overflow-hidden">
      <aside className="flex w-80 flex-col border-r border-rule bg-ink-elev">
        <div className="flex items-baseline justify-between border-b border-rule-soft px-5 py-4">
          <p className="eyebrow">Judges</p>
          <span className="font-mono text-[10px] tabular-nums text-bone-mute">
            {judges.length.toString().padStart(3, "0")} total
          </span>
        </div>
        <div className="flex-1 overflow-y-auto">
          {loading ? (
            <p className="px-5 py-4 eyebrow">
              Reading
              <span className="cursor-block ml-2 align-baseline" />
            </p>
          ) : judges.length === 0 ? (
            <p className="px-5 py-6 text-sm italic leading-relaxed text-bone-mute">
              <span className="font-display not-italic text-bone">
                No judges yet.
              </span>
              <br />
              Generate one in chat with “make a judge for…”
            </p>
          ) : (
            <ul>
              {judges.map((j, idx) => {
                const active = selectedId === j.id;
                return (
                  <li key={j.id}>
                    <button
                      onClick={() => setSelectedId(j.id)}
                      className={`group flex w-full items-baseline gap-3 border-b border-rule-soft border-l-2 px-4 py-3 text-left transition-colors ${
                        active
                          ? "border-l-ember bg-ink-raised"
                          : "border-l-transparent hover:border-l-rule hover:bg-ink-raised/40"
                      }`}
                    >
                      <span
                        className={`font-mono text-[10px] tabular-nums ${
                          active ? "text-ember" : "text-bone-mute"
                        }`}
                      >
                        {(judges.length - idx).toString().padStart(3, "0")}
                      </span>
                      <div className="min-w-0 flex-1">
                        <div className={`truncate text-[13px] leading-tight ${active ? "text-bone" : "text-bone-dim"}`}>
                          {j.name || "Untitled judge"}
                        </div>
                        <div className="mt-1 flex items-baseline gap-2 font-mono text-[10px] uppercase tracking-eyebrow text-bone-mute">
                          <span>{j.domain}</span>
                          <span aria-hidden>·</span>
                          <span className="tabular-nums">
                            {j.criteria.length} criteria
                          </span>
                        </div>
                      </div>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </aside>

      <div className="flex-1 overflow-y-auto">
        {detail ? (
          <div className="mx-auto max-w-3xl px-8 py-10">
            <div className="reveal border-b border-rule pb-6">
              <p className="eyebrow">Judge</p>
              <h2 className="font-display mt-2 text-3xl leading-tight text-bone">
                {detail.name}
              </h2>
              <p className="mt-2 font-mono text-[11px] uppercase tracking-eyebrow text-bone-mute">
                <span>{detail.config?.domain ?? "general"}</span>
                <span className="mx-2" aria-hidden>·</span>
                <span className="tabular-nums">{criteria.length} criteria</span>
                <span className="mx-2" aria-hidden>·</span>
                <span>Created {formatTimestamp(detail.created_at)}</span>
              </p>
              <div className="mt-4 flex items-center gap-3">
                {confirmingDelete ? (
                  <>
                    <button
                      onClick={confirmDelete}
                      disabled={busy}
                      className="eyebrow border border-ember px-3 py-1.5 text-ember hover:bg-ember/10"
                    >
                      Confirm delete
                    </button>
                    <button
                      onClick={() => setConfirmingDelete(false)}
                      className="eyebrow text-bone-mute hover:text-bone-dim"
                    >
                      Cancel
                    </button>
                  </>
                ) : (
                  <button
                    onClick={() => setConfirmingDelete(true)}
                    className="eyebrow border border-rule px-3 py-1.5 hover:border-ember hover:text-ember"
                  >
                    × Delete judge
                  </button>
                )}
              </div>
            </div>

            <ul className="reveal stagger-1">
              {criteria.map((c, i) => (
                <li key={`${c.name}-${i}`} className="border-b border-rule-soft py-5">
                  <div className="flex items-baseline gap-3">
                    <span className="font-mono text-[10px] tabular-nums uppercase tracking-eyebrow text-ember">
                      {(i + 1).toString().padStart(2, "0")}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="font-display text-xl leading-tight text-bone">
                        {c.name}
                      </div>
                      {c.description && (
                        <p className="mt-2 whitespace-pre-wrap break-words text-[0.95rem] leading-relaxed text-bone-dim">
                          {c.description}
                        </p>
                      )}
                      {typeof c.weight === "number" && (
                        <p className="mt-2 font-mono text-[10px] uppercase tracking-eyebrow text-bone-mute">
                          Weight: <span className="tabular-nums text-bone-dim">{c.weight}</span>
                        </p>
                      )}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <div className="flex h-full items-center justify-center px-8">
            <div className="max-w-md text-center">
              <p className="eyebrow">No judge selected</p>
              <h3 className="font-display mt-3 text-4xl leading-tight text-bone">
                <em className="text-ember">Choose</em> a judge to inspect.
              </h3>
              <p className="mt-4 text-sm text-bone-dim">
                Judges encode the rubric used to score eval samples.
                {error && <span className="block mt-2 text-ember">{error}</span>}
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
