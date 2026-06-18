import { createContext, useContext, useState, useCallback, useEffect, useRef } from "react";
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
  isCancelling: boolean;
  sendMessage: (content: string) => Promise<void>;
  cancelRequest: () => Promise<void>;
  handleDocumentsUploaded: (result: UploadResult) => void;
  createNewChat: () => void;
  loadChat: (sessionId: string) => void;
  // If `sessionId` is still streaming on the backend (e.g. after a page
  // refresh mid-response), reattach to the live SSE stream. Returns true if a
  // reconnect happened. No-op if the session already finished.
  reconnectIfRunning: (sessionId: string) => Promise<boolean>;
}

const ChatContext = createContext<ChatContextType | undefined>(undefined);

export function ChatProvider({ children }: { children: React.ReactNode }) {
  const { user, isLoading: authLoading } = useAuth();
  const [messages, setMessages] = useState<Message[]>([]);
  const [chatSessions, setChatSessions] = useState<ChatSession[]>([]);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  // isCancelling: true between the moment Stop is clicked and the
  // moment the SSE stream actually terminates. Without this, the
  // button has no visual state change on click — users mash it
  // because they think it didn't register.
  const [isCancelling, setIsCancelling] = useState(false);
  // Timestamp when Stop was clicked. Used to enforce a brief cooldown
  // (~2s) between Stop and the next message so the backend's async
  // cleanup (MCP cancel + reconnect) has time to drain. Without this,
  // sending too fast triggers race conditions that surface as
  // "network error" in the UI.
  const cancelledAtRef = useRef<number | null>(null);
  const POST_CANCEL_COOLDOWN_MS = 2000;

  const loadUserSessions = async () => {
    if (!user?.name) return;
    try {
      const response = await fetch(`/api/sessions?user_id=${encodeURIComponent(user.name)}`);
      if (response.ok) {
        const data = await response.json();
        setChatSessions(data.sessions || []);
        // No auto-load — the /chat and /history pages decide which session
        // (if any) is active based on the URL.
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
    if (!currentSessionId || !isLoading || isCancelling) return;

    // Flip immediately so the Stop button shows "Stopping…" and goes
    // disabled — no waiting for the HTTP fetch or SSE termination.
    setIsCancelling(true);
    // Stamp the cancel time so sendMessage's finally can enforce a
    // minimum stopping period before re-enabling the input. Without
    // this, sending right after Stop races the backend's async
    // cleanup and surfaces as a fetch-level network error.
    cancelledAtRef.current = Date.now();
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
  }, [currentSessionId, isLoading, isCancelling]);

  // Drain an SSE response into the given assistant bubble. Shared by
  // sendMessage and reconnectIfRunning so the event handling never diverges.
  // Returns the final assistant text + the backend session id (if announced).
  const consumeStream = useCallback(
    async (
      response: Response,
      assistantMessageId: string,
    ): Promise<{ content: string; sessionId: string | null }> => {
      let assistantContent = "";
      let statusContent = "";
      const statusHistory: string[] = [];
      let streamSessionId: string | null = null;

      const patchAssistant = (fields: Partial<Message>) =>
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === assistantMessageId ? { ...msg, ...fields } : msg,
          ),
        );

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

                if (currentEventType === "session") {
                  if (data.session_id) {
                    streamSessionId = data.session_id;
                    setCurrentSessionId(data.session_id);
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
                  patchAssistant({
                    content: assistantContent || statusContent,
                    metadata: { isStreaming: true, progress: statusContent },
                  });
                } else if (currentEventType === "tool_call") {
                  const toolText = `🔧 ${data.tool}`;
                  statusHistory.push(toolText);
                  statusContent = statusHistory.join(' → ');
                  patchAssistant({
                    content: assistantContent || statusContent,
                    metadata: { isStreaming: true, tool: data.tool, progress: statusContent },
                  });
                } else if (currentEventType === "tool_result") {
                  statusContent = `✓ Tool ${data.tool} completed`;
                  patchAssistant({
                    content: assistantContent || statusContent,
                    metadata: { isStreaming: true, progress: statusContent },
                  });
                } else if (currentEventType === "text") {
                  assistantContent += data.content || "";
                  patchAssistant({
                    content: assistantContent,
                    metadata: { isStreaming: true },
                  });
                } else if (currentEventType === "thinking") {
                  const thinkingText = `💭 ${data.message?.slice(0, 100) || 'Thinking'}${data.message?.length > 100 ? '...' : ''}`;
                  statusHistory.push(thinkingText);
                  statusContent = statusHistory.join(' → ');
                  patchAssistant({
                    content: assistantContent || statusContent,
                    metadata: { isStreaming: true, progress: statusContent },
                  });
                } else if (currentEventType === "complete") {
                  assistantContent = data.response;
                  patchAssistant({
                    content: assistantContent,
                    metadata: { isStreaming: false },
                  });
                } else if (currentEventType === "cancelled") {
                  assistantContent += "\n\n*[Request cancelled]*";
                  patchAssistant({
                    content: assistantContent,
                    metadata: { isStreaming: false },
                  });
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

      // Mark complete in case the stream ended without an explicit terminal event.
      patchAssistant({ metadata: { isStreaming: false } });
      return { content: assistantContent, sessionId: streamSessionId };
    },
    [],
  );

  // After a page refresh mid-response, the backend keeps the agent running
  // (the SSE client just disconnected). Re-POST with an empty message to
  // reattach to the live queue and show tokens as they continue to arrive.
  const reconnectIfRunning = useCallback(
    async (sessionId: string): Promise<boolean> => {
      let running = false;
      try {
        const r = await fetch(`/api/chat/status/${sessionId}`);
        if (r.ok) running = (await r.json()).running === true;
      } catch {
        return false;
      }
      if (!running) return false;

      const assistantMessageId = crypto.randomUUID();
      setMessages((prev) => [
        ...prev,
        {
          id: assistantMessageId,
          role: "assistant",
          content: "💭 Reconnecting…",
          timestamp: new Date().toISOString(),
          metadata: { isStreaming: true },
        },
      ]);
      setIsLoading(true);
      try {
        const response = await fetch("/api/chat/message", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          // Empty message: if the run finishes between status-check and POST,
          // this starts a harmless empty turn rather than duplicating text.
          body: JSON.stringify({ message: "", session_id: sessionId, user_id: user?.id, stream: true }),
        });
        if (!response.ok || !response.headers.get("content-type")?.includes("text/event-stream")) {
          throw new Error("reconnect failed");
        }
        await consumeStream(response, assistantMessageId);
        // Pull the now-complete transcript from the DB so the cached session
        // (used by loadChat on later navigation) is consistent.
        await loadUserSessions();
        return true;
      } catch (error) {
        console.error("Reconnect failed:", error);
        setMessages((prev) => prev.filter((m) => m.id !== assistantMessageId));
        return false;
      } finally {
        setIsLoading(false);
      }
    },
    [consumeStream, user?.id],
  );

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
          const streamingMessage: Message = {
            id: assistantMessageId,
            role: "assistant",
            content: "💭 Thinking...",
            timestamp: new Date().toISOString(),
            metadata: { isStreaming: true },
          };
          setMessages((prev) => [...prev, streamingMessage]);

          const { content: assistantContent, sessionId } = await consumeStream(
            response,
            assistantMessageId,
          );
          const actualSessionId = sessionId || currentSessionId;

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

        // Surface the actual error class + ref so the user (or whoever's
        // tailing logs) can correlate. The backend's `_user_safe_error`
        // gives us "ExceptionType (ref: <id>)" which is safe to show
        // (no internal paths, no message text). Falls back to the
        // generic phrase if we somehow lost the message.
        const detail =
          error instanceof Error && error.message
            ? error.message
            : "unknown error";
        const errorMessage: Message = {
          id: crypto.randomUUID(),
          role: "assistant",
          content: `Sorry, the request failed: ${detail}. Try again, or share the ref id if it keeps happening.`,
          timestamp: new Date().toISOString(),
        };

        setMessages((prev) => [...prev, errorMessage]);
      } finally {
        setIsLoading(false);
        // Enforce a post-cancel cooldown: keep isCancelling true until
        // ~2s after Stop was clicked so the backend's async cleanup
        // (MCP cancel + reconnect) has time to drain. Without this,
        // sending immediately after the SSE closes races the in-flight
        // cleanup and surfaces as a network error.
        if (cancelledAtRef.current !== null) {
          const elapsed = Date.now() - cancelledAtRef.current;
          const remaining = POST_CANCEL_COOLDOWN_MS - elapsed;
          if (remaining > 0) {
            await new Promise((r) => setTimeout(r, remaining));
          }
          cancelledAtRef.current = null;
        }
        setIsCancelling(false);
      }
    },
    [user?.id, currentSessionId, consumeStream]
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
        isCancelling,
        sendMessage,
        cancelRequest,
        handleDocumentsUploaded,
        createNewChat,
        loadChat,
        reconnectIfRunning,
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
