import { useChat } from "@/contexts/ChatContext";
import { useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

function formatTime(ts: string): string {
  try {
    return new Date(ts).toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  } catch {
    return "";
  }
}

export default function MessageList() {
  const { messages, isLoading } = useChat();
  const navigate = useNavigate();
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const showEmpty = messages.length === 0 && !isLoading;

  return (
    <div className="flex-1 overflow-y-auto px-6 py-10">
      <div className="mx-auto max-w-3xl">
        {showEmpty && (
          <div className="reveal mt-8 border-t border-rule pt-10">
            <p className="eyebrow">New conversation</p>
            <h2 className="font-display mt-3 text-4xl leading-tight text-bone">
              What would you like to{" "}
              <em className="text-ember">evaluate?</em>
            </h2>
            <p className="mt-5 max-w-md text-sm leading-relaxed text-bone-dim">
              Try:{" "}
              <span className="text-bone">
                &ldquo;Compare Sonnet and Haiku on the qa_v3 dataset and judge
                with strictness.&rdquo;
              </span>{" "}
              Or drop a CSV of test cases below to start a new evaluation.
            </p>
          </div>
        )}

        <ul>
          {messages.map((message, idx) => {
            const isUser = message.role === "user";
            const prev = idx > 0 ? messages[idx - 1] : null;
            const roleChanged = !prev || prev.role !== message.role;
            const progress = message.metadata?.progress;
            const isStreaming = message.metadata?.isStreaming;
            const showContent =
              message.content && message.content !== progress;

            return (
              <li
                key={message.id}
                className={`relative py-5 ${
                  roleChanged && idx > 0 ? "border-t border-rule-soft" : ""
                } ${idx === 0 ? "border-t border-rule-soft" : ""}`}
              >
                <div className="flex items-baseline gap-3">
                  <span
                    className={`font-mono text-[10px] uppercase tracking-eyebrow ${
                      isUser ? "text-ember" : "text-bone-mute"
                    }`}
                  >
                    {isUser ? "You" : "Observatory"}
                  </span>
                  <span className="font-mono text-[10px] tabular-nums text-bone-mute">
                    {formatTime(message.timestamp)}
                  </span>
                  {isStreaming && (
                    <span className="font-mono text-[10px] uppercase tracking-eyebrow text-ember">
                      Live
                    </span>
                  )}
                </div>

                <div className="mt-2 text-[0.95rem] leading-relaxed text-bone">
                  {isStreaming && progress && (
                    <div className="mb-2 flex items-center gap-2 text-sm italic text-bone-dim">
                      <span>{progress}</span>
                      <span className="cursor-block bg-ember" />
                    </div>
                  )}

                  {showContent &&
                    (isUser ? (
                      <div className="whitespace-pre-wrap break-words">
                        {message.content}
                      </div>
                    ) : (
                      <div className="markdown-content break-words">
                        <ReactMarkdown
                          remarkPlugins={[remarkGfm]}
                          components={{
                            a: ({ ...props }) => {
                              const href = props.href || "";
                              const isResults = href.includes("/results");
                              if (isResults) {
                                const path = href.replace(
                                  /^https?:\/\/[^/]+/,
                                  "",
                                );
                                return (
                                  <a
                                    {...props}
                                    href={path}
                                    onClick={(e) => {
                                      e.preventDefault();
                                      navigate(path);
                                    }}
                                  />
                                );
                              }
                              return (
                                <a
                                  {...props}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                />
                              );
                            },
                          }}
                        >
                          {message.content}
                        </ReactMarkdown>
                      </div>
                    ))}
                </div>
              </li>
            );
          })}

          {(() => {
            // Show the "Working" placeholder only in the gap between the
            // user sending a message and the assistant's stream starting.
            // Once the assistant message exists (streaming or not), it
            // renders its own header + progress — showing both gave a
            // duplicate "Observatory" row.
            if (!isLoading || messages.length === 0) return null;
            const last = messages[messages.length - 1];
            if (last?.role === "assistant") return null;
            return (
              <li className="border-t border-rule-soft py-5">
                <div className="flex items-baseline gap-3">
                  <span className="font-mono text-[10px] uppercase tracking-eyebrow text-bone-mute">
                    Observatory
                  </span>
                  <span className="font-mono text-[10px] uppercase tracking-eyebrow text-ember">
                    Working
                  </span>
                </div>
                <div className="mt-2 text-sm italic text-bone-dim">
                  Composing response
                  <span className="cursor-block ml-2 bg-ember align-baseline" />
                </div>
              </li>
            );
          })()}
        </ul>

        <div ref={bottomRef} />
      </div>
    </div>
  );
}
