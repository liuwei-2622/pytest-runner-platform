from __future__ import annotations

import json
from threading import Lock

from .config import BASE_DIR
from .models import RunTemplate

RUN_TEMPLATES_PATH = BASE_DIR / "run_templates.json"
MAX_TEMPLATE_NAME_LENGTH = 80
_lock = Lock()


def _read_templates_file() -> list[RunTemplate]:
    if not RUN_TEMPLATES_PATH.exists():
        return []
    data = json.loads(RUN_TEMPLATES_PATH.read_text(encoding="utf-8"))
    return [RunTemplate.from_dict(item) for item in data.get("templates", [])]


def _save_templates(templates: list[RunTemplate]) -> None:
    payload = {"templates": [template.to_dict() for template in templates]}
    tmp_path = RUN_TEMPLATES_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(RUN_TEMPLATES_PATH)


def validate_template_name(name: str) -> str:
    value = name.strip()
    if not value:
        raise ValueError("模板名称不能为空")
    if len(value) > MAX_TEMPLATE_NAME_LENGTH:
        raise ValueError(f"模板名称不能超过 {MAX_TEMPLATE_NAME_LENGTH} 个字符")
    return value


def list_run_templates(project_id: str | None = None) -> list[RunTemplate]:
    templates = _read_templates_file()
    if project_id:
        templates = [template for template in templates if template.project_id == project_id]
    return sorted(templates, key=lambda template: template.name.lower())


def get_run_template(template_id: str) -> RunTemplate | None:
    for template in _read_templates_file():
        if template.id == template_id:
            return template
    return None


def save_run_template(template: RunTemplate) -> RunTemplate:
    template.name = validate_template_name(template.name)
    template.options.env_vars = {}
    with _lock:
        templates = _read_templates_file()
        templates = [item for item in templates if item.id != template.id]
        templates.append(template)
        _save_templates(templates)
    return template


def delete_run_template(template_id: str) -> bool:
    with _lock:
        templates = _read_templates_file()
        remaining = [template for template in templates if template.id != template_id]
        if len(remaining) == len(templates):
            return False
        _save_templates(remaining)
    return True
