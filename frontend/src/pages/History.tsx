import { useAuth, login } from "@/contexts/AuthContext";
import { useChat } from "@/contexts/ChatContext";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
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

function preview(session: ChatSession): string {
  const firstUser = session.messages?.find((m) => m.role === "user");
  if (firstUser?.content) {
    const snip = firstUser.content.replace(/\s+/g, " ").trim();
    return snip.length > 140 ? snip.slice(0, 140) + "…" : snip;
  }
  return "No messages yet.";
}

export default function HistoryPage() {
  const navigate = useNavigate();
  const { user, isLoading: authLoading } = useAuth();
  const { createNewChat } = useChat();
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");

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

  const filtered = search.trim()
    ? sessions.filter((s) => {
        const q = search.toLowerCase();
        return (
          s.title?.toLowerCase().includes(q) ||
          s.messages?.some((m) => m.content?.toLowerCase().includes(q))
        );
      })
    : sessions;

  function openInChat(sessionId: string) {
    navigate(`/chat?session=${encodeURIComponent(sessionId)}`);
  }

  return (
    <div className="flex h-screen flex-col bg-ink">
      <Header />

      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-4xl px-8 py-10">
          <div className="reveal flex items-end justify-between border-b border-rule pb-6">
            <div>
              <p className="eyebrow">Conversation archive</p>
              <h1 className="font-display mt-2 text-5xl leading-none text-bone">
                {sessions.length}{" "}
                <span className="text-bone-mute">
                  {sessions.length === 1 ? "conversation" : "conversations"}
                </span>
              </h1>
            </div>
            <button
              onClick={() => {
                // Explicit "start fresh" — push a new session into the
                // context BEFORE navigating, so /chat's useEffect sees a
                // newly-set currentSessionId rather than the previous
                // chat's id and keeps it instead of starting over.
                createNewChat();
                navigate("/chat");
              }}
              className="eyebrow inline-flex items-center gap-2 border border-rule px-3 py-1.5 transition-colors hover:border-bone-mute hover:text-bone"
            >
              <span className="font-mono text-sm leading-none">+</span>
              New conversation
            </button>
          </div>

          <div className="reveal stagger-1 mt-6 border-b border-rule-soft pb-4">
            <input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search transcripts…"
              className="w-full border-b border-rule bg-transparent py-2 font-mono text-[12px] text-bone placeholder:text-bone-mute focus:border-bone-mute focus:outline-none"
            />
          </div>

          {loading ? (
            <p className="px-1 py-10 eyebrow">
              Reading
              <span className="cursor-block ml-2 align-baseline" />
            </p>
          ) : filtered.length === 0 ? (
            <div className="reveal stagger-2 px-1 py-10">
              <p className="eyebrow">{sessions.length === 0 ? "Empty archive" : "No matches"}</p>
              <h3 className="font-display mt-3 text-3xl leading-tight text-bone">
                {sessions.length === 0 ? (
                  <>
                    Start your first <em className="text-ember">conversation</em>.
                  </>
                ) : (
                  <>Try a different <em className="text-ember">search</em>.</>
                )}
              </h3>
              {sessions.length === 0 && (
                <p className="mt-4 max-w-md text-sm text-bone-dim">
                  Sessions you start in chat appear here. Click any past
                  conversation to pick up where you left off.
                </p>
              )}
            </div>
          ) : (
            <ul className="reveal stagger-2">
              {filtered.map((session, idx) => {
                const num = (filtered.length - idx).toString().padStart(3, "0");
                return (
                  <li key={session.id}>
                    <button
                      onClick={() => openInChat(session.id)}
                      className="group flex w-full items-baseline gap-5 border-b border-rule-soft px-1 py-5 text-left transition-colors hover:bg-ink-elev"
                    >
                      <span className="font-mono text-[11px] tabular-nums text-bone-mute">
                        {num}
                      </span>

                      <div className="min-w-0 flex-1">
                        <h3 className="font-display text-xl leading-tight text-bone transition-colors group-hover:text-ember">
                          {session.title || "Untitled conversation"}
                        </h3>
                        <p className="mt-1 line-clamp-2 text-[0.95rem] leading-relaxed text-bone-dim">
                          {preview(session)}
                        </p>
                        <p className="mt-2 font-mono text-[10px] uppercase tracking-eyebrow text-bone-mute">
                          {session.messages?.length ?? 0} message
                          {(session.messages?.length ?? 0) === 1 ? "" : "s"}
                          <span className="mx-2" aria-hidden>·</span>
                          {formatDateTime(session.createdAt)}
                        </p>
                      </div>

                      <span className="font-mono text-base text-bone-mute transition-all group-hover:translate-x-0.5 group-hover:text-ember">
                        →
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
