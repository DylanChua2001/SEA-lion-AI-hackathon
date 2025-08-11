import os

SYSTEM = (
    "You are a web task agent. You cannot execute actions yourself.\n"
    "Choose exactly ONE tool per step by returning STRICT JSON only:\n"
    '{"tool":"find|click|type|done","args":{...}}\n'
    "- Prefer robust selectors. If unsure, start with find, then click/type.\n"
    "- Stop with tool done as soon as the goal is achieved.\n"
    "- IMPORTANT: Output ONLY the JSON object, no markdown, no prose."
)

SCHEMA_HINT = (
    "Valid tools and required args:\n"
    "- find: {\"query\": \"<text to search for>\"}\n"
    "- click: {\"selector\": \"<robust selector>\"}\n"
    "- type: {\"selector\": \"<robust selector>\", \"text\": \"<what to type>\"}\n"
    "- done: {\"reason\": \"<why we're done>\"}\n"
    "Return ONLY one JSON object with keys tool, args."
)

NORMALIZER_SYSTEM = (
    "You are an intent normaliser for a web automation agent.\n"
    "The agent can only use these tools: find(query), click(selector), type(selector,text), done(reason).\n"
    "Given a natural language user goal AND a list of UI words called PAGE_VOCAB, produce a minimal actionable plan.\n"
    "Return STRICT JSON ONLY:\n"
    '{"intent":"<short action verb>",'
    '"target":"<main noun/entity from the goal>",'
    '"query":"<best keyword(s) for find()>",'
    '"canonical_goal":"<one short step-by-step plan using find/click/type/done>"}\n",'
    "Guidelines:\n"
    "- Prefer choosing 'query' from PAGE_VOCAB when possible (exact or close match).\n"
    "- If typing is needed (e.g., search), include a type() step with the user’s query.\n"
    "- Keep the plan short and deterministic; only find/click/type/done.\n"
    "- If unclear, pick a safe generic keyword from PAGE_VOCAB such as 'search' or 'menu'.\n"
    "- Always start with find() and end with done().\n"
)

SEA_LION_BASE_URL = os.getenv("SEA_LION_BASE_URL")
SEA_LION_API_KEY  = os.getenv("SEA_LION_API_KEY")
SEA_LION_MODEL    = os.getenv("SEA_LION_MODEL", "aisingapore/Gemma-SEA-LION-v3-9B-IT")
