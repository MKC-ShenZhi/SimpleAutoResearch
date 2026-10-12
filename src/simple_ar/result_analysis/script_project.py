"""Copy and validate historical code-analysis delivery inventories.

Legacy implementation, ingestion and report consumers still use this contract.
New creation uses native inputs and does not manufacture a repair scaffold.
"""
from __future__ import annotations

from pathlib import Path


MAX_DATA_BYTES = 20 * 1024 * 1024
SCRIPT_RESULT_REQUIREMENTS = (
    'In object-shaped outputs/results.json, record each figure as '
    '{"path": "outputs/<filename>", "caption": "<one paragraph>"} in the figures list. '
    'Use an actual package-relative exported image path. Explain the displayed comparison, '
    'units and meaning of lines, markers or uncertainty. These authored captions accompany '
    'registered figures into later reports; they are not independent scientific checks.'
    ' When supplied source identities are available, preserve them in results.json citation_map '
    'using the existing citation_map.v1 format: {"schema_version":"citation_map.v1",'
    '"entries":[{"paper_id":"stable-source-id","title":"recorded title",'
    '"url":"recorded public URL","bibliography":{"authors":[],"year":"", "doi":""}}]}. '
    'Copy only supplied identity fields; unknown authors, dates and DOI stay empty. '
    'This metadata is source-reported, not verification of the source or its claims.'
)


def copy_code_analysis_package(source: Path, destination: Path | None, *, workspace: bool = False) -> dict:
    """Copy declared script deliverables without executing or certifying results.

    Initial publication follows a successful CodeTask validation. Later copies
    use only the saved relative inventory, so relocation needs no old workspace.
    """
    from shutil import copy2
    from simple_ar.core.artifacts import read_json, write_json

    root = source.resolve() if workspace else source.resolve().parent
    if workspace:
        files = ["analysis.py", "README.md", "outputs/report.md", "outputs/results.json"]
        files.extend(path.relative_to(root).as_posix() for folder in ("data", "tests", "src", "outputs")
                     for path in (root / folder).rglob("*") if path.is_file() and "__pycache__" not in path.parts)
        files.extend(path.relative_to(root).as_posix() for path in root.glob("*")
                     if path.is_file() and path.suffix in {".py", ".dot"} and path.name != "analysis.py")
        figure_groups = {}
        for name in sorted(set(files)):
            path = Path(name)
            if path.parts[0] == "outputs" and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".svg", ".pdf"}:
                figure_groups.setdefault(path.with_suffix("").as_posix(), {})[path.suffix.lower()[1:]] = name
        figures = []
        for stem, exports in figure_groups.items():
            if set(exports) == {"pdf"}:
                continue  # A standalone PDF may be a document, not a figure.
            primary = next(exports[ext] for ext in ("png", "jpg", "jpeg", "svg", "pdf") if ext in exports)
            figures.append({"path": primary, "caption": f"Figure: {Path(stem).name}.",
                            "exports": {ext: name for ext, name in exports.items() if name != primary}})
        result = {"schema_version": "code_analysis.v1", "files": list(dict.fromkeys(files)),
                  "figures": figures,
                  "evidence_role": "validated_script_output_not_independently_recomputed"}
    else:
        result = read_json(source)
        if not isinstance(result, dict) or result.get("schema_version") != "code_analysis.v1":
            raise ValueError("Expected a code_analysis.v1 package.")
    files = result.get("files")
    required = {"analysis.py", "outputs/report.md", "outputs/results.json"}
    if not isinstance(files, list) or not all(isinstance(path, str) for path in files) or not required.issubset(files):
        raise ValueError("Code analysis package is missing its source or delivery inventory.")
    resolved = []
    for name in files:
        relative = Path(name)
        path = (root / relative).resolve()
        if relative.is_absolute() or ".." in relative.parts or not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Code analysis attachments must be existing package-local files.")
        resolved.append((relative, path))
    if sum(path.stat().st_size for _, path in resolved) > 4 * MAX_DATA_BYTES:
        raise ValueError("Code analysis attachment package exceeds 80 MiB.")
    figures = result.get("figures")
    if not isinstance(figures, list):
        raise ValueError("Code analysis figures must be a list (empty for results without figures).")
    for figure in figures:
        if (not isinstance(figure, dict) or not isinstance(figure.get("caption"), str)
                or not isinstance(figure.get("exports", {}), dict)
                or any(path not in files for path in [figure.get("path"), *figure.get("exports", {}).values()])):
            raise ValueError("Code analysis figures must refer to inventoried attachments.")
    recorded = read_json(root / "outputs/results.json")
    if not isinstance(recorded, (dict, list)) or not recorded:
        raise ValueError("Code analysis results must be a nonempty JSON object or array.")
    if workspace and isinstance(recorded, dict) and isinstance(recorded.get("figures"), list):
        # Captions belong to the producing analysis, not filename inference.
        # Match only inventoried exports; legacy path-only entries still work.
        captions = {row["path"]: row["caption"].strip() for row in recorded["figures"]
                    if isinstance(row, dict) and isinstance(row.get("path"), str)
                    and isinstance(row.get("caption"), str) and row["caption"].strip()}
        for figure in figures:
            figure["caption"] = next((captions[name] for name in
                (figure["path"], *figure.get("exports", {}).values()) if name in captions), figure["caption"])
    result = {**result, "results": recorded}
    if destination is None:
        return result
    # Validate the entire inventory before copying; never follow package paths
    # into another project, even if the manifest says the project was validated.
    for relative, path in resolved:
        target = destination / relative
        if not target.resolve().is_relative_to(destination.resolve()):
            raise ValueError("Code analysis destination must stay inside its package.")
        target.parent.mkdir(parents=True, exist_ok=True)
        copy2(path, target)
    write_json(destination / "analysis.json", result)
    return result
