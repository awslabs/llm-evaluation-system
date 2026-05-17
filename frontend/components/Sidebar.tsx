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
    <aside className="flex w-64 flex-col border-r border-rule bg-ink-elev">
      <div className="px-4 pb-3 pt-4">
        <button
          onClick={createNewChat}
          disabled={isLoading}
          className="group flex w-full items-center justify-between border border-bone px-3 py-2 text-left text-sm tracking-wide transition-colors hover:bg-bone hover:text-ink disabled:cursor-not-allowed disabled:opacity-40"
        >
          <span className="font-mono text-[11px] uppercase tracking-eyebrow">
            New session
          </span>
          <span className="font-mono text-base transition-transform group-hover:translate-x-0.5">
            +
          </span>
        </button>
      </div>

      <div className="flex items-baseline justify-between border-t border-rule-soft px-4 pb-1 pt-3">
        <span className="eyebrow">Archive</span>
        <span className="font-mono text-[10px] tabular-nums text-bone-mute">
          {chatSessions.length.toString().padStart(2, "0")}
        </span>
      </div>

      <div className="flex-1 overflow-y-auto px-2 pb-3">
        {chatSessions.length === 0 ? (
          <p className="px-3 py-4 text-xs italic leading-relaxed text-bone-mute">
            <span className="font-display text-sm not-italic">No sessions yet.</span>
            <br />
            Start a new one to begin recording.
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
                    className={`group relative flex w-full items-baseline gap-3 border-l-2 px-3 py-2 text-left transition-colors ${
                      isActive
                        ? "border-ember bg-ink-raised"
                        : "border-transparent hover:border-rule hover:bg-ink-raised/40"
                    } ${isLoading && !isActive ? "cursor-not-allowed opacity-50" : ""}`}
                  >
                    <span
                      className={`font-mono text-[10px] tabular-nums ${
                        isActive ? "text-ember" : "text-bone-mute"
                      }`}
                    >
                      {(chatSessions.length - idx).toString().padStart(2, "0")}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span
                        className={`block truncate text-sm leading-tight ${
                          isActive ? "text-bone" : "text-bone-dim"
                        }`}
                      >
                        {session.title || "Untitled session"}
                      </span>
                      {session.createdAt && (
                        <span className="mt-0.5 block font-mono text-[10px] uppercase tracking-wider text-bone-mute">
                          {formatDate(session.createdAt)}
                        </span>
                      )}
                    </span>
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
