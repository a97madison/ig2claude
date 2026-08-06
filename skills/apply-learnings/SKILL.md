---
name: apply-learnings
description: Use when the user wants to apply the repo's distilled learnings to a specific project (e.g. "apply learnings to glide", "/apply-learnings <project>"). Reads the target project, selects the learnings relevant to it, and edits the project's own files directly, left uncommitted for review.
---

# Apply Learnings

Take the distilled learnings from a vid2learn base and put the relevant ones to work
inside a target project by editing that project's own files. Changes are left
**uncommitted** so the user reviews a diff before anything is final.

## Inputs

- **Learnings source:** `<base>/topics/*/learnings.md` (default base: the vid2learn
  repo the user points you at, e.g. `~/Desktop/projects/sandbox/learnings`).
- **Target project:** a directory under `~/Desktop/projects/<name>` given by the user.

## Steps

1. **Understand the target.** Read the project's `README`, `CLAUDE.md`, `notes.md`,
   and skim its structure so you know what it is, its voice, and where knowledge
   lives (docs, sales files, prompts, etc.). Note whether it is a git repo.
2. **Read all learnings.** Read every `<base>/topics/*/learnings.md`. Each bullet is
   already source-cited back to a capture.
3. **Select what's relevant.** Keep only learnings that genuinely serve this
   project's goals. A sales project wants cold-call / objection learnings; an AI-tools
   project wants tooling learnings. Be strict: irrelevant "learnings" pollute the
   project. If nothing is relevant, say so and stop.
4. **Decide where each lands.** Map each relevant learning to the project file it
   belongs in (append to an existing doc, add a section, or a new file only if there
   is no natural home). Prefer editing existing files over creating new ones.
5. **Edit the project's files directly.** Make surgical edits in the project's own
   voice (match CLAUDE.md §0 and the project's existing style). Preserve each
   learning's source citation, rewritten as a reference to the originating post/video
   so provenance survives (e.g. "(from a saved @theobjectionlab post)").
6. **Do not commit.** Leave every change uncommitted. If the project is a git repo,
   run `git status` and show what changed; if not, list the files you touched.
7. **Report:** which learnings were applied, to which files, which were skipped as
   irrelevant, and any conflicts with what the project already says (surface both,
   never silently overwrite the project's existing stance).

## Rules

- Edits are **surgical** (CLAUDE.md §3): touch only what the applied learnings
  require; do not refactor or reformat surrounding project code.
- **Uncommitted, always** — the user reviews before committing. Never push.
- Relevance is judgment, not keyword match: read the learning and the project and
  decide if it actually helps. When unsure, list it as a suggestion rather than
  editing it in.
- If a learning contradicts something the project already asserts, flag it in the
  report and leave the project's line intact for the user to reconcile.
- Preserve provenance: an applied learning keeps a pointer to its source.
