"""Cross-platform CLI for mirroring a Skill across AI coding tools.

Default mode is a read-only preview. Use --apply with the fingerprint and
approved targets returned by a preview the user explicitly approved.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sync_core

ExitCode = sync_core.ExitCode

_EPILOG = """\
exit codes:
  0  success (all approved targets mirrored or no-op)
  1  partial failure (at least one target failed)
  2  invalid input (bad source, missing --approved-target / --plan-fingerprint,
     or the plan changed since the approved preview)
  3  user input required (no peer skills directory was found)
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sync_skills.py",
        description="Mirror a Skill directory across AI coding tool skills directories.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--source", required=True,
                        help="Authoritative source Skill directory (must contain SKILL.md).")
    parser.add_argument("--user-home", default=None,
                        help="User home directory for automatic discovery.")
    parser.add_argument("--target", action="append", default=[],
                        help="Extra tool root or skills directory (repeatable).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="Only print the plan; do not modify targets (default).")
    mode.add_argument("--apply", action="store_true",
                      help="Execute the mirror to the approved targets.")
    parser.add_argument("--approved-target", action="append", default=[],
                        help="Destination Skill path the user approved in this session "
                             "(repeatable, --apply only).")
    parser.add_argument("--plan-fingerprint", default=None,
                        help="Fingerprint from the preview the user approved (--apply only).")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="Emit a machine-readable JSON summary.")
    return parser


def exit_code_for_results(results) -> sync_core.ExitCode:
    """Map execution results to an exit code."""
    if any(item.status == "failed" for item in results):
        return sync_core.ExitCode.PARTIAL_FAILURE
    return sync_core.ExitCode.SUCCESS


def _summarize_plan(plan: sync_core.TargetPlan) -> dict:
    creates = sum(1 for op in plan.operations if op.action == "create")
    replaces = sum(1 for op in plan.operations if op.action == "replace")
    deletes = sum(1 for op in plan.operations if op.action == "delete")
    return {
        "destination": str(plan.destination),
        "skills_root": str(plan.skills_root),
        "creates": creates,
        "replaces": replaces,
        "deletes": deletes,
        "operations": len(plan.operations),
    }


def _preview(args, source, skill_name, plans, skipped, fingerprint) -> int:
    summaries = [_summarize_plan(p) for p in plans]
    requires_input = len(plans) == 0

    if args.as_json:
        print(json.dumps({
            "source": str(source),
            "skill_name": skill_name,
            "mode": "dry-run",
            "plan_fingerprint": fingerprint,
            "requires_confirmation": len(plans) > 0,
            "requires_user_input": requires_input,
            "targets": summaries,
            "skipped": skipped,
        }, indent=2, ensure_ascii=False))
    else:
        print(f"Source:           {source}")
        print(f"Skill:            {skill_name}")
        print(f"Plan fingerprint: {fingerprint}")
        if skipped:
            print("\nSkipped:")
            for item in skipped:
                print(f"  - {item['destination']} ({item['reason']})")
        if summaries:
            print("\nTargets:")
            for item in summaries:
                print(f"  {item['destination']}")
                print(f"    +{item['creates']} create  ~{item['replaces']} replace  -{item['deletes']} delete")
        if requires_input:
            print("\nNo peer skills directory found.")
            print("Provide an existing tool root or skills directory.")

    if requires_input:
        return sync_core.ExitCode.USER_INPUT_REQUIRED
    return sync_core.ExitCode.SUCCESS


def _apply(args, source, skill_name, plans, skipped, fingerprint) -> int:
    # Verify the plan has not changed since the approved preview.
    if fingerprint != args.plan_fingerprint:
        msg = "plan has changed; preview again and request new approval"
        if args.as_json:
            print(json.dumps({
                "source": str(source),
                "skill_name": skill_name,
                "mode": "apply",
                "rejected": True,
                "reason": msg,
                "plan_fingerprint": fingerprint,
            }, indent=2, ensure_ascii=False))
        else:
            sys.stderr.write(msg + "\n")
        return sync_core.ExitCode.INVALID_INPUT

    approved_targets = [Path(t) for t in args.approved_target]
    approved = sync_core.select_approved_plans(plans, approved_targets)
    if not approved:
        sys.stderr.write("no approved target matches the current plan\n")
        return sync_core.ExitCode.INVALID_INPUT

    results = [sync_core.execute_plan(source, plan) for plan in approved]

    if args.as_json:
        print(json.dumps({
            "source": str(source),
            "skill_name": skill_name,
            "mode": "apply",
            "plan_fingerprint": fingerprint,
            "requires_confirmation": False,
            "requires_user_input": False,
            "results": [
                {"destination": str(r.destination), "status": r.status, "message": r.message}
                for r in results
            ],
        }, indent=2, ensure_ascii=False))
    else:
        for r in results:
            print(f"  {r.destination}: {r.status} ({r.message})")

    return exit_code_for_results(results)


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # --apply prerequisites: fail fast before touching the filesystem.
    if args.apply:
        if not args.approved_target:
            sys.stderr.write("--apply requires at least one --approved-target\n")
            return sync_core.ExitCode.INVALID_INPUT
        if not args.plan_fingerprint:
            sys.stderr.write("--apply requires --plan-fingerprint\n")
            return sync_core.ExitCode.INVALID_INPUT

    try:
        source, skill_name = sync_core.validate_source(Path(args.source))
    except (ValueError, OSError) as error:
        sys.stderr.write(f"invalid source: {error}\n")
        return sync_core.ExitCode.INVALID_INPUT

    user_home = Path(args.user_home) if args.user_home else None
    explicit_targets = [Path(t) for t in args.target]
    try:
        roots = sync_core.discover_skills_roots(
            source, user_home, explicit_targets, dict(os.environ)
        )
    except (ValueError, OSError) as error:
        sys.stderr.write(f"discovery error: {error}\n")
        return sync_core.ExitCode.INVALID_INPUT

    plans: list = []
    skipped: list = []
    for root in roots:
        plan = sync_core.plan_target(source, root)
        if sync_core._path_key(plan.destination) == sync_core._path_key(source):
            skipped.append({"destination": str(plan.destination), "reason": "source directory"})
        elif not plan.operations:
            skipped.append({"destination": str(plan.destination), "reason": "already mirrored"})
        else:
            plans.append(plan)

    source_manifest = sync_core.build_manifest(source)
    fingerprint = sync_core.plan_fingerprint(source_manifest, plans)

    if args.apply:
        return _apply(args, source, skill_name, plans, skipped, fingerprint)
    return _preview(args, source, skill_name, plans, skipped, fingerprint)


if __name__ == "__main__":
    raise SystemExit(main())
