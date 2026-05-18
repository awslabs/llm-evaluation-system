"use client";

import type { DatasetSummary } from "@/lib/data-types";
import { formatTimestamp, sourceGlyph, sourceLabel } from "@/lib/data-types";

interface Props {
  datasets: DatasetSummary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  loading: boolean;
  search: string;
  onSearch: (q: string) => void;
}

export default function DatasetList({
  datasets,
  selectedId,
  onSelect,
  loading,
  search,
  onSearch,
}: Props) {
  return (
    <aside className="flex w-80 flex-col border-r border-rule bg-ink-elev">
      <div className="flex items-baseline justify-between border-b border-rule-soft px-5 py-4">
        <p className="eyebrow">Datasets</p>
        <span className="font-mono text-[10px] tabular-nums text-bone-mute">
          {datasets.length.toString().padStart(3, "0")} total
        </span>
      </div>

      <div className="border-b border-rule-soft px-5 py-3">
        <input
          type="search"
          value={search}
          onChange={(e) => onSearch(e.target.value)}
          placeholder="Search…"
          className="w-full border-b border-rule bg-transparent py-1.5 font-mono text-[12px] text-bone placeholder:text-bone-mute focus:border-bone-mute focus:outline-none"
        />
      </div>

      <div className="flex-1 overflow-y-auto">
        {loading ? (
          <p className="px-5 py-4 eyebrow">
            Reading
            <span className="cursor-block ml-2 align-baseline" />
          </p>
        ) : datasets.length === 0 ? (
          <p className="px-5 py-6 text-sm italic leading-relaxed text-bone-mute">
            <span className="font-display not-italic text-bone">
              No datasets yet.
            </span>
            <br />
            Upload a CSV in chat or generate synthetic Q/A pairs to populate
            this list.
          </p>
        ) : (
          <ul>
            {datasets.map((ds, idx) => {
              const active = selectedId === ds.id;
              return (
                <li key={ds.id}>
                  <button
                    onClick={() => onSelect(ds.id)}
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
                      {(datasets.length - idx).toString().padStart(3, "0")}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-baseline gap-2">
                        <span
                          className={`font-mono text-[12px] ${
                            active ? "text-ember" : "text-bone-mute"
                          }`}
                          aria-hidden
                        >
                          {sourceGlyph(ds.source)}
                        </span>
                        <span
                          className={`truncate text-[13px] leading-tight ${
                            active ? "text-bone" : "text-bone-dim"
                          }`}
                        >
                          {ds.name || "Untitled dataset"}
                        </span>
                      </div>
                      <div className="mt-1 flex items-baseline gap-2 font-mono text-[10px] uppercase tracking-eyebrow text-bone-mute">
                        <span className="tabular-nums">
                          {ds.num_samples.toString().padStart(3, "0")} samples
                        </span>
                        <span aria-hidden>·</span>
                        <span>{formatTimestamp(ds.created_at)}</span>
                      </div>
                      <div className="mt-0.5 truncate font-mono text-[9px] uppercase tracking-eyebrow text-bone-mute/70">
                        {sourceLabel(ds.source)}
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
  );
}
