"use client";

import { Suspense, useEffect, useRef } from "react";
import { useSearchParams } from "next/navigation";
import { useAuth, login } from "@/contexts/AuthContext";
import { useChat } from "@/contexts/ChatContext";
import ChatInterface from "@/components/ChatInterface";
import Header from "@/components/Header";

function ChatContent() {
  const { user, isLoading } = useAuth();
  const { loadChat, createNewChat, chatSessions, currentSessionId } = useChat();
  const searchParams = useSearchParams();
  const sessionParam = searchParams.get("session");

  // Once we've auto-loaded the URL's ?session=X for this URL value,
  // don't re-load it. Without this guard, when the user clicks
  // "+ New chat", createNewChat updates currentSessionId, this effect
  // re-fires (chatSessions changed), sees sessionParam=X still in the
  // URL plus session X still in chatSessions, and calls loadChat(X) —
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
  ]);

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

export default function ChatPage() {
  return (
    <Suspense
      fallback={
        <div className="flex h-screen items-center justify-center bg-ink">
          <span className="eyebrow">
            Loading
            <span className="cursor-block ml-2 align-baseline" />
          </span>
        </div>
      }
    >
      <ChatContent />
    </Suspense>
  );
}
