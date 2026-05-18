"use client";

import { useChat } from "@/contexts/ChatContext";

function formatDate(iso?: string): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      month: "short",
      day: "2-digit",
    });
  } catch {
    return "";
  }
}

export default function Sidebar() {
  const { chatSessions, currentSessionId, isLoading, createNewChat, loadChat } = useChat();

  return (
    <aside className="flex w-80 flex-col border-r border-rule bg-ink-elev">
      <div className="flex items-baseline justify-between border-b border-rule-soft px-5 py-4">
        <p className="eyebrow">Sessions</p>
        <span className="font-mono text-[10px] tabular-nums text-bone-mute">
          {chatSessions.length.toString().padStart(3, "0")} total
        </span>
      </div>

      <div className="border-b border-rule-soft px-5 py-3">
        <button
          onClick={createNewChat}
          disabled={isLoading}
          className="group inline-flex items-center gap-2 font-mono text-[11px] uppercase tracking-eyebrow text-bone-dim transition-colors hover:text-ember disabled:cursor-not-allowed disabled:opacity-40"
        >
          <span className="font-mono text-sm leading-none">+</span>
          New session
        </button>
      </div>

      <div className="flex-1 overflow-y-auto">
        {chatSessions.length === 0 ? (
          <p className="px-5 py-6 text-sm italic leading-relaxed text-bone-mute">
            <span className="font-display not-italic text-bone">
              No sessions yet.
            </span>
            <br />
            Open a new session to begin a conversation with the instrument.
          </p>
        ) : (
          <ul>
            {chatSessions.map((session, idx) => {
              const isActive = currentSessionId === session.id;
              return (
                <li key={session.id}>
                  <button
                    onClick={() => !isLoading && loadChat(session.id)}
                    disabled={isLoading}
                    className={`group flex w-full items-baseline gap-3 border-b border-rule-soft border-l-2 px-4 py-3 text-left transition-colors ${
                      isActive
                        ? "border-l-ember bg-ink-raised"
                        : "border-l-transparent hover:border-l-rule hover:bg-ink-raised/40"
                    } ${isLoading && !isActive ? "cursor-not-allowed opacity-50" : ""}`}
                  >
                    <span
                      className={`font-mono text-[10px] tabular-nums ${
                        isActive ? "text-ember" : "text-bone-mute"
                      }`}
                    >
                      {(chatSessions.length - idx).toString().padStart(3, "0")}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div
                        className={`truncate text-[14px] leading-snug ${
                          isActive ? "text-bone" : "text-bone-dim"
                        }`}
                      >
                        {session.title || "Untitled session"}
                      </div>
                      {session.createdAt && (
                        <div className="mt-1 font-mono text-[10px] uppercase tracking-eyebrow text-bone-mute">
                          {formatDate(session.createdAt)}
                        </div>
                      )}
                    </div>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </aside>
  );
}
