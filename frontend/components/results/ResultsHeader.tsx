"use client";

import { useAuth } from "@/contexts/AuthContext";
import { useRouter } from "next/navigation";

interface ResultsHeaderProps {
  groupId: string | null;
}

export default function ResultsHeader({ groupId }: ResultsHeaderProps) {
  const { user, logoutUrl } = useAuth();
  const router = useRouter();

  return (
    <div className="border-b border-claude-border bg-claude-bg px-4 py-3">
      <div className="mx-auto flex max-w-7xl items-center justify-between">
        <div className="flex items-center gap-3">
          <h1 className="text-lg font-semibold text-claude-text">
            LLM Evaluation Platform
          </h1>
          <span className="text-claude-muted">|</span>
          {groupId ? (
            <button
              onClick={() => router.push("/results")}
              className="text-sm text-claude-accent hover:text-claude-hover"
            >
              &larr; All Evaluations
            </button>
          ) : (
            <span className="text-sm text-claude-muted">Results</span>
          )}
        </div>
        <div className="flex items-center gap-4">
          {user?.name && (
            <div className="text-sm text-claude-muted">
              Signed in as <span className="text-claude-text font-medium">{user.name}</span>
            </div>
          )}
          <button
            onClick={() => router.push("/chat")}
            className="rounded-lg bg-claude-accent px-4 py-2 text-sm font-semibold text-white hover:bg-claude-hover"
          >
            Back to Chat
          </button>
          <button
            onClick={() => { window.location.href = logoutUrl; }}
            className="rounded-lg border border-claude-border px-4 py-2 text-sm text-claude-text hover:bg-claude-surface"
          >
            Sign Out
          </button>
        </div>
      </div>
    </div>
  );
}
