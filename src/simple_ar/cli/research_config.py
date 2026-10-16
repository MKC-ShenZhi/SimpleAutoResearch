"""Map research TOML onto the same options used by the CLI."""
import argparse
from dataclasses import dataclass
from pathlib import Path
import tomllib


FIELDS = {
    "task": {"goal": ("topic", str), "kind": ("task_kind", str), "outputs": ("outputs", list), "output_root": ("output_root", str),
              "selected_idea_id": ("selected_idea_id", str)},
    "model": {"name": ("model", str), "feasibility_review_model": ("feasibility_review_model", str),
              "max_output_tokens": ("max_output_tokens", int)},
    "budget": {"total_tokens": ("total_tokens", int), "llm_requests": ("llm_requests", int),
               "process_invocations": ("process_invocations", int), "process_wall_seconds": ("process_wall_seconds", int)},
    "continuation": {"authorization_id": ("authorization_id", str),
                     "reason": ("authorization_reason", str),
                     "additional_attempts": ("additional_attempts", int),
                     "additional_no_progress": ("additional_no_progress", int),
                     "decision_id": ("decision_id", str),
                     "decision_response": ("decision_response", str),
                     "decision_guidance": ("decision_guidance", str),
                     "remaining": ("authorize_remaining", dict)},
    "research": {"providers": ("providers", list), "queries": ("queries", list),
                 "max_results": ("max_results", int), "max_chunks": ("max_chunks", int),
                 "max_pdf_pages": ("research_max_pdf_pages", int),
                 "max_fulltext_documents": ("research_max_fulltext_documents", int),
                 "max_pdf_mb": ("research_max_pdf_mb", int),
                 "read_max_shortlist": ("read_max_shortlist", int),
                 "idea_limit": ("idea_limit", int), "cache_dir": ("cache_dir", str),
                 "use_fulltext": ("research_use_fulltext", bool),
                 "materials_only": ("research_materials_only", bool),
                 "allow_pdf_download": ("research_allow_pdf_download", bool),
                  "max_iterations": ("max_research_iterations", int),
                 "keep_raw_pdf": ("research_keep_raw_pdf", bool),
                 "web_extract_backend": ("research_web_extract_backend", str),
                 "interaction": ("interaction", str)},
    "assets": {"papers": ("local_document", list), "materials": ("material", list), "data": ("data_path", list)},
    "analysis": {"file": ("data_file", str), "value_columns": ("value_column", list),
                 "group_column": ("group_column", str), "observation_unit": ("observation_unit", str),
                 "value_unit": ("value_unit", str), "mode": ("data_mode", str),
                 "missing": ("data_missing", str), "width": ("figure_width", str), "max_mb": ("data_max_mb", int),
                 "max_figures": ("data_max_figures", int), "plot": ("data_plot", str),
                 "x_column": ("x_column", str), "x_unit": ("x_unit", str), "max_points": ("data_max_points", int),
                 "series_layout": ("series_layout", str), "paired_baseline": ("paired_baseline", str),
                 "association": ("data_association", str),
                 "attribution": ("data_attribution", str)},
    "execution": {"command": ("command_argv", list), "cwd": ("cwd", str),
                  "timeout_sec": ("timeout_sec", int), "code_task_config": ("code_task_config", str),
                  "primary_metric": ("primary_metric", str), "metrics": ("metric", list),
                  "metric_directions": ("metric_direction", list)},
    "report": {"writing_method": ("report_writing_method", str),
               "template": ("report_template", str), "reviewer": ("report_reviewer", str),
               "outline_strategy": ("report_outline_strategy", str),
               "data_tables": ("report_data_tables", str),
               "max_review_iterations": ("max_review_iterations", int),
               "document_review": ("report_document_review", bool),
               "review_scope": ("report_review_scope", str),
               "draft_scope": ("report_draft_scope", str),
               "max_document_review_prompt_chars": ("max_document_review_prompt_chars", int),
               "max_section_tokens": ("max_section_tokens", int),
               "max_cited_sources": ("report_max_cited_sources", int),
               "figures": ("report_figures", dict)},
}
PATHS = {"output_root", "cache_dir", "cwd", "code_task_config", "local_document", "material", "data_file", "data_path"}
LIST_FLAGS = {"providers": "--provider", "queries": "--query", "local_document": "--local-document", "material": "--material",
              "metric": "--metric", "metric_direction": "--metric-direction", "value_column": "--value-column", "data_path": "--data-path"}


