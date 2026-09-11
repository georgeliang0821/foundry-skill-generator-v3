from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "reference" / "manifest.json"


class ReferenceSyncError(RuntimeError):
    pass


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReferenceSyncError(f"Cannot read manifest {path}: {exc}") from exc
    if manifest.get("schema_version") != 1:
        raise ReferenceSyncError("Unsupported reference manifest schema_version")
    if not manifest.get("files"):
        raise ReferenceSyncError("Reference manifest has no files")
    return manifest


def git_blob(checkout: Path, commit: str, source: str) -> bytes:
    command = ["git", "-C", str(checkout), "show", f"{commit}:{source}"]
    try:
        result = subprocess.run(command, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise ReferenceSyncError("git is not installed or not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        raise ReferenceSyncError(f"Cannot read {source} at {commit}: {detail}") from exc
    return result.stdout


def _find_symbol(tree: ast.Module, dotted_name: str) -> ast.AST | None:
    current: ast.AST = tree
    for part in dotted_name.split("."):
        body = getattr(current, "body", [])
        current = next(
            (
                node
                for node in body
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == part
            ),
            None,
        )
        if current is None:
            return None
    return current


def function_parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    parameters = [argument.arg for argument in (*node.args.posonlyargs, *node.args.args)]
    if node.args.vararg:
        parameters.append(f"*{node.args.vararg.arg}")
    parameters.extend(argument.arg for argument in node.args.kwonlyargs)
    if node.args.kwarg:
        parameters.append(f"**{node.args.kwarg.arg}")
    return parameters


def class_fields(node: ast.ClassDef) -> set[str]:
    fields: set[str] = set()
    for child in node.body:
        if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            fields.add(child.target.id)
    return fields


def validate_contracts(
    contents: dict[str, bytes], contracts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    trees: dict[str, ast.Module] = {}
    for contract in contracts:
        source = contract["source"]
        try:
            tree = trees.setdefault(source, ast.parse(contents[source], filename=source))
        except (KeyError, SyntaxError) as exc:
            findings.append({
                "source": source,
                "symbol": contract["symbol"],
                "problem": f"source cannot be parsed: {exc}",
                "review": contract.get("review", []),
            })
            continue
        node = _find_symbol(tree, contract["symbol"])
        if node is None:
            findings.append({
                "source": source,
                "symbol": contract["symbol"],
                "problem": "symbol is missing",
                "review": contract.get("review", []),
            })
            continue
        if "parameters" in contract:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                actual: Any = f"{type(node).__name__}, not a function"
            else:
                actual = function_parameters(node)
            if actual != contract["parameters"]:
                findings.append({
                    "source": source,
                    "symbol": contract["symbol"],
                    "problem": "parameters changed",
                    "expected": contract["parameters"],
                    "actual": actual,
                    "review": contract.get("review", []),
                })
        if "fields" in contract:
            actual_fields = class_fields(node) if isinstance(node, ast.ClassDef) else set()
            missing = sorted(set(contract["fields"]) - actual_fields)
            if missing:
                findings.append({
                    "source": source,
                    "symbol": contract["symbol"],
                    "problem": "required fields are missing",
                    "missing": missing,
                    "review": contract.get("review", []),
                })
    return findings


def _dict_key_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            names.update(
                key.value
                for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            )
        elif isinstance(node, ast.Call):
            names.update(keyword.arg for keyword in node.keywords if keyword.arg)
    return names


def _string_constants(tree: ast.Module) -> set[str]:
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def check_wire_coverage(
    repo_root: Path, contracts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Report runtime parameters the Generator never puts on the wire.

    A matching signature only proves the snapshot still parses. It does not prove
    the Generator actually exercises a parameter the runtime now accepts.
    """
    findings: list[dict[str, Any]] = []
    caches: dict[str, tuple[set[str], set[str]]] = {}
    for contract in contracts:
        wire = contract.get("wire")
        if not wire:
            continue
        consumer = wire["file"]
        if consumer not in caches:
            path = repo_root / consumer
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=consumer)
            except (OSError, SyntaxError) as exc:
                findings.append({
                    "source": contract["source"],
                    "symbol": contract["symbol"],
                    "file": consumer,
                    "problem": f"consumer cannot be parsed: {exc}",
                })
                caches[consumer] = (set(), set())
                continue
            caches[consumer] = (_dict_key_names(tree), _string_constants(tree))
        dict_keys, literals = caches[consumer]
        use_dict_keys = wire.get("match", "dict_key") == "dict_key"
        for parameter, token in wire["params"].items():
            found = (
                token in dict_keys
                if use_dict_keys
                else any(token in literal for literal in literals)
            )
            if found:
                continue
            findings.append({
                "source": contract["source"],
                "symbol": contract["symbol"],
                "file": consumer,
                "parameter": parameter,
                "expected_token": token,
                "problem": "runtime accepts this parameter but the Generator never sends it",
            })
    return findings


def _symbol_signatures(content: bytes, filename: str) -> dict[str, list[str]]:
    try:
        tree = ast.parse(content, filename=filename)
    except SyntaxError:
        return {}
    signatures: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            signatures[node.name] = function_parameters(node)
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    signatures[f"{node.name}.{child.name}"] = function_parameters(child)
    return signatures


def changed_symbols(before: bytes | None, after: bytes, filename: str) -> dict[str, list[str]]:
    old = _symbol_signatures(before or b"", filename)
    new = _symbol_signatures(after, filename)
    return {
        "added": sorted(new.keys() - old.keys()),
        "removed": sorted(old.keys() - new.keys()),
        "signature_changed": sorted(name for name in old.keys() & new.keys() if old[name] != new[name]),
    }


def _collection_members(value: ast.AST) -> list[str] | None:
    node = value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"frozenset", "set", "tuple", "list"}
        and node.args
    ):
        node = node.args[0]
    if not isinstance(node, (ast.Set, ast.List, ast.Tuple)) or not node.elts:
        return None
    members = [
        element.value
        for element in node.elts
        if isinstance(element, ast.Constant) and isinstance(element.value, str)
    ]
    return sorted(members) if len(members) == len(node.elts) else None


def extract_surface(content: bytes | None, filename: str) -> dict[str, dict[str, list[str]]]:
    """The externally observable contract of a snapshot, ignoring function bodies."""
    empty: dict[str, dict[str, list[str]]] = {"functions": {}, "fields": {}, "collections": {}}
    if not content:
        return empty
    try:
        tree = ast.parse(content, filename=filename)
    except SyntaxError:
        return empty
    fields: dict[str, list[str]] = {}
    collections: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            declared = class_fields(node)
            if declared:
                fields[node.name] = sorted(declared)
        elif isinstance(node, ast.Assign):
            members = _collection_members(node.value)
            if members is None:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    collections[target.id] = members
    return {
        "functions": _symbol_signatures(content, filename),
        "fields": fields,
        "collections": collections,
    }


def diff_surface(
    before: dict[str, dict[str, list[str]]], after: dict[str, dict[str, list[str]]]
) -> list[dict[str, Any]]:
    deltas: list[dict[str, Any]] = []
    for kind in ("functions", "fields", "collections"):
        old, new = before[kind], after[kind]
        for token in sorted(new.keys() - old.keys()):
            deltas.append({"kind": kind, "token": token, "change": "added", "after": new[token]})
        for token in sorted(old.keys() - new.keys()):
            deltas.append({"kind": kind, "token": token, "change": "removed", "before": old[token]})
        for token in sorted(old.keys() & new.keys()):
            if old[token] != new[token]:
                deltas.append({
                    "kind": kind,
                    "token": token,
                    "change": "changed",
                    "before": old[token],
                    "after": new[token],
                })
    return deltas


def impact_tokens(deltas: list[dict[str, Any]]) -> set[str]:
    """What to look for in Generator code: member names for collections, else the symbol."""
    tokens: set[str] = set()
    for delta in deltas:
        if delta["kind"] == "collections":
            tokens |= set(delta.get("before") or []) ^ set(delta.get("after") or [])
        else:
            tokens.add(delta["token"].rsplit(".", 1)[-1])
    return tokens


def impact_index(
    repo_root: Path, tokens: set[str], roots: tuple[str, ...] = ("backend", "tests")
) -> dict[str, list[str]]:
    if not tokens:
        return {}
    hits: dict[str, list[str]] = {token: [] for token in tokens}
    for root in roots:
        base = repo_root / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, SyntaxError):
                continue
            relative = path.relative_to(repo_root).as_posix()
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    name = node.id
                elif isinstance(node, ast.Attribute):
                    name = node.attr
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    name = node.name
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    name = node.value
                else:
                    continue
                if name in hits:
                    hits[name].append(f"{relative}:{getattr(node, 'lineno', 0)}")
    return {token: sorted(set(sites)) for token, sites in hits.items() if sites}


def explain_symbol(content: bytes | None, symbol: str) -> str | None:
    if not content:
        return None
    text = content.decode("utf-8", errors="replace")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    node = _find_symbol(tree, symbol)
    if node is None:
        node = next(
            (
                item
                for item in tree.body
                if isinstance(item, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == symbol for t in item.targets)
            ),
            None,
        )
    return ast.get_source_segment(text, node) if node is not None else None


def _field_reads(content: bytes, filename: str) -> set[str]:
    """String constants that are consumed, excluding keys of dicts being built.

    The exclusion matters: `{"reason": exc.reason}` constructs an error envelope,
    it does not read the runtime's `reason` field.
    """
    try:
        tree = ast.parse(content, filename=filename)
    except SyntaxError:
        return set()
    built_keys = {
        id(key)
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for key in node.keys
        if isinstance(key, ast.Constant)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in built_keys
    }


def check_response_coverage(
    repo_root: Path, upstream_contents: dict[str, bytes], manifest: dict[str, Any]
) -> list[dict[str, str]]:
    """Every field the runtime may return must be classified as required or ignored.

    Wire coverage checks what the Generator sends; this checks what it reads back.
    Classification lives in the manifest so the fields this project will never use
    stay silent instead of becoming permanent noise.
    """
    spec = manifest.get("response_wire")
    if not spec:
        return []
    source, symbol, target_file = spec["source"], spec["symbol"], spec["file"]
    members = set(extract_surface(upstream_contents.get(source), source)["collections"].get(symbol, []))
    if not members:
        return [{
            "file": target_file,
            "problem": f"{source}::{symbol} is missing or is no longer a set of string constants.",
        }]
    required, ignored = set(spec.get("required", [])), set(spec.get("ignored", {}))
    findings = [
        {
            "file": target_file,
            "problem": (
                f"{source}::{symbol} now carries `{field}`, which the manifest classifies "
                "as neither required nor ignored. Decide which it is."
            ),
        }
        for field in sorted(members - required - ignored)
    ]
    findings += [
        {
            "file": target_file,
            "problem": (
                f"{source}::{symbol} no longer carries `{field}`, but the manifest still "
                "classifies it. Drop the stale entry."
            ),
        }
        for field in sorted((required | ignored) - members)
    ]
    target = repo_root / target_file
    reads = _field_reads(target.read_bytes(), target_file) if target.is_file() else set()
    findings += [
        {
            "file": target_file,
            "problem": f"`{field}` is classified required, but it is never read there.",
        }
        for field in sorted(required - reads)
    ]
    return findings


def extract_version(content: bytes) -> str:
    text = content.decode("utf-8", errors="replace")
    match = re.search(r"^VERSION\s*[:=]\s*[\"']?([^\"'\s]+)", text, re.MULTILINE)
    return match.group(1) if match else "unknown"


def _checkout_path(manifest: dict[str, Any], override: str | None) -> Path:
    configured = override or os.getenv("REFERENCE_UPSTREAM_REPO") or manifest["upstream"].get("local_checkout")
    if not configured:
        raise ReferenceSyncError("Pass --upstream or set REFERENCE_UPSTREAM_REPO")
    checkout = Path(configured).expanduser().resolve()
    if not (checkout / ".git").exists():
        raise ReferenceSyncError(f"Upstream checkout is not a Git repository: {checkout}")
    return checkout


def inspect_reference(
    manifest: dict[str, Any], checkout: Path | None, commit: str | None, repo_root: Path
) -> tuple[dict[str, Any], dict[str, bytes], dict[str, bytes]]:
    """With no checkout, the committed snapshots stand in for upstream.

    That still proves the Generator honours the pinned contract; it cannot prove
    the pin is current.
    """
    upstream_contents: dict[str, bytes] = {}
    local_contents: dict[str, bytes] = {}
    files: list[dict[str, Any]] = []
    deltas: list[dict[str, Any]] = []
    for spec in manifest["files"]:
        source = spec["source"]
        destination = (repo_root / spec["destination"]).resolve()
        try:
            destination.relative_to(repo_root.resolve())
        except ValueError as exc:
            raise ReferenceSyncError(f"Destination escapes repository: {destination}") from exc
        local = destination.read_bytes() if destination.exists() else None
        if local is not None:
            local_contents[source] = local
        if checkout is not None:
            upstream = git_blob(checkout, commit or "", source)
        elif local is not None:
            upstream = local
        else:
            raise ReferenceSyncError(f"Offline check needs the committed snapshot: {spec['destination']}")
        upstream_contents[source] = upstream
        upstream_hash = sha256_bytes(upstream)
        surface = diff_surface(
            extract_surface(local, source), extract_surface(upstream, source)
        )
        deltas.extend(dict(delta, source=source) for delta in surface)
        files.append({
            "source": source,
            "destination": spec["destination"],
            "status": "pinned" if checkout is None else "current" if local == upstream else "missing" if local is None else "drift",
            "local_sha256": sha256_bytes(local) if local is not None else None,
            "upstream_sha256": upstream_hash,
            "manifest_sha256": spec.get("sha256"),
            "manifest_matches_upstream": spec.get("sha256") == upstream_hash,
            "version": extract_version(upstream),
            "symbols": changed_symbols(local, upstream, source),
            "surface": surface,
        })
    report = {
        "checkout": str(checkout) if checkout else None,
        "commit": commit,
        "offline": checkout is None,
        "files": files,
        "surface_deltas": deltas,
        "impact": impact_index(repo_root, impact_tokens(deltas)),
        "contract_findings": validate_contracts(upstream_contents, manifest.get("contracts", [])),
        "coverage_findings": (
            check_wire_coverage(repo_root, manifest.get("contracts", []))
            + check_response_coverage(repo_root, upstream_contents, manifest)
        ),
    }
    return report, upstream_contents, local_contents


_CHANGE_MARK = {"added": "+", "removed": "-", "changed": "~"}


def _describe_delta(delta: dict[str, Any]) -> str:
    kind, token, change = delta["kind"], delta["token"], delta["change"]
    mark = _CHANGE_MARK[change]
    if kind == "collections":
        before, after = set(delta.get("before") or []), set(delta.get("after") or [])
        moved = sorted(after - before) or sorted(before - after)
        return f"{mark} {change} collection `{token}` members: {', '.join(moved)}"
    if kind == "functions" and change == "changed":
        return (
            f"{mark} {token}({', '.join(delta['before'])})"
            f" -> {token}({', '.join(delta['after'])})"
        )
    return f"{mark} {change} {kind[:-1]} `{token}`"


def _print_report(report: dict[str, Any]) -> None:
    if report.get("offline"):
        print("Reference source: committed snapshots (upstream not consulted)")
    else:
        print(f"Reference source: {report['checkout']} @ {report['commit']}")
    for item in report["files"]:
        print(f"[{item['status'].upper():7}] {item['source']} -> {item['destination']} (version {item['version']})")
        for delta in item.get("surface", []):
            print(f"          {_describe_delta(delta)}")
    impact = report.get("impact", {})
    if report.get("surface_deltas"):
        if impact:
            print("[IMPACT] Generator code referencing the changed contract:")
            for token, sites in sorted(impact.items()):
                print(f"  - {token}: {', '.join(sites)}")
        else:
            print("[IMPACT] No Generator file references the changed contract.")
    for finding in report.get("coverage_findings", []):
        if "parameter" in finding:
            print(
                f"[ACTION] {finding['file']}: {finding['source']}::{finding['symbol']} accepts "
                f"`{finding['parameter']}`, but no `{finding['expected_token']}` is sent there."
            )
        else:
            print(f"[ACTION] {finding['file']}: {finding['problem']}")
    if report["contract_findings"]:
        print("[RED] Known compatibility contracts changed:")
        for finding in report["contract_findings"]:
            review = ", ".join(finding.get("review", []))
            print(f"  - {finding['source']}::{finding['symbol']}: {finding['problem']}; review {review}")
    elif any(item["status"] not in ("current", "pinned") for item in report["files"]):
        print("[YELLOW] Snapshot drift found; known contracts still match. Run the sync command, then tests.")
    elif stale := [i["destination"] for i in report["files"] if not i["manifest_matches_upstream"]]:
        print(f"[YELLOW] Snapshot bytes do not match the manifest sha256: {', '.join(stale)}.")
        print("         The snapshot was edited by hand. Restore it, or re-run sync to re-pin.")
    elif report.get("coverage_findings"):
        print("[ACTION] Snapshots are current, but the Generator's use of the runtime contract is incomplete.")
    elif report.get("offline"):
        print("[GREEN] The Generator matches the pinned snapshots. Run without --offline to check for upstream drift.")
    else:
        print("[GREEN] Snapshots and known compatibility contracts are current.")


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _run_validation(manifest: dict[str, Any], repo_root: Path) -> None:
    for command in manifest.get("validation", {}).get("commands", []):
        print(f"Running: {' '.join(command)}")
        result = subprocess.run(command, cwd=repo_root)
        if result.returncode:
            raise ReferenceSyncError(f"Validation failed with exit code {result.returncode}: {' '.join(command)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check or synchronize external runtime reference snapshots.")
    parser.add_argument("action", choices=("check", "sync"), nargs="?", default="check")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--upstream", help="Path to the upstream Git checkout")
    parser.add_argument("--commit", help="Git commit to inspect; defaults to the manifest commit")
    parser.add_argument("--json", action="store_true", help="Print the inspection report as JSON")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Check the Generator against the committed snapshots, without an upstream checkout",
    )
    parser.add_argument("--explain", metavar="SYMBOL", help="Print the local and upstream source of one symbol")
    parser.add_argument("--no-tests", action="store_true", help="Do not run focused validation after sync")
    return parser


def _print_explanation(
    symbol: str, local_contents: dict[str, bytes], upstream_contents: dict[str, bytes]
) -> None:
    found = False
    for source, upstream in upstream_contents.items():
        before = explain_symbol(local_contents.get(source), symbol)
        after = explain_symbol(upstream, symbol)
        if before is None and after is None:
            continue
        found = True
        print(f"\n--- {source}: local ---")
        print(before if before is not None else "(absent)")
        print(f"\n+++ {source}: upstream +++")
        print(after if after is not None else "(absent)")
    if not found:
        print(f"[ERROR] `{symbol}` was not found in any snapshot.", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Snapshots carry CJK comments; the Windows console defaults to cp1252.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    manifest_path = args.manifest.resolve()
    repo_root = manifest_path.parents[1]
    try:
        manifest = load_manifest(manifest_path)
        if args.offline and args.action == "sync":
            raise ReferenceSyncError("sync needs the upstream checkout; --offline only applies to check")
        checkout = None if args.offline else _checkout_path(manifest, args.upstream)
        commit = None if args.offline else (args.commit or manifest["upstream"]["commit"])
        report, contents, local_contents = inspect_reference(manifest, checkout, commit, repo_root)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            _print_report(report)
        if args.explain:
            _print_explanation(args.explain, local_contents, contents)
        if report["contract_findings"]:
            return 2
        if args.action == "check":
            manifest_mismatch = any(not item["manifest_matches_upstream"] for item in report["files"])
            snapshot_drift = any(item["status"] not in ("current", "pinned") for item in report["files"])
            if manifest_mismatch or snapshot_drift:
                return 1
            return 4 if report["coverage_findings"] else 0
        for spec in manifest["files"]:
            destination = repo_root / spec["destination"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(contents[spec["source"]])
            spec["sha256"] = sha256_bytes(contents[spec["source"]])
            spec["version"] = extract_version(contents[spec["source"]])
        manifest["upstream"]["commit"] = commit
        manifest["upstream"]["verified_at"] = date.today().isoformat()
        _write_manifest(manifest_path, manifest)
        if not args.no_tests:
            _run_validation(manifest, repo_root)
        print("[GREEN] Reference snapshots synchronized and validated.")
        return 0
    except ReferenceSyncError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())