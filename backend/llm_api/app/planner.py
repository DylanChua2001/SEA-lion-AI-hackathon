import json
from typing import List, Optional
from langchain_core.messages import AnyMessage, AIMessage, ToolMessage

# Treat these as single-turn tools (align with four-path plans)
_STEP_TOOLS = {"find", "click", "type", "wait"}
_MAX_STEPS = 6  # keep turns short & safe


def _normalize_args(name: Optional[str], args: dict) -> dict:
    """Discard malformed args so we don't emit unusable steps."""
    if not isinstance(args, dict):
        return {}
    if name == "find" and "query" not in args:
        return {}
    if name == "click" and "selector" not in args:
        return {}
    if name == "type" and not {"selector", "text"} <= args.keys():
        return {}
    return args


def messages_to_plan(msgs: List[AnyMessage]) -> List[dict]:
    steps: List[dict] = []
    last_step = None
    seen_click_selectors = set()

    for m in msgs:
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                name = tc.get("name")
                args = _normalize_args(name, tc.get("args", {}) or {})

                if name in _STEP_TOOLS and args:
                    step = {"tool": name, "args": args}

                    # Drop consecutive duplicates (prevents 8x identical clicks)
                    if last_step == step:
                        continue

                    # Also drop repeated clicks to the same selector within one plan
                    if name == "click":
                        sel = args.get("selector")
                        if sel in seen_click_selectors:
                            continue
                        seen_click_selectors.add(sel)

                    steps.append(step)
                    last_step = step

                    # Cap plan size (no navigation in minimal four-path flow)
                    if len(steps) >= _MAX_STEPS:
                        return steps

                elif name == "done":
                    steps.append({"tool": name, "args": args})
                    return steps

    return steps


def done_reason(msgs: List[AnyMessage]) -> Optional[str]:
    for m in msgs[::-1]:
        if isinstance(m, ToolMessage) and getattr(m, "name", "") == "done":
            try:
                payload = json.loads(m.content or "{}")
                return payload.get("reason")
            except Exception:
                return None
    return None
