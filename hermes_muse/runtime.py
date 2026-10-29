"""Plugin registration: install/upgrade, the model surface, the Skill and system prompt section, the hooks.

The model surface (tool schemas and handlers, design 2.6) lives in `hermes_muse.surface` and exposes
`TOOLS = [{"name", "toolset", "schema"}]` and `handle(kernel, name, args, caller) -> str`. `caller` is
{"kind": user|cron|child|group|other, "session_id", "turn_id", "tool_call_id", "task_id"}, set from the
host here and never from tool arguments.
"""
import logging
from functools import partial
from pathlib import Path

from .hooks import Hooks
from .host import HostAdapter
from .host.sessions import caller
from .install import install
from .kernel import Kernel

log = logging.getLogger(__name__)
REQUIRED = ("register_tool", "register_skill", "register_system_prompt_section", "register_hook", "on_unload")


def call(surface, kernel, name, args, **kwargs):
    who = {"kind": caller(**kwargs), **{f: kwargs.get(f) or "" for f in ("session_id", "turn_id", "tool_call_id", "task_id")}}
    return surface.handle(kernel, name, args, who)


def register(ctx, *, plugin_root):
    if any(not callable(getattr(ctx, name, None)) for name in REQUIRED):
        raise RuntimeError("Hermes Muse needs Hermes 0.21.5+ with prompt sections, plugin skills and unload hooks")
    root = Path(plugin_root).resolve()
    adapter = HostAdapter()
    try:
        install(adapter, root)
    except Exception as exc:  # the marker keeps jobs silent and the main session is told; hooks still load
        log.error("Hermes Muse install failed: %s", exc)
    kernel = Kernel(adapter.home, adapter=adapter)
    hooks = Hooks(kernel)
    prompt = ((root / "prompts" / "system.md").read_text(encoding="utf-8")
              .replace("{{MUSE_HOME}}", str(adapter.home / "muse")).replace("{{HERMES_HOME}}", str(adapter.home)))
    ctx.register_skill("companion", root / "skills" / "companion" / "SKILL.md",
                       description="Goals, interests, proactive reminders, reflection, Feed and the Muse tool")
    ctx.register_system_prompt_section("hermes-muse.companion", prompt, position="after_memory", max_chars=4000)
    try:
        from . import surface
    except ImportError as exc:
        log.error("Hermes Muse model surface unavailable, no tool registered: %s", exc)
    else:
        for tool in surface.TOOLS:
            ctx.register_tool(name=tool["name"], toolset=tool["toolset"], schema=tool["schema"],
                              handler=partial(call, surface, kernel, tool["name"]))
    ctx.register_hook("pre_llm_call", hooks.pre_turn)
    ctx.register_hook("post_llm_call", hooks.post_turn)
    ctx.register_hook("post_tool_call", hooks.post_tool)
    ctx.on_unload(hooks.close)
    return hooks
