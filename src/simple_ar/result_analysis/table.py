"""Explicit, descriptive table analysis; no model, experiment or code execution.

The input capability freezes the supplied bytes and settings once. The analysis
capability consumes that registered snapshot, so recovery never rereads a user's
changed file. Aggregated values and row-level observations remain distinct.
"""
from __future__ import annotations

import csv
import hashlib
from dataclasses import MISSING, asdict, dataclass, fields
import io
import json
import math
from pathlib import Path
import statistics

from simple_ar.core.capabilities import ArtifactRef, ArtifactStore, CapabilityContext, CapabilityResult

TABLE_MODE_DESCRIPTIONS = {
    "observations": "Aggregate observation rows into group count/mean/sample standard deviation and empirical quartiles; bar means or min-max box plots.",
    "values": "Retain supplied numeric values without aggregation: line/scatter keeps each complete x/y pair, including individual observations; bars require unique category labels.",
}

TABLE_PLOT_DESCRIPTIONS = {
    "bar": "Descriptive group means or supplied summary values.",
    "box": "Observation Q1–Q3, median and min–max; not uncertainty or outlier testing.",
    "line": "Supplied numeric x/y coordinates, ordered within series; missing y breaks lines.",
    "scatter": "Supplied numeric x/y coordinates, retaining individual complete pairs.",
    "heatmap": "Supplied matrix values with unique row labels and a common quantity/unit; no normalization, clustering or inferred correlation.",
}

TABLE_CHOICES = {
    "mode": tuple(TABLE_MODE_DESCRIPTIONS), "plot": tuple(TABLE_PLOT_DESCRIPTIONS),
    "missing": ("reject", "omit"), "width": ("column", "wide"),
    "series_layout": ("separate", "shared"), "association": ("none", "pearson"),
}


@dataclass(frozen=True)
class TableSpec:
    value_columns: tuple[str, ...]
    observation_unit: str
    group_column: str = ""
    value_unit: str = ""
    mode: str = "observations"
    missing: str = "reject"
    width: str = "wide"
    max_mb: int = 20
    max_figures: int = 100
    plot: str = "bar"
    x_column: str = ""
    x_unit: str = ""
    max_points: int = 10000
    series_layout: str = "separate"
    paired_baseline: str = ""
    attribution: str = ""
    association: str = "none"

    def __post_init__(self):
        if not isinstance(self.attribution, str):
            raise ValueError("attribution must be user-declared source text, not inferred bibliographic metadata.")
        if not self.value_columns or any(not isinstance(v, str) or not v.strip() for v in self.value_columns):
            raise ValueError("Select at least one nonempty numeric value column; columns are not guessed.")
        if len(set(self.value_columns)) != len(self.value_columns) or self.group_column in self.value_columns:
            raise ValueError("Value columns must be distinct and cannot be the grouping column.")
        if not isinstance(self.observation_unit, str) or not self.observation_unit.strip():
            raise ValueError("State what one row represents (observation_unit).")
        if not isinstance(self.group_column, str) or not isinstance(self.value_unit, str):
            raise ValueError("group_column and value_unit must be strings.")
        if self.mode not in TABLE_CHOICES["mode"] or self.missing not in TABLE_CHOICES["missing"]:
            raise ValueError("mode must be observations/values; missing must be reject/omit.")
        if self.width not in TABLE_CHOICES["width"] or type(self.max_mb) is not int or self.max_mb < 1:
            raise ValueError("width must be column/wide; max_mb must be a positive integer.")
        if type(self.max_figures) is not int or self.max_figures < 1:
            raise ValueError("max_figures must be a positive physical output limit.")
        if self.plot not in TABLE_PLOT_DESCRIPTIONS:
            raise ValueError("plot must be " + "/".join(TABLE_PLOT_DESCRIPTIONS) + ".")
        if self.plot == "box" and self.mode != "observations":
            raise ValueError("Box plots require row-level observations; quartiles cannot be inferred from supplied summaries.")
        if not isinstance(self.association, str) or self.association not in TABLE_CHOICES["association"] or (self.association != "none" and self.plot not in {"line", "scatter"}):
            raise ValueError("association must be none/pearson; Pearson requires explicit line/scatter x and y coordinates, not mean bars or paired differences.")
        if self.series_layout not in TABLE_CHOICES["series_layout"]:
            raise ValueError("series_layout must be separate/shared.")
        if self.series_layout == "shared" and self.plot not in {"line", "scatter"}:
            raise ValueError("Shared coordinate axes require line/scatter; bar groups retain their own layout.")
        if not isinstance(self.x_column, str) or not isinstance(self.x_unit, str):
            raise ValueError("x_column and x_unit must be strings.")
        if type(self.max_points) is not int or self.max_points < 1:
            raise ValueError("max_points must be a positive physical SVG point limit.")
        if self.plot in {"line", "scatter"}:
            if self.mode != "values" or not self.x_column.strip():
                raise ValueError("line/scatter require mode=values and an explicit numeric x_column; no aggregation is inferred.")
            if self.group_column == self.x_column:
                raise ValueError("The grouping column must differ from the numeric x column.")
            if self.x_column in self.value_columns:
                raise ValueError("x_column must differ from value_columns.")
        elif self.x_column or self.x_unit:
            raise ValueError("x_column/x_unit require a line or scatter plot.")
        if self.mode == "values" and self.plot == "bar" and not self.group_column:
            raise ValueError("Already aggregated values require a unique row-label group_column; no re-aggregation is inferred.")
        if self.plot == "heatmap" and (self.mode != "values" or not self.group_column):
            raise ValueError("Heatmaps require mode=values and a unique row-label group_column; choose columns of a common quantity/unit. No aggregation or normalization is inferred.")
        if not isinstance(self.paired_baseline, str):
            raise ValueError("paired_baseline must be a column name.")
        if self.paired_baseline and (self.mode != "observations" or self.plot not in {"bar", "box"}
                or self.paired_baseline not in self.value_columns or len(self.value_columns) < 2):
            raise ValueError("Paired comparison requires row-level observations, bar/box plots and at least two selected columns including the explicit baseline.")

    @classmethod
    def defaults(cls) -> dict:
        """Expose optional defaults to adapters without constructing a task."""
        return {field.name: field.default for field in fields(cls) if field.default is not MISSING}

    @classmethod
    def from_config(cls, config):
        values = config.get("value_columns", [])
        if not isinstance(values, (list, tuple)):
            raise ValueError("value_columns must be a list of explicit column names.")
        options = {field.name: config[field.name] for field in fields(cls)
                   if field.name in config and field.name not in {"value_columns", "observation_unit"}}
        return cls(value_columns=tuple(values), observation_unit=config.get("observation_unit", ""), **options)

    def to_config(self) -> dict:
        """Keep old default packages unchanged; attribution is optional metadata."""
        result = asdict(self)
        if not self.attribution:
            result.pop("attribution")
        if self.association == "none":
            result.pop("association")
        return result


