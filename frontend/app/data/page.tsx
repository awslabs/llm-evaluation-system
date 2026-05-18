"use client";

import { useEffect, useState } from "react";
import { useAuth, login } from "@/contexts/AuthContext";
import Header from "@/components/Header";
import SubToggle, { type DataTab } from "@/components/data/SubToggle";
import DatasetList from "@/components/data/DatasetList";
import DatasetDetailView from "@/components/data/DatasetDetail";
import DocumentsView from "@/components/data/DocumentsView";
import JudgesView from "@/components/data/JudgesView";
import { listDatasets, listDocuments, listJudges } from "@/lib/data-api";
import type { DatasetSummary } from "@/lib/data-types";

export default function DataPage() {
  const { user, isLoading: authLoading } = useAuth();
  const [tab, setTab] = useState<DataTab>("datasets");
  const [datasets, setDatasets] = useState<DatasetSummary[]>([]);
  const [datasetsLoading, setDatasetsLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [counts, setCounts] = useState<Partial<Record<DataTab, number>>>({});

  useEffect(() => {
    if (!authLoading && !user) {
      login();
    }
  }, [authLoading, user]);

  // Initial load — datasets always (cheap and needed for cross-tab counts).
  useEffect(() => {
    if (!user) return;
    setDatasetsLoading(true);
    listDatasets(search)
      .then((d) => {
        setDatasets(d);
        setCounts((c) => ({ ...c, datasets: d.length }));
      })
      .finally(() => setDatasetsLoading(false));
  }, [user, search]);

  // Best-effort secondary counts so the toggle has numbers.
  useEffect(() => {
    if (!user) return;
    listDocuments()
      .then((r) => setCounts((c) => ({ ...c, documents: r.documents?.length ?? 0 })))
      .catch(() => {});
    listJudges()
      .then((j) => setCounts((c) => ({ ...c, judges: j.length })))
      .catch(() => {});
  }, [user]);

  function refreshDatasets() {
    listDatasets(search).then((d) => {
      setDatasets(d);
      setCounts((c) => ({ ...c, datasets: d.length }));
    });
  }

  if (authLoading || !user) {
    return (
      <div className="flex h-screen items-center justify-center bg-ink">
        <span className="eyebrow">
          Identifying
          <span className="cursor-block ml-2 align-baseline" />
        </span>
      </div>
    );
  }

  return (
    <div className="flex h-screen flex-col bg-ink">
      <Header />

      <SubToggle active={tab} counts={counts} onChange={setTab} />

      <div className="flex flex-1 overflow-hidden">
        {tab === "datasets" && (
          <>
            <DatasetList
              datasets={datasets}
              selectedId={selectedId}
              onSelect={setSelectedId}
              loading={datasetsLoading}
              search={search}
              onSearch={setSearch}
            />
            <div className="flex-1 overflow-y-auto">
              {selectedId ? (
                <DatasetDetailView
                  datasetId={selectedId}
                  onRenamed={(id, name) => {
                    setDatasets((cur) =>
                      cur.map((d) => (d.id === id ? { ...d, name } : d)),
                    );
                  }}
                  onDeleted={(id) => {
                    setDatasets((cur) => cur.filter((d) => d.id !== id));
                    setSelectedId(null);
                    refreshDatasets();
                  }}
                />
              ) : (
                <div className="flex h-full items-center justify-center px-8">
                  <div className="max-w-md text-center">
                    <p className="eyebrow">No dataset selected</p>
                    <h3 className="font-display mt-3 text-4xl leading-tight text-bone">
                      <em className="text-ember">Verify</em> before you evaluate.
                    </h3>
                    <p className="mt-4 text-sm text-bone-dim">
                      Pick a dataset to inspect the Q/A pairs, edit answers,
                      delete bad rows, or export to CSV.
                    </p>
                  </div>
                </div>
              )}
            </div>
          </>
        )}

        {tab === "documents" && (
          <div className="flex-1 overflow-y-auto">
            <DocumentsView datasets={datasets} />
          </div>
        )}

        {tab === "judges" && <JudgesView />}
      </div>
    </div>
  );
}
