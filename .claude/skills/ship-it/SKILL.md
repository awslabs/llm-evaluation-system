---
name: ship-it
description: Ship work in the llm-evaluation-system repo end-to-end — commit with conventional-commit messages, push to a feature branch (never directly to main), open a PR with the proper title format, and after merge either run `make release` to publish to PyPI or just clean up branches. Use this skill whenever the user says ship/commit/push/open a PR/release/publish, or after code changes are done and ready to go out. Don't invent your own git workflow — invoke this skill so the repo conventions (conventional commits, manual release, setuptools-scm tag flow, branch cleanup) are applied consistently.
---

# Ship It

This skill ships changes in the `llm-evaluation-system` repo from "I have
local changes" through to "merged + (optionally) on PyPI." It exists
because this repo has specific conventions that are easy to get wrong
individually and straightforward when followed as a unit.

Read the whole skill before acting if it's your first invocation in a
session. After that, the quick reference at the bottom is usually enough.

## Conventions this repo follows

- **Conventional Commits** for every commit message AND every PR title.
  Valid prefixes: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`,
  `build`, `ci`, `perf`, `style`, `revert`. Use `feat(scope):` when a
  scope adds clarity (e.g. `feat(mcp): ...`, `fix(release): ...`).
- **Never push directly to `main`.** Every change goes through a PR.
  Direct pushes are blocked by the auto-mode classifier anyway, but
  the deeper reason is reviewability and not bypassing CI checks on
  `publish.yml` paths.
- **Never force-push.** Rewriting public history breaks others' clones
  and is essentially never the right answer.
- **Releases are deliberate human actions.** Use `make release` /
  `make release-minor` / `make release-major` from a clean `main`. There
  is no auto-release on merge (this was explored and rejected; see the
  Releasing section below for why).
- **After PRs merge, branches need manual cleanup** (auto-delete head
  branches isn't enabled on this repo).

## Branch state

You should already be on a feature branch with your changes —
worktree-based by default, per [CLAUDE.md](../../../CLAUDE.md#conventions-worth-knowing-up-front).
This skill picks up from there.

If you're invoked from `main` with uncommitted changes, **stop and ask
the user** before doing anything. Moving in-flight work into a worktree
retroactively is awkward (stash + apply, or branch in place, or
something else), and the user should pick which.

## Workflow: commit and open a PR

1. **Verify the working tree state.** Run `git status --short`. If there
   are untracked or modified files that aren't part of THIS change
   (e.g. someone else's experimental directory, build artifacts from a
   previous run, leftover test files), pause and check with the user
   before touching them. Don't auto-stage with `git add -A` or `git add .`.

2. **Stage only the files relevant to this change** by name:

   ```bash
   git add path/to/file1 path/to/file2
   ```

3. **Commit with a conventional commit message.**

   Title format:
   ```
   <type>(<scope>): <short imperative subject>
   ```

   Body should explain WHY, not WHAT — the diff already shows the what.
   Examples from this repo's actual history:
   - `feat(mcp): apply mcp-builder best practices to eval-mcp tools`
   - `fix(release): force tag version + strip local identifier in CI`
   - `docs: document PyPI release workflow + add CLAUDE.md pointer`
   - `revert: remove release-please + PR title lint automation`

   Use a HEREDOC for multi-line commit messages so formatting is preserved:

   ```bash
   git commit -m "$(cat <<'EOF'
   feat(scope): short subject line

   Longer explanation of why this change is being made and what
   problem it solves. Reference other PRs or issues if relevant.
   EOF
   )"
   ```

4. **Push the branch.** From inside the worktree (or the main checkout
   if not using a worktree):

   ```bash
   git push -u origin <branch-name>
   ```

5. **Open the PR** with `gh pr create`. The PR title must follow the
   same conventional-commit format as the commit message — the lint
   workflow (if/when re-added) reads PR titles, and human reviewers
   expect consistency.

   Use a HEREDOC for the body so formatting is preserved:

   ```bash
   gh pr create --base main --title "<type>(<scope>): <subject>" --body "$(cat <<'EOF'
   ## Summary

   - what changed in 1-3 bullet points
   - link to related PRs / issues if applicable

   ## Why

   The motivation. What problem this solves.

   ## Test plan

   - [x] tests run locally (uv run pytest ...)
   - [x] manual smoke test of the changed behavior
   - [ ] post-merge verification step (if applicable)
   EOF
   )"
   ```

6. **Stop and report the PR URL to the user.** Merging is the user's
   call, never auto-merge. Wait for the user to say it's merged.

## Workflow: after the PR is merged

When the user reports the PR is merged (or you verify via `gh pr view`):

1. **Switch to main and pull:**

   ```bash
   cd <main-repo-path>           # if you were in a worktree
   git checkout main
   git pull origin main
   ```

2. **Remove the worktree** (if you used one):

   ```bash
   git worktree remove .claude/worktrees/<branch-name>
   ```

   `git worktree remove` refuses if the worktree has uncommitted changes.
   If it does, surface what's there and ask the user before forcing.

3. **Delete the local branch** (cleanup; the worktree removal doesn't
   delete the ref):

   ```bash
   git branch -d <branch-name>
   ```

4. **Delete the remote branch:**

   ```bash
   git push origin --delete <branch-name>
   ```

   Auto-delete-on-merge is intentionally NOT enabled on this repo; the
   manual cleanup step is the convention.

## Workflow: cutting a release

Releases publish a new version to PyPI as `llm-evaluation-system==X.Y.Z`.
This is a deliberate human-initiated step — never auto-release on merge.

1. **Sync main first.** Releases must come from a clean `main`:

   ```bash
   git checkout main
   git pull --ff-only origin main
   git status --short    # should be empty (or only untracked files unrelated to release)
   ```

2. **Pick the version bump from semver:**

   | Bump | Command | When |
   |---|---|---|
   | patch | `make release` | bug fixes only, no new public API |
   | minor | `make release-minor` | new features, backwards-compatible |
   | major | `make release-major` | breaking changes (rare) |

   Pick conservatively but accurately. New tool parameters or new
   functionality = minor (don't undersell as patch — users pin against
   minor versions and miss features otherwise). Pure bug fixes = patch.
   Breaking API changes = major.

3. **Run the chosen target.** It reads the latest `v*` tag, computes the
   next version, tags it, and pushes the tag. Versions are derived from
   the tag at build time by `setuptools-scm` — no source file is bumped
   and no "Release vX.Y.Z" commits appear on `main`.

4. **Watch the publish workflow.** The tag push triggers
   `.github/workflows/publish.yml` which builds the frontend, builds the
   Python distribution, and uploads to PyPI via trusted publisher:

   ```bash
   gh run list --workflow=publish.yml --limit 1
   gh run watch <run-id> --exit-status
   ```

5. **Verify the release on PyPI:**

   ```bash
   curl -s https://pypi.org/pypi/llm-evaluation-system/json | \
     python3 -c "import sys,json; print(json.load(sys.stdin)['info']['version'])"
   ```

   The latest version should match what you just tagged.

## Pitfalls and rejected patterns

These are explicitly rejected — don't reintroduce them:

- **Don't push directly to `main`.** Blocked by classifier; bypasses
  review.
- **Don't force-push to `main` or any branch with active PRs.**
- **Don't add `release-please` or similar auto-release bots.** Requires
  enabling "Allow GitHub Actions to create and approve pull requests"
  which OpenSSF best practices explicitly recommend against. Real
  supply-chain attacks have exploited this exact pattern
  (CVE-2025-30066 / tj-actions/changed-files compromise affected
  23,000+ repos). awslabs peer projects (`awslabs/mcp`,
  `aws-lambda-powertools-python`, etc.) all use manual releases.
- **Don't reintroduce a static `version` field to `pyproject.toml`.**
  Version is dynamic via `setuptools-scm` (declared as
  `dynamic = ["version"]`). Adding a static field reintroduces the
  drift bug where source said one version and PyPI said another.
- **Don't release without explicit user instruction.** "I merged the PR"
  is not "release it." Wait for an explicit ship/release/publish
  instruction.
- **Don't use `gh pr merge --admin` to bypass branch protection** or
  any required checks. If a check is blocking the merge, fix the check
  (e.g. fix the PR title), don't override.

## Quick reference

| Situation | Command |
|---|---|
| New worktree + branch | `git worktree add .claude/worktrees/<name> -b <type>/<name>` |
| Stage specific files | `git add path/to/file1 path/to/file2` |
| Commit | `git commit -m "<type>(<scope>): <subject>"` |
| Push branch | `git push -u origin <branch>` |
| Open PR | `gh pr create --base main --title "..." --body "..."` |
| Pull main after merge | `git checkout main && git pull` |
| Remove worktree | `git worktree remove .claude/worktrees/<name>` |
| Delete branch local | `git branch -d <name>` |
| Delete branch remote | `git push origin --delete <name>` |
| Patch release | `make release` |
| Minor release | `make release-minor` |
| Major release | `make release-major` |
| Watch publish | `gh run watch $(gh run list --workflow=publish.yml --limit 1 --json databaseId --jq '.[0].databaseId')` |
| Verify PyPI | `curl -s https://pypi.org/pypi/llm-evaluation-system/json \| python3 -c "import sys,json; print(json.load(sys.stdin)['info']['version'])"` |
