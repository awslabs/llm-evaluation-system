import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";
import { useAuth, login } from "@/contexts/AuthContext";
import Header from "@/components/Header";
import OptimizationRail from "@/components/optimizations/OptimizationRail";
import OptimizationDetail from "@/components/optimizations/OptimizationDetail";

export default function OptimizationsPage() {
  const { user, isLoading } = useAuth();
  // URL is the source of truth (see Results.tsx) — react-router handles the
  // ?id= sync and back/forward natively.
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedId = searchParams.get("id");
  // owner travels in the URL so a shared optimization's reads stay authorized
  // across refresh/deep-link (mirrors Results.tsx).
  const selectedOwner = searchParams.get("owner");

  const handleSelect = (id: string | null, owner?: string) => {
    if (!id) {
      setSearchParams({});
      return;
    }
    setSearchParams(owner ? { id, owner } : { id });
  };

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
        <OptimizationRail selectedId={selectedId} onSelect={handleSelect} />

        <div className="flex-1 overflow-hidden">
          {selectedId ? (
            <OptimizationDetail optimizationId={selectedId} owner={selectedOwner} />
          ) : (
            <div className="flex h-full items-center justify-center px-8">
              <div className="max-w-md text-center">
                <p className="eyebrow">No run selected</p>
                <h3 className="font-display mt-3 text-4xl leading-tight text-bone">
                  <em className="text-ember">Pick</em> an optimization to
                  read its diff.
                </h3>
                <p className="mt-4 text-sm text-bone-dim">
                  Each entry on the left is an optimize_prompt run — the
                  initial template, the winner, train pass rate per
                  iteration, and the rationale behind each proposal.
                </p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