def parse_table(text: str, suffix: str) -> list[dict]:
    """Read one homogeneous records array or a rectangular UTF-8 CSV/TSV."""
    if suffix == ".json":
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError(f"Duplicate JSON key: {key!r}")
                result[key] = value
            return result
        rows = json.loads(text, object_pairs_hook=pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"Nonfinite JSON number: {value}")))
        if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
            raise ValueError("JSON input must be a nonempty array of homogeneous records, not a nested object.")
        keys = set(rows[0])
        if not keys or any(set(row) != keys for row in rows):
            raise ValueError("JSON records must have the same nonempty column set.")
        if any(isinstance(value, (dict, list)) for row in rows for value in row.values()):
            raise ValueError("Nested JSON cells are not supported.")
        return rows
    if suffix not in {".csv", ".tsv"}:
        raise ValueError("Data input must be UTF-8 CSV, TSV or a JSON records array.")
    reader = csv.reader(io.StringIO(text), delimiter="\t" if suffix == ".tsv" else ",", strict=True)
    header = next(reader, [])
    if not header or any(not name.strip() for name in header) or len(set(header)) != len(header):
        raise ValueError("Table header must contain distinct nonempty column names.")
    rows = []
    for index, row in enumerate(reader, start=2):
        if not row:
            continue
        if len(row) != len(header):
            raise ValueError(f"Row {index} has {len(row)} cells; expected {len(header)}.")
        rows.append(dict(zip(header, row)))
    if not rows:
        raise ValueError("Data table has no observations.")
    return rows


