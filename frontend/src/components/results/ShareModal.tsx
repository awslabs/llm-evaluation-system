import { useEffect, useState } from "react";

interface Share {
  id: string;
  groupId: string | null;
  resourceType?: string;
  principalType: string;
  principalId: string | null;
  role: string;
  createdAt: string;
}

interface Props {
  // The resource being shared. `resourceId` is the eval group / dataset id /
  // judge id / optimization id / document path. `apiBase` is the resource's
  // route prefix (e.g. "/api/compare", "/api/datasets") — its /shares,
  // /shares/{id} endpoints live under it. `label` names the resource in copy.
  resourceId: string;
  apiBase: string;
  label?: string;
  // Documents are path-keyed, so per-item grants are fragile — force share-all.
  shareAllOnly?: boolean;
  onClose: () => void;
}

/**
 * Generic share dialog for any resource the caller OWNS (eval, dataset, judge,
 * optimization, document). Grants another user (by id/email), a team, or the
 * whole org read access — either this one resource or all of that type. Mirrors
 * the per-resource /shares endpoints. Read-only (viewer) is the only role.
 */
export default function ShareModal({
  resourceId,
  apiBase,
  label = "resource",
  shareAllOnly = false,
  onClose,
}: Props) {
  const [shares, setShares] = useState<Share[]>([]);
  const [principalType, setPrincipalType] = useState("user");
  const [principalId, setPrincipalId] = useState("");
  const [shareAll, setShareAll] = useState(shareAllOnly);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [suggestions, setSuggestions] = useState<{ id: string; email: string | null }[]>([]);

  const loadShares = () => {
    fetch(`${apiBase}/shares`)
      .then((r) => (r.ok ? r.json() : { shares: [] }))
      .then((d) => setShares(d.shares || []))
      .catch(() => setShares([]));
  };

  useEffect(loadShares, [apiBase]);

  // Email/id autocomplete for the "user" principal. The search endpoint is
  // resource-agnostic (lives under /api/compare). Grants key on the id.
  useEffect(() => {
    if (principalType !== "user" || principalId.trim().length < 2) {
      setSuggestions([]);
      return;
    }
    const t = setTimeout(() => {
      fetch(`/api/compare/users/search?q=${encodeURIComponent(principalId.trim())}`)
        .then((r) => (r.ok ? r.json() : { users: [] }))
        .then((d) => setSuggestions(d.users || []))
        .catch(() => setSuggestions([]));
    }, 250);
    return () => clearTimeout(t);
  }, [principalId, principalType]);

  const submit = async () => {
    setError(null);
    if (principalType !== "org" && !principalId.trim()) {
      setError("Enter a user or team id.");
      return;
    }
    setBusy(true);
    try {
      const res = await fetch(`${apiBase}/shares`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          principal_type: principalType,
          principal_id: principalType === "org" ? null : principalId.trim(),
          resource_id: shareAll || shareAllOnly ? null : resourceId,
          role: "viewer",
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `Failed: ${res.status}`);
      }
      setPrincipalId("");
      loadShares();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to share");
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (id: string) => {
    await fetch(`${apiBase}/shares/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
    loadShares();
  };

  const describe = (s: Share) => {
    const who =
      s.principalType === "org"
        ? "Everyone (org)"
        : `${s.principalType}: ${s.principalId}`;
    const what = s.groupId ? `this ${label}` : `ALL my ${label}s`;
    return `${who} · ${what}`;
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/80 p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-lg border border-rule bg-ink-elev p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-baseline justify-between">
          <p className="eyebrow">Share {label}</p>
          <button
            onClick={onClose}
            className="font-mono text-bone-mute hover:text-bone"
          >
            ✕
          </button>
        </div>

        <div className="mt-5 flex flex-col gap-3">
          <div className="flex gap-2">
            <select
              value={principalType}
              onChange={(e) => setPrincipalType(e.target.value)}
              className="border border-rule bg-transparent px-2 py-1.5 font-mono text-[12px] text-bone focus:outline-none"
            >
              <option value="user">User</option>
              <option value="team">Team</option>
              <option value="org">Everyone</option>
            </select>
            {principalType !== "org" && (
              <div className="relative flex-1">
                <input
                  value={principalId}
                  onChange={(e) => setPrincipalId(e.target.value)}
                  placeholder={
                    principalType === "user" ? "user id or email" : "team id"
                  }
                  className="w-full border border-rule bg-transparent px-2 py-1.5 font-mono text-[12px] text-bone placeholder:text-bone-mute focus:border-bone-mute focus:outline-none"
                />
                {suggestions.length > 0 && (
                  <ul className="absolute z-10 mt-1 max-h-40 w-full overflow-y-auto border border-rule bg-ink-elev">
                    {suggestions.map((u) => (
                      <li key={u.id}>
                        <button
                          onClick={() => {
                            setPrincipalId(u.id);
                            setSuggestions([]);
                          }}
                          className="block w-full px-2 py-1.5 text-left font-mono text-[11px] text-bone-dim hover:bg-ink-raised"
                        >
                          {u.email ? `${u.email} · ${u.id}` : u.id}
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
            <button
              onClick={submit}
              disabled={busy}
              className="eyebrow border border-rule px-3 py-1.5 hover:border-bone-mute hover:text-bone-dim disabled:opacity-50"
            >
              Share
            </button>
          </div>

          {shareAllOnly ? (
            <p className="font-mono text-[11px] text-bone-mute">
              {label}s are shared all-at-once (per-item sharing isn't supported
              for {label}s).
            </p>
          ) : (
            <label className="flex items-center gap-2 font-mono text-[11px] text-bone-dim">
              <input
                type="checkbox"
                checked={shareAll}
                onChange={(e) => setShareAll(e.target.checked)}
              />
              Share ALL my {label}s (including future ones), not just this one
            </label>
          )}

          {principalType === "org" && (
            <p className="font-mono text-[11px] text-ember">
              ⚠ This makes {shareAll || shareAllOnly ? `ALL your ${label}s` : `this ${label}`}{" "}
              readable by EVERYONE in the organization.
            </p>
          )}

          {error && (
            <p className="font-mono text-[11px] text-oxide">{error}</p>
          )}
        </div>

        <div className="mt-6">
          <p className="eyebrow mb-2">Current shares</p>
          {shares.length === 0 ? (
            <p className="font-mono text-[11px] text-bone-mute">
              Not shared with anyone yet.
            </p>
          ) : (
            <ul className="flex flex-col gap-1">
              {shares.map((s) => (
                <li
                  key={s.id}
                  className="flex items-center justify-between border-b border-rule-soft py-1.5 font-mono text-[11px] text-bone-dim"
                >
                  <span>{describe(s)}</span>
                  <button
                    onClick={() => revoke(s.id)}
                    className="text-bone-mute hover:text-oxide"
                  >
                    revoke
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
