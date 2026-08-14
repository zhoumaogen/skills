---
name: syncing-skills-across-tools
description: Use when creating, modifying, updating, repairing, or synchronizing a Skill that may need to be mirrored across multiple AI coding tools or skills directories.
---

# Syncing Skills Across Tools

## Core rule

Treat the edited Skill as the only source of truth. Never write to a target before the user has seen and explicitly approved the normalized target paths and deletion summary in the current conversation.

## Required workflow

1. Finish and validate the source Skill.
2. Run the bundled script without `--apply` and request JSON output.
3. Present the source, every target absolute path, and each create/replace/delete count.
4. Ask whether to synchronize to exactly those targets. Stop and wait.
5. After explicit approval, run with the returned plan fingerprint and only the approved paths.
6. If the plan changed, return to step 2 and ask again.
7. Report every success, skip, and failure.

## Approval gate

- Treat the user's initial request to create and sync as intent, not final approval.
- Accept approval only after showing the final normalized paths and deletion counts in the current conversation.
- Treat vague replies, silence, and approval from an earlier conversation as no approval.
- Apply only to paths explicitly approved in the latest reply.
- If the fingerprint changes, preview again and ask again.
- If no peer directory is found, ask for an existing tool root or `skills` directory.
- If the user declines or does not reply, stop without target changes.

Never guess a directory, create a tool root, manually copy around the script, or reuse stale approval.

## Quick reference

| Situation | Required action |
|---|---|
| Preview found peers | Show exact paths and deletion counts, then wait |
| No peer found | Ask for an existing tool root or `skills` path |
| User approved a subset | Pass only that subset as `--approved-target` |
| Fingerprint changed | Preview again and request new approval |

## Common mistakes

- Calling each product an Agent instead of an AI coding tool or Skill host.
- Adding tool-specific metadata such as `agents/openai.yaml`.
- Recursively scanning caches, plugins, dependencies, or projects.
- Treating the initial request as approval or hiding deletion counts.
- Copying manually instead of using the preview and fingerprint workflow.

## Running the script

```bash
python -B scripts/sync_skills.py --source <skill-dir> --dry-run --json
python -B scripts/sync_skills.py --source <skill-dir> --apply \
  --plan-fingerprint <fingerprint> --approved-target <path> [--approved-target <path> ...]
```

Exit codes: 0 success, 1 partial failure, 2 invalid input or changed plan, 3 user input required.