def describe_table(rows: list[dict], spec: TableSpec) -> dict:
    validate_table_columns(rows, spec)
    if spec.plot in {"line", "scatter"}:
        return _coordinate_values(rows, spec)
    groups = {}
    for index, row in enumerate(rows, start=1):
        raw_group = row.get(spec.group_column) if spec.group_column else "All observations"
        if raw_group is None or str(raw_group).strip() == "":
            raise ValueError(f"Missing grouping value at data row {index}.")
        group = str(raw_group)
        if spec.mode == "values" and group in groups:
            raise ValueError("Already aggregated values require unique labels; refusing to average repeated summaries.")
        bucket = groups.setdefault(group, {name: [] for name in spec.value_columns})
        for name in spec.value_columns:
            value = row[name]
            if value is None or (isinstance(value, str) and not value.strip()):
                if spec.missing == "reject":
                    raise ValueError(f"Missing {name!r} at data row {index}; choose omit explicitly, not zero imputation.")
                bucket[name].append(None)
                continue
            bucket[name].append(_number(value, name, index))
    records = []
    for group, columns in groups.items():
        for name, cells in columns.items():
            values = [v for v in cells if v is not None]
            if not values and spec.plot != "heatmap":
                raise ValueError(f"No numeric values remain for {name!r}, group {group!r}.")
            record = {"group": group, "column": name, "count": len(values), "missing": len(cells) - len(values)}
            if spec.mode == "observations":
                mean = statistics.mean(values)
                std = statistics.stdev(values) if len(values) > 1 else None
                if not math.isfinite(mean) or (std is not None and not math.isfinite(std)):
                    raise ValueError("Computed statistic exceeds finite numeric range.")
                record.update(mean=mean, sample_std=std, min=min(values), max=max(values))
            else:
                record["value"] = values[0] if values else None
            records.append(record)
    result = {"schema_version": "table_analysis.v1", "status": "completed", "evidence_role": "computed_from_user_supplied_data",
            "row_count": len(rows), "spec": spec.to_config(), "records": records,
            "limitations": ["Input collection and column semantics are user-declared, not independently verified.",
                            "Descriptive only: no significance, causal, paired-comparison or replication claim.",
                            "No uncertainty bars are inferred; sample standard deviation describes rows, not confidence in a method."]}
    if spec.mode == "observations":
        result["observation_summaries"] = [
            {"group": group, "column": name, "count": len(cells) - cells.count(None),
             "missing": cells.count(None), "value": _distribution([value for value in cells if value is not None])}
            for group, columns in groups.items() for name, cells in columns.items()
        ]
    if spec.plot == "heatmap":
        result["limitations"].append("Heatmap columns share one user-declared quantity/unit and a global raw-value color scale; no normalization, clustering, correlation or improvement ranking is inferred. Missing cells are retained, not zero-imputed.")
    if spec.paired_baseline:
        result["paired_comparisons"] = _paired_comparisons(groups, spec)
        result["limitations"] = [
            "Input collection, same-row pairing and common column units are user-declared, not independently verified.",
            "Differences are candidate minus baseline; a positive difference does not automatically mean improvement.",
            "Missing pairs are jointly omitted, never formed by subtracting separately filtered column means.",
            "Sample standard deviation describes paired differences. Standard error assumes independent pairs; it is not a confidence interval or a significance test.",
            "Observed differences do not establish causality or generalize beyond these data.",
        ]
    return result


def _paired_comparisons(groups: dict, spec: TableSpec) -> list[dict]:
    """Compute aligned differences; absent pairs never become zero observations."""
    output = []
    for group, columns in groups.items():
        baseline = columns[spec.paired_baseline]
        for column, cells in columns.items():
            if column == spec.paired_baseline:
                continue
            differences = [candidate - reference for reference, candidate in zip(baseline, cells)
                           if reference is not None and candidate is not None]
            if any(not math.isfinite(value) for value in differences):
                raise ValueError("Paired difference exceeds finite numeric range.")
            count = len(differences)
            mean = statistics.mean(differences) if count else None
            std = statistics.stdev(differences) if count > 1 else None
            se = std / math.sqrt(count) if std is not None else None
            if any(value is not None and not math.isfinite(value) for value in (mean, std, se)):
                raise ValueError("Paired statistic exceeds finite numeric range.")
            output.append({"group": group, "baseline_column": spec.paired_baseline, "candidate_column": column,
                "count": count, "missing_pairs": len(cells) - count, "differences": differences,
                "mean_difference": mean, "sample_std_difference": std, "standard_error": se,
                "interpretation": "candidate minus baseline; SE conditional on independent pairs; no significance test"})
    return output


def _number(value, name: str, index: int) -> float:
    if isinstance(value, bool):
        raise ValueError(f"Boolean is not a numeric observation: {name!r}, row {index}.")
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"Nonnumeric {name!r} at data row {index}.") from exc
    if not math.isfinite(number):
        raise ValueError(f"Nonfinite {name!r} at data row {index}.")
    return number


