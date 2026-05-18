"use client";

import { Suspense } from "react";
import { useAuth, login } from "@/contexts/AuthContext";
import { useEffect } from "react";
import { useSearchParams } from "next/navigation";
import ComparisonView from "@/components/results/ComparisonView";
import ResultsHeader from "@/components/results/ResultsHeader";
import RunRail from "@/components/results/RunRail";

function ResultsContent() {
  const { user, isLoading } = useAuth();
  const searchParams = useSearchParams();
  const groupId = searchParams.get("group");

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
      <ResultsHeader groupId={groupId} />

      <div className="flex flex-1 overflow-hidden">
        <RunRail selectedId={groupId} />

        <div className="flex-1 overflow-hidden">
          {groupId ? (
            <ComparisonView groupId={groupId} />
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

export default function ResultsPage() {
  return (
    <Suspense
      fallback={
        <div className="flex h-screen items-center justify-center bg-ink">
          <span className="eyebrow">
            Loading
            <span className="cursor-block ml-2 align-baseline" />
          </span>
        </div>
      }
    >
      <ResultsContent />
    </Suspense>
  );
}
