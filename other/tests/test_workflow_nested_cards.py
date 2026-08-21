from api.services.workflows.compiler import compile_definition


def _child_definition():
    return {
        "schemaVersion": 1,
        "inputSchema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        "startStepId": "wait",
        "steps": {
            "wait": {"type": "delay", "delaySeconds": 0, "next": "finish"},
            "finish": {"type": "end", "output": {"echo": "${input.value}"}},
        },
        "limits": {"timeoutSeconds": 30, "maxTransitions": 10},
        "output": {},
    }


def test_compile_expands_referenced_card_and_rewrites_input_output_templates():
    parent = {
        "schemaVersion": 1,
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        "startStepId": "child",
        "steps": {
            "child": {
                "type": "card",
                "title": "子流程",
                "cardRef": {"id": "wcard_child", "versionId": "wver_child", "name": "子卡片"},
                "_definition": _child_definition(),
                "input": {"value": "${input.name}"},
                "saveAs": "child_result",
                "next": "finish",
                "onError": "failed",
            },
            "failed": {"type": "end", "output": {"status": "failed"}},
            "finish": {"type": "end", "output": {"echo": "${steps.child_result.result.echo}"}},
        },
        "limits": {"timeoutSeconds": 60, "maxTransitions": 20},
        "output": {},
    }

    compiled = compile_definition(parent)["definition"]

    assert compiled["steps"]["child"]["type"] == "_card_enter"
    nested_return = next(step for step in compiled["steps"].values() if step.get("type") == "_card_return")
    assert nested_return["output"] == {
        "echo": "${steps.child_result.result.input.value}",
    }
    assert nested_return["next"] == "finish"
    assert compiled["steps"]["finish"]["output"]["echo"] == "${steps.child_result.result.echo}"
