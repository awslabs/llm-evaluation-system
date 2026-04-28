"use client";

import { useAuth, login } from "@/contexts/AuthContext";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

interface ChatSession {
  id: string;
  title: string;
  createdAt: string;
  messages: { role: string; content: string; timestamp: string }[];
}

export default function HistoryPage() {
  const { user, isLoading: authLoading } = useAuth();
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [selectedSession, setSelectedSession] = useState<ChatSession | null>(null);
  const [loading, setLoading] = useState(true);
  const router = useRouter();

  useEffect(() => {
    if (!authLoading && !user) {
      login();
    }
  }, [authLoading, user]);

  useEffect(() => {
    if (!user?.name) return;
    fetch(`/api/sessions?user_id=${encodeURIComponent(user.name)}`)
      .then((res) => res.ok ? res.json() : { sessions: [] })
      .then((data) => {
        setSessions(data.sessions || []);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, [user?.name]);

  if (authLoading || !user) {
    return (
      <div className="flex h-screen items-center justify-center bg-claude-bg">
        <div className="text-claude-muted">Loading...</div>
      </div>
    );
  }

  return (
    <div className="flex h-screen flex-col bg-claude-bg">
      {/* Header */}
      <div className="border-b border-claude-border bg-claude-bg px-4 py-3">
        <div className="mx-auto flex max-w-5xl items-center justify-between">
          <div className="flex items-center gap-3">
            <h1 className="text-lg font-semibold text-claude-text">
              LLM Evaluation Platform
            </h1>
            <span className="text-claude-muted">|</span>
            <span className="text-sm text-claude-muted">Chat History</span>
          </div>
          <button
            onClick={() => router.push("/chat")}
            className="rounded-lg bg-claude-accent px-4 py-2 text-sm font-semibold text-white hover:bg-claude-hover"
          >
            Back to Chat
          </button>
        </div>
      </div>

      {/* Content */}
      <div className="flex flex-1 overflow-hidden">
        {/* Session list */}
        <div className="w-80 border-r border-claude-border overflow-y-auto">
          {loading ? (
            <div className="p-4 text-claude-muted text-sm">Loading...</div>
          ) : sessions.length === 0 ? (
            <div className="p-4 text-claude-muted text-sm">No conversations yet.</div>
          ) : (
            sessions.map((session) => (
              <button
                key={session.id}
                onClick={() => setSelectedSession(session)}
                className={`w-full border-b border-claude-border px-4 py-3 text-left hover:bg-claude-surface ${
                  selectedSession?.id === session.id ? "bg-claude-surface" : ""
                }`}
              >
                <div className="truncate text-sm font-medium text-claude-text">
                  {session.title || "New Chat"}
                </div>
                <div className="mt-1 text-xs text-claude-muted">
                  {new Date(session.createdAt).toLocaleDateString(undefined, {
                    month: "short",
                    day: "numeric",
                    hour: "2-digit",
                    minute: "2-digit",
                  })}
                  {session.messages && (
                    <span> · {session.messages.length} messages</span>
                  )}
                </div>
              </button>
            ))
          )}
        </div>

        {/* Message view */}
        <div className="flex-1 overflow-y-auto p-6">
          {selectedSession ? (
            <div className="mx-auto max-w-3xl space-y-4">
              <h2 className="text-lg font-medium text-claude-text mb-4">
                {selectedSession.title || "Conversation"}
              </h2>
              {selectedSession.messages?.map((msg, i) => (
                <div
                  key={i}
                  className={`rounded-lg p-4 ${
                    msg.role === "user"
                      ? "bg-claude-surface ml-12"
                      : "bg-claude-bg border border-claude-border mr-12"
                  }`}
                >
                  <div className="text-xs text-claude-muted mb-1">
                    {msg.role === "user" ? "You" : "Assistant"}
                  </div>
                  <div className="text-sm text-claude-text whitespace-pre-wrap">
                    {msg.content}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="flex h-full items-center justify-center">
              <p className="text-claude-muted">Select a conversation to view</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
