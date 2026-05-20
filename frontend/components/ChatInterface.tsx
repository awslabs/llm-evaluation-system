"use client";

import { useState } from "react";
import { useChat } from "@/contexts/ChatContext";
import MessageList from "./MessageList";
import MessageInput from "./MessageInput";

export default function ChatInterface() {
  const {
    sendMessage,
    cancelRequest,
    handleDocumentsUploaded,
    isLoading,
    isCancelling,
    createNewChat,
  } = useChat();
  const [input, setInput] = useState("");

  // + New chat needs to BOTH swap the session id AND clear ?session=X
  // from the URL. Without the URL clear, /chat/page.tsx's useEffect
  // re-fires when chatSessions/currentSessionId change, reads the
  // still-present sessionParam, and calls loadChat(X) — instantly
  // restoring the old messages.
  //
  // We use window.history.replaceState directly rather than
  // next/navigation's router.replace, because router.replace is a
  // no-op in BUILD_MODE=export (the local viewer's deploy mode):
  // the static router doesn't update the URL bar, so useSearchParams
  // keeps returning the old value. replaceState always works.
  const handleNewChat = () => {
    if (typeof window !== "undefined" && window.location.search) {
      window.history.replaceState({}, "", "/chat");
    }
    createNewChat();
  };

  const handleSend = async () => {
    if (!input.trim() || isLoading || isCancelling) return;

    await sendMessage(input);
    setInput("");
  };

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      {/* Lightweight toolbar — gives users a way to start a fresh
          session without leaving /chat. The /history "+ New conversation"
          works too but isn't discoverable from inside an open chat. */}
      <div className="flex items-center justify-end border-b border-rule-soft bg-ink px-6 py-2">
        <button
          onClick={handleNewChat}
          disabled={isLoading}
          className="eyebrow inline-flex items-center gap-2 border border-rule px-3 py-1.5 transition-colors hover:border-bone-mute hover:text-bone disabled:cursor-not-allowed disabled:opacity-40"
          title="Start a fresh conversation"
        >
          <span className="font-mono text-sm leading-none">+</span>
          New chat
        </button>
      </div>
      <MessageList />
      <MessageInput
        value={input}
        onChange={setInput}
        onSend={handleSend}
        onCancel={cancelRequest}
        // Disable input AND send during the post-cancel cooldown so
        // the user can't fire a new request into the backend's
        // still-draining cleanup → "network error" race.
        disabled={isLoading || isCancelling}
        isStreaming={isLoading}
        isCancelling={isCancelling}
        onDocumentsUploaded={handleDocumentsUploaded}
      />
    </div>
  );
}
