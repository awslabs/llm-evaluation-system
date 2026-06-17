import { useEffect, useRef } from "react";
import { useSearchParams } from "react-router-dom";
import type { ChatSession } from "@/contexts/ChatContext";
import { useAuth, login } from "@/contexts/AuthContext";
import { useChat } from "@/contexts/ChatContext";
import ChatInterface from "@/components/ChatInterface";
import Header from "@/components/Header";

export default function ChatPage() {
  const { user, isLoading } = useAuth();
  const { loadChat, createNewChat, chatSessions, currentSessionId, reconnectIfRunning } =
    useChat();
  const [searchParams, setSearchParams] = useSearchParams();
  const sessionParam = searchParams.get("session");

  // Once we've auto-loaded the URL's ?session=X for this URL value, don't
  // re-load it. Without this guard, when the user clicks "+ New chat",
  // createNewChat updates currentSessionId, this effect re-fires (chatSessions
  // changed), sees session X still in chatSessions, and calls loadChat(X) —
  // instantly undoing the new-chat action.
  const autoLoadedRef = useRef<string | null>(null);

  useEffect(() => {
    if (!isLoading && !user) {
      login();
    }
  }, [isLoading, user]);

  // When the URL carries ?session=X, load that conversation ONCE.
  // When it doesn't, start a fresh new chat. /chat is the focused
  // single-conversation view; the past list lives on /history.
  useEffect(() => {
    if (isLoading || !user) return;

    if (sessionParam) {
      if (autoLoadedRef.current === sessionParam) return;
      if (chatSessions.some((s) => s.id === sessionParam)) {
        loadChat(sessionParam);
        autoLoadedRef.current = sessionParam;
        // If this session was still streaming when the page was loaded/
        // refreshed, reattach to the live backend stream so tokens keep
        // arriving instead of the response appearing frozen.
        reconnectIfRunning(sessionParam);
      }
      // If sessions haven't loaded yet, this effect re-runs when they do.
      return;
    }

    // URL has no ?session — if we previously auto-loaded one, reset the
    // marker so a later /history → /chat?session=X navigation works.
    autoLoadedRef.current = null;
    if (!currentSessionId) {
      createNewChat();
    }
  }, [
    isLoading,
    user,
    sessionParam,
    chatSessions,
    currentSessionId,
    loadChat,
    createNewChat,
    reconnectIfRunning,
  ]);

  // Reflect the active session into the URL once it has real content, so a
  // refresh restores it (previously a fresh chat had no ?session= and a reload
  // dropped the whole conversation). Only fires for a session that exists in
  // the list AND has messages — never the empty new-chat stub — so it can't
  // race the "+ New chat" clear.
  useEffect(() => {
    if (!currentSessionId || sessionParam === currentSessionId) return;
    const s = chatSessions.find(
      (x: ChatSession) => x.id === currentSessionId,
    );
    if (s && s.messages.length > 0) {
      setSearchParams({ session: currentSessionId }, { replace: true });
      autoLoadedRef.current = currentSessionId;
    }
  }, [currentSessionId, chatSessions, sessionParam, setSearchParams]);

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center bg-ink">
        <span className="eyebrow">
          Identifying
          <span className="cursor-block ml-2 align-baseline" />
        </span>
      </div>
    );
  }

  if (!user) {
    return null;
  }

  return (
    <div className="flex h-screen flex-col bg-ink">
      <Header />
      <ChatInterface />
    </div>
  );
}
