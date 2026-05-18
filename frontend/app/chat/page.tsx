"use client";

import { useAuth, login } from "@/contexts/AuthContext";
import { useEffect } from "react";
import ChatInterface from "@/components/ChatInterface";
import Header from "@/components/Header";
import Sidebar from "@/components/Sidebar";

export default function ChatPage() {
  const { user, isLoading } = useAuth();

  useEffect(() => {
    if (!isLoading && !user) {
      login();
    }
  }, [isLoading, user]);

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
      <div className="flex flex-1 overflow-hidden">
        <Sidebar />
        <ChatInterface />
      </div>
    </div>
  );
}
