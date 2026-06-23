import { useEffect, useState } from "react";
import { listDocuments } from "@/lib/data-api";
import type { DocumentEntry, DatasetSummary } from "@/lib/data-types";
import ShareModal from "@/components/results/ShareModal";

interface Props {
  datasets: DatasetSummary[];
}

function basename(p: string): string {
  const i = p.lastIndexOf("/");
  return i === -1 ? p : p.slice(i + 1);
}

export default function DocumentsView({ datasets }: Props) {
  const [docs, setDocs] = useState<DocumentEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [shareOpen, setShareOpen] = useState(false);

  useEffect(() => {
    listDocuments()
      .then((r) => setDocs(r.documents ?? []))
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  // Build an index: doc filename -> dataset names that reference it.
  const usageIndex = new Map<string, string[]>();
  for (const ds of datasets) {
    const src = ds.source;
    if (!src) continue;
    if (src.kind === "imported" && src.origin) {
      const arr = usageIndex.get(src.origin) ?? [];
      arr.push(ds.name);
      usageIndex.set(src.origin, arr);
    }
    if (src.kind === "synthetic" && src.documents) {
      for (const d of src.documents) {
        const key = basename(d);
        const arr = usageIndex.get(key) ?? [];
        arr.push(ds.name);
        usageIndex.set(key, arr);
      }
    }
  }

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <span className="eyebrow">
          Reading
          <span className="cursor-block ml-2 align-baseline" />
        </span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="mx-auto max-w-3xl px-8 py-10">
        <p className="eyebrow">Could not read documents</p>
        <p className="mt-2 text-sm text-bone-mute">{error}</p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl px-8 py-10">
      <div className="reveal border-b border-rule pb-6">
        <p className="eyebrow">Documents</p>
        <h2 className="font-display mt-2 text-3xl leading-tight text-bone">
          Source material
        </h2>
        <p className="mt-2 text-sm text-bone-dim">
          Files uploaded for synthetic Q/A generation or as evaluation context.
          Each row links to datasets generated from it.
        </p>
        <div className="mt-4">
          <button
            onClick={() => setShareOpen(true)}
            className="eyebrow inline-flex items-center gap-2 border border-rule px-3 py-1.5 transition-colors hover:border-bone-mute hover:text-bone-dim"
          >
            <span className="font-mono">⤷</span> Share all documents
          </button>
        </div>
      </div>
      {shareOpen && (
        <ShareModal
          resourceId=""
          apiBase="/api/documents"
          label="document"
          shareAllOnly
          onClose={() => setShareOpen(false)}
        />
      )}

      {docs.length === 0 ? (
        <p className="reveal stagger-1 px-1 py-10 text-sm italic text-bone-mute">
          <span className="font-display not-italic text-bone">
            No documents yet.
          </span>{" "}
          Upload PDFs or text in chat and they will appear here.
        </p>
      ) : (
        <ul className="reveal stagger-1">
          {docs.map((d, idx) => {
            const name = basename(d.path);
            const usedIn = usageIndex.get(name) ?? [];
            return (
              <li key={d.path} className="border-b border-rule-soft py-4">
                <div className="flex items-baseline gap-3">
                  <span className="font-mono text-[10px] tabular-nums text-bone-mute">
                    {(idx + 1).toString().padStart(3, "0")}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-baseline gap-2 truncate text-[0.95rem] text-bone">
                      <span className="truncate">{name}</span>
                      {d.shared && (
                        <span
                          title={`Shared by ${d.owner}`}
                          className="shrink-0 rounded-sm border border-rule px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-eyebrow text-bone-mute"
                        >
                          shared
                        </span>
                      )}
                    </div>
                    <div className="mt-1 truncate font-mono text-[10px] uppercase tracking-eyebrow text-bone-mute">
                      {d.path}
                    </div>
                    {usedIn.length > 0 ? (
                      <p className="mt-2 font-mono text-[10px] uppercase tracking-eyebrow text-bone-mute">
                        <span className="text-ember">
                          Used in {usedIn.length} dataset{usedIn.length === 1 ? "" : "s"}
                        </span>
                        <span className="ml-2 text-bone-mute">
                          {usedIn.slice(0, 3).join(" · ")}
                          {usedIn.length > 3 ? ` · +${usedIn.length - 3} more` : ""}
                        </span>
                      </p>
                    ) : (
                      <p className="mt-2 font-mono text-[10px] uppercase tracking-eyebrow text-bone-mute/60">
                        Not yet referenced by any dataset
                      </p>
                    )}
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