def _coordinate_values(rows: list[dict], spec: TableSpec) -> dict:
    """Preserve row identities, groups and missing coordinates without aggregation."""
    if len(rows) > spec.max_points:
        raise ValueError(f"Data has {len(rows)} rows; exceeds physical max_points={spec.max_points}. Explicitly increase the limit or supply a smaller table; no points were sampled.")
    records, seen = [], set()
    valid_counts = dict.fromkeys(spec.value_columns, 0)
    for index, row in enumerate(rows, start=1):
        category = ""
        if spec.group_column:
            raw_group = row[spec.group_column]
            if raw_group is None or isinstance(raw_group, (list, dict, bool)) or not str(raw_group).strip():
                raise ValueError(f"Missing or invalid group at data row {index}.")
            category = str(raw_group).strip()
        raw_x = row[spec.x_column]
        missing_x = raw_x is None or (isinstance(raw_x, str) and not raw_x.strip())
        if missing_x and (spec.missing == "reject" or spec.plot == "line"):
            raise ValueError(f"Missing x coordinate at data row {index}; line order cannot be inferred. Scatter permits explicit omit of missing coordinates.")
        x = None if missing_x else _number(raw_x, spec.x_column, index)
        if spec.plot == "line" and (category, x) in seen:
            raise ValueError("Line plots require unique numeric x coordinates within each group; no replicate aggregation is inferred. Use scatter or supply an explicit summary.")
        seen.add((category, x))
        for column in spec.value_columns:
            raw = row[column]
            missing = raw is None or (isinstance(raw, str) and not raw.strip())
            if missing and spec.missing == "reject":
                raise ValueError(f"Missing {column!r} at data row {index}; choose omit explicitly, not zero imputation.")
            unavailable = missing or missing_x
            records.append({"group": f"Row {index}", "column": column, "x": x,
                            "value": None if missing else _number(raw, column, index),
                            **({"series_group": category} if spec.group_column else {}),
                            "count": 0 if unavailable else 1, "missing": int(unavailable)})
            valid_counts[column] += not unavailable
    if any(count == 0 for count in valid_counts.values()):
        raise ValueError("No numeric y values remain for at least one selected column.")
    return {"schema_version": "table_analysis.v1", "status": "completed",
            "evidence_role": "computed_from_user_supplied_data", "row_count": len(rows),
            "spec": spec.to_config(), "records": records,
            "coordinate_summaries": coordinate_summaries(records, association=spec.association),
            "limitations": ["Input collection, coordinate semantics, groups and units are user-declared, not independently verified.",
                            "Supplied coordinates only: no aggregation, smoothing, fitted trend, significance or uncertainty is inferred.",
                            "Line points are ordered by numeric x within each group; missing y points remain explicit gaps. Scatter rows with missing coordinates are retained but not plotted."]}


def _distribution(values: list[float]) -> dict | None:
    """Empirical five-number summary; linear interpolation, not uncertainty."""
    if not values:
        return None
    ordered = sorted(values)
    def quantile(p: float) -> float:
        position = (len(ordered) - 1) * p
        lower, upper = math.floor(position), math.ceil(position)
        fraction = position - lower
        if lower == upper or ordered[lower] == ordered[upper]:
            return ordered[lower]
        return ordered[lower] * (1 - fraction) + ordered[upper] * fraction
    return {'min': ordered[0], 'q1': quantile(.25), 'median': quantile(.5),
            'q3': quantile(.75), 'max': ordered[-1]}