def setup_option_contract(kind: str | None = None) -> dict:
    """Semantic setup fields: one owner for model exposure and conversion.

    Defaults stay on parsed CLI arguments. Paths, execution proposals and their
    authorization are deliberately not part of this value-conversion contract.
    """
    from simple_ar.report.templates import BUILTIN_TEMPLATE_NAMES
    from simple_ar.result_analysis.table import TABLE_CHOICES, TABLE_MODE_DESCRIPTIONS, TABLE_PLOT_DESCRIPTIONS
    options = {}
    for field, (destination, value_type) in FIELDS["analysis"].items():
        if field in {"file", "width", "max_mb", "max_figures", "max_points"}:
            continue
        options[destination] = {"type": "list of names" if value_type is list else "string", "kinds": ["data_analysis"]}
        if field in TABLE_CHOICES:
            options[destination]["allowed_values"] = list(TABLE_CHOICES[field])
    options["data_mode"]["operations"] = TABLE_MODE_DESCRIPTIONS
    options["data_plot"]["operations"] = TABLE_PLOT_DESCRIPTIONS
    options.update({
        "sources": {"type": "string", "kinds": ["survey", "writing", "reproduction", "data_analysis"],
                    "allowed_values": ["materials", "search"] if kind in {None, "survey"} else ["materials"]},
        "template": {"type": "string", "kinds": ["writing"], "allowed_values": sorted(BUILTIN_TEMPLATE_NAMES)},
        "environment": {"type": "string", "kinds": ["reproduction"], "allowed_values": ["current", "venv"]},
        "requirements": {"type": "list of names", "kinds": ["reproduction"],
                         "description": "Inspected requirements.txt/requirements-dev.txt only; packaging declarations use install_project."},
        "install_project": {"type": "boolean", "kinds": ["reproduction"], "description": "Inspected root packaging declaration; requires venv and confirmation."},
        "with_report": {"type": "boolean", "kinds": ["data_analysis"], "description": "Add a model-written report to preset analysis."},
        "scripted": {"type": "boolean", "kinds": ["data_analysis"], "description": "Generate analysis/plot code when presets do not cover the goal."},
    })
    return {key: value for key, value in options.items() if kind is None or kind in value["kinds"]}


def normalize_setup_options(value: object, args: argparse.Namespace, locked: set[str], kind: str) -> dict:
    """Convert optional model values without erasing CLI defaults or explicit choices."""
    if not isinstance(value, dict):
        raise ValueError("Setup options must be an object; no paths or commands")
    contract = setup_option_contract()
    if unknown := set(value) - contract.keys():
        raise ValueError(f"Unsupported option keys: {sorted(unknown)}. Allowed option keys: {sorted(setup_option_contract(kind))}")
    options = {key: item for key, item in value.items() if item is not None}
    if kind == "reproduction" and options.get("template") == "reproduction" and "template" not in locked:
        options.pop("template")  # Restating the fixed task template is not a customization.
    for key, item in options.items():
        spec = contract[key]
        expected = {"string": str, "boolean": bool, "list of names": list}[spec["type"]]
        if type(item) is not expected:
            if expected is str:
                raise ValueError(f"{key} must be one string, not {type(item).__name__}; allowed values: {spec.get('allowed_values', 'text')}")
            raise ValueError(f"{key} must be a {spec['type']}")
        if expected is list and ((key == "value_column" and not item) or any(not isinstance(v, str) or not v.strip() for v in item)):
            raise ValueError(f"{key} must be a {'nonempty ' if key == 'value_column' else ''}list of names")
        if "allowed_values" in spec and item not in spec["allowed_values"]:
            raise ValueError(f"{key} allowed values: {spec['allowed_values']}")
        if key in locked and item != getattr(args, key):
            raise ValueError(f"Do not override explicit {key}; ask the user about a conflict")
        if kind not in spec["kinds"]:
            raise ValueError(f"Data semantics or option {key} requires {', '.join(spec['kinds'])}")
    if "sources" in options:
        if kind == "data_analysis" and not options.get("with_report", args.with_report):
            raise ValueError("Source scope requires a literature/material task")
        if kind != "survey" and options["sources"] != "materials":
            raise ValueError("Native writing/reproduction preparation here uses supplied material only")
    return options


