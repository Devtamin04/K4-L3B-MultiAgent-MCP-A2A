from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import workflow
from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .evidence_cache import CachedGateway, run_cache_dir
from .mcp_gateway import connect_gateway
from .report import write_summary
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


async def _solve_all(root: Path, gateway: CachedGateway, contracts: Contracts) -> None:
    case_set = load_case_set(root)
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    for stale in output_root.glob("*.json"):
        stale.unlink()
    trace_path.unlink(missing_ok=True)
    trace = TraceWriter(trace_path, contracts)
    workflow.REPORT_DIR = root / "reports"

    outputs = {}
    for number, case_id in enumerate(case_set.case_ids, 1):
        case = case_set.cases[case_id]
        trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
        output = await solve_case(case, gateway, trace)
        contracts.validate_output(output, f"outputs/{case_id}.json")
        if output.get("case_id") != case_id:
            raise ValueError(f"solver returned a mismatched case_id for {case_id}")
        target = output_root / f"{case_id}.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(target)
        trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
        outputs[case_id] = output
        print(
            f"[{number:3d}/{len(case_set.case_ids)}] {case_id} "
            f"{output['assessment']['primary_issue']:<24} "
            f"refund={output['financial_resolution']['recommended_refund_brl']:>6.2f} "
            f"tools={gateway.calls_by_case.get(case_id, 0)}",
            flush=True,
        )
    summary = write_summary(
        root / "reports", case_set.cases, outputs, gateway.calls_by_case, gateway.network_calls
    )
    print(
        f"OK: {len(outputs)} outputs; MCP network calls={gateway.network_calls}, "
        f"cache hits={gateway.cache_hits}; report: {summary.relative_to(root)}"
    )


async def _run(root: Path, offline: bool) -> None:
    contracts = Contracts(root / "contracts" / "schemas")
    if offline:
        Settings.load(root)
        cache_dir = run_cache_dir(root)
        if cache_dir is None:
            raise RuntimeError("offline mode needs an unexpired DAY09_RUN_EXPIRES_AT cache")
        await _solve_all(root, CachedGateway(None, cache_dir), contracts)
        return
    settings = Settings.load(root)
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        discovered_tools = await gateway.list_tools()
        if not discovered_tools:
            raise RuntimeError("MCP Gateway returned no tools")
        missing = set().union(*workflow.ACTOR_TOOLS.values()) - set(discovered_tools)
        if missing:
            raise RuntimeError(f"MCP Gateway does not expose required tools: {sorted(missing)}")
        await _solve_all(root, CachedGateway(gateway, run_cache_dir(root)), contracts)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument(
        "--offline", action="store_true", help="replay from the run-scoped evidence cache only"
    )
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / {len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root, args.offline))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
