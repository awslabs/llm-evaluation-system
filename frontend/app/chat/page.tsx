"use client";

import { useAuth, login } from "@/contexts/AuthContext";
import { useEffect } from "react";
import ChatInterface from "@/components/ChatInterface";
import Sidebar from "@/components/Sidebar";
import { ChatProvider } from "@/contexts/ChatContext";

export default function ChatPage() {
  const { user, isLoading } = useAuth();

  useEffect(() => {
    if (!isLoading && !user) {
      login();
    }
  }, [isLoading, user]);

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center bg-claude-bg">
        <div className="text-claude-muted">Loading...</div>
      </div>
    );
  }

  if (!user) {
    return null;
  }

  return (
    <ChatProvider>
      <div className="flex h-screen bg-claude-bg">
        <Sidebar />
        <ChatInterface />
      </div>
    </ChatProvider>
  );
}
