"use client";

import { useAuth } from "@/contexts/AuthContext";
import { useState } from "react";

export default function Header() {
  const { user, logoutUrl } = useAuth();
  const [isLoadingViewer, setIsLoadingViewer] = useState(false);

  const openViewer = async () => {
    if (!user?.id) return;

    setIsLoadingViewer(true);
    try {
      // Start the viewer (uses relative URL for portability)
      const response = await fetch(`/api/viewer/url`);

      if (!response.ok) {
        throw new Error("Failed to start viewer");
      }

      // Open viewer via proxy URL
      const proxyUrl = `/viewer/${encodeURIComponent(user.id)}/evals`;
      window.open(proxyUrl, "_blank");
    } catch (error) {
      console.error("Error opening viewer:", error);
      alert("Failed to open viewer. Please try again.");
    } finally {
      setIsLoadingViewer(false);
    }
  };

  return (
    <div className="border-b border-claude-border bg-claude-bg px-4 py-3">
      <div className="mx-auto flex max-w-3xl items-center justify-between">
        <h1 className="text-lg font-semibold text-claude-text">
          LLM Evaluation Platform
        </h1>
        <div className="flex items-center gap-4">
          {user?.name && (
            <div className="text-sm text-claude-muted">
              Signed in as <span className="text-claude-text font-medium">{user.name}</span>
            </div>
          )}
          <button
            onClick={openViewer}
            disabled={isLoadingViewer || !user?.id}
            className="rounded-lg bg-claude-accent px-4 py-2 text-sm font-semibold text-white hover:bg-claude-hover disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isLoadingViewer ? (
              <span className="flex items-center gap-2">
                <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
                </svg>
                Loading...
              </span>
            ) : (
              "View Results"
            )}
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