def report_settings(args: argparse.Namespace, *, explicit_destinations: set[str] | None = None) -> dict:
    """Project report options from the existing TOML/CLI field owner.

    New sessions consume parsed defaults; recovery consumes only supplied
    destinations. False, zero and empty mappings are values, not omissions.
    """
    settings = {}
    for key, (destination, _) in FIELDS["report"].items():
        if key == "writing_method":
            continue  # Execution owner, not a ReportRuntimeConfig field.
        if args.command == "research-report":
            destination = key
        if explicit_destinations is not None and destination not in explicit_destinations:
            continue
        value = getattr(args, destination, None)
        if value is None:
            continue
        if key in {"max_document_review_prompt_chars", "max_section_tokens"} and value < 0:
            raise SystemExit(f"--{key.replace('_', '-')} cannot be negative; use 0 to omit the cap.")
        settings[key] = value
    return settings


def report_writing_method(args: argparse.Namespace, settings: dict) -> tuple[str, dict]:
    """Map explicit argparse destinations to the new-task report owner."""
    from simple_ar.report.writing import resolve_report_writing_config

    explicit = set(getattr(args, "_explicit_resume_destinations", ()))
    fields = {key for key, (destination, _) in FIELDS["report"].items()
              if (key if getattr(args, "command", None) == "research-report" else destination) in explicit}
    return resolve_report_writing_config(settings,
        method=getattr(args, "report_writing_method", None), explicit_fields=fields)


@dataclass(frozen=True)
class SessionArguments:
    """Validated CLI/TOML input, not another persisted task model."""
    task_kind: str
    command: tuple[str, ...]
    execution_details: dict
    outputs: list[str] | None
    materials: list[Path]
    data_analysis: dict | None


def data_options_supplied(args: argparse.Namespace) -> bool:
    """Detect nondefault table options using the same mapping as configuration."""
    from simple_ar.result_analysis.table import TableSpec
    defaults = {"file": None, "value_columns": [], "observation_unit": "", **TableSpec.defaults()}
    return any(hasattr(args, destination) and getattr(args, destination) != defaults[key]
               for key, (destination, _) in FIELDS["analysis"].items())


def data_settings(args: argparse.Namespace) -> dict:
    """Validate table settings shared by guided and direct session input."""
    from simple_ar.result_analysis.table import TableSpec, read_table_source, validate_table_columns
    spec = TableSpec.from_config({key: getattr(args, destination)
        for key, (destination, _) in FIELDS["analysis"].items()
        if key != "file" and hasattr(args, destination)})
    if args.data_file is None:
        raise ValueError("Data analysis requires --data-file.")
    path = args.data_file.expanduser().resolve()
    # Preview checks shape and names; ingestion freezes and validates fresh bytes.
    _, rows = read_table_source(path, max_mb=spec.max_mb)
    validate_table_columns(rows, spec)
    return {"file": str(path), **spec.to_config()}


