"""AUTO helper — model-owned web research for the LEARN rail."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from cognition.web_research import merge_research_pages


def run_learn_web_research(
    generate: Callable[..., Any],
    *,
    seed: str,
    domain: str,
    catalog: list,
    invoke: Callable[[str, dict[str, Any]], Mapping[str, Any]],
    max_tokens: int,
    max_rounds: int = 3,
) -> dict[str, Any]:
    """Run the research prompt; the model chooses topic and URL. Returns pages + last response."""
    from auto_loop.execute.brain_skill_executor import _TOOLS
    from guardian.external_ingress import harden
    from organ.promptorgan import PromptContext, phrase

    request = phrase(
        PromptContext(
            kind="tool",
            question=seed,
            data={
                "web_research": True,
                "web_catalog": catalog,
                "query": seed,
                "domain": domain,
                "agent_type": "research",
                "tool": "web_fetch",
                "tool_reason": "model-owned web research",
                "params": {"query": seed, "domain": domain},
            },
        )
    )
    specs = [_TOOLS["web_search"].spec(), _TOOLS["web_fetch"].spec()]
    messages: list[dict[str, Any]] = [{"role": "user", "content": request.user}]
    pages: list[dict[str, Any]] = []
    errors: list[str] = []
    invoked = False
    last: dict[str, Any] | None = None
    for round_no in range(max_rounds + 1):
        exhausted = round_no >= max_rounds
        last = generate(
            operation="web_research",
            prompt=None,
            system=request.system,
            messages=[dict(turn) for turn in messages],
            tools=None if exhausted else specs,
            max_tokens=max_tokens,
            enable_thinking=False,
        )
        if not isinstance(last, Mapping):
            break
        calls = [call for call in (last.get("tool_calls") or ()) if isinstance(call, Mapping)]
        if not calls or exhausted:
            break
        messages.append(
            {
                "role": "assistant",
                "content": str(last.get("text") or ""),
                "tool_calls": calls,
            }
        )
        for call in calls:
            function = call.get("function") if isinstance(call.get("function"), Mapping) else {}
            name = str(function.get("name") or "")
            raw = function.get("arguments")
            if isinstance(raw, Mapping):
                args = dict(raw)
            else:
                try:
                    parsed = json.loads(str(raw or "{}"))
                    args = dict(parsed) if isinstance(parsed, dict) else {}
                except json.JSONDecodeError:
                    args = {}
            if name not in {"web_search", "web_fetch"}:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(call.get("id") or ""),
                        "name": name,
                        "content": f"{name}: not a web research tool",
                    }
                )
                continue
            fetch_args = dict(args)
            if fetch_args.get("url") and not fetch_args.get("query"):
                fetch_args["query"] = fetch_args.pop("url")
            bound = {
                key: fetch_args[key]
                for key in ("query", "max_results", "domain", "source")
                if key in fetch_args
            }
            if not bound.get("query"):
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(call.get("id") or ""),
                        "name": name,
                        "content": "query is required",
                    }
                )
                continue
            invoked = True
            inv = invoke("web_fetch", bound)
            if not isinstance(inv, Mapping) or not inv.get("ok", True):
                reported_error = inv.get("error") if isinstance(inv, Mapping) else None
                errors.append(str(reported_error or "web_fetch failed"))
                page = inv.get("result") if isinstance(inv, Mapping) else None
            else:
                page = inv.get("result")
            page_error = page.get("error") if isinstance(page, Mapping) else None
            if page_error:
                errors.append(str(page_error))
                page = None
            if isinstance(page, Mapping):
                pages.append(dict(page))
            if isinstance(page, Mapping):
                raw = json.dumps(page, ensure_ascii=False)[:4000]
            else:
                raw = str(inv)[:4000]
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(call.get("id") or ""),
                    "name": name,
                    "content": f"Source: web page (untrusted)\n{harden(raw)}",
                }
            )
    merged = merge_research_pages(pages)
    error = "; ".join(dict.fromkeys(errors))[:500] if errors else ""
    if error:
        merged["error"] = error
    return {
        "pages": pages,
        "response": last,
        "tool_result": merged,
        "called": invoked,
        "error": error,
        "system": request.system,
        "user": request.user,
    }
