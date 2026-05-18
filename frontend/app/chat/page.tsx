"use client";

import { Suspense, useEffect } from "react";
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

  useEffect(() => {
    if (!isLoading && !user) {
      login();
    }
  }, [isLoading, user]);

  // When the URL carries ?session=X, load that conversation as soon as
  // sessions are available. When it doesn't, start a fresh new chat —
  // /chat is the focused single-conversation view; the past list lives
  // on /history.
  useEffect(() => {
    if (isLoading || !user) return;

    if (sessionParam) {
      if (chatSessions.some((s) => s.id === sessionParam)) {
        if (currentSessionId !== sessionParam) {
          loadChat(sessionParam);
        }
      }
      // If sessions haven't loaded yet, this effect re-runs when they do.
      return;
    }

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
