"use client";

import { useAuth, login } from "@/contexts/AuthContext";
import { useEffect, useState } from "react";
import Header from "@/components/Header";

interface ChatSession {
  id: string;
  title: string;
  createdAt: string;
  messages: { role: string; content: string; timestamp: string }[];
}

function formatDateTime(iso: string): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "";
  }
}

function formatTime(iso: string): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  } catch {
    return "";
  }
}

export default function HistoryPage() {
  const { user, isLoading: authLoading } = useAuth();
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [selectedSession, setSelectedSession] = useState<ChatSession | null>(
    null,
  );
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!authLoading && !user) {
      login();
    }
  }, [authLoading, user]);

  useEffect(() => {
    if (!user?.name) return;
    fetch(`/api/sessions?user_id=${encodeURIComponent(user.name)}`)
      .then((res) => (res.ok ? res.json() : { sessions: [] }))
      .then((data) => {
        setSessions(data.sessions || []);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, [user?.name]);

  if (authLoading || !user) {
    return (
      <div className="flex h-screen items-center justify-center bg-ink">
        <span className="eyebrow">
          Identifying
          <span className="cursor-block ml-2 align-baseline" />
        </span>
      </div>
    );
  }

  return (
    <div className="flex h-screen flex-col bg-ink">
      <Header />

      <div className="flex flex-1 overflow-hidden">
        <aside className="flex w-80 flex-col border-r border-rule bg-ink-elev">
          <div className="flex items-baseline justify-between border-b border-rule-soft px-5 py-4">
            <p className="eyebrow">Archive</p>
            <span className="font-mono text-[10px] tabular-nums text-bone-mute">
              {sessions.length.toString().padStart(3, "0")} sessions
            </span>
          </div>

          <div className="flex-1 overflow-y-auto">
            {loading ? (
              <p className="px-5 py-4 eyebrow">
                Reading
                <span className="cursor-block ml-2 align-baseline" />
              </p>
            ) : sessions.length === 0 ? (
              <p className="px-5 py-6 text-sm italic leading-relaxed text-bone-mute">
                <span className="font-display not-italic text-bone">
                  No conversations yet.
                </span>
                <br />
                Sessions you start in chat will appear here.
              </p>
            ) : (
              <ul>
                {sessions.map((session, idx) => {
                  const active = selectedSession?.id === session.id;
                  return (
                    <li key={session.id}>
                      <button
                        onClick={() => setSelectedSession(session)}
                        className={`group flex w-full items-baseline gap-3 border-b border-rule-soft border-l-2 px-4 py-3 text-left transition-colors ${
                          active
                            ? "border-l-ember bg-ink-raised"
                            : "border-l-transparent hover:border-l-rule hover:bg-ink-raised/40"
                        }`}
                      >
                        <span
                          className={`font-mono text-[10px] tabular-nums ${
                            active ? "text-ember" : "text-bone-mute"
                          }`}
                        >
                          {(sessions.length - idx).toString().padStart(3, "0")}
                        </span>
                        <div className="min-w-0 flex-1">
                          <div
                            className={`truncate text-[13px] leading-tight ${
                              active ? "text-bone" : "text-bone-dim"
                            }`}
                          >
                            {session.title || "Untitled session"}
                          </div>
                          <div className="mt-1 flex items-baseline gap-2 font-mono text-[10px] uppercase tracking-eyebrow text-bone-mute">
                            <span>{formatDateTime(session.createdAt)}</span>
                            {session.messages && (
                              <>
                                <span aria-hidden>·</span>
                                <span>{session.messages.length} msgs</span>
                              </>
                            )}
                          </div>
                        </div>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </aside>

        <div className="flex-1 overflow-y-auto">
          {selectedSession ? (
            <div className="mx-auto max-w-3xl px-8 py-10">
              <div className="reveal border-b border-rule pb-6">
                <p className="eyebrow">Session transcript</p>
                <h2 className="font-display mt-2 text-3xl leading-tight text-bone">
                  {selectedSession.title || "Untitled session"}
                </h2>
                <p className="mt-2 font-mono text-[11px] uppercase tracking-eyebrow text-bone-mute">
                  Opened {formatDateTime(selectedSession.createdAt)} ·{" "}
                  {selectedSession.messages?.length ?? 0} messages
                </p>
              </div>

              <ul className="reveal stagger-1">
                {selectedSession.messages?.map((msg, i) => {
                  const isUser = msg.role === "user";
                  return (
                    <li key={i} className="border-b border-rule-soft py-5">
                      <div className="flex items-baseline gap-3">
                        <span
                          className={`font-mono text-[10px] uppercase tracking-eyebrow ${
                            isUser ? "text-ember" : "text-bone-mute"
                          }`}
                        >
                          {isUser ? "You" : "Observatory"}
                        </span>
                        <span className="font-mono text-[10px] tabular-nums text-bone-mute">
                          {formatTime(msg.timestamp)}
                        </span>
                      </div>
                      <div className="mt-2 whitespace-pre-wrap break-words text-[0.95rem] leading-relaxed text-bone">
                        {msg.content}
                      </div>
                    </li>
                  );
                })}
              </ul>
            </div>
          ) : (
            <div className="flex h-full items-center justify-center px-8">
              <div className="max-w-md text-center">
                <p className="eyebrow">No session selected</p>
                <h3 className="font-display mt-3 text-4xl leading-tight text-bone">
                  <em className="text-ember">Choose</em> a session to read.
                </h3>
                <p className="mt-4 text-sm text-bone-dim">
                  Past conversations are preserved here in full — including any
                  results they produced.
                </p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
