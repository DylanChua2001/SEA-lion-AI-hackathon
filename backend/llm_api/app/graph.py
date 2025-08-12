import json, uuid
from typing import TypedDict, Annotated, List
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.message import add_messages

from langchain_core.messages import (
    AnyMessage, SystemMessage, HumanMessage, AIMessage, ToolMessage
)

from .llm import make_llm
from .config import SYSTEM, SCHEMA_HINT
from .normalizer import build_page_vocab, llm_normalize_goal
from .tools import build_tools
from .adapter import as_tool_call_ai_message
from .utils import safe_excerpt, norm_text

class AgentState(TypedDict):
    messages: Annotated[List[AnyMessage], add_messages]

def build_app_for_page(page: dict):
    llm = make_llm(temperature=0)
    tools = build_tools(page)
    allowed = {t.name for t in tools}
    page_vocab = build_page_vocab(page)

    def normalize_node(state: AgentState) -> AgentState:
        if not state["messages"]:
            return {"messages": []}
        new_msgs = list(state["messages"])
        for i, msg in enumerate(new_msgs):
            if isinstance(msg, HumanMessage) and isinstance(msg.content, str) and msg.content.startswith("GOAL:"):
                original_goal = msg.content[len("GOAL:"):].strip()
                canon = llm_normalize_goal(original_goal, page_vocab)
                if canon:
                    new_msgs[i] = HumanMessage(f"GOAL: {canon}")
                break
        return {"messages": new_msgs}

    def agent_node(state: AgentState) -> AgentState:
        if state["messages"] and isinstance(state["messages"][-1], ToolMessage):
            last_name = getattr(state["messages"][-1], "name", "").lower()
            if last_name == "done":
                return {"messages": [AIMessage(content="✅ Finished.")]}
            if last_name == "find":
                try:
                    payload = json.loads(state["messages"][-1].content or "{}")
                    if payload.get("total") == 1 and payload.get("matches"):
                        sel = payload["matches"][0]["selector"]
                        return {"messages": [AIMessage(
                            content="",
                            tool_calls=[{
                                "id": f"call_autoclick_{uuid.uuid4().hex[:6]}",
                                "type": "tool_call",
                                "name": "click",
                                "args": {"selector": sel},
                            }]
                        )]}
                except Exception:
                    pass
            if last_name == "click":
                # If the click result contains a URL, ask the runner to navigate now
                try:
                    payload = json.loads(state["messages"][-1].content or "{}")
                    nav = payload.get("navigate_to")
                    if nav:
                        return {"messages": [AIMessage(
                            content="",
                            tool_calls=[{
                                "id": f"call_goto_{uuid.uuid4().hex[:6]}",
                                "type": "tool_call",
                                "name": "goto",
                                "args": {"url": nav},
                            }]
                        )]}
                except Exception:
                    pass

        # keep HTML extremely small; the vocab already carries labels
        raw_html_excerpt = ""
        if isinstance(page.get("raw_html"), str):
            raw_html_excerpt = safe_excerpt(page["raw_html"], max_chars=400)

        sample_buttons = [
           {"text": norm_text(b.get("text","")), "selector": b.get("selector","")}
           for b in page.get("buttons", []) if b.get("text")
        ][:12]
        sample_inputs  = [
           {"name": norm_text(i.get("name") or i.get("placeholder","")), "selector": i.get("selector","")}
           for i in page.get("inputs", []) if (i.get("name") or i.get("placeholder"))
        ][:8]

        page_context = {
            "url": page.get("url"),
            "title": page.get("title"),
            "counts": {
                "buttons": len(page.get("buttons", [])),
                "links": len(page.get("links", [])),
                "inputs": len(page.get("inputs", [])),
                "vocab": len(page_vocab),
            },
            "vocab_top": page_vocab[:30],
            "samples": { "buttons": sample_buttons, "inputs": sample_inputs },
            "raw_html_excerpt": raw_html_excerpt,
        }

        messages = state["messages"] + [
            HumanMessage(f"PAGE_CONTEXT_JSON: {json.dumps(page_context, ensure_ascii=False)}"),
            HumanMessage(SCHEMA_HINT),
        ]
        resp = llm.invoke(messages)
        ai_msg = as_tool_call_ai_message(resp.content, allowed)
        return {"messages": [ai_msg]}

    def after_tools(state: AgentState):
        last = state["messages"][-1]
        if isinstance(last, ToolMessage) and getattr(last, "name", "").lower() in ("done", "goto"):
            return END
        return "agent"

    g = StateGraph(AgentState)
    g.add_node("normalize", normalize_node)
    g.add_node("agent", agent_node)
    g.add_node("tools", ToolNode(tools))

    g.add_edge(START, "normalize")
    g.add_edge("normalize", "agent")
    g.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    g.add_conditional_edges("tools", after_tools, {"agent": "agent", END: END})

    return g.compile(checkpointer=MemorySaver())

