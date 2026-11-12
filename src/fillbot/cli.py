"""Command-line interface: mock-serve, validate, run, resume, report."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from typing import Sequence
from fillbot.config import BotConfig, load_config
from fillbot.dsl.schema import load_script
from fillbot.errors import BotError, TaskValidationError
from fillbot.intake import load_and_validate_tasks
from fillbot.ledger.db import Ledger
from fillbot.ledger.report import print_summary
from fillbot.mocksite.chaos import ChaosSettings
from fillbot.runner.scheduler import Runner
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fillbot", description="Compliant form-filling RPA for authorized systems only."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    mock = sub.add_parser("mock-serve", help="start the loopback-only mock site")
    mock.add_argument("--port", type=int, default=5000)
    mock.add_argument("--chaos", action="store_true", help="enable fault injection")
    mock.add_argument("--chaos-seed", type=int)
    mock.add_argument("--chaos-rate", type=float)
    mock.add_argument("--delay-ms", type=int)
    mock.add_argument("--flaky-submit", action="store_true")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="YAML config file")
    validate = sub.add_parser("validate", parents=[common], help="validate tasks and script offline")
    validate.add_argument("--tasks", required=True)
    validate.add_argument("--script", required=True)
    run = sub.add_parser("run", parents=[common], help="execute a task batch")
    run.add_argument("--tasks", required=True)
    run.add_argument("--script", required=True)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--rate-limit", type=int)
    run.add_argument("--base-url")
    resume = sub.add_parser("resume", parents=[common], help="continue an interrupted run")
    resume.add_argument("run_id")
    resume.add_argument("--base-url")
    report = sub.add_parser("report", parents=[common], help="print ledger summary")
    report.add_argument("run_id")
    return parser
def _config(args: argparse.Namespace) -> BotConfig:
    overrides: dict[str, object] = {}
    if getattr(args, "rate_limit", None) is not None:
        if args.rate_limit < 1:
            raise BotError("--rate-limit must be >= 1")
        overrides["rate_limit_per_minute"] = args.rate_limit
    if getattr(args, "base_url", None):
        overrides["base_url"] = args.base_url
    return load_config(getattr(args, "config", None), overrides=overrides)
def _validate(args: argparse.Namespace) -> int:
    tasks, script = load_and_validate_tasks(args.tasks), load_script(args.script)
    print(f"OK: {len(tasks)} task(s), script={script.name} steps={len(script.steps)}; no network requests were made")
    return 0
def _run(args: argparse.Namespace, config: BotConfig) -> int:
    tasks, script = load_and_validate_tasks(args.tasks), load_script(args.script)
    runner = Runner(config)
    run = runner.run(
        tasks,
        script,
        task_source=str(Path(args.tasks).resolve()),
        script_source=str(Path(args.script).resolve()),
        dry_run=args.dry_run,
        options={"rate_limit_per_minute": config.rate_limit_per_minute},
    )
    print_summary(runner.ledger, run.run_id)
    print(f"summary: {config.runs_dir / run.run_id / 'summary.json'}")
    return 0
def _resume(args: argparse.Namespace, config: BotConfig) -> int:
    ledger = Ledger(config.ledger_path)
    runner = Runner(config, ledger=ledger)
    row = ledger.get_run(args.run_id)
    if row is None:
        raise BotError(f"run not found: {args.run_id}")
    task_source, script_source = str(row["task_source"] or ""), str(row["script_source"] or "")
    if not task_source or not script_source:
        raise BotError("run is missing its original tasks/script paths")
    run = runner.resume(
        args.run_id,
        load_and_validate_tasks(task_source),
        load_script(script_source),
        task_source=task_source,
        script_source=script_source,
        dry_run=bool(row["dry_run"]),
    )
    print_summary(ledger, run.run_id)
    return 0
def _report(args: argparse.Namespace, config: BotConfig) -> int:
    print_summary(Ledger(config.ledger_path), args.run_id)
    return 0


def _intake_diagnosis(args: argparse.Namespace, exc: TaskValidationError) -> dict[str, object] | None:
    """Classify a pre-runtime validation error and emit one JSON diagnostic line.

    Best-effort and output-only: never changes the exit code or re-raises.
    """
    try:
        from fillbot.diagnostics.collector import diagnosis_from_intake
        from fillbot.intake.validate import validation_feature_details

        diagnosis = diagnosis_from_intake(exc)
        payload = diagnosis.as_ledger_json()
        payload["features"]["details"].update(validation_feature_details(exc))
        payload["source_file"] = str(getattr(args, "tasks", "") or "")
        return payload
    except Exception:  # pragma: no cover - diagnostics must be inert
        return None
def _mock_serve(args: argparse.Namespace) -> int:
    if not 1 <= args.port <= 65535:
        raise BotError("--port must be between 1 and 65535")
    from fillbot.mocksite.app import create_app
    custom = (
        args.chaos
        or args.flaky_submit
        or any(value is not None for value in (args.chaos_seed, args.chaos_rate, args.delay_ms))
    )
    chaos = None
    if custom:
        chaos = ChaosSettings(
            enabled=True,
            seed=args.chaos_seed if args.chaos_seed is not None else 7,
            failure_rate=max(0.0, min(1.0, args.chaos_rate or 0.0)),
            delay_ms=max(0, args.delay_ms or 0),
            flaky_submit=bool(args.flaky_submit),
        )
    app = create_app(chaos=chaos)
    print(f"mock site: http://127.0.0.1:{args.port} (loopback only)")
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False, threaded=True)
    return 0
def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "mock-serve":
            return _mock_serve(args)
        if args.command == "validate":
            return _validate(args)
        config = _config(args)
        if args.command == "run":
            return _run(args, config)
        if args.command == "resume":
            return _resume(args, config)
        if args.command == "report":
            return _report(args, config)
        parser.error(f"unknown command: {args.command}")
    except TaskValidationError as exc:
        print(str(exc), file=sys.stderr)
        for issue in exc.issues:
            print(f"  {issue.render()}", file=sys.stderr)
        diagnosis = _intake_diagnosis(args, exc)
        if diagnosis is not None:
            print("diagnosis: " + json.dumps(diagnosis, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    except (BotError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 2