def coordinate_summaries(records: list[dict], *, association: str = "none") -> list[dict]:
    """Exact descriptive axis summaries and opt-in pair association, not inference.

    Keep all raw coordinates. Quantiles use linear interpolation at (n-1)*p,
    with no fit, error bar or population claim. Separate axis distributions do
    not prove a joint relationship, overlap shape or inspected-image content.
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in records:
        groups.setdefault((row['column'], row.get('series_group', '')), []).append(row)
    if association not in {"none", "pearson"}:
        raise ValueError("Unknown descriptive association method.")
    summaries = []
    for (column, group), rows in groups.items():
        present = [row for row in rows if row['x'] is not None and row['value'] is not None]
        summary = {'column': column, 'series_group': group, 'retained_rows': len(rows),
            'plotted_rows': len(present), 'missing_coordinate_rows': len(rows) - len(present),
            'missing_x_rows': sum(row['x'] is None for row in rows),
            'missing_value_rows': sum(row['value'] is None for row in rows),
            'missing_both_rows': sum(row['x'] is None and row['value'] is None for row in rows),
            'x': _distribution([row['x'] for row in present]), 'value': _distribution([row['value'] for row in present])}
        if association == "pearson":
            summary['association'] = _pearson_association(present)
        summaries.append(summary)
    return summaries


def _pearson_association(pairs: list[dict]) -> dict:
    """Descriptive complete-pair Pearson r, not a difference or hypothesis test."""
    result = {"method": "pearson", "coefficient": None, "status": "insufficient_complete_pairs"}
    if len(pairs) < 2:
        return result
    x, y = [row['x'] for row in pairs], [row['value'] for row in pairs]
    if min(x) == max(x) or min(y) == max(y):
        return {**result, "status": "constant_coordinate"}
    # Center before scaling: direct division of large-offset, narrow-spread
    # coordinates can distort their differences. A half-sum midpoint avoids
    # overflowing max-min; positive rescaling keeps squared magnitudes bounded.
    # This only conditions computation, never saved/plotted data.
    def conditioned(values):
        center = min(values) * .5 + max(values) * .5
        shifted = [value - center for value in values]
        scale = max(map(abs, shifted))
        return [value / scale for value in shifted]
    try:
        coefficient = statistics.correlation(conditioned(x), conditioned(y))
    except statistics.StatisticsError:
        return {**result, "status": "numerically_unresolved"}
    if not math.isfinite(coefficient):
        return {**result, "status": "numerically_unresolved"}
    return {**result, "status": "computed", "coefficient": max(-1.0, min(1.0, coefficient))}


def read_table_source(path: Path, *, max_mb: int) -> tuple[bytes, list[dict]]:
    """One bounded decoding/parser owner for setup and actual ingestion."""
    if type(max_mb) is not int or max_mb < 1:
        raise ValueError("max_mb must be a positive integer.")
    if path.suffix.lower() not in {".csv", ".tsv", ".json"} or not path.is_file():
        raise ValueError("Provide an existing UTF-8 CSV/TSV or JSON records file.")
    with path.open("rb") as handle:
        raw = handle.read(max_mb * 1024 * 1024 + 1)
    if len(raw) > max_mb * 1024 * 1024:
        raise ValueError(f"Data exceeds the configured {max_mb} MiB input limit.")
    try:
        return raw, parse_table(raw.decode("utf-8-sig"), path.suffix.lower())
    except UnicodeDecodeError as exc:
        raise ValueError("Data must be UTF-8 text; export a CSV/TSV or JSON records file in UTF-8.") from exc
    except csv.Error as exc:
        raise ValueError(f"Invalid delimited table: {exc}") from exc


class TableSourceVersionError(ValueError):
    """A table profile cannot attest a different source version."""


def preview_table_source(path: Path, *, max_mb: int, expected_sha256: str | None = None) -> dict:
    """Shared bounded shape/examples for dialogue and generated analysis."""
    raw, rows = read_table_source(path, max_mb=max_mb)
    if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise TableSourceVersionError('Table bytes differ from the bound inspection source')
    columns = list(rows[0])
    shown = columns[:40]
    return {**({'source_sha256': expected_sha256} if expected_sha256 is not None else {}),
        "row_count": len(rows), "columns": [name[:200] for name in shown],
        "column_summary": [{"column": name[:200], "column_name_truncated": len(name) > 200,
            "missing": sum(row[name] is None or (isinstance(row[name], str) and not row[name].strip()) for row in rows),
            "distinct_nonmissing": len({json.dumps(row[name], sort_keys=True, ensure_ascii=False)
                for row in rows if row[name] is not None and not (isinstance(row[name], str) and not row[name].strip())})}
            for name in shown],
        "columns_omitted": max(0, len(columns) - len(shown)),
        "column_names_truncated": any(len(name) > 200 for name in shown),
        "example_rows": [{name[:200]: str(row[name])[:200] for name in shown} for row in rows[:5]],
        "cell_values_truncated": any(len(str(row[name])) > 200 for row in rows[:5] for name in shown),
        "status": "counts cover the complete bounded table; distinct counts preserve raw typed values, not unique participants; examples and names are bounded, truncated names are not selectable; meanings, independence, units and pairing are not verified"}


def validate_table_columns(rows: list[dict], spec: TableSpec) -> None:
    """Report exact available names, without guessing column meaning or types."""
    if not rows:
        raise ValueError("Data table has no observations.")
    required = [*spec.value_columns, *([spec.group_column] if spec.group_column else []),
                *([spec.x_column] if spec.x_column else [])]
    available = list(rows[0])
    unknown = set(required) - set(available)
    if unknown:
        shown = available[:20]
        omitted = len(available) - len(shown)
        raise ValueError(f"Selected columns not found: {sorted(unknown)!r}. Available columns: {shown!r}"
                         + (f" ({omitted} more)" if omitted else "")
                         + ". Choose explicit column names; no field is inferred.")


def snapshot_table_capability(context: CapabilityContext, request: dict) -> CapabilityResult:
    path = Path(request.get("file", "")).expanduser().resolve()
    spec = TableSpec.from_config(request)
    raw, rows = read_table_source(path, max_mb=spec.max_mb)
    # Validate before committing a usable snapshot; settings are frozen with it.
    describe_table(rows, spec)
    source = context.store.ref("input" + path.suffix.lower(), kind="user_data")
    context.store.resolve(source).write_bytes(raw)
    ref = context.store.write_json("table_input.json", {"schema_version": "table_input.v1", "source_name": path.name,
        "source": source.to_dict(), "spec": spec.to_config(), "input_bytes": len(raw)}, kind="table_input", schema="table_input.v1")
    return CapabilityResult("completed", (ref, source))


def analyze_table_capability(context: CapabilityContext, request: ArtifactRef) -> CapabilityResult:
    payload = json.loads(context.require_input(request).read_text(encoding="utf-8"))
    if payload.get("schema_version") != "table_input.v1":
        raise ValueError("Expected a table_input.v1 snapshot.")
    spec = TableSpec.from_config(payload["spec"])
    # The source is a sibling of this registered snapshot, never the original locator.
    source_ref = ArtifactRef.from_dict(payload["source"])
    source = context.require_input(request).parent / source_ref.path
    raw = source.read_bytes()
    text = raw.decode("utf-8-sig")
    result = describe_table(parse_table(text, source.suffix.lower()), spec)
    copied = context.store.ref(source.name, kind="user_data")
    context.store.resolve(copied).write_bytes(raw)
    result["source"] = copied.to_dict()
    result["source_name"] = payload["source_name"]
    ref, report, *figures = _write_analysis_delivery(result, context.store)
    return CapabilityResult("completed", (ref, copied, report, *figures))


def table_input_handling(result: dict) -> dict:
    """Rebuild observed input use separately from configured missingness policy.

    Counts use all recorded values, not a preview. Coordinate counts describe
    x/y pairs; other counts describe each selected column. These are not unique
    participants or a claim that source collection was verified.
    """
    coordinate = result["spec"].get("plot") in {"line", "scatter"}
    counts = {}
    for row in result["records"]:
        if not all(key in row for key in ("column", "count", "missing")):
            return {"observed_use": "unavailable_in_this_snapshot"}
        column = counts.setdefault(row["column"], {"column": row["column"], "used": 0, "missing": 0})
        column["used"] += row["count"]
        column["missing"] += row["missing"]
    policy = result["spec"].get("missing", "reject")
    return {"configured_missing_policy": policy,
            "policy_behavior": ("Missing selected values stop analysis before computing; this policy does not delete rows."
                                if policy == "reject" else
                                "Missing values are excluded only from affected column summaries or coordinate pairs; input rows are retained, not globally deleted or zero-imputed."),
            "input_rows": result.get("row_count"),
            "count_unit": "complete x/y pairs per column" if coordinate else "values per column",
            "observed_use": list(counts.values())}


def _input_handling_markdown(result: dict) -> str:
    view = table_input_handling(result)
    if not isinstance(view["observed_use"], list):
        return "Observed input use is unavailable in this snapshot."
    return (f"Configured missing policy: {view['configured_missing_policy']}. {view['policy_behavior']} "
            f"Observed use ({view['count_unit']}): " + "; ".join(
                f"{row['column']}: {row['used']} used, {row['missing']} missing" for row in view['observed_use']) + ".")


def table_markdown(result: dict) -> str:
    """Describe computed values without changing their evidence role."""
    spec = TableSpec.from_config(result["spec"])
    lines = ["# Descriptive data analysis", "", f"Input: `{result['source_name']}`; rows: {result['row_count']}.",
             f"Observation unit (user-declared): {spec.observation_unit}. Value unit: {spec.value_unit or 'not supplied'}.",
             f"Mode: {spec.mode}. {_input_handling_markdown(result)}", ""]
    if attribution := data_attribution_markdown(result):
        lines.extend([attribution, ""])
    for row in result["records"]:
        if spec.plot in {"line", "scatter"}:
            x = f"{row['x']:.12g}" if row['x'] is not None else "missing"
            metric = f"x={x}, value={row['value'] if row['value'] is not None else 'missing'}"
            if spec.group_column:
                metric += f"; {spec.group_column}={row['series_group']!r}"
        else:
            metric = (f"mean={row['mean']:.12g}, sample_std={row['sample_std']}" if spec.mode == "observations"
                      else f"value={row['value']:.12g}" if row['value'] is not None else "value=missing")
        lines.append(f"- {row['group']!r} / {row['column']!r}: {metric}; n={row['count']}; missing={row['missing']}.")
    if result.get("paired_comparisons"):
        lines.extend(["", "## Paired comparisons", "", _paired_values_markdown(result)])
    if result.get("observation_summaries"):
        lines.extend(["", "## Observed distributions", "", _observation_values_markdown(result)])
    if result['spec'].get('association', 'none') != 'none':
        lines.extend(["", "## Descriptive coordinate association", "", _association_values_markdown(result)])
    lines.extend(["", *[f"![Descriptive values]({item['path']})\n\n{item['caption']}\n"
                       + " · ".join(f"[{kind.upper()}]({path})" for kind, path in item.get("exports", {}).items())
                       for item in result["figures"]],
                  "## Limits", "", *result["limitations"], "", "Rebuild from this directory (no model/API calls):", "", "```sh",
                  "python -m simple_ar.result_analysis.table analysis.json", "```", "",
                  "The copied input can contain sensitive data; review before sharing this directory."])
    return "\n".join(lines) + "\n"


def data_attribution_markdown(result: dict) -> str:
    """Display a supplied declaration literally, not as verified publication data."""
    from html import escape
    text = result.get("spec", {}).get("attribution", "")
    if not text:
        return ""
    # Metadata must not create headings, links or remote images in a report.
    # The original declaration remains in analysis.json.
    text = escape(text, quote=False).replace("\n", " ").replace("\r", " ")
    text = text.translate({ord(char): f"&#{ord(char)};" for char in "\\`*_{}[]()#+.!|@"})
    return f"Data attribution (user-declared, not independently verified): {text}."


def load_analysis_package(path: Path, *, max_mb: int | None = 20) -> tuple[dict, bytes, str]:
    """Recheck a v1 result against its package-local input, not its prose/SVG.

    Imported packages are external input, not trusted just because they name
    our schema. Limit reads and reject paths/symlinks outside the selected pack.
    Never execute a supplied script or follow its figure/resource links.
    """
    path = path.resolve()
    limit = max_mb * 1024 * 1024 if max_mb is not None else None
    with path.open("rb") as handle:
        raw_result = handle.read(limit + 1) if limit is not None else handle.read()
    if limit is not None and len(raw_result) > limit:
        raise ValueError(f"Analysis package result exceeds the {max_mb} MiB writing import limit.")
    payload = json.loads(raw_result.decode("utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("schema_version") != "table_analysis.v1" or payload.get("status") != "completed":
        raise ValueError("Writing JSON material must be a completed table_analysis.v1 package with its copied input.")
    if not isinstance(payload.get("spec"), dict) or not isinstance(payload.get("source"), dict):
        raise ValueError("Analysis package must include its column settings and copied input reference.")
    if not isinstance(payload["source"].get("path"), str) or not payload["source"]["path"]:
        raise ValueError("Analysis package must name its copied input with a relative path.")
    spec = TableSpec.from_config(payload["spec"])
    source_ref = ArtifactRef.from_dict(payload["source"])
    source = (path.parent / source_ref.path).resolve()
    if not source.is_relative_to(path.parent) or source == path:
        raise ValueError("Analysis input must stay inside the selected package directory.")
    input_limit = spec.max_mb * 1024 * 1024
    if limit is not None:
        input_limit = min(limit, input_limit)
    with source.open("rb") as handle:
        raw = handle.read(input_limit + 1)
    if len(raw) > input_limit:
        raise ValueError("Analysis input exceeds the configured size limit.")
    result = describe_table(parse_table(raw.decode("utf-8-sig"), source.suffix.lower()), spec)
    if result["records"] != payload.get("records") or result["row_count"] != payload.get("row_count"):
        raise ValueError("Copied data no longer matches the saved analysis; refusing stale or altered results.")
    if result.get("paired_comparisons") != payload.get("paired_comparisons"):
        raise ValueError("Copied paired data no longer matches the saved comparisons.")
    if "observation_summaries" in payload and result.get("observation_summaries") != payload["observation_summaries"]:
        raise ValueError("Copied observations no longer match the saved distributions.")
    if "coordinate_summaries" in payload:
        saved = payload["coordinate_summaries"]
        computed = result.get("coordinate_summaries", [])
        required = {'column', 'series_group', 'retained_rows', 'plotted_rows', 'missing_coordinate_rows', 'x', 'value'}
        # Older snapshots need not contain later-added missingness counts.
        # Check every recorded statistic; return complete recomputed data.
        if (not isinstance(saved, list) or len(saved) != len(computed)
                or any(not isinstance(old, dict) or not required.issubset(old)
                       or any(key not in new or value != new[key] for key, value in old.items())
                       for old, new in zip(saved, computed))):
            raise ValueError("Copied coordinates no longer match the saved axis summaries.")
    result["source_name"] = Path(str(payload.get("source_name") or source.name)).name
    result["source"] = source_ref.to_dict()
    return result, raw, source.suffix.lower()


def copy_analysis_package(path: Path, output_dir: Path) -> dict:
    """Freeze rechecked data and rebuild native figures for a new consumer."""
    result, raw, suffix = load_analysis_package(path)
    store = ArtifactStore(output_dir)
    source = store.ref("input" + suffix, kind="user_data")
    output_dir.mkdir(parents=True, exist_ok=True)
    store.resolve(source).write_bytes(raw)
    result["source"] = source.to_dict()
    _write_analysis_delivery(result, store)
    return result


def _write_analysis_delivery(result: dict, store: ArtifactStore, *, name: str = "analysis.json") -> tuple[ArtifactRef, ...]:
    """One output owner for analysis, writing imports and explicit rebuilding."""
    from simple_ar.result_analysis.figures import render_table_figures
    result["figures"] = render_table_figures(result, store.root)
    ref = store.write_json(name, result, kind="table_analysis", schema="table_analysis.v1")
    report = store.write_text("analysis.md", table_markdown(result), kind="table_report")
    return (ref, report, *[store.ref(item["path"], kind="figure") for item in result["figures"]])


def _table_cell(value: object) -> str:
    from html import escape
    return escape(str(value)).replace("|", "&#124;").replace("\n", " ").replace("\r", " ")


def table_values_markdown(result: dict) -> str:
    """Deterministic numerical evidence for a report, without inferred tests."""
    cell = _table_cell
    fields = ("mean", "sample_std", "min", "max") if result["spec"]["mode"] == "observations" else ("value",)
    if result["spec"].get("plot", "bar") in {"line", "scatter"}:
        fields = ("x", *fields)
    series_column = bool(result["spec"].get("group_column") and result["spec"].get("plot", "bar") in {"line", "scatter"})
    lines = ["| Group | " + ("Series | " if series_column else "") + "Column | Count | Missing | " + " | ".join(fields) + " |",
             "| --- | " + ("--- | " if series_column else "") + "--- | ---: | ---: | " + " | ".join("---:" for _ in fields) + " |"]
    for row in result["records"]:
        numbers = ["not defined" if row[field] is None else f"{row[field]:.12g}" for field in fields]
        lines.append("| " + " | ".join([cell(row["group"]), *([cell(row["series_group"])] if series_column else []), cell(row["column"]), str(row["count"]), str(row["missing"]), *numbers]) + " |")
    spec = result["spec"]
    return ("\n".join([f"Row unit (user-declared): {cell(spec['observation_unit'])}. Value unit: {cell(spec['value_unit'] or 'not supplied')}.",
                      f"Mode: {spec['mode']}. {_input_handling_markdown(result)}", "", *lines])
        + ("\n\n" + _paired_values_markdown(result) if result.get("paired_comparisons") else "")
        + ("\n\n" + _observation_values_markdown(result) if result.get("observation_summaries") else "")
        + ("\n\n" + _association_values_markdown(result) if result['spec'].get('association', 'none') != 'none' else ""))


def _association_values_markdown(result: dict) -> str:
    cell = _table_cell
    lines = ["Pearson r describes linear association of the jointly present supplied x/y coordinates, "
        "separately for each declared group and value column; groups are not pooled. This is not a paired mean difference, "
        "significance test, confidence interval, causal effect or population inference. "
        "With two nonconstant pairs r is necessarily ±1; inspect counts, missingness and the actual points. "
        "A zero or small r does not exclude a nonlinear relationship. Coordinates and figures are unchanged.",
        "", "| Series | X column | Y column | Complete pairs | Missing-coordinate rows | Pearson r | Status |",
        "| --- | --- | --- | ---: | ---: | ---: | --- |"]
    for row in result['coordinate_summaries']:
        association = row.get('association')
        if not association:
            continue
        value = association['coefficient']
        lines.append("| " + " | ".join([cell(row['series_group'] or 'All supplied coordinates'),
            cell(result['spec']['x_column']), cell(row['column']), str(row['plotted_rows']),
            str(row['missing_coordinate_rows']), 'not defined' if value is None else f"{value:.12g}",
            cell(association['status'])]) + " |")
    return "\n".join(lines)


def _observation_values_markdown(result: dict) -> str:
    cell = _table_cell
    lines = ["Per-column empirical distributions of all nonmissing observations in each group. "
        "Quartiles use linear interpolation at (n−1)p; they describe rows, not confidence intervals. "
        "These are marginal distributions, not paired differences or joint relationships. "
        + ("Box plots show Q1–Q3, median and empirical min–max whiskers; no outlier classification."
           if result['spec'].get('plot') == 'box' else "Figures still display their labeled means."),
        "", "| Group | Column | Count | Missing | Min | Q1 | Median | Q3 | Max |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in result["observation_summaries"]:
        numbers = [f"{row['value'][key]:.12g}" for key in ("min", "q1", "median", "q3", "max")]
        lines.append("| " + " | ".join([cell(row["group"]), cell(row["column"]), str(row["count"]), str(row["missing"]), *numbers]) + " |")
    return "\n".join(lines)


def _paired_values_markdown(result: dict) -> str:
    cell = _table_cell
    lines = ["Differences are candidate minus baseline, using matched nonmissing rows.",
        "SE assumes independent pairs; no confidence interval, significance or improvement verdict is inferred.", "",
        "| Group | Candidate | Baseline | Pairs | Missing pairs | Mean difference | Sample std | SE |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for row in result["paired_comparisons"]:
        numbers = ["not defined" if row[key] is None else f"{row[key]:.12g}"
                   for key in ("mean_difference", "sample_std_difference", "standard_error")]
        lines.append("| " + " | ".join([cell(row["group"]), cell(row["candidate_column"]), cell(row["baseline_column"]),
            str(row["count"]), str(row["missing_pairs"]), *numbers]) + " |")
    return "\n".join(lines)


def rebuild(path: Path) -> None:
    # Rebuilding one's own package keeps its explicitly configured input limit.
    # The narrower external-writing limit must not restrict this expert entry.
    result, _, _ = load_analysis_package(path, max_mb=None)
    # Explicit rebuilding refreshes the generated delivery, including its
    # captions/encoding. Normal completed-session recovery never calls this.
    _write_analysis_delivery(result, ArtifactStore(path.parent), name=path.name)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Rebuild figures from a saved descriptive analysis and its copied data.")
    parser.add_argument("result", type=Path)
    rebuild(parser.parse_args().result.resolve())
