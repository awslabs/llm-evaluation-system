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
    createNewChat,
  } = useChat();
  const [input, setInput] = useState("");

  const handleSend = async () => {
    if (!input.trim() || isLoading) return;

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
          onClick={createNewChat}
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
        disabled={isLoading}
        isStreaming={isLoading}
        onDocumentsUploaded={handleDocumentsUploaded}
      />
    </div>
  );
}
