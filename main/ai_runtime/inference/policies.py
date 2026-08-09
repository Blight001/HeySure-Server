"""Pure inference-budget policies."""


def coerce_max_steps(value: object, default: int = 48) -> int:
    try:
        return max(1, min(999, int(value or default)))
    except Exception:
        return max(1, min(999, int(default)))


def has_active_todo_plan(plan_state: object) -> bool:
    return plan_state is not None


def can_start_inference_step(completed_steps: int, max_steps: int, plan_state: object) -> bool:
    return completed_steps < max_steps or has_active_todo_plan(plan_state)
