# CLAUDE.md

Contributor and agent-facing instructions for this repo live in
[AGENTS.md](./AGENTS.md). That file is the canonical source — read it
first. Same convention is used by Codex, Cursor, and other agentic
tools, so we keep a single file rather than splitting per-tool docs.

On-demand workflows live as skills under [`.claude/skills/`](./.claude/skills/).
Claude Code auto-loads each `SKILL.md`'s frontmatter at session start and
loads the full body only when the skill triggers, so they don't bloat
context. Today there's one: [`ship-it`](./.claude/skills/ship-it/SKILL.md)
for the commit → PR → release workflow.