def validate_session_arguments(args: argparse.Namespace) -> SessionArguments:
    """Validate merged arguments before model setup or session writes."""
    if not getattr(args, "session_root", None):
        try:
            report_writing_method(args, report_settings(args))
        except ValueError as exc:
            raise SystemExit(f'Invalid report configuration: {exc}') from exc
    if getattr(args, "reanalyze", False) and not getattr(args, "session_root", None):
        raise SystemExit("--reanalyze requires --session-root.")
    if getattr(args, "recover_interrupted", False) and not getattr(args, "session_root", None):
        raise SystemExit("--recover-interrupted requires --session-root.")
    if args.max_results < 1 or args.max_chunks < 1 or args.idea_limit < 1:
        raise SystemExit(
            "--max-results, --max-chunks, and --idea-limit must be positive."
        )
    if getattr(args, "research_max_pdf_pages", None) is not None and args.research_max_pdf_pages < 1:
        raise SystemExit("research.max_pdf_pages must be positive.")
    for name in ("research_max_fulltext_documents", "research_max_pdf_mb"):
        value = getattr(args, name, None)
        if value is not None and value < 1:
            raise SystemExit(f"{name.removeprefix('research_')} must be positive when provided.")
    if args.timeout_sec is not None and args.timeout_sec < 1:
        raise SystemExit("--timeout-sec must be positive when provided.")
    if args.max_review_iterations < 0:
        raise SystemExit("--max-review-iterations cannot be negative.")
    if args.max_research_iterations < 0:
        raise SystemExit("--max-research-iterations cannot be negative.")
    task_kind = str(getattr(args, "task_kind", "auto") or "auto").strip().lower()
    if task_kind not in {"auto", "survey", "bug_fix", "measurement", "reproduction", "writing", "data_analysis"}:
        raise SystemExit("--task-kind must be auto, survey, bug_fix, measurement, reproduction, writing or data_analysis.")
    command = tuple(args.command_argv or ())
    execution_details = getattr(args, "execution_details", {})
    if "initial_files" in execution_details and not getattr(args, "code_task_config", None):
        raise SystemExit("execution.initial_files requires explicit CodeTask preparation.")
    if command and execution_details.get("pairs"):
        raise SystemExit("Use execution.pairs or a single command, not both; paired argv must be explicit.")
    outputs = getattr(args, "outputs", None)
    materials = getattr(args, "material", [])
    data_paths = getattr(args, "data_path", [])
    if data_paths and task_kind not in {"reproduction", "measurement", "auto", "bug_fix"}:
        raise SystemExit("--data-path/assets.data belongs to experiment inputs; table analysis uses analysis.file/--data-file.")
    if outputs and "data_analysis" in outputs and task_kind != "data_analysis":
        raise SystemExit("data_analysis output requires an explicit data_analysis task.")
    data_analysis = None
    data_report = task_kind == "data_analysis" and ("report" in (outputs or ()) or args.with_report)
    measurement_report = task_kind == 'measurement' and 'report' in (outputs or ())
    if task_kind == "data_analysis":
        if command or execution_details or args.code_task_config or args.queries or args.providers or (outputs is not None and set(outputs) not in ({"data_analysis"}, {"data_analysis", "report"})):
            raise SystemExit("Data analysis accepts a supplied table, descriptive settings and an optional report, without research, CodeTask or experiment execution.")
        if (args.local_document or materials) and not data_report:
            raise SystemExit("Data documentation and references require outputs=[data_analysis, report].")
        if not getattr(args, "session_root", None):
            try:
                data_analysis = data_settings(args)
            except (OSError, ValueError) as exc:
                raise SystemExit(str(exc)) from exc
    elif any((args.data_file, args.value_column, args.group_column, args.observation_unit, args.value_unit,
              args.data_mode != "observations", args.data_missing != "reject", args.figure_width != "wide", args.data_max_mb != 20, args.data_max_figures != 100,
              args.data_plot != "bar", args.x_column, args.x_unit, args.data_max_points != 10000, args.series_layout != "separate", args.paired_baseline, args.data_attribution, getattr(args, "data_association", "none") != "none")):
        raise SystemExit("Data options require --task-kind data_analysis.")
    if materials and task_kind == "survey":
        from simple_ar.research.documents.ingest import retained_document_materials
        supplied = [Path(path).expanduser().resolve() for path in materials]
        try:
            valid_sources = retained_document_materials(supplied, original_sources_only=True)
        except (OSError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        if any(path.suffix.lower() != ".json" for path in supplied) or len(valid_sources) != len(supplied):
            raise SystemExit("Survey materials must be saved original-source document bundles; use --local-document for ordinary sources.")
    elif materials and task_kind not in {"writing", "data_analysis", "reproduction"} and not measurement_report:
        raise SystemExit("--material/assets.materials requires survey source bundles, writing, reproduction preparation or data_analysis with a report.")
    if task_kind == "writing":
        if command or execution_details or getattr(args, "code_task_config", None) or args.no_report or outputs not in (None, ["report"]):
            raise SystemExit("Writing requests only a report without execution or CodeTask configuration.")
        if not getattr(args, "session_root", None) and not (materials or args.local_document):
            raise SystemExit("Writing requires --material and/or --local-document.")
    if task_kind == "writing" or data_report or measurement_report:
        if args.queries or args.providers or getattr(args, "research_materials_only", None) is False or getattr(args, "research_allow_pdf_download", None):
            raise SystemExit("Material-based reports use supplied local material only; online research is a survey task.")
        supplied = [Path(path).expanduser().resolve() for path in [*materials, *args.local_document]]
        if len(supplied) != len(set(supplied)):
            raise SystemExit("Writing material must have one unambiguous role per file; do not repeat a paper as material.")
        from simple_ar.research.documents.ports import SUPPORTED_DOCUMENT_SUFFIXES, SUPPORTED_MATERIAL_SUFFIXES
        for paths, suffixes in ((args.local_document, SUPPORTED_DOCUMENT_SUFFIXES),
                               (materials, SUPPORTED_MATERIAL_SUFFIXES if task_kind == "writing" or measurement_report else SUPPORTED_DOCUMENT_SUFFIXES)):
            if any(not Path(path).expanduser().is_file() or Path(path).suffix.lower() not in suffixes for path in paths):
                raise SystemExit("Report inputs must be local text/HTML/PDF; writing also accepts JSON material and table_analysis.v1 packages.")
    code_delivery = bool(getattr(args, "code_task_config", None) and outputs and set(outputs) & {"bug_fix", "code_task"} and "experiments" not in outputs)
    if outputs and task_kind != "bug_fix" and not code_delivery and "experiments" not in outputs and (command or execution_details or getattr(args, "code_task_config", None)):
        raise SystemExit("Execution configuration requires experiments in --outputs/task.outputs.")
    if task_kind == "bug_fix" and outputs and ("bug_fix" not in outputs or set(outputs) - {"bug_fix", "report"}):
        raise SystemExit("--task-kind bug_fix requires bug_fix and optionally report in --outputs.")
    if task_kind == "survey" and not code_delivery and (command or execution_details or getattr(args, "code_task_config", None)):
        raise SystemExit("--task-kind survey cannot include execution or CodeTask configuration.")
    if task_kind == "measurement":
        if not outputs or 'experiments' not in outputs or set(outputs) - {'experiments', 'report'}:
            raise SystemExit("--task-kind measurement requires --outputs experiments and optionally report.")
        if not command and not execution_details.get("pairs") or getattr(args, "code_task_config", None):
            raise SystemExit("--task-kind measurement requires explicit command(s), without CodeTask.")
        if (execution_details.get('baseline_policy') == 'reuse' and not execution_details.get('baseline_ref')
                and not getattr(args, 'session_root', None)):
            raise SystemExit('Measurement baseline reuse requires an explicit baseline_ref result file.')
    if task_kind == "reproduction":
        if not outputs or "experiments" not in outputs or set(outputs) - {"experiments", "report"}:
            raise SystemExit("--task-kind reproduction requires outputs experiments and optionally report.")
        if not command or execution_details.get("pairs") or execution_details.get("baseline_policy") in {"run", "reuse"}:
            raise SystemExit("Reproduction requires one explicit measurement command without paired runs or baseline training.")
        if getattr(args, "code_task_config", None) and execution_details.get("environment"):
            profile = execution_details["environment"]
            if profile["mode"] != "venv" or profile.get("check_command"):
                raise SystemExit("CodeTask preparation permits explicit venv installation, with its own independent validator instead of an environment check.")
        protocol = execution_details.get("protocol")
        if not isinstance(protocol, dict) or any(not str(protocol.get(key) or "").strip()
                                                for key in ("hypothesis", "dataset", "expected_outcome")):
            raise SystemExit("Prepared reproduction requires execution.protocol hypothesis, dataset and expected_outcome.")
        if not getattr(args, "local_document", None) or not getattr(args, "research_materials_only", False):
            raise SystemExit("Prepared reproduction requires local documents and research.materials_only=true.")
    if outputs and (args.with_report or args.no_report):
        raise SystemExit("Use explicit outputs or --with-report/--no-report, not both.")
    for field in ("total_tokens", "llm_requests", "max_output_tokens", "process_invocations", "process_wall_seconds"):
        value = getattr(args, field, None)
        if value is not None and value < (0 if field.startswith("process_") else 1):
            raise SystemExit(f"Invalid {field}: {value}")
    if task_kind == "bug_fix" and not getattr(args, "code_task_config", None):
        raise SystemExit("--task-kind bug_fix requires --code-task-config for an existing project.")
    return SessionArguments(task_kind, command, execution_details, outputs, materials, data_analysis)


def research_defaults(
    arguments: list[str], *, explicit_destinations: set[str] | None = None,
) -> dict:
    if not arguments or arguments[0] != "research-session":
        return {}
    # Everything after --command belongs to the external process.
    options = arguments[:arguments.index("--command")] if "--command" in arguments else arguments
    probe = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    probe.add_argument("--config", type=Path)
    path = probe.parse_known_args(options)[0].config
    if path is None:
        return {}
    path = path.resolve()
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    defaults = {"model": "env"}
    for section, values in data.items():
        if section not in FIELDS or not isinstance(values, dict):
            raise ValueError(f"Unknown research configuration section: {section}")
        if section == "execution":
            from simple_ar.app.research_execution import validate_execution_declaration
            validate_execution_declaration(values)
        for name, value in values.items():
            if section == "execution" and name in {
                "baseline", "pairs", "protocol", "seeds", "seed_flag", "seed_count", "baseline_policy", "baseline_ref", "output_files", "metric_sources", "environment", "initial_files",
            }:
                if name == "initial_files":
                    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item.strip() for item in value):
                        raise ValueError("execution.initial_files must be a nonempty list of exact new Python paths")
                elif name == "output_files":
                    from simple_ar.experiment.execution.outputs import output_files
                    output_files({"output_files": value})
                elif name == "metric_sources":
                    from simple_ar.experiment.execution.outputs import metric_sources
                    metric_sources({"output_files": values.get("output_files", {}), "metric_sources": value})
                elif name == "baseline":
                    if not isinstance(value, dict):
                        raise ValueError("execution.baseline must be a table")
                elif name == "protocol":
                    if not isinstance(value, dict):
                        raise ValueError("execution.protocol must be a table")
                elif name == "environment":
                    from simple_ar.research.project_environment import environment_profile
                    value = environment_profile(value)
                defaults.setdefault("execution_details", {})[name] = value
                continue
            if name not in FIELDS[section]:
                raise ValueError(f"Unknown research configuration field: {section}.{name}")
            dest, expected = FIELDS[section][name]
            if explicit_destinations is not None:
                explicit_destinations.add(dest)
            if type(value) is not expected or (expected is list and any(type(item) is not str for item in value)):
                raise ValueError(f"Invalid type for {section}.{name}: expected {expected.__name__}")
            if expected is int and dest not in {"additional_attempts", "additional_no_progress"} and value < (0 if dest in {"max_review_iterations", "max_section_tokens", "max_document_review_prompt_chars", "report_max_cited_sources", "max_research_iterations", "process_invocations", "process_wall_seconds"} else 1):
                raise ValueError(f"Invalid value for {section}.{name}: {value}")
            if dest == "interaction" and value not in {"assisted", "checkpoints", "autonomous"}:
                raise ValueError("research.interaction must be assisted, checkpoints or autonomous")
            if dest == "research_web_extract_backend" and value not in {"direct", "tavily_basic"}:
                raise ValueError("research.web_extract_backend must be direct or tavily_basic")
            if dest == "report_outline_strategy" and value not in {"auto", "template", "adaptive"}:
                raise ValueError("report.outline_strategy must be auto, template or adaptive")
            if dest == "report_writing_method" and value not in {"article", "sections"}:
                raise ValueError("report.writing_method must be article or sections")
            if dest == "report_review_scope" and value not in {"section", "document"}:
                raise ValueError("report.review_scope must be section or document")
            if dest == "report_draft_scope" and value not in {"section", "document", "auto"}:
                raise ValueError("report.draft_scope must be section, document or auto")
            if dest == "report_data_tables" and value not in {"linked", "full"}:
                raise ValueError("report.data_tables must be linked or full")
            if dest == "decision_response" and value not in {"accept", "reject", "revise"}:
                raise ValueError("continuation.decision_response must be accept, reject or revise")
            if dest in PATHS:
                def resolve(item):
                    target = Path(item).expanduser()
                    return str(target if target.is_absolute() else (path.parent / target).resolve())
                value = [resolve(item) for item in value] if expected is list else resolve(value)
            defaults[dest] = value
    allowances = defaults.get("authorize_remaining")
    if allowances is not None:
        if not allowances or any(
            not isinstance(key, str) or not key.strip()
            or type(value) not in {int, float}
            for key, value in allowances.items()
        ):
            raise ValueError("continuation.remaining must map resource names to numeric amounts")
    if "outputs" in defaults and (not defaults["outputs"] or set(defaults["outputs"]) - {"summary", "report", "experiments", "bug_fix", "code_task", "data_analysis"}):
        raise ValueError("task.outputs must contain summary, report, experiments, bug_fix and/or data_analysis")
    if defaults.get("task_kind", "auto") not in {"auto", "survey", "bug_fix", "measurement", "reproduction", "writing", "data_analysis"}:
        raise ValueError("task.kind must be auto, survey, bug_fix, measurement, reproduction, writing or data_analysis")
    if "analysis" in data and defaults.get("task_kind") != "data_analysis":
        raise ValueError("analysis configuration requires task.kind=data_analysis")
    if defaults.get("report_reviewer", "llm") not in {"llm", "disabled"}:
        raise ValueError("report.reviewer must be llm or disabled")
    for dest, flag in LIST_FLAGS.items():
        if any(option == flag or option.startswith(flag + "=") for option in options):
            defaults.pop(dest, None)
    return defaults
