"use client";

import { useAuth } from "@/contexts/AuthContext";
import { useRouter } from "next/navigation";

export default function Header() {
  const { user, logoutUrl } = useAuth();
  const router = useRouter();

  return (
    <div className="border-b border-claude-border bg-claude-bg px-4 py-3">
      <div className="mx-auto flex items-center justify-between">
        <h1 className="text-lg font-semibold text-claude-text">
          LLM Evaluation Platform
        </h1>
        <div className="flex items-center gap-3">
          <button
            onClick={() => router.push("/results")}
            className="rounded-lg bg-claude-accent px-4 py-2 text-sm font-semibold text-white hover:bg-claude-hover"
          >
            View Results
          </button>
          {user?.name && (
            <span className="text-sm text-claude-muted">
              {user.name}
            </span>
          )}
          <button
            onClick={() => { window.location.href = logoutUrl; }}
            className="rounded-lg border border-claude-border px-3 py-1.5 text-sm text-claude-text hover:bg-claude-surface"
          >
            Sign Out
          </button>
        </div>
      </div>
    </div>
  );
}
