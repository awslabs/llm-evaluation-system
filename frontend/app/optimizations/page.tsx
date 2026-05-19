"use client";

import { Suspense, useEffect, useState } from "react";
import { useAuth, login } from "@/contexts/AuthContext";
import { useSearchParams } from "next/navigation";
import Header from "@/components/Header";
import OptimizationRail from "@/components/optimizations/OptimizationRail";
import OptimizationDetail from "@/components/optimizations/OptimizationDetail";

function OptimizationsContent() {
  const { user, isLoading } = useAuth();
  const searchParams = useSearchParams();
  const urlId = searchParams.get("id");

  // Same in-place selection pattern as /results: URL stays in sync via
  // pushState but the rail-and-pane swap doesn't trigger a Next.js
  // navigation. Browser back/forward still works.
  const [selectedId, setSelectedId] = useState<string | null>(urlId);

  useEffect(() => {
    setSelectedId(urlId);
  }, [urlId]);

  useEffect(() => {
    const onPop = () => {
      const params = new URLSearchParams(window.location.search);
      setSelectedId(params.get("id"));
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const handleSelect = (id: string | null) => {
    setSelectedId(id);
    const url = id ? `/optimizations?id=${encodeURIComponent(id)}` : "/optimizations";
    if (typeof window !== "undefined") {
      window.history.pushState({}, "", url);
    }
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
      <Header />

      <div className="flex flex-1 overflow-hidden">
        <OptimizationRail selectedId={selectedId} onSelect={handleSelect} />

        <div className="flex-1 overflow-hidden">
          {selectedId ? (
            <OptimizationDetail optimizationId={selectedId} />
          ) : (
            <div className="flex h-full items-center justify-center px-8">
              <div className="max-w-md text-center">
                <p className="eyebrow">No run selected</p>
                <h3 className="font-display mt-3 text-4xl leading-tight text-bone">
                  <em className="text-ember">Pick</em> an optimization to
                  read its diff.
                </h3>
                <p className="mt-4 text-sm text-bone-dim">
                  Each entry on the left is an optimize_prompt run — the
                  initial template, the winner, train pass rate per
                  iteration, and the rationale behind each proposal.
                </p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default function OptimizationsPage() {
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
      <OptimizationsContent />
    </Suspense>
  );
}
