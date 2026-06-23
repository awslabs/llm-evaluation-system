import { useEffect, useState } from "react";
import type { DatasetDetail, DatasetTest } from "@/lib/data-types";
import { formatTimestamp, sourceLabel } from "@/lib/data-types";
import {
  deleteDataset,
  exportDatasetUrl,
  getDataset,
  patchDataset,
} from "@/lib/data-api";
import ShareModal from "@/components/results/ShareModal";

interface Props {
  datasetId: string;
  // Owner of a shared dataset (absent/own → editable). When set, the view is
  // read-only and reads are scoped to the owner via the resolver.
  owner?: string | null;
  onRenamed: (id: string, name: string) => void;
  onDeleted: (id: string) => void;
}

const PAGE_SIZE = 50;

export default function DatasetDetailView({ datasetId, owner, onRenamed, onDeleted }: Props) {
  const isShared = !!owner;
  const [shareOpen, setShareOpen] = useState(false);
  const [detail, setDetail] = useState<DatasetDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const [editingIdx, setEditingIdx] = useState<number | null>(null);
  const [editingQ, setEditingQ] = useState("");
  const [editingA, setEditingA] = useState("");
  const [renaming, setRenaming] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setOffset(0);
    setEditingIdx(null);
    setRenaming(false);
    setConfirmingDelete(false);
  }, [datasetId]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getDataset(datasetId, offset, PAGE_SIZE, owner)
      .then((d) => {
        if (!cancelled) {
          setDetail(d);
          setNameDraft(d.name);
        }
      })
      .catch((e) => {
        if (!cancelled) setError(String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [datasetId, offset, owner]);

  if (loading && !detail) {
    return (
      <div className="flex h-full items-center justify-center">
        <span className="eyebrow">
          Reading
          <span className="cursor-block ml-2 align-baseline" />
        </span>
      </div>
    );
  }

  if (error || !detail) {
    return (
      <div className="mx-auto max-w-3xl px-8 py-10">
        <p className="eyebrow">Could not read dataset</p>
        <p className="mt-2 text-sm text-bone-mute">{error ?? "Unknown error"}</p>
      </div>
    );
  }

  const totalPages = Math.max(1, Math.ceil(detail.total / PAGE_SIZE));
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  async function saveName() {
    if (!nameDraft.trim() || nameDraft === detail!.name) {
      setRenaming(false);
      return;
    }
    setBusy(true);
    try {
      await patchDataset(datasetId, { name: nameDraft.trim() });
      setDetail({ ...detail!, name: nameDraft.trim() });
      onRenamed(datasetId, nameDraft.trim());
      setRenaming(false);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function saveEdit() {
    if (editingIdx === null || !detail) return;
    setBusy(true);
    try {
      // Patch the FULL test list — pull current full set then replace one row.
      const full = await getDataset(datasetId, 0, detail.total || 1);
      const next: DatasetTest[] = [...full.tests];
      const globalIdx = offset + editingIdx;
      const existing = next[globalIdx] || { vars: {} };
      next[globalIdx] = {
        ...existing,
        vars: {
          ...(existing.vars ?? {}),
          question: editingQ,
          golden_answer: editingA,
        },
      };
      await patchDataset(datasetId, { tests: next });
      // Refresh the current window
      const refreshed = await getDataset(datasetId, offset, PAGE_SIZE);
      setDetail(refreshed);
      setEditingIdx(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function deleteRow(idx: number) {
    if (!detail) return;
    setBusy(true);
    try {
      const full = await getDataset(datasetId, 0, detail.total || 1);
      const next = [...full.tests];
      next.splice(offset + idx, 1);
      await patchDataset(datasetId, { tests: next });
      const newOffset = offset >= next.length ? Math.max(0, next.length - PAGE_SIZE) : offset;
      setOffset(newOffset);
      const refreshed = await getDataset(datasetId, newOffset, PAGE_SIZE);
      setDetail(refreshed);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function confirmDelete() {
    setBusy(true);
    try {
      await deleteDataset(datasetId);
      onDeleted(datasetId);
    } catch (e) {
      setError(String(e));
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-3xl px-8 py-10">
      <div className="reveal border-b border-rule pb-6">
        <p className="eyebrow">Dataset</p>

        {renaming && !isShared ? (
          <div className="mt-2 flex items-baseline gap-3">
            <input
              autoFocus
              value={nameDraft}
              onChange={(e) => setNameDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") saveName();
                if (e.key === "Escape") setRenaming(false);
              }}
              className="font-display flex-1 border-b border-bone-mute bg-transparent text-3xl leading-tight text-bone focus:border-ember focus:outline-none"
            />
            <button
              onClick={saveName}
              disabled={busy}
              className="eyebrow border border-rule px-2 py-1 hover:border-ember hover:text-bone"
            >
              Save
            </button>
            <button
              onClick={() => setRenaming(false)}
              className="eyebrow text-bone-mute hover:text-bone-dim"
            >
              Cancel
            </button>
          </div>
        ) : isShared ? (
          <h2 className="font-display mt-2 text-3xl leading-tight text-bone">
            {detail.name || "Untitled dataset"}
          </h2>
        ) : (
          <h2
            className="font-display mt-2 cursor-text text-3xl leading-tight text-bone hover:text-ember"
            onClick={() => setRenaming(true)}
            title="Click to rename"
          >
            {detail.name || "Untitled dataset"}
          </h2>
        )}
        {isShared && (
          <p className="mt-2 font-mono text-[11px] text-bone-mute">
            Shared by <span className="text-bone-dim">{owner}</span> · read-only
          </p>
        )}

        <p className="mt-3 font-mono text-[11px] uppercase tracking-eyebrow text-bone-mute">
          <span className="tabular-nums">{detail.total.toString().padStart(3, "0")} samples</span>
          <span className="mx-2" aria-hidden>·</span>
          <span>{sourceLabel(detail.source)}</span>
          <span className="mx-2" aria-hidden>·</span>
          <span>Created {formatTimestamp(detail.created_at)}</span>
          {detail.updated_at && detail.updated_at !== detail.created_at && (
            <>
              <span className="mx-2" aria-hidden>·</span>
              <span>Updated {formatTimestamp(detail.updated_at)}</span>
            </>
          )}
        </p>

        <div className="mt-4 flex items-center gap-3">
          <a
            href={exportDatasetUrl(datasetId)}
            className="eyebrow border border-rule px-3 py-1.5 hover:border-bone-mute hover:text-bone"
            download
          >
            ↓ Export CSV
          </a>
          {!isShared && (
            <button
              onClick={() => setShareOpen(true)}
              className="eyebrow inline-flex items-center gap-2 border border-rule px-3 py-1.5 transition-colors hover:border-bone-mute hover:text-bone-dim"
            >
              <span className="font-mono">⤷</span> Share
            </button>
          )}
          {!isShared && (confirmingDelete ? (
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
              × Delete dataset
            </button>
          ))}
        </div>
      </div>
      {shareOpen && (
        <ShareModal
          resourceId={datasetId}
          apiBase="/api/datasets"
          label="dataset"
          onClose={() => setShareOpen(false)}
        />
      )}

      <ul className="reveal stagger-1">
        {detail.tests.map((t, i) => {
          const vars = t.vars ?? {};
          const q = (vars.question as string) ?? "";
          const a = (vars.golden_answer as string) ?? "";
          const extras = Object.entries(vars).filter(
            ([k]) => k !== "question" && k !== "golden_answer",
          );
          const isEditing = editingIdx === i;
          return (
            <li key={offset + i} className="border-b border-rule-soft py-5">
              <div className="flex items-baseline justify-between gap-3">
                <span className="font-mono text-[10px] uppercase tracking-eyebrow text-ember tabular-nums">
                  Q{(offset + i + 1).toString().padStart(3, "0")}
                </span>
                <div className="flex items-baseline gap-2">
                  {isShared ? null : isEditing ? (
                    <>
                      <button
                        onClick={saveEdit}
                        disabled={busy}
                        className="font-mono text-[10px] uppercase tracking-eyebrow text-ember hover:text-bone"
                      >
                        Save
                      </button>
                      <button
                        onClick={() => setEditingIdx(null)}
                        className="font-mono text-[10px] uppercase tracking-eyebrow text-bone-mute hover:text-bone-dim"
                      >
                        Cancel
                      </button>
                    </>
                  ) : (
                    <>
                      <button
                        onClick={() => {
                          setEditingIdx(i);
                          setEditingQ(q);
                          setEditingA(a);
                        }}
                        className="font-mono text-[10px] uppercase tracking-eyebrow text-bone-mute hover:text-bone"
                      >
                        Edit
                      </button>
                      <span className="text-bone-mute" aria-hidden>·</span>
                      <button
                        onClick={() => deleteRow(i)}
                        disabled={busy}
                        className="font-mono text-[10px] uppercase tracking-eyebrow text-bone-mute hover:text-ember"
                      >
                        Delete
                      </button>
                    </>
                  )}
                </div>
              </div>

              {isEditing ? (
                <div className="mt-3 space-y-3">
                  <textarea
                    value={editingQ}
                    onChange={(e) => setEditingQ(e.target.value)}
                    rows={3}
                    placeholder="Question"
                    className="w-full border border-rule bg-ink-raised p-3 text-[1.0625rem] leading-relaxed text-bone placeholder:text-bone-mute focus:border-ember focus:outline-none"
                  />
                  <textarea
                    value={editingA}
                    onChange={(e) => setEditingA(e.target.value)}
                    rows={6}
                    placeholder="Expected answer"
                    className="w-full border border-rule bg-ink-raised p-3 text-[1.0625rem] leading-relaxed text-bone-dim placeholder:text-bone-mute focus:border-ember focus:outline-none"
                  />
                </div>
              ) : (
                <>
                  <p className="mt-3 whitespace-pre-wrap break-words text-[1.0625rem] leading-relaxed text-bone">
                    {q}
                  </p>
                  {a && (
                    <p className="mt-3 whitespace-pre-wrap break-words border-l-2 border-ember-deep pl-4 text-[1rem] leading-relaxed text-bone-dim">
                      {a}
                    </p>
                  )}
                  {extras.length > 0 && (
                    <ul className="mt-3 flex flex-wrap gap-x-4 gap-y-1">
                      {extras.map(([k, v]) => (
                        <li
                          key={k}
                          className="font-mono text-[11px] uppercase tracking-eyebrow"
                        >
                          <span className="text-bone-mute">{k}:</span>{" "}
                          <span className="text-bone-dim">
                            {typeof v === "string" ? v : JSON.stringify(v)}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </>
              )}
            </li>
          );
        })}
      </ul>

      {totalPages > 1 && (
        <div className="mt-6 flex items-center justify-between border-t border-rule pt-4">
          <button
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            disabled={offset === 0}
            className="eyebrow disabled:opacity-30 enabled:hover:text-bone"
          >
            ← Prev
          </button>
          <span className="font-mono text-[10px] uppercase tracking-eyebrow text-bone-mute tabular-nums">
            Page {currentPage} of {totalPages}
          </span>
          <button
            onClick={() => setOffset(offset + PAGE_SIZE)}
            disabled={offset + PAGE_SIZE >= detail.total}
            className="eyebrow disabled:opacity-30 enabled:hover:text-bone"
          >
            Next →
          </button>
        </div>
      )}
    </div>
  );
}
