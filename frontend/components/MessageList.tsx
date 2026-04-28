"use client";

import { useChat } from "@/contexts/ChatContext";
import { useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export default function MessageList() {
  const { messages, isLoading } = useChat();
  const router = useRouter();
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  return (
    <div className="flex-1 overflow-y-auto px-4 py-8">
      <div className="mx-auto max-w-3xl space-y-6">
        {messages.map((message) => (
          <div
            key={message.id}
            className={`flex ${
              message.role === "user" ? "justify-end" : "justify-start"
            }`}
          >
            <div
              className={`max-w-[80%] rounded-lg px-4 py-3 ${
                message.role === "user"
                  ? "bg-claude-accent text-white"
                  : "bg-claude-surface text-claude-text"
              }`}
            >
              {/* Show progress/thinking when streaming */}
              {message.metadata?.isStreaming && message.metadata?.progress && (
                <div className="mb-2 text-sm opacity-70 italic">
                  {message.metadata.progress}
                </div>
              )}

              {/* Only show content if it's not just the status message */}
              {message.content && message.content !== message.metadata?.progress && (
                <div className={message.role === "user" ? "whitespace-pre-wrap break-words" : "break-words"}>
                  {message.role === "assistant" ? (
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm]}
                      components={{
                        a: ({ node, ...props }) => {
                          const href = props.href || "";
                          const isResults = href.includes("/results");
                          if (isResults) {
                            const path = href.replace(/^https?:\/\/[^/]+/, "");
                            return (
                              <a
                                {...props}
                                href={path}
                                onClick={(e) => {
                                  e.preventDefault();
                                  router.push(path);
                                }}
                              />
                            );
                          }
                          return <a {...props} target="_blank" rel="noopener noreferrer" />;
                        },
                        p: ({ children }) => <p style={{ margin: '0 0 8px 0', lineHeight: '1.5' }}>{children}</p>,
                        ul: ({ children }) => <ul style={{ margin: '0 0 8px 0', paddingLeft: '1.5rem', lineHeight: '1.5' }}>{children}</ul>,
                        ol: ({ children }) => <ol style={{ margin: '0 0 8px 0', paddingLeft: '1.5rem', lineHeight: '1.5' }}>{children}</ol>,
                        li: ({ children }) => <li style={{ margin: 0, lineHeight: '1.5' }}>{children}</li>,
                      }}
                    >
                      {message.content}
                    </ReactMarkdown>
                  ) : (
                    message.content
                  )}
                </div>
              )}
              <div className="mt-2 text-xs opacity-60">
                {new Date(message.timestamp).toLocaleTimeString()}
              </div>
            </div>
          </div>
        ))}
        {isLoading && (
          <div className="flex justify-start">
            <div className="max-w-[80%] rounded-lg bg-claude-surface px-4 py-3 text-claude-text">
              <div className="flex space-x-2">
                <div className="h-2 w-2 animate-bounce rounded-full bg-claude-muted"></div>
                <div className="h-2 w-2 animate-bounce rounded-full bg-claude-muted delay-100"></div>
                <div className="h-2 w-2 animate-bounce rounded-full bg-claude-muted delay-200"></div>
              </div>
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
