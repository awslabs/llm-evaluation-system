"use client";

import { useState } from "react";
import { useChat } from "@/contexts/ChatContext";
import MessageList from "./MessageList";
import MessageInput from "./MessageInput";

export default function ChatInterface() {
  const { sendMessage, cancelRequest, handleDocumentsUploaded, isLoading } = useChat();
  const [input, setInput] = useState("");

  const handleSend = async () => {
    if (!input.trim() || isLoading) return;

    await sendMessage(input);
    setInput("");
  };

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
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
