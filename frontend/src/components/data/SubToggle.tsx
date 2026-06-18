export type DataTab = "datasets" | "documents" | "judges";

const TABS: Array<{ id: DataTab; label: string }> = [
  { id: "datasets", label: "Datasets" },
  { id: "documents", label: "Documents" },
  { id: "judges", label: "Judges" },
];

interface Props {
  active: DataTab;
  counts: Partial<Record<DataTab, number>>;
  onChange: (tab: DataTab) => void;
}

export default function SubToggle({ active, counts, onChange }: Props) {
  return (
    <div className="flex items-baseline gap-4 border-b border-rule px-6 py-3">
      <p className="font-serif italic text-[15px] leading-none text-bone-dim">
        Library
      </p>
      <span className="h-3 w-px bg-rule" aria-hidden />
      {TABS.map((tab) => {
        const isActive = active === tab.id;
        const count = counts[tab.id];
        return (
          <button
            key={tab.id}
            onClick={() => onChange(tab.id)}
            className={`relative px-3 py-1.5 font-mono text-[11px] uppercase tracking-eyebrow transition-colors ${
              isActive ? "text-bone" : "text-bone-mute hover:text-bone-dim"
            }`}
            aria-current={isActive ? "page" : undefined}
          >
            <span>{tab.label}</span>
            {typeof count === "number" && (
              <span className="ml-2 tabular-nums text-bone-mute">
                {count.toString().padStart(2, "0")}
              </span>
            )}
            {isActive && (
              <span
                className="absolute inset-x-3 -bottom-px h-px bg-ember"
                aria-hidden
              />
            )}
          </button>
        );
      })}
    </div>
  );
}
