"use client";

import { useChat } from "@/contexts/ChatContext";

export default function Sidebar() {
  const { chatSessions, currentSessionId, isLoading, createNewChat, loadChat } = useChat();

  return (
    <div className="flex w-56 flex-col border-r border-claude-border bg-claude-surface">
      <div className="p-3">
        <button
          onClick={createNewChat}
          disabled={isLoading}
          className="w-full rounded-lg bg-claude-accent px-4 py-2 text-sm font-semibold text-white hover:bg-claude-hover disabled:opacity-50 disabled:cursor-not-allowed"
        >
          + New Chat
        </button>
      </div>

      <div className="border-t border-claude-border px-3 pt-2">
        <div className="text-xs font-medium uppercase tracking-wider text-claude-muted px-3 py-1">
          History
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-2">
        {chatSessions.map((session) => {
          const isActive = currentSessionId === session.id;
          return (
            <button
              key={session.id}
              onClick={() => !isLoading && loadChat(session.id)}
              disabled={isLoading}
              className={`w-full rounded-lg px-3 py-2 text-left mb-0.5 ${
                isActive
                  ? "bg-claude-bg text-claude-text"
                  : isLoading
                  ? "text-claude-muted/50 cursor-not-allowed"
                  : "text-claude-muted hover:text-claude-text hover:bg-claude-bg"
              }`}
            >
              <div className="truncate text-sm">
                {session.title || "New Chat"}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
