"""Config-driven flow engine.

Reads a JSON flow config (see flows/*.json) and constructs Pipecat Flows
NodeConfigs + FlowsFunctionSchema handlers at runtime — replacing the hardcoded
node definitions that used to live in flow.py. Behavior is identical; the flow is
now defined by data.

Config vocabulary
-----------------
Top level: `name`, `persona` (template), `initial_node`, `functions` (a library of
LLM-callable functions), `nodes`.

Node types (the `type` field): `say`, `collect`, `branch`, `tool`, `handoff`,
`end`. `end`/`handoff` nodes are terminal (post_action `end_conversation`).
A node has `instructions` (spoken text/questions — templated), the `functions` it
exposes, an optional `context` ("reset"), and an optional `dynamic` block (builds
a value like a slot list from a tool for the instructions template).

Function behaviors describe what happens when the LLM calls the function:
`guards` (early exits), `steps` (`store` state / call a `tool` / `log`), and
either `routes` (conditional) or a plain `result` + `goto`. A handler returns
`(result, next_node)` exactly as the hand-written flow did.

Expressions
-----------
`$args.x` / `$result` / `$result.x` / `$state.x` reference the live context;
literals pass through; `@slots(N)` builds the N-slot payload. `goto` targets are a
node id, `null` (stay), or `{ "cond": "slots_available", "then": .., "else": .. }`.
Instruction templates support `{path}`, `{path || "default"}`, and
`{cond ? "a" : "b"}`.
"""

import json
import re
from pathlib import Path

from loguru import logger

import config
import tools

try:
    from pipecat_flows import (
        ContextStrategy,
        ContextStrategyConfig,
        FlowsFunctionSchema,
        NodeConfig,
    )
except ImportError:  # pragma: no cover - vendored location in some builds
    from pipecat.flows import (  # type: ignore
        ContextStrategy,
        ContextStrategyConfig,
        FlowsFunctionSchema,
        NodeConfig,
    )

PROJECT_DIR = Path(__file__).parent

# Only these tool names may be invoked from config (they exist in tools.py).
ALLOWED_TOOLS = {
    "look_up_patient",
    "get_available_slots",
    "book_appointment",
    "create_patient",
    "get_cost_estimate",
    "send_confirmation",
}


