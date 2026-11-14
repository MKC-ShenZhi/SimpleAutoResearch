"""Frozen report requirements shared by current and historical consumers."""
from __future__ import annotations

from typing import Any


def freeze_report_requirement(settings: dict[str, Any], *, method: str | None = None) -> dict[str, Any]:
    """Freeze resolved report demands and template bytes at task creation."""
    from simple_ar.report.writing import resolve_report_writing_config
    from simple_ar.report.schema import ReportRuntimeConfig, ReportContext
    from simple_ar.report.templates import load_report_template_bundle, is_builtin_template
    from simple_ar.report.memory import initialize_report_memory
    if method is None:
        method, settings = resolve_report_writing_config(settings)
    config = ReportRuntimeConfig.model_validate(settings)
    template = load_report_template_bundle(report_mode='supplied_materials', config=config)
    fixed = config.outline_strategy == 'template' or not is_builtin_template(template, config)
    memory = initialize_report_memory(context=ReportContext(topic='Implementation report',
        report_mode='supplied_materials'), template=template)
    return validate_report_requirement({'method': method, 'config': config.model_dump(mode='json'),
        'template': template.model_dump(mode='json'),
        'outline': [{'heading': row.heading, 'required': row.required}
            for row in sorted(memory.section_plan, key=lambda row: row.final_order)] if fixed else []})


def validate_report_requirement(value: dict[str, Any]) -> dict[str, Any]:
    from simple_ar.report.schema import ReportRuntimeConfig, ReportTemplateBundle
    from simple_ar.report.writing import validate_report_writing_config
    if set(value) != {'method', 'config', 'template', 'outline'}:
        raise ValueError('Unknown frozen report requirement')
    config = ReportRuntimeConfig.model_validate(value['config'])
    validate_report_writing_config(config, method=value['method'])
    ReportTemplateBundle.model_validate(value['template'])
    if any(set(row) != {'heading', 'required'} or type(row['required']) is not bool
           or not isinstance(row['heading'], str) or not row['heading'].strip() for row in value['outline']):
        raise ValueError('Invalid frozen report outline')
    return value


def working_report_requirement(value: dict[str, Any]) -> dict[str, Any]:
    """Expose the existing output requirements to the coordinator and author."""
    config = value['config']
    return {'outline': value['outline'],
        'target_words': config.get('longform', {}).get('target_words', 0),
        'max_cited_sources': config.get('max_cited_sources', 0),
        'scope': 'These apply to the final report; use the relevant parts for this work unit. '
                 'The fixed outline and citation ceiling remain in force; length is a preference.'}
