import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";
import { useAuth, login } from "@/contexts/AuthContext";
import ComparisonView from "@/components/results/ComparisonView";
import ResultsHeader from "@/components/results/ResultsHeader";
import RunRail from "@/components/results/RunRail";

export default function ResultsPage() {
  const { user, isLoading } = useAuth();
  // The URL is the source of truth — react-router updates it and re-renders,
  // and handles browser back/forward, in dev and prod alike. (The old Next.js
  // static-export router couldn't update the URL, which forced a pile of
  // manual history.pushState/popstate plumbing that used to live here.)
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedId = searchParams.get("group");
  // owner travels in the URL so deep links / refreshes keep the cross-user
  // read authorized. Absent (or self) means the caller's own eval.
  const selectedOwner = searchParams.get("owner");

  const handleSelect = (id: string | null, owner?: string) => {
    if (!id) {
      setSearchParams({});
      return;
    }
    setSearchParams(owner ? { group: id, owner } : { group: id });
  };

  useEffect(() => {
    if (!isLoading && !user) {
      login();
    }
  }, [isLoading, user]);

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center bg-ink">
        <span className="eyebrow">
          Identifying
          <span className="cursor-block ml-2 align-baseline" />
        </span>
      </div>
    );
  }

  if (!user) {
    return null;
  }

  return (
    <div className="flex h-screen flex-col bg-ink">
      <ResultsHeader groupId={selectedId} owner={selectedOwner} />

      <div className="flex flex-1 overflow-hidden">
        <RunRail selectedId={selectedId} onSelect={handleSelect} />

        <div className="flex-1 overflow-hidden">
          {selectedId ? (
            <ComparisonView groupId={selectedId} owner={selectedOwner} />
          ) : (
            <div className="flex h-full items-center justify-center px-8">
              <div className="max-w-md text-center">
                <p className="eyebrow">No run selected</p>
                <h3 className="font-display mt-3 text-4xl leading-tight text-bone">
                  <em className="text-ember">Pick</em> a run to read its
                  scores.
                </h3>
                <p className="mt-4 text-sm text-bone-dim">
                  Each entry on the left is a recorded evaluation —
                  per-criterion scores, sample-level judgments, and full
                  transcripts are one click away.
                </p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
