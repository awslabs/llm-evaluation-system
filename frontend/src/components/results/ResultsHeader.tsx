import { useState } from "react";
import { useAuth } from "@/contexts/AuthContext";
import { useLocation, useNavigate } from "react-router-dom";
import ShareModal from "./ShareModal";

interface ResultsHeaderProps {
  groupId: string | null;
  // Owner of the selected eval. Absent/self = caller's own eval. A shared
  // eval (owner != caller) can't be re-shared and the report read must carry
  // the owner hint for the backend resolver.
  owner?: string | null;
  sessionId?: string | null;
}

interface NavItem {
  href: string;
  label: string;
  fullOnly?: boolean;
}

const NAV: NavItem[] = [
  { href: "/chat", label: "Chat", fullOnly: true },
  { href: "/history", label: "History", fullOnly: true },
  { href: "/results", label: "Results" },
  { href: "/data", label: "Data" },
  { href: "/optimizations", label: "Optimized" },
  { href: "/teams", label: "Teams", fullOnly: true },
];

export default function ResultsHeader({ groupId, owner }: ResultsHeaderProps) {
  const { user, logoutUrl, mode } = useAuth();
  const navigate = useNavigate();
  const pathname = useLocation().pathname;
  const visibleNav = NAV.filter((item) => !(mode === "viewer" && item.fullOnly));
  const [downloading, setDownloading] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);

  // A shared eval (owner present and not the caller) is not the caller's to
  // re-share. Only own evals get the Share affordance.
  const isOwn = !owner || owner === user?.id;

  const handleDownloadReport = async () => {
    if (!groupId) return;
    setDownloading(true);
    try {
      // This serves the caller's OWN previously-generated PDF. For a shared
      // eval the viewer won't have one yet (report generation for shared evals
      // is a later phase), so this 404s and prompts them to generate one.
      const response = await fetch(
        `/api/compare/report/${encodeURIComponent(groupId)}`,
      );
      if (response.status === 404) {
        alert(
          "No report has been generated yet for this evaluation.\n\n" +
            "Ask the AI assistant to generate one — e.g., 'Generate a report for this eval'.",
        );
        return;
      }
      if (!response.ok)
        throw new Error(`Failed to load report: ${response.status}`);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `eval_report_${groupId.replace(/[/\\]/g, "_")}.pdf`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error("Report download failed:", err);
    } finally {
      setDownloading(false);
    }
  };

  const showChatActions = import.meta.env.VITE_SHOW_CHAT === "true";

  return (
    <header className="relative border-b border-rule bg-ink">
      <div className="flex items-center justify-between px-6 py-3">
        <div className="flex items-baseline gap-4">
          <button
            onClick={() => navigate(mode === "viewer" ? "/data" : "/chat")}
            className="font-display text-xl italic leading-none text-bone transition-opacity hover:opacity-80"
          >
            Observatory
          </button>
          <span
            className="hidden h-3 w-px bg-rule sm:inline-block"
            aria-hidden
          />
          <span className="eyebrow hidden sm:inline-block">
            Evaluation index
          </span>
        </div>

        <nav className="absolute left-1/2 -translate-x-1/2">
          <ul className="flex items-center gap-1">
            {visibleNav.map((item) => {
              const active = pathname.startsWith(item.href);
              return (
                <li key={item.href}>
                  <button
                    onClick={() => navigate(item.href)}
                    className={`relative px-3 py-2 font-mono text-[11px] uppercase tracking-eyebrow transition-colors ${
                      active
                        ? "text-bone"
                        : "text-bone-mute hover:text-bone-dim"
                    }`}
                  >
                    {item.label}
                    {active && (
                      <span
                        className="absolute inset-x-3 -bottom-px h-px bg-ember"
                        aria-hidden
                      />
                    )}
                  </button>
                </li>
              );
            })}
          </ul>
        </nav>

        <div className="flex items-center gap-3">
          {groupId && isOwn && (
            <button
              onClick={() => setShareOpen(true)}
              className="eyebrow inline-flex items-center gap-2 border border-rule px-3 py-1.5 transition-colors hover:border-bone-mute hover:text-bone-dim"
            >
              <span className="font-mono">⤷</span> Share
            </button>
          )}
          {groupId && (
            <button
              onClick={handleDownloadReport}
              disabled={downloading}
              className="eyebrow inline-flex items-center gap-2 border border-rule px-3 py-1.5 transition-colors hover:border-bone-mute hover:text-bone-dim disabled:cursor-not-allowed disabled:opacity-50"
            >
              {downloading ? (
                <>
                  Downloading
                  <span className="cursor-block bg-ember align-baseline" />
                </>
              ) : (
                <>
                  <span className="font-mono">↓</span> Report
                </>
              )}
            </button>
          )}
          {showChatActions && user?.name && (
            <span className="hidden font-mono text-[11px] text-bone-dim sm:inline-block">
              <span className="text-bone-mute">SIGNED</span>{" "}
              <span className="text-bone">{user.name}</span>
            </span>
          )}
          {showChatActions && (
            <button
              onClick={() => {
                window.location.href = logoutUrl;
              }}
              className="eyebrow border border-rule px-3 py-1.5 transition-colors hover:border-bone-mute hover:text-bone-dim"
            >
              Sign out
            </button>
          )}
        </div>
      </div>
      {shareOpen && groupId && (
        <ShareModal
          resourceId={groupId}
          apiBase="/api/compare"
          label="evaluation"
          onClose={() => setShareOpen(false)}
        />
      )}
    </header>
  );
}
