import pytest

from api.services.workflows.compiler import WorkflowValidationError, compile_definition
from api.services.workflows.expression import TemplateResolutionError, render_template


def _definition():
    return {
        "schemaVersion": 1,
        "name": "read two files",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "startStepId": "read_first",
        "limits": {"timeoutSeconds": 60, "maxTransitions": 5},
        "steps": {
            "read_first": {
                "type": "mcp",
                "toolRef": {"namespace": "device", "name": "fs.read", "schemaDigest": "sha256:test"},
                "arguments": {"path": "${input.path}"},
                "saveAs": "first",
                "next": "finish",
            },
            "finish": {"type": "end"},
        },
        "output": {"content": "${steps.first.result.content}"},
    }


def test_compile_normalizes_and_hashes_valid_phase_one_definition():
    compiled = compile_definition(_definition())
    assert compiled["definition"]["limits"]["timeoutSeconds"] == 60
    assert compiled["digest"].startswith("sha256:")
    assert compiled["warnings"] == []


def test_compile_rejects_cycles_and_unsupported_steps():
    definition = _definition()
    definition["steps"]["finish"] = {
        "type": "condition",
        "expression": {"op": "eq", "left": 1, "right": 1},
        "onTrue": "read_first",
        "onFalse": "read_first",
    }
    with pytest.raises(WorkflowValidationError) as raised:
        compile_definition(definition)
    assert any("supports only mcp and end" in item for item in raised.value.errors)


def test_compile_rejects_unsafe_template_namespace():
    definition = _definition()
    definition["steps"]["read_first"]["arguments"]["path"] = "${environment.PATH}"
    with pytest.raises(WorkflowValidationError) as raised:
        compile_definition(definition)
    assert any("unknown namespace" in item for item in raised.value.errors)


def test_template_full_reference_preserves_type_and_interpolation_is_string():
    context = {
        "input": {"count": 3, "path": "a.txt"},
        "steps": {},
        "run": {},
        "device": {},
    }
    rendered = render_template(
        {"count": "${input.count}", "label": "file=${input.path}"},
        context,
    )
    assert rendered == {"count": 3, "label": "file=a.txt"}


def test_template_blocks_dunder_and_missing_values():
    context = {"input": {}, "steps": {}, "run": {}, "device": {}}
    with pytest.raises(TemplateResolutionError):
        render_template("${input.__class__}", context)
    with pytest.raises(TemplateResolutionError):
        render_template("${steps.not_finished.result}", context)
