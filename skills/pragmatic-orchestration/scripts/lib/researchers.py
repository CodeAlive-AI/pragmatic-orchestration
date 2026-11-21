#!/usr/bin/env python3
"""Read-only researchers a review worker may start (`review ask --researcher`).

One place says, per backend, how a worker in mode `review-researchers` reaches
its researchers, and refuses a backend that cannot keep the read-only posture
for them. backend_run.sh builds argv from the plan; prompt_pipeline.py puts
the same plan into the worker's contract as the list of permitted channels.

Routes:
  native  the harness's own subagent, limited to a read-only researcher and
          run on the model of a `--researcher` profile of the same backend
  porch   a nested `porch review ask -a PROFILE` from the worker's shell

Verified live on 2026-11-21 (codex-cli 0.170.0, Claude Code 2.1.328,
OpenCode 2.3.2, Grok Build 1.0.89, agy 1.5.14); references/review.md has the
evidence per backend.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import backend_contract


# The worker's shell carries this variable. Set (even to an empty string) it
# marks a shell that belongs to a read-only review worker: porch started from
# it runs nothing but `review ask -a` one of the listed profiles.
NESTED_ENV = "PORCH_RESEARCHER_PROFILES"

# Name of the researcher agent porch defines at run time where the harness
# takes agent definitions on the command line (OpenCode, Grok Build).
RESEARCHER_AGENT = "porch-researcher"

# Built-in Claude Code subagent types other than Explore, denied by rule in
# researcher mode. Claude Code has no allowlist for types under --safe-mode
# (custom agents, and with them `tools: Agent(a, b)`, do not load), so a type
# a later release adds stays available until it is listed here. It would still
# run under the session's Edit/Write denial and the forced researcher model.
CLAUDE_DENIED_AGENT_TYPES = (
    "general-purpose",
    "Plan",
    "claude",
    "claude-code-guide",
    "web-fetch",
    "statusline-setup",
)

ROUTES: Dict[str, Tuple[str, ...]] = {
    # Kernel read-only sandbox without network: a nested porch cannot write
    # its run folder or resolve a host. Sub-agents inherit the sandbox.
    "codex-cli": ("native",),
    # Explore is the one type left; Bash is unsandboxed, as in plain review.
    "claude-code": ("native", "porch"),
    # A run-time subagent with its own model and permissions; shell unsandboxed.
    "opencode": ("native", "porch"),
    # The sandbox covers the whole process and leaves only temp and ~/.grok
    # writable, so a nested porch cannot keep its run folder or start a CLI.
    "grok-build": ("native",),
    # Its own subagents can be neither switched off nor limited to one type
    # and take a model tier, not a model; the shell runs a nested porch.
    "antigravity-cli": ("porch",),
}

REFUSED: Dict[str, str] = {
    "gemini-cli": (
        "its review runs with subagents switched off, and researchers under "
        "its report-only prompt contract have not been verified"
    ),
    "devin-cli": (
        "`devin acp --agent-type review` has no verified way to start or "
        "limit read-only subagents"
    ),
    "cursor-cli": (
        "`--mode plan` blocks shell writes, so a nested porch cannot run, and "
        "its own subagents cannot be limited to read-only researchers"
    ),
}


class ResearcherError(ValueError):
    """The requested researchers cannot be provided; never downgrade silently."""


@dataclass
class ResearcherPlan:
    agent_id: str
    backend: str
    routes: Tuple[str, ...]
    native_profile: str = ""
    native_model: str = ""
    native_effort: str = ""
    porch_profiles: List[str] = field(default_factory=list)
    unreachable: List[str] = field(default_factory=list)
    channels: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "backend": self.backend,
            "routes": list(self.routes),
            "native_profile": self.native_profile,
            "native_model": self.native_model,
            "native_effort": self.native_effort,
            "porch_profiles": list(self.porch_profiles),
            "unreachable": list(self.unreachable),
            "channels": list(self.channels),
            "nested_env": ",".join(self.porch_profiles),
        }


def supported_backends() -> Tuple[str, ...]:
    return tuple(ROUTES)


def _runs_on(model: str, effort: str) -> str:
    text = f"model {model}" if model else "the account's default model"
    if effort:
        text += f", effort {effort}"
    return text


def _native_channel(backend: str, model: str, effort: str) -> str:
    on = _runs_on(model, effort)
    if backend == "codex-cli":
        return (
            f"- the spawn_agent tool of your collaboration tools "
            f'(collaboration.spawn_agent), always with fork_turns "none": each '
            f"sub-agent it starts runs on {on}, inside your read-only sandbox;"
        )
    if backend == "claude-code":
        return (
            f'- your Agent tool with subagent_type "Explore": a read-only '
            f"agent that runs on {on}; no other subagent type is available;"
        )
    if backend == "opencode":
        return (
            f'- your subagent tool with the agent "{RESEARCHER_AGENT}": a '
            f"read-only agent that runs on {on}; no other agent is available;"
        )
    if backend == "grok-build":
        return (
            f"- your spawn_subagent tool: every subagent it starts is the "
            f'read-only "{RESEARCHER_AGENT}", runs on {on} and stays inside '
            f"your read-only sandbox;"
        )
    raise ResearcherError(f"backend {backend} has no native researcher channel")


def _porch_channel(porch_path: str, profiles: Sequence[str]) -> str:
    listed = ", ".join(profiles)
    return (
        f"- the shell command `{porch_path} review ask -a PROFILE --progress "
        f'none "QUESTION"`, run from your working directory, with PROFILE one '
        f"of: {listed}. Quote the question for your shell. The command prints "
        f"the researcher's report on stdout and can "
        f"take minutes: start it in your shell tool's background or "
        f"long-running mode and read the output when it ends. No other porch "
        f"command works in your shell;"
    )


def plan_researchers(
    agent_id: str,
    researcher_ids: Sequence[str],
    *,
    porch_path: str = "porch",
) -> ResearcherPlan:
    """Decide how AGENT_ID's worker reaches the named researcher profiles."""
    wanted: List[str] = []
    for rid in researcher_ids:
        rid = rid.strip()
        if rid and rid not in wanted:
            wanted.append(rid)
    if not wanted:
        raise ResearcherError("name at least one researcher profile")

    try:
        worker = backend_contract.get_agent(agent_id)
    except KeyError:
        raise ResearcherError(f"unknown profile '{agent_id}'") from None
    backend = str(worker.get("backend") or "")
    routes = ROUTES.get(backend)
    if not routes:
        reason = REFUSED.get(backend, "it has not been verified for researchers")
        raise ResearcherError(
            f"profile '{agent_id}' (backend {backend}) cannot start researchers: "
            f"{reason}. Backends that can: {', '.join(supported_backends())}. "
            f"Run it without --researcher (it then works alone), or use "
            f"`porch delegate`, where only the brief forbids writes."
        )

    profiles: Dict[str, Dict[str, Any]] = {}
    for rid in wanted:
        try:
            profiles[rid] = backend_contract.get_agent(rid)
        except KeyError:
            raise ResearcherError(
                f"unknown researcher profile '{rid}'; use porch --list-agents"
            ) from None

    plan = ResearcherPlan(agent_id=agent_id, backend=backend, routes=routes)
    if "native" in routes:
        for rid in wanted:
            cfg = profiles[rid]
            if str(cfg.get("backend") or "") != backend:
                continue
            model, effort = backend_contract.resolve_model_effort(backend, cfg)
            plan.native_profile = rid
            plan.native_model = model
            # Claude Code forces a subagent model, not an effort.
            plan.native_effort = "" if backend == "claude-code" else effort
            break
    if "porch" in routes:
        plan.porch_profiles = list(wanted)
    plan.unreachable = [
        rid for rid in wanted
        if rid != plan.native_profile and rid not in plan.porch_profiles
    ]

    if not plan.native_profile and not plan.porch_profiles:
        raise ResearcherError(
            f"profile '{agent_id}' (backend {backend}) starts researchers only "
            f"as its own subagents and cannot run a nested porch, so none of "
            f"the researcher profiles ({', '.join(wanted)}) can be reached. "
            f"Name a {backend} profile whose model the researchers should use."
        )

    if plan.native_profile:
        plan.channels.append(
            _native_channel(backend, plan.native_model, plan.native_effort)
        )
    if plan.porch_profiles:
        plan.channels.append(_porch_channel(porch_path, plan.porch_profiles))
    plan.channels[-1] = plan.channels[-1].rstrip(";") + "."
    return plan


def nested_allowlist(env: Optional[Dict[str, str]] = None) -> Optional[List[str]]:
    """Profiles a nested porch may run, or None outside a review worker's shell."""
    source = os.environ if env is None else env
    if NESTED_ENV not in source:
        return None
    return [item.strip() for item in source[NESTED_ENV].split(",") if item.strip()]


def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Porch read-only researchers")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_plan = sub.add_parser("plan", help="How a worker reaches its researchers")
    p_plan.add_argument("agent_id")
    p_plan.add_argument("--researcher", action="append", default=[])
    p_plan.add_argument("--porch", default="porch")
    sub.add_parser("backends", help="Backends that can start researchers")
    args = ap.parse_args()

    if args.cmd == "backends":
        print(json.dumps({"supported": ROUTES, "refused": REFUSED}, indent=2))
        return 0
    researchers: List[str] = []
    for value in args.researcher:
        researchers.extend(value.split(","))
    try:
        plan = plan_researchers(args.agent_id, researchers, porch_path=args.porch)
    except (ResearcherError, FileNotFoundError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 4
    print(json.dumps(plan.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
