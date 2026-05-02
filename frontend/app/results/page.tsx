"use client";

import { Suspense } from "react";
import { useAuth, login } from "@/contexts/AuthContext";
import { useEffect } from "react";
import { useSearchParams } from "next/navigation";
import ComparisonGroupList from "@/components/results/ComparisonGroupList";
import ComparisonView from "@/components/results/ComparisonView";
import ResultsHeader from "@/components/results/ResultsHeader";

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
      <div className="flex h-screen items-center justify-center bg-claude-bg">
        <div className="text-claude-muted">Loading...</div>
      </div>
    );
  }

  if (!user) {
    return null;
  }

  return (
    <div className="flex h-screen flex-col bg-claude-bg">
      <ResultsHeader groupId={groupId} />
      <div className="flex-1 overflow-auto">
        {groupId ? (
          <ComparisonView groupId={groupId} />
        ) : (
          <ComparisonGroupList />
        )}
      </div>
    </div>
  );
}

export default function ResultsPage() {
  return (
    <Suspense
      fallback={
        <div className="flex h-screen items-center justify-center bg-claude-bg">
          <div className="text-claude-muted">Loading...</div>
        </div>
      }
    >
      <ResultsContent />
    </Suspense>
  );
}