class FlowEngine:
    def __init__(self, config_path: str):
        path = Path(config_path)
        if not path.is_absolute():
            path = PROJECT_DIR / path
        with open(path, "r", encoding="utf-8") as f:
            self.spec = json.load(f)

        self.functions = self.spec["functions"]
        self.nodes = self.spec["nodes"]
        self.initial_node = self.spec["initial_node"]
        self.branding = {
            "CLINIC_NAME": config.CLINIC_NAME,
            "ASSISTANT_NAME": config.ASSISTANT_NAME,
        }
        # Persona references branding only — render once.
        self.persona = self._render_text(self.spec["persona"], self.branding)
        logger.debug(f"FlowEngine loaded '{self.spec.get('name')}' from {path}")

    # -- public --------------------------------------------------------------
    def build_initial_node(self) -> NodeConfig:
        return self.build_node(self.initial_node, None)

    def build_node(self, node_id: str, flow_manager) -> NodeConfig:
        spec = self.nodes[node_id]
        state = flow_manager.state if flow_manager is not None else {}

        node_locals = {}
        if "dynamic" in spec:
            d = spec["dynamic"]
            enriched = self._slots_enriched(d.get("limit"))
            rendered = [self._render_text(d["item"], s) for s in enriched]
            node_locals[d["var"]] = d.get("join", "\n").join(rendered)

        ctx = {**self.branding, **state, **node_locals}
        instructions = self._render_text(spec["instructions"], ctx)

        node: dict = {
            "name": node_id,
            "role_message": self.persona,
            "task_messages": [{"role": "developer", "content": instructions}],
            "functions": [self._build_function(f) for f in spec.get("functions", [])],
            "respond_immediately": True,
        }
        if spec.get("context") == "reset":
            node["context_strategy"] = ContextStrategyConfig(strategy=ContextStrategy.RESET)
        if spec.get("type") in ("end", "handoff"):
            node["post_actions"] = [{"type": "end_conversation"}]
        return NodeConfig(**node)

    # -- function schemas + handlers ----------------------------------------
    def _build_function(self, fname: str) -> FlowsFunctionSchema:
        fdef = self.functions[fname]
        properties = {}
        required = []
        for p in fdef["parameters"]:
            prop = {"type": p["type"]}
            if p.get("description"):
                prop["description"] = p["description"]
            properties[p["name"]] = prop
            if p.get("required"):
                required.append(p["name"])

        async def handler(args, flow_manager, _fdef=fdef):
            return self._run_behavior(_fdef, args, flow_manager)

        return FlowsFunctionSchema(
            name=fname,
            description=fdef["description"],
            properties=properties,
            required=required,
            handler=handler,
        )

    def _run_behavior(self, fdef, args, flow_manager):
        coerced = {}
        for p in fdef["parameters"]:
            raw = args.get(p["name"])
            if p["type"] == "string":
                coerced[p["name"]] = (raw or "").strip()
            elif p["type"] == "boolean":
                coerced[p["name"]] = bool(raw)
            else:
                coerced[p["name"]] = raw

        ctx = {"args": coerced, "result": None, "state": flow_manager.state}
        behavior = fdef["behavior"]

        for guard in behavior.get("guards", []):
            if self._match(guard["when"], ctx):
                result = self._resolve_expr(guard.get("result"), ctx)
                goto = self._resolve_goto(guard.get("goto"), ctx, flow_manager)
                return result, goto

        self._run_steps(behavior.get("steps", []), ctx, flow_manager)

        if "routes" in behavior:
            return self._eval_routes(behavior["routes"], ctx, flow_manager)

        result = self._resolve_expr(behavior.get("result"), ctx)
        goto = self._resolve_goto(behavior.get("goto"), ctx, flow_manager)
        return result, goto

    def _run_steps(self, steps, ctx, flow_manager):
        for step in steps:
            if "store" in step:
                for key, expr in step["store"].items():
                    flow_manager.state[key] = self._resolve_expr(expr, ctx)
            elif "tool" in step:
                t = step["tool"]
                fn = self._tool(t["name"])
                kwargs = {k: self._resolve_expr(v, ctx) for k, v in t.get("args", {}).items()}
                result = fn(**kwargs)
                ctx["result"] = result
                if t.get("store_result"):
                    flow_manager.state[t["store_result"]] = result
            elif "log" in step:
                log_ctx = {**flow_manager.state, "args": ctx["args"], "result": ctx["result"]}
                logger.info(self._render_text(step["log"], log_ctx))

    def _eval_routes(self, routes, ctx, flow_manager):
        for route in routes:
            if not self._match(route["when"], ctx):
                continue
            if "increment" in route:
                key = route["increment"]
                flow_manager.state[key] = flow_manager.state.get(key, 0) + 1
            for key in route.get("clear", []):
                flow_manager.state.pop(key, None)
            for key, expr in route.get("store", {}).items():
                flow_manager.state[key] = self._resolve_expr(expr, ctx)
            if "steps" in route:
                self._run_steps(route["steps"], ctx, flow_manager)
            if "routes" in route:
                return self._eval_routes(route["routes"], ctx, flow_manager)
            result = self._resolve_expr(route["result"], ctx) if "result" in route else None
            goto = self._resolve_goto(route.get("goto"), ctx, flow_manager)
            return result, goto
        return None, None

    # -- conditions / targets -----------------------------------------------
    def _match(self, when, ctx) -> bool:
        if when == "always":
            return True
        if when == "result_is_null":
            return ctx["result"] is None
        result = ctx["result"] or {}
        if when == "result_success":
            return bool(result.get("success"))
        if when == "result_failure":
            return not bool(result.get("success"))
        if isinstance(when, dict):
            if "state_gte" in when:
                spec = when["state_gte"]
                return ctx["state"].get(spec["key"], 0) >= spec["value"]
            if "state_falsy" in when:
                return not ctx["state"].get(when["state_falsy"])
        raise ValueError(f"Unknown condition: {when!r}")

    def _eval_cond(self, cond) -> bool:
        if cond == "slots_available":
            return bool(tools.get_available_slots())
        raise ValueError(f"Unknown cond: {cond!r}")

    def _resolve_goto(self, goto, ctx, flow_manager):
        if goto is None:
            return None
        if isinstance(goto, str):
            return self.build_node(goto, flow_manager)
        if isinstance(goto, dict) and "cond" in goto:
            branch = goto["then"] if self._eval_cond(goto["cond"]) else goto["else"]
            return self._resolve_goto(branch, ctx, flow_manager)
        raise ValueError(f"Unknown goto: {goto!r}")

    # -- expressions ---------------------------------------------------------
    def _resolve_expr(self, expr, ctx):
        if isinstance(expr, str):
            if expr.startswith("$"):
                parts = expr[1:].split(".")
                cur = ctx.get(parts[0])
                for p in parts[1:]:
                    if isinstance(cur, dict):
                        cur = cur.get(p)
                    else:
                        cur = getattr(cur, p, None)
                    if cur is None:
                        break
                return cur
            if expr.startswith("@slots(") and expr.endswith(")"):
                n = int(expr[len("@slots("):-1])
                return self._slots_payload(n)
            return expr
        if isinstance(expr, dict):
            # Reserved: {"or_null": expr} -> value if truthy else None.
            if set(expr.keys()) == {"or_null"}:
                return self._resolve_expr(expr["or_null"], ctx) or None
            return {k: self._resolve_expr(v, ctx) for k, v in expr.items()}
        if isinstance(expr, list):
            return [self._resolve_expr(v, ctx) for v in expr]
        return expr

    def _tool(self, name):
        if name not in ALLOWED_TOOLS:
            raise ValueError(f"Tool '{name}' is not in the allowed tool list.")
        return getattr(tools, name)

    def _slots_enriched(self, limit):
        slots = tools.get_available_slots()
        if limit is not None:
            slots = slots[:limit]
        return [{**s, "when": tools.format_when(s["datetime"])} for s in slots]

    def _slots_payload(self, n):
        return [
            {"slot_id": s["slot_id"], "when": tools.format_when(s["datetime"]), "provider": s["provider"]}
            for s in tools.get_available_slots()[:n]
        ]

    # -- text templating -----------------------------------------------------
    _TERNARY_STR = re.compile(r'"((?:[^"\\]|\\.)*)"')

    def _render_text(self, template: str, ctx: dict) -> str:
        out = []
        i = 0
        while i < len(template):
            ch = template[i]
            if ch == "{":
                j = template.index("}", i)
                out.append(str(self._eval_token(template[i + 1:j].strip(), ctx)))
                i = j + 1
            else:
                out.append(ch)
                i += 1
        return "".join(out)

    def _eval_token(self, token: str, ctx: dict):
        if "?" in token and ":" in token:
            cond = token[: token.index("?")].strip()
            rest = token[token.index("?") + 1:]
            options = self._TERNARY_STR.findall(rest)
            value = self._path(ctx, cond)
            return options[0] if value else options[1]
        if "||" in token:
            left, right = token.split("||", 1)
            default = right.strip()
            if default.startswith('"') and default.endswith('"'):
                default = default[1:-1]
            value = self._path(ctx, left.strip())
            return value if value else default
        return self._path(ctx, token)

    @staticmethod
    def _path(ctx: dict, dotted: str):
        cur = ctx
        for part in dotted.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                cur = getattr(cur, part, None)
            if cur is None:
                break
        return cur
