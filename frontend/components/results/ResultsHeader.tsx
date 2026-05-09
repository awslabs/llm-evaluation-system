"use client";

import { useState } from "react";
import { useAuth } from "@/contexts/AuthContext";
import { useRouter } from "next/navigation";

interface ResultsHeaderProps {
  groupId: string | null;
  sessionId?: string | null;
}

export default function ResultsHeader({ groupId, sessionId }: ResultsHeaderProps) {
  const { user, logoutUrl } = useAuth();
  const router = useRouter();
  const [generating, setGenerating] = useState(false);

  const handleDownloadReport = async () => {
    if (!groupId) return;
    setGenerating(true);
    try {
      const params = new URLSearchParams({ group_id: groupId });
      if (sessionId) params.set("session_id", sessionId);
      const response = await fetch(`/api/compare/report/pdf?${params.toString()}`);
      if (!response.ok) throw new Error("Failed to generate report");
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `eval_report_${groupId.replace(/[/\\]/g, "_")}.pdf`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error("Report generation failed:", err);
    } finally {
      setGenerating(false);
    }
  };

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
          {groupId && (
            <button
              onClick={handleDownloadReport}
              disabled={generating}
              className="rounded-lg border border-claude-border px-4 py-2 text-sm text-claude-text hover:bg-claude-surface disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {generating ? "Generating..." : "Download Report"}
            </button>
          )}
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
