"use client";

import { useAuth } from "@/contexts/AuthContext";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

const CONTENTS: Array<[string, string, string]> = [
  ["01", "Conversational driver", "Script evaluations through plain language."],
  ["02", "Comparison grid", "Every sample, every model, scored side by side."],
  ["03", "Judge transparency", "See how each criterion was decided and by whom."],
  ["04", "Reproducible reports", "Sign and export every run as a PDF."],
];

function Today() {
  const [label, setLabel] = useState("");
  useEffect(() => {
    setLabel(
      new Date().toLocaleDateString("en-US", {
        year: "numeric",
        month: "short",
        day: "2-digit",
      }),
    );
  }, []);
  return <span className="eyebrow tabular-nums">{label || " "}</span>;
}

export default function Home() {
  const { user, isLoading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!isLoading && user) {
      router.push("/chat");
    }
  }, [user, isLoading, router]);

  const status = isLoading ? "Identifying" : user ? "Redirecting" : null;

  // Auth handler — kept verbatim. oauth2-proxy is a separate service, not a
  // Next route, so this MUST be a hard navigation, not router.push. Do not
  // touch this logic without re-checking the oauth2-proxy + Cognito flow.
  const startSignIn = () => {
    window.location.href = "/oauth2/start";
  };

  return (
    <main className="relative flex min-h-screen flex-col bg-ink text-bone">
      <header className="flex items-center justify-between border-b border-rule-soft px-8 py-5">
        <div className="flex items-baseline gap-4">
          <span className="font-display text-xl italic leading-none text-bone">
            Observatory
          </span>
          <span className="hidden h-3 w-px bg-rule sm:inline-block" aria-hidden />
          <span className="eyebrow hidden sm:inline-block">LLM Evaluation</span>
        </div>
        <Today />
      </header>

      <section className="flex flex-1 items-center px-8">
        <div className="mx-auto grid w-full max-w-6xl grid-cols-12 gap-x-8 gap-y-12 py-20">
          <div className="reveal col-span-12 md:col-span-8">
            <p className="eyebrow">
              Bulletin No. 01 — Restricted instrument
            </p>
            <h1 className="font-display mt-6 text-balance text-[clamp(2.75rem,7.2vw,6.5rem)] leading-[0.95]">
              Measure how
              <br />
              language models
              <br />
              <em className="text-ember">actually</em> behave.
            </h1>
            <p className="mt-8 max-w-prose text-base leading-relaxed text-bone-dim">
              Compare responses across tasks, judges, and rubrics. Drill into individual
              samples. Generate signed reports. Treat the answers as data, not vibes.
            </p>

            <div className="mt-10 flex flex-wrap items-center gap-x-8 gap-y-4">
              {status ? (
                <span className="eyebrow">
                  {status}
                  <span className="cursor-block ml-2 align-baseline" />
                </span>
              ) : (
                <button
                  onClick={startSignIn}
                  className="group inline-flex items-center gap-3 border border-bone px-6 py-3 font-mono text-[11px] uppercase tracking-eyebrow text-bone transition-colors hover:bg-bone hover:text-ink focus:outline-none focus:ring-1 focus:ring-bone focus:ring-offset-2 focus:ring-offset-ink"
                >
                  Sign in to continue
                  <span className="font-mono transition-transform group-hover:translate-x-1">
                    →
                  </span>
                </button>
              )}
            </div>
          </div>

          <aside className="reveal stagger-2 col-span-12 border-rule pl-0 md:col-span-4 md:border-l md:pl-8">
            <div className="eyebrow mb-5">Contents</div>
            <dl className="space-y-4 text-sm">
              {CONTENTS.map(([n, title, desc]) => (
                <div key={n} className="flex items-baseline gap-4">
                  <dt className="w-6 font-mono text-bone-mute tabular-nums">
                    {n}
                  </dt>
                  <dd className="leading-snug">
                    <span className="text-bone">{title}</span>
                    <span className="text-bone-dim"> &mdash; {desc}</span>
                  </dd>
                </div>
              ))}
            </dl>
          </aside>
        </div>
      </section>

      <footer className="flex items-center justify-between border-t border-rule-soft px-8 py-5">
        <span className="eyebrow">Open source · Apache 2.0</span>
        <span className="eyebrow tabular-nums">awslabs · llm-evaluation-system</span>
      </footer>
    </main>
  );
}
