"""Prepare an existing project with the established CodeTask workspace builder."""

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping
import os
import re
import shlex
import subprocess
import sys
import tomllib
from importlib import metadata

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion

from simple_ar.code_task.orchestration.workflow import initialize_code_task
from simple_ar.code_task.review_pipeline import authorized_review_index, build_review_index
from simple_ar.core.capabilities import CapabilityContext, CapabilityResult
from simple_ar.experiment.execution.backend import RunRequest
from simple_ar.experiment.templates import build_experiment_code
from simple_ar.research.text_dataset import read_text_dataset


def _requirement_lines(excerpt: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read the plain-specifier subset, not pip's installer instruction language.

    Retain line provenance and unresolved directives. Includes, environment
    expansion, hashes and editable installs are never followed or executed.
    """
    output, pending, start = [], "", 1
    lines = excerpt["text"].splitlines(keepends=True)
    for number, physical in enumerate(lines, 1):
        text = physical.rstrip("\r\n")
        if not pending:
            start = number
        continued = (len(text) - len(text.rstrip("\\"))) % 2 == 1
        pending += text[:-1] if continued else text
        unread_end = number == len(lines) and excerpt.get("has_unread_tail", excerpt.get("truncated", False))
        if unread_end and (continued or not physical.endswith(("\r", "\n"))):
            output.append({"declared": pending, "source_path": excerpt["path"],
                "source_lines": [start, number], "status": "incomplete_requirement_line"})
            pending = ""
            break
        if continued:
            continue
        value = re.sub(r"\s+#.*$", "", pending).strip()
        pending = ""
        if not value or value.startswith("#"):
            continue
        row = {"declared": value, "source_path": excerpt["path"], "source_lines": [start, number]}
        if value.startswith("-"):
            row["status"] = "pip_directive_not_processed"
        elif "${" in value:
            row["status"] = "environment_reference_not_expanded"
        output.append(row)
    if pending:
        output.append({"declared": pending, "source_path": excerpt["path"],
            "source_lines": [start, len(lines)], "status": "incomplete_requirement_line"})
    return output


def _dependency_probe(declared: Mapping[str, Any], *, requirements_excerpts: tuple[Mapping[str, Any], ...] = ()) -> dict[str, Any]:
    """Inspect distribution metadata in this interpreter, never import the project.

    This is not a resolver or environment installer. URL provenance, extras,
    transitive dependencies and binary compatibility require other evidence.
    """
    probe = {"scope": "inspecting_interpreter_only", "python_executable": sys.executable,
             "project_imported": False, "packages": [], "limitations": [
        "Distribution metadata does not establish import/runtime or binary compatibility.",
        "Project interpreter, extras, transitive dependencies and URL provenance are not verified."]}
    python_requirement = declared.get("requires-python")
    if isinstance(python_requirement, str):
        try:
            probe["python_requirement"] = {"declared": python_requirement,
                "matches_inspecting_python": SpecifierSet(python_requirement).contains(sys.version.split()[0])}
        except InvalidSpecifier:
            probe["python_requirement"] = {"declared": python_requirement, "status": "invalid_declaration"}
    requirements = declared.get("dependencies")
    rows = [{"declared": value, "source_path": "pyproject.toml"}
            for value in requirements] if isinstance(requirements, list) else []
    for excerpt in requirements_excerpts:
        rows.extend(_requirement_lines(excerpt))
    if requirements_excerpts:
        probe["requirements_sources"] = [{"path": row["path"], "truncated": row["truncated"]}
            for row in requirements_excerpts]
        probe["limitations"].append(
            "Requirements files are observed separately, not combined into a resolved environment. "
            "Only plain PEP 508 specifiers are compared; includes, constraints, pip options, environment expansion and install artifacts remain unresolved.")
    if not isinstance(requirements, list) and not requirements_excerpts:
        probe["status"] = "no_static_dependency_list"
        return probe
    probe["status"] = "metadata_inspected"
    for row in rows:
        probe["packages"].append(row)
        if "status" in row:
            continue
        value = row["declared"]
        try:
            if not isinstance(value, str):
                raise InvalidRequirement("Requirement must be text")
            requirement = Requirement(value)
            row["name"] = requirement.name
            if requirement.marker and not requirement.marker.evaluate():
                row["status"] = "marker_not_applicable_here"
                continue
            try:
                version = metadata.version(requirement.name)
            except metadata.PackageNotFoundError:
                row["status"] = "distribution_not_found_here"
                continue
            row["installed_version"] = version
            if requirement.url:
                row["status"] = "installed_source_not_verified"
            else:
                row["matches_declared_version"] = requirement.specifier.contains(version)
                row["status"] = "version_matches" if row["matches_declared_version"] else "version_mismatch"
            if requirement.extras:
                row["unverified_extras"] = sorted(requirement.extras)
        except (InvalidRequirement, InvalidVersion, ValueError) as exc:
            row.update(status="unresolved_declaration", reason=type(exc).__name__)
        except (OSError, UnicodeError) as exc:
            row.update(status="metadata_unreadable", reason=type(exc).__name__)
    return probe


@dataclass(frozen=True)
class PreparationRequest:
    execution: Mapping[str, Any]
    task_text: str
    run: RunRequest | None = None
    run_dir: Path | None = None
    source_project: Path | None = None
    data_paths: tuple[Path, ...] = ()
    generation_source: Mapping[str, Any] | None = None


def inspect_project_preparation(
    project: Path, *, data_paths: tuple[Path, ...] = (), index: Mapping[str, Any] | None = None,
    read_paths: tuple[str, ...] = (),
    access_boundary: Path | None = None,
) -> dict[str, Any]:
    """Read preparation inputs; neither README commands nor manifests execute.

    Use the same repository index as design/CodeTask, not another discovery
    engine. Runtime identity describes the inspecting interpreter only.
    """
    from simple_ar.code_task.execution.environment import DEPENDENCY_FILE_NAMES

    # A raw user path is a new source, not its own authorization proof.
    # Task-derived projects instead carry their existing mount/workspace grant.
    from simple_ar.tools.workspace import resolve_source_mount
    from simple_ar.code_task.runtime.state import review_workspace_boundary
    root = (resolve_source_mount(project) if access_boundary is None
            else project.expanduser().absolute())
    boundary = review_workspace_boundary(root, boundary=access_boundary or root)
    if not root.is_dir():
        raise ValueError(f"Project directory not found: {root}")
    index = build_review_index(root, access_boundary=boundary) if index is None else index
    # Saved inventories are observations, never grants. Project their paths
    # through the current grant without rewriting the historical artifact.
    from simple_ar.code_task.analysis.index import code_task_access_filter
    allowed = code_task_access_filter(root, boundary=boundary)
    index = authorized_review_index(root, index, access_boundary=boundary)
    available_paths = {row['path'] for row in index['files']}
    documents = []
    # Generated lock files are intentionally absent from the code-review
    # text index. Their top-level names still belong in preparation inventory.
    for path in root.iterdir():
        if allowed(path) and path.is_file() and path.name.endswith(".lock") and path.name in DEPENDENCY_FILE_NAMES:
            documents.append((path.name, "dependency_declaration"))
    for row in index.get("files", []):
        relative = str(row["path"])
        path = root / relative
        is_dependency = path.name in DEPENDENCY_FILE_NAMES
        is_readme = path.name.lower().startswith("readme") and path.suffix.lower() in {".md", ".markdown", ".rst", ".txt", ""}
        if not (is_dependency or is_readme):
            continue
        # Top-level declarations first; nested examples often describe a
        # different environment. Both retain exact provenance and scope.
        documents.append((relative, "dependency_declaration" if is_dependency else "project_instructions"))
    documents.sort(key=lambda item: (len(Path(item[0]).parts), item[0]))
    selected_documents = documents[:12]
    root_manifest = ("pyproject.toml", "dependency_declaration")
    if root_manifest in documents and root_manifest not in selected_documents:
        selected_documents[-1:] = [root_manifest]
    excerpts = []
    notes = []
    for relative, role in selected_documents:
        path = root / relative
        if path.name.endswith(".lock"):
            # Lockfile identities are useful; their large generated contents
            # are not preparation prose. Keep the path available for later use.
            continue
        if not allowed(path):
            notes.append(f"Not read: {relative} no longer passes project access checks.")
            continue
        try:
            with path.open("r", encoding="utf-8") as stream:
                text = stream.read(16001)
        except (OSError, UnicodeError) as exc:
            notes.append(f"Could not read {relative}: {type(exc).__name__}")
            continue
        excerpts.append({"path": relative, "role": role, "text": text[:16000],
                         "truncated": len(text) > 16000, "has_unread_tail": len(text) > 16000})
    if len(documents) > 12:
        notes.append(f"{len(documents) - 12} additional instruction/dependency files not read; their paths are retained.")
    declared = {}
    manifest = next((row for row in excerpts if row["path"] == "pyproject.toml"), None)
    if manifest and not manifest["truncated"]:
        try:
            payload = tomllib.loads(manifest["text"])
            project_metadata = payload.get("project", {})
            if isinstance(project_metadata, Mapping):
                declared = {key: project_metadata[key] for key in ("name", "requires-python", "dependencies", "optional-dependencies", "scripts") if key in project_metadata}
            else:
                notes.append("pyproject.toml project metadata is not a table; its original text is retained.")
        except tomllib.TOMLDecodeError:
            notes.append("pyproject.toml could not be parsed; its original text is retained.")
    # Entry source explains flags and emitted measurements without importing
    # the project. It shares the established index; notebooks remain unread.
    # Preparation can inspect declared lockfiles on request without adding
    # generated contents to the code-review index or initial model context.
    lock_paths = {relative for relative, _ in documents if relative.endswith(".lock")
                  and allowed(root / relative)}
    available_paths.update(lock_paths)
    if len(read_paths) > 15 or any(path not in available_paths for path in read_paths):
        raise ValueError("Preparation reads must name indexed project text files (at most 15 extra files); no external or excluded paths.")
    # Console entries commonly have no main guard. Read their indexed source
    # within the same five-file allowance, not just arbitrary script prefixes.
    # These are candidate source locations, not installed executable bindings.
    script_declarations = declared.get("scripts", {})
    declared_entries = []
    for name, reference in script_declarations.items() if isinstance(script_declarations, Mapping) else ():
        module = reference.split(":", 1)[0] if isinstance(reference, str) and ":" in reference else ""
        parts = module.split(".")
        candidates = []
        if all(part.isidentifier() for part in parts):
            stem = "/".join(parts)
            candidates = [path for prefix in ("", "src/") for path in
                (f"{prefix}{stem}.py", f"{prefix}{stem}/__init__.py") if path in available_paths]
        declared_entries.append({"name": name, "reference": reference, "source_candidates": candidates,
            "scope": "Static project declaration; root/src source candidates only, installed binding and callable unverified."})
    entrypoints = list(dict.fromkeys([path for row in declared_entries for path in row["source_candidates"]]
                                    + list(index.get("entrypoints", []))))
    for relative in entrypoints[:5]:
        if any(row["path"] == relative for row in excerpts):
            continue
        path = root / relative
        if not allowed(path):
            continue
        try:
            with path.open("r", encoding="utf-8") as stream:
                text = stream.read(8001)
        except (OSError, UnicodeError) as exc:
            notes.append(f"Could not read entry {relative}: {type(exc).__name__}")
            continue
        excerpts.append({"path": relative, "role": "entry_source", "text": text[:8000],
                         "truncated": len(text) > 8000, "has_unread_tail": len(text) > 8000, "read_limit_characters": 8000})
    from simple_ar.code_task.analysis.source_context import requested_source_context
    read_index = {**index, "files": [*index.get("files", []), *[
        {"path": path} for path in sorted(lock_paths)
        if not any(row["path"] == path for row in index.get("files", []))]]}
    for relative in read_paths:
        windows = requested_source_context(root, read_index, {"files": [relative]}, supplied=excerpts,
            max_files=1, max_chars=8000)
        for window in windows:
            excerpts.append({**window, "role": "project_source", "read_limit_characters": 8000})
    datasets = []
    for supplied in data_paths:
        path = supplied.expanduser()
        path = path if path.is_absolute() else root / path
        path = path.resolve()
        row = {"path": str(path), "available": path.exists()}
        if path.is_file():
            row.update(kind="file", size_bytes=path.stat().st_size, suffix=path.suffix.lower())
        elif path.is_dir():
            row["kind"] = "directory"
        datasets.append(row)
    reading_coverage = {}
    for row in excerpts:
        coverage = reading_coverage.setdefault(row["path"], {"ranges": [], "has_unread_tail": True})
        coverage["ranges"].append([row.get("source_offset", 0), row.get("source_offset", 0) + len(row["text"])])
        coverage["has_unread_tail"] = row.get("has_unread_tail", False)
    return {
        "schema_version": "project_preparation.v1", "project": str(root),
        "file_count": index.get("file_count", 0), "entrypoint_candidates": entrypoints,
        "declared_entrypoints": declared_entries,
        "notebook_candidates": list(index.get("notebooks", [])),
        "source_file_paths": sorted(available_paths)[:2000],
        "source_paths_omitted": max(0, len(available_paths) - 2000),
        "reading_coverage": reading_coverage,
        "test_files": [str(row["path"]) for row in index.get("files", []) if "test" in str(row.get("role", ""))],
        "runtime": {"python_executable": sys.executable, "python_version": sys.version.split()[0]},
        "dependency_probe": _dependency_probe(declared, requirements_excerpts=tuple(row for row in excerpts
            if row["role"] == "dependency_declaration" and Path(row["path"]).name in {"requirements.txt", "requirements-dev.txt"})),
        "declared_project": declared, "document_paths": [row[0] for row in documents],
        "excerpts": excerpts, "data_paths": datasets, "notes": notes,
        "open_questions": [
            "Which published conclusion or software behavior should be checked?",
            "Which documented command and data split correspond to that scope?",
            "What computation limit and accepted adaptations apply?",
        ],
        "limitations": [
            "Read-only preparation: no project imports, dependency installation, downloads, tests or training.",
            "Declarations/instructions are project-authored, not verified dependency availability or execution authority.",
            "Data presence/size does not establish contents, completeness, splits or paper correspondence.",
        ],
    }


def project_preparation_markdown(facts: Mapping[str, Any]) -> str:
    """Readable material for an independent writing/planning task."""
    lines = ["# Project preparation", "", f"Project: {facts['project']}", "",
             "## Inspecting runtime", f"Python: {facts['runtime']['python_version']} ({facts['runtime']['python_executable']})",
             "", "## Declared console entries (bindings and callables unverified)",
             *[f"- {row['name']}: {row['reference']}; indexed source candidates: {row['source_candidates']}"
               for row in facts.get("declared_entrypoints", [])],
             "", "## Entry candidates (not executed)", *[f"- {path}" for path in facts["entrypoint_candidates"]],
             "", "## Notebook candidates (cells and saved outputs unread)",
             *[f"- {row['path']}: {row['size_bytes']} bytes; not a confirmed command" for row in facts.get("notebook_candidates", [])],
             "", "## Data locations", *[f"- {row['path']}: {'present' if row['available'] else 'missing'}; {row.get('size_bytes', 'size not inspected')}" for row in facts["data_paths"]],
             "", "## Decisions still needed", *[f"- {text}" for text in facts["open_questions"]],
             "", "## Inspection limits", *[f"- {text}" for text in [*facts["limitations"], *facts["notes"]]],
             "", "## Project-authored instructions and declarations"]
    for row in facts["excerpts"]:
        lines.extend(["", f"### {row['path']} ({row['role']})", "",
                      f"Read range: offset {row.get('source_offset', 0)}, up to {row.get('read_limit_characters', 16000)} characters; " +
                      ("excerpt, not a complete-file read." if row["truncated"] else "complete file.") +
                      (" Further tail unread." if row.get('has_unread_tail') else ""),
                      "", *["> " + line for line in row["text"].splitlines()]])
    probe = facts.get("dependency_probe")
    if isinstance(probe, Mapping):
        lines.extend(["", "## Dependency metadata in the inspecting interpreter", "",
            f"Interpreter: {probe['python_executable']}. This is not a project-environment readiness check.",
            *[f"- {row.get('source_path', 'declaration')}" + (f":{row['source_lines'][0]}–{row['source_lines'][1]}" if row.get('source_lines') else "")
              + f" / {row['declared']!r}: {row['status']}; installed version: {row.get('installed_version', 'not observed')}"
              + (f"; extras not verified: {row['unverified_extras']}" if row.get('unverified_extras') else "")
              for row in probe["packages"]],
            *[f"- {limit}" for limit in probe["limitations"]]])
    return "\n".join(lines) + "\n"


def inspect_execution_entry(execution: Mapping[str, Any], *,
                            access_boundary: Path | None = None,
                            project_index: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Inspect the supplied project boundary without creating a workspace.

    The result is a compact fact pack for research design. It records existing
    entrypoints and the caller's benchmark argv, but never executes a process,
    installs dependencies, or grants a model a new cwd/timeout/resource scope.
    """

    config = dict(execution)
    facts: dict[str, Any] = {
        "schema_version": "execution_entry_facts.v1",
        "authorized_argv_prefixes": [],
        "entrypoint_candidates": [],
        "limitations": [
            "Static entry inspection does not prove runtime success or metric correctness.",
        ],
    }
    command = config.get("command")
    if isinstance(command, (list, tuple)) and command and all(isinstance(item, str) for item in command):
        facts["benchmark_argv"] = list(command)
        facts["authorized_argv_prefixes"].append(list(command))
    baseline = config.get("baseline")
    if isinstance(baseline, Mapping):
        baseline_command = baseline.get("command")
        if isinstance(baseline_command, (list, tuple)) and baseline_command and all(isinstance(item, str) for item in baseline_command):
            facts["baseline_argv"] = list(baseline_command)
            facts["authorized_argv_prefixes"].append(list(baseline_command))

    task = config.get("code_task")
    if isinstance(task, Mapping) and task.get("code_root"):
        from simple_ar.tools.workspace import resolve_source_mount
        from simple_ar.code_task.runtime.state import review_workspace_boundary
        raw = Path(str(task["code_root"]))
        root = resolve_source_mount(raw) if access_boundary is None else raw.expanduser().absolute()
        boundary = review_workspace_boundary(root, boundary=access_boundary or root)
        if not root.is_dir():
            raise ValueError("code_task.code_root must be an existing absolute project directory.")
        index = (build_review_index(root, result_schema=config.get("result_schema"), access_boundary=boundary)
                 if project_index is None else authorized_review_index(root, project_index, access_boundary=boundary))
        entrypoints = [str(item) for item in index.get("entrypoints", [])]
        facts["project"] = {
            "root": str(root),
            "file_count": int(index.get("file_count", 0)),
            "python_file_count": int(index.get("python_file_count", 0)),
            "test_file_count": sum(
                1 for row in index.get("files", [])
                if isinstance(row, Mapping) and "test" in str(row.get("role") or "")
            ),
            "entrypoint_candidates": entrypoints,
        }
        facts["entrypoint_candidates"] = entrypoints
        facts["preparation"] = inspect_project_preparation(root, index=index, access_boundary=boundary)
        for entrypoint in entrypoints:
            prefix = [sys.executable, entrypoint]
            if prefix not in facts["authorized_argv_prefixes"]:
                facts["authorized_argv_prefixes"].append(prefix)
    elif "dataset" in config:
        path = Path(str(config["dataset"])).expanduser().resolve()
        inspected = read_text_dataset(path)
        facts["dataset"] = {
            key: value for key, value in inspected.items() if key != "rows"
        }

    facts["configured_result_schema"] = dict(config.get("result_schema", {})) if isinstance(config.get("result_schema"), Mapping) else {}
    facts["configured_timeout_sec"] = config.get("timeout_sec")
    facts["configured_cwd"] = str(config.get("cwd") or "")
    return facts


def revision_preparation_config(config: Mapping[str, Any], prepared: Mapping[str, Any], *,
                                source_root: Path) -> dict[str, Any]:
    """Re-import the declared baseline or copy the accepted candidate, not both.

    Candidate continuation inherits its resolved interpreter. Import filters and
    venv discovery belong to the original source, not an already selected copy.
    The file-size ceiling remains explicit; a lossy candidate copy is rejected.
    """
    raw = dict(config)
    task = dict(raw["code_task"])
    task.pop("run_dir", None)
    task["code_root"] = str(source_root)
    if source_root.resolve() == Path(prepared["workspace"]).resolve():
        task["workspace_mode"] = "copy"
        for key in ("workspace_include", "workspace_exclude", "workspace_reuse_source_venv"):
            task.pop(key, None)
        actual = prepared["execution"]["code_task"]
        task.update({key: actual[key] for key in ("env_mode", "python_executable") if key in actual})
    else:
        task.setdefault("workspace_mode", "copy")
    raw.update(code_task=task, cwd=str(source_root))
    if isinstance(raw.get("baseline"), Mapping):
        raw["baseline"] = {**raw["baseline"], "cwd": str(source_root)}
    return raw


def run_preparation_capability(*, context: CapabilityContext, request: PreparationRequest, backend=None) -> CapabilityResult:
    if "environment" in request.execution and "code_task" not in request.execution:
        from simple_ar.research.project_environment import prepare_project_environment
        return prepare_project_environment(context=context, request=request, backend=backend)
    if "dataset" in request.execution:
        return _prepare_text_baseline(context, request)
    config = dict(request.execution)
    task = dict(config["code_task"])
    if set(task) - {
        "kind", "code_root", "approval_note", "max_repairs", "allowed_patterns",
        "budget_profile", "allow_large_edits", "edit_budget_overrides", "workspace_mode", "protected_patterns",
        "env_mode", "python_executable",
        "validation_command", "validation_timeout_sec", "initial_files",
        "max_file_bytes", "workspace_include", "workspace_exclude", "workspace_reuse_source_venv",
    }:
        raise ValueError(
            "Preparing code_task accepts code_root, approval_note, max_repairs, "
            "allowed_patterns, protected_patterns, budget_profile, allow_large_edits, workspace_mode, "
            "max_file_bytes, workspace_include, workspace_exclude, workspace_reuse_source_venv, "
            "env_mode, python_executable, validation_command, validation_timeout_sec and initial_files."
        )
    allowed = task.pop("allowed_patterns", None)
    initial_files = task.pop("initial_files", ())
    if initial_files or "environment" in config:
        from simple_ar.app.research_execution import code_task_validation
        code_task_validation(config, required=True)
    protected = task.pop("protected_patterns", ())
    workspace_mode = task.pop("workspace_mode", "auto")
    workspace_options = {name: task.pop(name) for name in (
        "max_file_bytes", "workspace_include", "workspace_exclude", "workspace_reuse_source_venv") if name in task}
    if "environment" in config and workspace_options.get("workspace_reuse_source_venv"):
        raise ValueError("Choose source-venv reuse or task environment preparation, not both.")
    kind = task.pop("kind", "existing_project")
    if kind not in {"existing_project", "greenfield"}:
        raise ValueError("Code preparation kind must be existing_project or greenfield.")
    if workspace_mode == "empty" and kind != "greenfield":
        raise ValueError("An empty research workspace requires greenfield creation, not an existing-project edit.")
    env_mode = task.get("env_mode", "current")
    python_executable = task.get("python_executable")
    for name, patterns in (("allowed_patterns", () if allowed is None else allowed), ("protected_patterns", protected)):
        if not isinstance(patterns, (list, tuple)) or any(not isinstance(p, str) or not p.strip() for p in patterns):
            raise ValueError(f"{name} must be a list of workspace-relative patterns; empty uses CodeTask defaults.")
    source_root = Path(task["code_root"]) if task.get("code_root") else None
    if source_root is None and (kind != "greenfield" or workspace_mode != "empty"):
        raise ValueError("Only greenfield with an empty workspace may omit code_root.")
    inherited = request.generation_source
    names = {'task_contract', 'architecture_plan', 'code_artifacts', 'dependency_advice', 'implementation_plan'}
    if inherited is not None and (kind != 'greenfield' or source_root is None or workspace_mode != 'copy'
            or set(inherited) != {'documents', 'result_schema'} or set(inherited['documents']) != names
            or any(not isinstance(row, dict) for row in inherited['documents'].values())
            or set(inherited['result_schema']) - {'primary_metric', 'metric_directions'}):
        raise ValueError('Generated revision requires its complete accepted generation contract and a copied project')
    root = source_root or request.run.cwd
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("code_task.code_root must be an existing absolute project directory.")
    lineage_root = Path(request.source_project) if request.source_project is not None else root
    if not lineage_root.is_absolute() or not lineage_root.is_dir():
        raise ValueError("Preparation source_project must be an existing absolute project directory.")
    if not isinstance(task.get("approval_note"), str) or not task["approval_note"].strip():
        raise ValueError("Preparing code_task requires explicit isolated-edit approval.")
    config.setdefault("cwd", str(root))
    if request.run.cwd.resolve() != root.resolve():
        raise ValueError("Preparation cwd must match code_root; shared data should use explicit external paths.")
    # Project-relative data keeps its runtime location, including on candidate
    # revisions copied from an earlier workspace. External inputs stay external.
    data_inputs = []
    lineage_root = lineage_root.resolve()
    for path in request.data_paths:
        path = Path(path).absolute()
        if path.is_relative_to(lineage_root):
            data_inputs.append(path.relative_to(lineage_root).as_posix())
    data_inputs = tuple(dict.fromkeys(data_inputs))
    source_snapshot = None
    if source_root is not None and source_root.resolve() != lineage_root:
        from simple_ar.code_task.analysis.index import build_codebase_index
        source_snapshot = {row["path"]: row["sha256"] for row in build_codebase_index(source_root)["files"]}
    task_ref = context.store.write_text("inputs/task.md", request.task_text, kind="task_input", schema="markdown.v1")
    # Keep the checker portable until the approved environment has been built.
    # request.run already resolves current Python, and is NOT the formal command.
    argv = task["validation_command"] if "environment" in config else request.run.command
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    initialized = initialize_code_task(
        run_dir=context.store.root / (request.run_dir or Path("project_run")), code_root=source_root, kind=kind,
        task_file=context.store.resolve(task_ref), benchmark_command=command,
        workspace_mode=workspace_mode,
        **workspace_options,
        env_mode=env_mode, python_executable=python_executable,
        edit_scope_allowed_patterns=tuple(allowed or ()),
        edit_scope_protected_patterns=tuple(protected),
        data_inputs=data_inputs,
        initial_files=initial_files,
    )
    if source_snapshot is not None:
        from simple_ar.code_task.runtime.state import implementation_file_hashes
        copied = implementation_file_hashes(initialized.run_dir)
        if any(copied.get(name) != digest for name, digest in source_snapshot.items()):
            raise ValueError("Candidate revision copy omitted or changed accepted source; inspect copy limits before implementation.")
    if inherited is not None:
        from simple_ar.code_task.runtime.state import load_code_task_manifest, save_code_task_manifest
        from simple_ar.core.artifacts import write_json
        for name, value in inherited['documents'].items():
            write_json(initialized.task_dir / 'meta' / f'{name}.json', value)
        current = load_code_task_manifest(initialized.run_dir)
        # Logical generation contract survives; only implementation strategy
        # changes to controlled edits of the actual accepted candidate.
        current['implementation'] = {'mode': 'controlled_patch', 'status': 'revision_prepared'}
        current['benchmark'].update(inherited['result_schema'])
        save_code_task_manifest(initialized.run_dir, current)
    config["cwd"] = str(initialized.workspace_dir)
    if "baseline" in config:
        baseline = dict(config["baseline"])
        if "cwd" in baseline and Path(baseline["cwd"]).resolve() == root.resolve():
            baseline["cwd"] = str(initialized.workspace_dir)
        config["baseline"] = baseline
    task.pop("code_root")
    if workspace_options.get("workspace_reuse_source_venv"):
        task.update(env_mode=initialized.environment_policy["mode"],
                    python_executable=initialized.environment_policy.get("python_executable"))
    task["run_dir"] = str(initialized.run_dir)
    config["code_task"] = task
    payload = {
        "schema_version": "prepared_execution.v1", "execution": config,
        "source_project": str(lineage_root.resolve()) if source_root is not None else None, "workspace": str(initialized.workspace_dir),
        "copy_report": initialized.copy_report.to_json(),
        "workspace_info": initialized.workspace.to_manifest(run_dir=initialized.run_dir),
        "limitations": ["No dependency installation or dataset download; external datasets remain external assets.",
                         "Declared project data is copied independently and protected from automated edits, not OS-sandboxed. Undeclared large files and excluded paths may remain absent; inspect the copy report.",
                         *initialized.workspace.warnings],
    }
    if "environment" in config:
        from simple_ar.research.project_environment import prepare_project_environment
        result = prepare_project_environment(context=context,
            request=replace(request, execution=config, run=replace(request.run, cwd=initialized.workspace_dir)),
            backend=backend, prepared_payload=payload)
        return replace(result, artifacts=(task_ref, *result.artifacts))
    ref = context.store.write_json("execution.json", payload,
        kind="prepared_execution", schema="prepared_execution.v1", producer="research.preparation")
    return CapabilityResult(status="completed", artifacts=(task_ref, ref))


def _prepare_text_baseline(context: CapabilityContext, request: PreparationRequest) -> CapabilityResult:
    config = request.execution
    if set(config) - {"dataset", "timeout_sec"}:
        raise ValueError("CSV baseline preparation accepts dataset and timeout_sec; other methods require an existing project.")
    path = Path(config["dataset"])
    timeout = config.get("timeout_sec")
    if not path.is_absolute() or path.suffix.lower() != ".csv":
        raise ValueError("dataset must be an absolute CSV path with text,label,split columns.")
    if type(timeout) is not int or timeout < 1:
        raise ValueError("CSV baseline requires positive timeout_sec.")
    inspected = read_text_dataset(path)
    rows = inspected.pop("rows")
    data = context.store.write_json("baseline/dataset.json", rows, kind="experiment_dataset")
    script = context.store.write_text("baseline/experiment.py", build_experiment_code({"template": "csv_text_classification"}),
                                      kind="experiment_code", schema="python.v1")
    inspection = context.store.write_json("dataset_inspection.json", inspected, kind="dataset_inspection")
    workspace = context.store.resolve(script).parent
    execution = {
        "command": [sys.executable, "experiment.py"], "cwd": str(workspace), "timeout_sec": timeout,
        "label": "text_baseline", "result_schema": {"primary_metric": "accuracy", "direction": "higher",
                                                     "required_metrics": ["accuracy", "macro_f1"]},
        "protocol": {"contract_id": "csv-text-baseline", "hypothesis": request.task_text,
                     "dataset_refs": [{"asset_id": "text_csv", "sha256": inspected["sha256"]}],
                     "split_spec": {split: [i for i, row in enumerate(rows) if row["split"] == split] for split in ("train", "eval")},
                     "metric_specs": [{"name": name, "unit": "fraction"} for name in ("accuracy", "macro_f1")],
                     "comparison_conditions": {"method": "CountVectorizer+LogisticRegression", "max_iter": 200, "seed": 0},
                     "protected_assets": [{"asset_id": "data", "path": "dataset.json"},
                                          {"asset_id": "evaluator", "path": "experiment.py"}]},
    }
    ref = context.store.write_json("execution.json", {
        "schema_version": "prepared_execution.v1", "execution": execution,
        "dataset_inspection": inspection.to_dict(),
        "limitations": inspected["limitations"] + ["One explicit CSV baseline, not paper reproduction or candidate search.",
                                                  "Default word tokenizer; language suitability has not been assessed."],
    }, kind="prepared_execution", schema="prepared_execution.v1", producer="research.preparation")
    return CapabilityResult(status="completed", artifacts=(data, script, inspection, ref),
                            diagnostics=tuple(inspected["limitations"]))
