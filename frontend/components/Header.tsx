"use client";

import { useAuth } from "@/contexts/AuthContext";
import { usePathname, useRouter } from "next/navigation";

const NAV: Array<{ href: string; label: string }> = [
  { href: "/chat", label: "Chat" },
  { href: "/history", label: "History" },
  { href: "/results", label: "Results" },
  { href: "/data", label: "Data" },
];

export default function Header() {
  const { user, logoutUrl, mode } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  return (
    <header className="relative border-b border-rule bg-ink">
      <div className="flex items-center justify-between px-6 py-3">
        <div className="flex items-baseline gap-4">
          <span className="font-display text-xl italic leading-none text-bone">
            Observatory
          </span>
          <span className="hidden h-3 w-px bg-rule sm:inline-block" aria-hidden />
          <span className="eyebrow hidden sm:inline-block">LLM Evaluation</span>
        </div>

        <nav className="absolute left-1/2 -translate-x-1/2">
          <ul className="flex items-center gap-1">
            {NAV.map((item) => {
              const active = pathname?.startsWith(item.href);
              return (
                <li key={item.href}>
                  <button
                    onClick={() => router.push(item.href)}
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

        <div className="flex items-center gap-4">
          {user?.name && (
            <span className="hidden font-mono text-[11px] text-bone-dim sm:inline-block">
              <span className="text-bone-mute">SIGNED</span>{" "}
              <span className="text-bone">{user.name}</span>
            </span>
          )}
          {mode !== "viewer" && (
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
    </header>
  );
}
