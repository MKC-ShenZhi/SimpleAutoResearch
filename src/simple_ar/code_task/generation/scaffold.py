from __future__ import annotations

import json
from typing import Any, Mapping


def fallback_file_content(path: str, result_schema: Mapping[str, Any], contract: Mapping[str, Any]) -> str:
    metrics = required_metrics(result_schema)
    if path == "main.py":
        return (
            "from __future__ import annotations\n\n"
            "from generated_experiment.runner import run_experiment\n\n\n"
            "def main() -> None:\n"
            "    metrics = run_experiment()\n"
            "    for name in sorted(metrics):\n"
            "        try:\n"
            "            value = float(metrics[name])\n"
            "        except (TypeError, ValueError):\n"
            "            continue\n"
            "        print(f\"{name}: {value:.6f}\")\n\n\n"
            "if __name__ == \"__main__\":\n"
            "    main()\n"
        )
    if path == "generated_experiment/__init__.py":
        return '"""Generated experiment package."""\n'
    if path == "config.json":
        return json.dumps(
            {
                "objective": str(contract.get("objective", "")),
                "contract_id": str(contract.get("contract_id", "")),
                "required_metrics": metrics,
            },
            indent=2,
            ensure_ascii=False,
        ) + "\n"
    if path == "generated_experiment/runner.py":
        return (
            "from __future__ import annotations\n\n"
            "import json\n"
            "from pathlib import Path\n\n\n"
            "def run_experiment() -> dict[str, float]:\n"
            "    config_path = Path(__file__).resolve().parents[1] / \"config.json\"\n"
            "    _config = json.loads(config_path.read_text(encoding=\"utf-8\"))\n"
            "    raise RuntimeError(\n"
            "        'Offline scaffold has no implemented measurement. Implement the requested '\n"
            "        'experiment and compute its metrics before running the benchmark.'\n"
            "    )\n"
        )
    if path.endswith(".py"):
        return "from __future__ import annotations\n\n# Reserved generated module.\n"
    return ""


def required_metrics(schema: Mapping[str, Any]) -> list[str]:
    value = schema.get("required_metrics")
    metrics = [str(item) for item in value if str(item).strip()] if isinstance(value, list) else []
    primary = str(schema.get("primary_metric") or "").strip()
    if primary and primary not in metrics:
        metrics.insert(0, primary)
    return metrics or ["score"]
