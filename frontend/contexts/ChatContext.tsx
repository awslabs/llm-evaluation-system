"use client";

import { createContext, useContext, useState, useCallback, useEffect } from "react";
import { useAuth } from "@/contexts/AuthContext";
import type { UploadResult } from "@/components/MessageInput";

export interface Message {
  id: string;
  role: "user" | "assistant" | "status";
  content: string;
  timestamp: string;
  metadata?: {
    isStreaming?: boolean;
    tool?: string;
    progress?: string;
  };
}

export interface ChatSession {
  id: string;
  title: string;
  createdAt: string;
  messages: Message[];
}

interface ChatContextType {
  messages: Message[];
  chatSessions: ChatSession[];
  currentSessionId: string | null;
  isLoading: boolean;
  sendMessage: (content: string) => Promise<void>;
  cancelRequest: () => Promise<void>;
  handleDocumentsUploaded: (result: UploadResult) => void;
  createNewChat: () => void;
  loadChat: (sessionId: string) => void;
}

const ChatContext = createContext<ChatContextType | undefined>(undefined);

export function ChatProvider({ children }: { children: React.ReactNode }) {
  const { user, isLoading: authLoading } = useAuth();
  const [messages, setMessages] = useState<Message[]>([]);
  const [chatSessions, setChatSessions] = useState<ChatSession[]>([]);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  const loadUserSessions = async () => {
    if (!user?.name) return;
    try {
      const response = await fetch(`/api/sessions?user_id=${encodeURIComponent(user.name)}`);
      if (response.ok) {
        const data = await response.json();
        const loadedSessions = data.sessions || [];
        setChatSessions(loadedSessions);

        // Load most recent session
        if (loadedSessions.length > 0) {
          setCurrentSessionId(loadedSessions[0].id);
          setMessages(loadedSessions[0].messages || []);
        }
      }
    } catch (error) {
      console.error("Failed to load sessions:", error);
    }
  };

  // Load chat sessions when authenticated
  useEffect(() => {
    if (!authLoading && user) {
      loadUserSessions();
    }
  }, [authLoading, user?.name]);


  const loadChat = useCallback((sessionId: string) => {
    const session = chatSessions.find((s) => s.id === sessionId);
    if (session) {
      setCurrentSessionId(sessionId);
      setMessages(session.messages);
    }
  }, [chatSessions]);

  const createNewChat = useCallback(() => {
    const newSession: ChatSession = {
      id: crypto.randomUUID(),
      title: "New Chat",
      createdAt: new Date().toISOString(),
      messages: [],
    };

    setChatSessions((prev) => [newSession, ...prev]);
    setCurrentSessionId(newSession.id);
    setMessages([]);
  }, []);


  const cancelRequest = useCallback(async () => {
    if (!currentSessionId || !isLoading) return;

    try {
      const response = await fetch(`/api/chat/cancel/${currentSessionId}`, {
        method: "POST",
      });
      if (response.ok) {
        console.log("Cancel request sent");
      }
    } catch (error) {
      console.error("Failed to cancel request:", error);
    }
  }, [currentSessionId, isLoading]);

  const sendMessage = useCallback(
    async (content: string) => {
      if (!user?.id) return;

      const userMessage: Message = {
        id: crypto.randomUUID(),
        role: "user",
        content: content,
        timestamp: new Date().toISOString(),
      };

      setMessages((prev) => [...prev, userMessage]);
      setIsLoading(true);

      try {
        const response = await fetch("/api/chat/message", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            message: content,
            session_id: currentSessionId,
            user_id: user?.id,
            stream: true,
          }),
        });

        if (!response.ok) {
          throw new Error("Failed to send message");
        }

        // Handle SSE streaming
        if (response.headers.get("content-type")?.includes("text/event-stream")) {
          const assistantMessageId = crypto.randomUUID();
          let assistantContent = "";
          let statusContent = "";
          let statusHistory: string[] = [];
          let actualSessionId = currentSessionId; // Track the actual session ID (may be updated by backend)

          // Add initial streaming message
          const streamingMessage: Message = {
            id: assistantMessageId,
            role: "assistant",
            content: "💭 Thinking...",
            timestamp: new Date().toISOString(),
            metadata: { isStreaming: true },
          };
          setMessages((prev) => [...prev, streamingMessage]);

          const reader = response.body?.getReader();
          const decoder = new TextDecoder();

          if (reader) {
            let currentEventType = "";

            while (true) {
              const { done, value } = await reader.read();
              if (done) break;

              const chunk = decoder.decode(value);
              const lines = chunk.split("\n");

              for (const line of lines) {
                if (line.startsWith("event: ")) {
                  currentEventType = line.slice(7).trim();
                  continue;
                }
                if (line.startsWith("data: ")) {
                  const jsonData = line.slice(6);
                  try {
                    const data = JSON.parse(jsonData);

                    // Handle different event types
                    if (currentEventType === "session") {
                      // Update session ID and add to sessions list if new
                      if (data.session_id) {
                        actualSessionId = data.session_id;
                        setCurrentSessionId(data.session_id);
                        // Add new session to list if it doesn't exist
                        setChatSessions((prev) => {
                          if (prev.some((s) => s.id === data.session_id)) {
                            return prev;
                          }
                          return [
                            {
                              id: data.session_id,
                              title: "New Chat",
                              createdAt: new Date().toISOString(),
                              messages: [],
                            },
                            ...prev,
                          ];
                        });
                      }
                    } else if (currentEventType === "progress" || currentEventType === "status") {
                      statusContent = data.message || data.content || "";
                      // Update status in the message
                      setMessages((prev) =>
                        prev.map((msg) =>
                          msg.id === assistantMessageId
                            ? {
                                ...msg,
                                content: assistantContent || statusContent,
                                metadata: { isStreaming: true, progress: statusContent },
                              }
                            : msg
                        )
                      );
                    } else if (currentEventType === "tool_call") {
                      const toolText = `🔧 ${data.tool}`;
                      statusHistory.push(toolText);
                      statusContent = statusHistory.join(' → ');
                      setMessages((prev) =>
                        prev.map((msg) =>
                          msg.id === assistantMessageId
                            ? {
                                ...msg,
                                content: assistantContent || statusContent,
                                metadata: { isStreaming: true, tool: data.tool, progress: statusContent },
                              }
                            : msg
                        )
                      );
                    } else if (currentEventType === "tool_result") {
                      statusContent = `✓ Tool ${data.tool} completed`;
                      setMessages((prev) =>
                        prev.map((msg) =>
                          msg.id === assistantMessageId
                            ? {
                                ...msg,
                                content: assistantContent || statusContent,
                                metadata: { isStreaming: true, progress: statusContent },
                              }
                            : msg
                        )
                      );
                    } else if (currentEventType === "text") {
                      // Streaming text token
                      assistantContent += data.content || "";
                      setMessages((prev) =>
                        prev.map((msg) =>
                          msg.id === assistantMessageId
                            ? {
                                ...msg,
                                content: assistantContent,
                                metadata: { isStreaming: true },
                              }
                            : msg
                        )
                      );
                    } else if (currentEventType === "thinking") {
                      // Agent thinking (legacy - now streamed via text events)
                      const thinkingText = `💭 ${data.message?.slice(0, 100) || 'Thinking'}${data.message?.length > 100 ? '...' : ''}`;
                      statusHistory.push(thinkingText);
                      statusContent = statusHistory.join(' → ');
                      setMessages((prev) =>
                        prev.map((msg) =>
                          msg.id === assistantMessageId
                            ? {
                                ...msg,
                                content: assistantContent || statusContent,
                                metadata: { isStreaming: true, progress: statusContent },
                              }
                            : msg
                        )
                      );
                    } else if (currentEventType === "complete") {
                      assistantContent = data.response;
                      setMessages((prev) =>
                        prev.map((msg) =>
                          msg.id === assistantMessageId
                            ? {
                                ...msg,
                                content: assistantContent,
                                metadata: { isStreaming: false },
                              }
                            : msg
                        )
                      );
                    } else if (currentEventType === "cancelled") {
                      // Request was cancelled by user
                      assistantContent += "\n\n*[Request cancelled]*";
                      setMessages((prev) =>
                        prev.map((msg) =>
                          msg.id === assistantMessageId
                            ? {
                                ...msg,
                                content: assistantContent,
                                metadata: { isStreaming: false },
                              }
                            : msg
                        )
                      );
                    } else if (currentEventType === "error") {
                      throw new Error(data.error || data.message || "Unknown error");
                    }
                  } catch (e) {
                    console.error("Error parsing SSE data:", e);
                  }
                }
              }
            }
          }

          // Final update - mark as complete
          setMessages((prev) =>
            prev.map((msg) =>
              msg.id === assistantMessageId
                ? { ...msg, metadata: { isStreaming: false } }
                : msg
            )
          );

          // Update session with new messages
          const finalAssistantMessage = {
            ...streamingMessage,
            content: assistantContent,
            metadata: { isStreaming: false },
          };

          setChatSessions((prev) =>
            prev.map((s) =>
              s.id === actualSessionId
                ? {
                    ...s,
                    messages: [...s.messages, userMessage, finalAssistantMessage],
                    title: s.messages.length === 0 ? content.slice(0, 50) : s.title,
                  }
                : s
            )
          );
        } else {
          // Fallback to non-streaming
          const data = await response.json();

          const assistantMessage: Message = {
            id: crypto.randomUUID(),
            role: "assistant",
            content: data.response,
            timestamp: new Date().toISOString(),
          };

          setMessages((prev) => [...prev, assistantMessage]);

          setChatSessions((prev) =>
            prev.map((s) =>
              s.id === currentSessionId
                ? {
                    ...s,
                    messages: [...s.messages, userMessage, assistantMessage],
                    title: s.messages.length === 0 ? content.slice(0, 50) : s.title,
                  }
                : s
            )
          );
        }
      } catch (error) {
        console.error("Failed to send message:", error);

        const errorMessage: Message = {
          id: crypto.randomUUID(),
          role: "assistant",
          content: "Sorry, I encountered an error processing your request.",
          timestamp: new Date().toISOString(),
        };

        setMessages((prev) => [...prev, errorMessage]);
      } finally {
        setIsLoading(false);
      }
    },
    [user?.id, currentSessionId]
  );

  // Handle document upload results - send a message to inform the AI
  const handleDocumentsUploaded = useCallback(
    (result: UploadResult) => {
      if (!result.success) {
        // Show error to user
        const errorMessage: Message = {
          id: crypto.randomUUID(),
          role: "status",
          content: `Upload failed: ${result.error || "Unknown error"}`,
          timestamp: new Date().toISOString(),
        };
        setMessages((prev) => [...prev, errorMessage]);
        return;
      }

      // Build message parts based on file types
      const messageParts: string[] = [];

      // Handle CSV results (already processed by backend)
      if (result.csv_results && result.csv_results.length > 0) {
        for (const csv of result.csv_results) {
          // Use the message from backend (includes success/error info)
          messageParts.push(csv.message);
        }
      }

      // Handle non-CSV files (PDFs, images, etc.) - use generate_qa_pairs flow
      if (result.non_csv_files && result.non_csv_files.length > 0) {
        const documentPaths = result.non_csv_files.map((f) =>
          result.folder ? `${result.folder}/${f}` : f
        );
        const pathList = documentPaths.join('", "');
        messageParts.push(`[Uploaded ${result.non_csv_files.length} document${result.non_csv_files.length > 1 ? "s" : ""}. Document paths for generate_qa_pairs: ["${pathList}"]]`);
      }

      // Fallback for old response format (no csv_results/non_csv_files)
      if (messageParts.length === 0) {
        const documentPaths = result.files.map((f) =>
          result.folder ? `${result.folder}/${f}` : f
        );
        const pathList = documentPaths.join('", "');
        messageParts.push(`[Uploaded ${result.count} document${result.count > 1 ? "s" : ""}. Document paths for generate_qa_pairs: ["${pathList}"]]`);
      }

      // Send combined message to agent
      const uploadMessage = messageParts.join("\n");
      sendMessage(uploadMessage);
    },
    [sendMessage]
  );

  return (
    <ChatContext.Provider
      value={{
        messages,
        chatSessions,
        currentSessionId,
        isLoading,
        sendMessage,
        cancelRequest,
        handleDocumentsUploaded,
        createNewChat,
        loadChat,
      }}
    >
      {children}
    </ChatContext.Provider>
  );
}

export function useChat() {
  const context = useContext(ChatContext);
  if (context === undefined) {
    throw new Error("useChat must be used within a ChatProvider");
  }
  return context;
}
