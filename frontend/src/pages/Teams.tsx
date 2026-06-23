import { useEffect, useState } from "react";
import { useAuth, login } from "@/contexts/AuthContext";
import Header from "@/components/Header";

interface Team {
  id: string;
  name: string;
  createdBy: string;
  role: string;
}

interface Member {
  userId: string;
  role: string;
  email: string | null;
}

/**
 * Team management. A team is a sharing principal: create one, add members,
 * and use its id in the Share dialog to grant the whole team access to an
 * eval. Membership is what makes a team grant resolve for a given user.
 */
export default function TeamsPage() {
  const { user, isLoading } = useAuth();
  const [teams, setTeams] = useState<Team[]>([]);
  const [selected, setSelected] = useState<Team | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [newTeamName, setNewTeamName] = useState("");
  const [newMember, setNewMember] = useState("");
  const [error, setError] = useState<string | null>(null);

  const loadTeams = () => {
    fetch("/api/teams")
      .then((r) => (r.ok ? r.json() : { teams: [] }))
      .then((d) => setTeams(d.teams || []))
      .catch(() => setTeams([]));
  };

  const loadMembers = (teamId: string) => {
    fetch(`/api/teams/${encodeURIComponent(teamId)}/members`)
      .then((r) => (r.ok ? r.json() : { members: [] }))
      .then((d) => setMembers(d.members || []))
      .catch(() => setMembers([]));
  };

  useEffect(() => {
    if (!isLoading && !user) login();
  }, [isLoading, user]);

  useEffect(loadTeams, []);
  useEffect(() => {
    if (selected) loadMembers(selected.id);
  }, [selected]);

  const createTeam = async () => {
    setError(null);
    if (!newTeamName.trim()) return;
    const res = await fetch("/api/teams", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: newTeamName.trim() }),
    });
    if (res.ok) {
      setNewTeamName("");
      loadTeams();
    } else {
      setError(`Failed to create team (${res.status})`);
    }
  };

  const addMember = async () => {
    if (!selected || !newMember.trim()) return;
    setError(null);
    const res = await fetch(
      `/api/teams/${encodeURIComponent(selected.id)}/members`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: newMember.trim(), role: "member" }),
      },
    );
    if (res.ok) {
      setNewMember("");
      loadMembers(selected.id);
    } else {
      const b = await res.json().catch(() => ({}));
      setError(b.detail || `Failed to add member (${res.status})`);
    }
  };

  const removeMember = async (memberId: string) => {
    if (!selected) return;
    await fetch(
      `/api/teams/${encodeURIComponent(selected.id)}/members/${encodeURIComponent(memberId)}`,
      { method: "DELETE" },
    );
    loadMembers(selected.id);
  };

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
  if (!user) return null;

  const isAdmin = selected?.role === "admin";

  return (
    <div className="flex h-screen flex-col bg-ink">
      <Header />
      <div className="flex flex-1 overflow-hidden">
        {/* Team list + create */}
        <aside className="flex w-96 flex-col border-r border-rule bg-ink-elev">
          <div className="border-b border-rule-soft px-5 py-4">
            <p className="eyebrow">Teams</p>
          </div>
          <div className="border-b border-rule-soft px-5 py-3">
            <div className="flex gap-2">
              <input
                value={newTeamName}
                onChange={(e) => setNewTeamName(e.target.value)}
                placeholder="New team name…"
                className="flex-1 border-b border-rule bg-transparent py-1.5 font-mono text-[12px] text-bone placeholder:text-bone-mute focus:border-bone-mute focus:outline-none"
              />
              <button
                onClick={createTeam}
                className="eyebrow border border-rule px-3 py-1 hover:border-bone-mute hover:text-bone-dim"
              >
                Create
              </button>
            </div>
          </div>
          <div className="flex-1 overflow-y-auto">
            {teams.length === 0 ? (
              <p className="px-5 py-6 text-sm italic text-bone-mute">
                No teams yet. Create one to share evals with a group.
              </p>
            ) : (
              <ul>
                {teams.map((t) => (
                  <li key={t.id}>
                    <button
                      onClick={() => setSelected(t)}
                      className={`flex w-full flex-col gap-1 border-b border-rule-soft border-l-2 px-4 py-3 text-left transition-colors ${
                        selected?.id === t.id
                          ? "border-l-ember bg-ink-raised"
                          : "border-l-transparent hover:border-l-rule hover:bg-ink-raised/40"
                      }`}
                    >
                      <span className="text-[14px] text-bone-dim">{t.name}</span>
                      <span className="font-mono text-[10px] uppercase tracking-eyebrow text-bone-mute">
                        {t.role} · id {t.id}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </aside>

        {/* Member panel */}
        <div className="flex-1 overflow-y-auto px-8 py-6">
          {!selected ? (
            <div className="flex h-full items-center justify-center">
              <div className="max-w-md text-center">
                <p className="eyebrow">No team selected</p>
                <h3 className="font-display mt-3 text-4xl leading-tight text-bone">
                  <em className="text-ember">Pick</em> a team to manage members.
                </h3>
                <p className="mt-4 text-sm text-bone-dim">
                  Share an eval with a team's id and every member can read it.
                </p>
              </div>
            </div>
          ) : (
            <div className="max-w-2xl">
              <p className="eyebrow">{selected.name}</p>
              <p className="mt-1 font-mono text-[11px] text-bone-mute">
                Team id <span className="text-bone-dim">{selected.id}</span> —
                use this in the Share dialog.
              </p>

              {isAdmin && (
                <div className="mt-5 flex gap-2">
                  <input
                    value={newMember}
                    onChange={(e) => setNewMember(e.target.value)}
                    placeholder="member user id (email)"
                    className="flex-1 border border-rule bg-transparent px-2 py-1.5 font-mono text-[12px] text-bone placeholder:text-bone-mute focus:border-bone-mute focus:outline-none"
                  />
                  <button
                    onClick={addMember}
                    className="eyebrow border border-rule px-3 py-1.5 hover:border-bone-mute hover:text-bone-dim"
                  >
                    Add
                  </button>
                </div>
              )}
              {error && (
                <p className="mt-2 font-mono text-[11px] text-oxide">{error}</p>
              )}

              <div className="mt-6">
                <p className="eyebrow mb-2">Members</p>
                <ul className="flex flex-col gap-1">
                  {members.map((m) => (
                    <li
                      key={m.userId}
                      className="flex items-center justify-between border-b border-rule-soft py-1.5 font-mono text-[11px] text-bone-dim"
                    >
                      <span>
                        {m.email || m.userId}
                        <span className="ml-2 text-bone-mute">{m.role}</span>
                      </span>
                      {(isAdmin || m.userId === user.id) && (
                        <button
                          onClick={() => removeMember(m.userId)}
                          className="text-bone-mute hover:text-oxide"
                        >
                          {m.userId === user.id ? "leave" : "remove"}
                        </button>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
