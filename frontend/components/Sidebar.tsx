"use client";

import { useChat } from "@/contexts/ChatContext";
import { useState } from "react";

export default function Sidebar() {
  const { chatSessions, currentSessionId, createNewChat, loadChat } = useChat();
  const [isCollapsed, setIsCollapsed] = useState(false);

  if (isCollapsed) {
    return (
      <div className="flex w-16 flex-col border-r border-claude-border bg-claude-surface">
        <button
          onClick={() => setIsCollapsed(false)}
          className="p-4 text-claude-muted hover:text-claude-text"
          aria-label="Expand sidebar"
        >
          <svg
            className="h-6 w-6"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M9 5l7 7-7 7"
            />
          </svg>
        </button>
      </div>
    );
  }

  return (
    <div className="flex w-64 flex-col border-r border-claude-border bg-claude-surface">
      <div className="flex items-center justify-between border-b border-claude-border p-4">
        <h2 className="text-sm font-semibold text-claude-text">Chat History</h2>
        <button
          onClick={() => setIsCollapsed(true)}
          className="text-claude-muted hover:text-claude-text"
          aria-label="Collapse sidebar"
        >
          <svg
            className="h-5 w-5"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M15 19l-7-7 7-7"
            />
          </svg>
        </button>
      </div>

      <div className="p-4">
        <button
          onClick={createNewChat}
          className="w-full rounded-lg bg-claude-accent px-4 py-2 text-sm font-semibold text-white hover:bg-claude-hover"
        >
          New Chat
        </button>
      </div>

      <div className="flex-1 overflow-y-auto">
        {chatSessions.map((session) => (
          <button
            key={session.id}
            onClick={() => loadChat(session.id)}
            className={`w-full border-b border-claude-border px-4 py-3 text-left hover:bg-claude-bg ${
              currentSessionId === session.id ? "bg-claude-bg" : ""
            }`}
          >
            <div className="truncate text-sm font-medium text-claude-text">
              {session.title || "New Chat"}
            </div>
            <div className="mt-1 text-xs text-claude-muted">
              {new Date(session.createdAt).toLocaleDateString()}
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}
