"use client";

import { useAuth } from "@/contexts/AuthContext";
import { useRouter } from "next/navigation";

export default function Header() {
  const { user, logoutUrl } = useAuth();
  const router = useRouter();

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
            onClick={() => router.push("/results")}
            disabled={!user?.id}
            className="rounded-lg bg-claude-accent px-4 py-2 text-sm font-semibold text-white hover:bg-claude-hover disabled:opacity-50 disabled:cursor-not-allowed"
          >
            View Results
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
