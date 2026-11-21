#!/usr/bin/env python3
"""Layered prompt pipeline for Porch.

Trusted layers (fixed order, lowest → highest provenance of user control):

  1. framework_policy   — Porch independent-advisory / operational rules
  2. mode_contract      — mode-specific review contract
  3. role               — specialist / analyst / lateral role text
  4. output_schema      — required output template or XML schema
  5. repository_facts   — orchestrator-collected facts
  6. user_input         — the caller's question / task / code under review
  7. framework_recap    — compact trusted reminder after untrusted content

Prompt purity:
  - raw delegate is user_input only (+ optional trusted metadata)
  - review --prompt-file changes transport only; review policy remains layered
  - code-review skips the default Assessment template (uses its own schema)

Review contract variants (prompts/review-*.txt), chosen by two switches:
  - delegation: the worker works alone (default), or may start read-only
    researchers through the channels the caller passes in (researchers.py)
  - style: an independent adviser under five headings (default), or the
    executor of a read-only assignment who answers in the structure of its
    brief: no adviser role, no profile role text, no answer template

Trust boundary: layers 1–5 and 7 are trusted orchestrator content; layer 6 is
untrusted user/repo content. The final recap keeps the operational contract
salient without treating repository or caller text as instructions.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


LAYER_ORDER = (
    "framework_policy",
    "mode_contract",
    "role",
    "output_schema",
    "repository_facts",
    "user_input",
    "framework_recap",
)

TRUSTED_LAYERS = frozenset(
    {
        "framework_policy",
        "mode_contract",
        "role",
        "output_schema",
        "repository_facts",
        "framework_recap",
    }
)
UNTRUSTED_LAYERS = frozenset({"user_input"})

# Modes that use the full review principles + role + Assessment template.
REVIEW_WRAP_MODES = frozenset(
    {
        "review",
        "review-ask",
        "ask",
    }
)
# Code review provides its own XML schema; skip Assessment template.
CODE_REVIEW_MODES = frozenset(
    {
        "review-code",
        "review-code-basic",
        "review-code-specialists",
        "basic",
        "specialists",
        "super",
        "ultra",
        "review-code-super",
        "review-code-ultra",
        "discovery",
        "judge",
    }
)
# Raw delegate: task body only (plus optional trusted metadata block).
RAW_MODES = frozenset({"delegate", "delegate-steerable", "raw", "steerable"})


_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"

STYLE_ADVISER = "adviser"
STYLE_ASSIGNMENT = "assignment"
REVIEW_STYLES = (STYLE_ADVISER, STYLE_ASSIGNMENT)

_DELEGATION_RULE_SLOT = "{{DELEGATION_RULE}}\n"
_DELEGATION_CLAUSE_SLOT = "{{DELEGATION_CLAUSE}}"
_RESEARCHER_CHANNELS_SLOT = "{{RESEARCHER_CHANNELS}}\n"
_CLAUSE_ALONE = "no subagents or delegation"
_CLAUSE_RESEARCHERS = (
    "no agents other than the read-only researchers this contract allows, and "
    "no other delegation"
)
_CONTRACT_FILES = {
    STYLE_ADVISER: ("review-framework.txt", "review-recap.txt"),
    STYLE_ASSIGNMENT: ("review-assignment-framework.txt", "review-assignment-recap.txt"),
}


def _prompt_asset(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


def _fill(template: str, slot: str, value: str) -> str:
    if template.count(slot) != 1:
        raise ValueError(f"review contract template must hold {slot.strip()} exactly once")
    return template.replace(slot, value)


def review_contract(
    style: str = STYLE_ADVISER, researcher_channels: str = ""
) -> Tuple[str, str]:
    """Return (framework policy, final recap) for one review contract variant.

    `researcher_channels` is the list of ways the worker may start read-only
    researchers, one "- ..." line each. Empty means the worker works alone.
    """
    if style not in REVIEW_STYLES:
        raise ValueError(f"unknown review style: {style!r}")
    framework_name, recap_name = _CONTRACT_FILES[style]
    channels = researcher_channels.strip("\n")
    if channels.strip():
        indented = "".join(f"  {line}\n" for line in channels.splitlines())
        rule = _fill(
            _prompt_asset("review-delegation-researchers.txt"),
            _RESEARCHER_CHANNELS_SLOT,
            indented,
        )
        clause = _CLAUSE_RESEARCHERS
    else:
        rule = _prompt_asset("review-delegation-alone.txt")
        clause = _CLAUSE_ALONE
    framework = _fill(_prompt_asset(framework_name), _DELEGATION_RULE_SLOT, rule)
    recap = _fill(_prompt_asset(recap_name), _DELEGATION_CLAUSE_SLOT, clause)
    return framework, recap


# The default contract: an independent adviser who works alone.
FRAMEWORK_POLICY_REVIEW, FRAMEWORK_RECAP_REVIEW = review_contract()

OUTPUT_TEMPLATE_REVIEW = """
RESPOND USING THIS STRUCTURE (adapt section depth to the question complexity):

## Assessment
Your independent take on the situation. Start with what YOU see, not what was asked.

## Key Findings
Concrete observations, numbered. Include evidence or reasoning for each.

## Blind Spots
What the question misses. Unstated assumptions. Risks not mentioned. Adjacent concerns.

## Alternatives
Options not presented in the query that deserve consideration.
Skip this section only if the query is purely analytical (no decision involved).

## Recommendation
Your top recommendation with reasoning. Include confidence level (high/medium/low) and what would change your mind.
"""


@dataclass
class PromptLayer:
    name: str
    content: str
    trusted: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "trusted": self.trusted,
            "chars": len(self.content),
            "sha_prefix": _sha_prefix(self.content),
        }


@dataclass
class BuiltPrompt:
    text: str
    layers: List[PromptLayer] = field(default_factory=list)
    mode: str = ""
    raw: bool = False

    def provenance(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "raw": self.raw,
            "layer_order": [L.name for L in self.layers if L.content.strip()],
            "layers": [L.to_dict() for L in self.layers if L.content.strip()],
            "trusted_boundary": {
                "trusted": [L.name for L in self.layers if L.trusted and L.content.strip()],
                "untrusted": [
                    L.name for L in self.layers if not L.trusted and L.content.strip()
                ],
            },
        }


def _sha_prefix(text: str, n: int = 12) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


class PromptBuilder:
    """Assemble prompts from explicit layers with fixed ordering."""

    def __init__(
        self,
        *,
        mode: str,
        role_text: str = "",
        user_input: str = "",
        output_schema: str = "",
        repository_facts: str = "",
        mode_contract: str = "",
        framework_policy: str = "",
        raw: bool = False,
        include_review_template: Optional[bool] = None,
        trusted_metadata: str = "",
        style: str = STYLE_ADVISER,
        researcher_channels: str = "",
    ):
        if style not in REVIEW_STYLES:
            raise ValueError(f"unknown review style: {style!r}")
        if mode == "review-researchers" and not researcher_channels.strip():
            raise ValueError("mode review-researchers needs the researcher channels")
        self.style = style
        self.researcher_channels = researcher_channels
        self.mode = mode
        self.role_text = role_text
        self.user_input = user_input
        self.output_schema = output_schema
        self.repository_facts = repository_facts
        self.mode_contract = mode_contract
        self.framework_policy = framework_policy
        self.raw = raw
        self.include_review_template = include_review_template
        self.trusted_metadata = trusted_metadata

    def _resolve_layers_content(self) -> Dict[str, str]:
        mode = self.mode
        raw = self.raw or mode in RAW_MODES and mode != "delegate"
        # Explicit raw flag always wins.
        if self.raw or mode == "raw":
            layers = {
                "framework_policy": "",
                "mode_contract": "",
                "role": "",
                "output_schema": "",
                "repository_facts": self.trusted_metadata or "",
                "user_input": self.user_input,
                "framework_recap": "",
            }
            # Raw delegate may carry minimal trusted metadata only when requested.
            return layers

        is_review = (
            mode in CODE_REVIEW_MODES
            or mode in REVIEW_WRAP_MODES
            or mode.startswith("review")
        )
        if is_review:
            framework, recap = review_contract(self.style, self.researcher_channels)
        if is_review and self.style == STYLE_ASSIGNMENT:
            # The assignment sets the role and the structure of the answer:
            # no adviser role, no profile role text, no answer template.
            return {
                "framework_policy": self.framework_policy or framework,
                "mode_contract": self.mode_contract,
                "role": "",
                "output_schema": self.output_schema,
                "repository_facts": self.repository_facts,
                "user_input": self.user_input,
                "framework_recap": recap,
            }

        if mode in CODE_REVIEW_MODES or (
            self.include_review_template is False
        ):
            # Role + framework + caller-provided schema; no Assessment template.
            return {
                "framework_policy": self.framework_policy or framework,
                "mode_contract": self.mode_contract,
                "role": self.role_text,
                "output_schema": self.output_schema,
                "repository_facts": self.repository_facts,
                "user_input": self.user_input,
                "framework_recap": recap,
            }

        if is_review:
            use_template = (
                OUTPUT_TEMPLATE_REVIEW
                if self.include_review_template is not False
                else ""
            )
            schema = self.output_schema if self.output_schema else use_template
            if self.include_review_template is False:
                schema = self.output_schema
            return {
                "framework_policy": self.framework_policy or framework,
                "mode_contract": self.mode_contract,
                "role": self.role_text,
                "output_schema": schema,
                "repository_facts": self.repository_facts,
                "user_input": self.user_input,
                "framework_recap": recap,
            }

        if mode in ("delegate", "delegate-steerable"):
            # Non-raw delegate still sends the task largely as-is; YOLO has no
            # review template. Optional trusted_metadata only.
            return {
                "framework_policy": "",
                "mode_contract": "",
                "role": "",
                "output_schema": "",
                "repository_facts": self.trusted_metadata or self.repository_facts,
                "user_input": self.user_input,
                "framework_recap": "",
            }

        # Unknown mode: fail closed to raw user_input only (no accidental review wrap).
        return {
            "framework_policy": "",
            "mode_contract": "",
            "role": "",
            "output_schema": "",
            "repository_facts": self.repository_facts,
            "user_input": self.user_input,
            "framework_recap": "",
        }

    def build(self) -> BuiltPrompt:
        content = self._resolve_layers_content()
        layers: List[PromptLayer] = []
        parts: List[str] = []

        for name in LAYER_ORDER:
            text = content.get(name) or ""
            trusted = name in TRUSTED_LAYERS
            layers.append(PromptLayer(name=name, content=text, trusted=trusted))
            if not text.strip():
                continue
            if name == "user_input":
                # Separator before untrusted content when anything trusted preceded.
                if parts:
                    parts.append("\n---\n\n" + text)
                else:
                    parts.append(text)
            elif name == "role":
                parts.append(text if text.endswith("\n") else text + "\n")
            elif name == "output_schema":
                parts.append(text if text.startswith("\n") else "\n" + text)
            elif name == "repository_facts":
                if parts:
                    parts.append("\n\n---\n\n" + text)
                else:
                    parts.append(text)
            else:
                parts.append(text if text.endswith("\n") else text + "\n")

        # Match legacy build_prompt shape for review-ask:
        # principles + role + template + "---\n\n" + user
        text = self._legacy_compatible_join(layers)
        return BuiltPrompt(
            text=text,
            layers=layers,
            mode=self.mode,
            raw=self.raw or self.mode in ("raw",),
        )

    def _legacy_compatible_join(self, layers: List[PromptLayer]) -> str:
        by = {L.name: L.content for L in layers}
        mode = self.mode
        if self.raw or mode == "raw":
            meta = by.get("repository_facts") or ""
            user = by.get("user_input") or ""
            if meta.strip():
                return meta.rstrip() + "\n\n" + user
            return user

        if mode in ("delegate", "delegate-steerable"):
            meta = by.get("repository_facts") or ""
            user = by.get("user_input") or ""
            if meta.strip():
                return meta.rstrip() + "\n\n" + user
            return user

        # Review / code-review style
        chunks: List[str] = []
        if by.get("framework_policy"):
            chunks.append(by["framework_policy"].rstrip() + "\n")
        if by.get("mode_contract"):
            chunks.append(by["mode_contract"].rstrip() + "\n")
        if by.get("role"):
            chunks.append(by["role"].rstrip() + "\n")
        if by.get("output_schema"):
            chunks.append(by["output_schema"].rstrip() + "\n")
        user = by.get("user_input") or ""
        facts = by.get("repository_facts") or ""
        body = "\n".join(chunks)
        if body:
            body = body.rstrip() + "\n---\n\n" + user
        else:
            body = user
        if facts.strip():
            body += "\n\n--- Context ---\n" + facts
        recap = by.get("framework_recap") or ""
        if recap.strip():
            body += "\n\n---\n\n" + recap.rstrip() + "\n"
        return body


def build_prompt(
    *,
    mode: str,
    user_input: str,
    role_text: str = "",
    output_schema: str = "",
    repository_facts: str = "",
    mode_contract: str = "",
    raw: bool = False,
    skip_output_template: bool = False,
    trusted_metadata: str = "",
    review_instructions: str = "",
    honor_env: bool = False,
    style: str = STYLE_ADVISER,
    researcher_channels: str = "",
) -> BuiltPrompt:
    """Public entry used by shell and tests.

    When honor_env=True (CLI / shell wrappers), PORCH_RAW_PROMPT and
    PORCH_SKIP_OUTPUT_TEMPLATE are honored. Library/unit callers leave
    honor_env=False so ambient env cannot strip review layers accidentally.
    """
    if honor_env:
        if os.environ.get("PORCH_RAW_PROMPT"):
            raw = True
        if os.environ.get("PORCH_SKIP_OUTPUT_TEMPLATE"):
            skip_output_template = True
    if review_instructions and role_text:
        role_text = (
            role_text.rstrip()
            + "\n\nMODEL-SPECIFIC REVIEW INSTRUCTIONS:\n"
            + review_instructions
        )
    include_template: Optional[bool] = None
    if skip_output_template or mode in CODE_REVIEW_MODES:
        include_template = False
    builder = PromptBuilder(
        mode=mode,
        role_text=role_text,
        user_input=user_input,
        output_schema=output_schema,
        repository_facts=repository_facts,
        mode_contract=mode_contract,
        raw=raw,
        include_review_template=include_template,
        trusted_metadata=trusted_metadata,
        style=style,
        researcher_channels=researcher_channels,
    )
    return builder.build()


def assert_layer_order(built: BuiltPrompt) -> None:
    """Verify emitted non-empty layers follow LAYER_ORDER."""
    names = [L.name for L in built.layers if L.content.strip()]
    indices = [LAYER_ORDER.index(n) for n in names]
    if indices != sorted(indices):
        raise AssertionError(f"layer order violated: {names}")


def assert_raw_purity(built: BuiltPrompt) -> None:
    if not built.raw and built.mode not in ("raw", "delegate", "delegate-steerable"):
        return
    if built.mode in ("delegate", "delegate-steerable") and not built.raw:
        # Non-raw delegate still has no review wrap.
        pass
    for L in built.layers:
        if L.name in ("framework_policy", "role", "output_schema", "mode_contract"):
            if L.content.strip() and built.raw:
                raise AssertionError(f"raw prompt leaked layer {L.name}")


def _main() -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Porch layered prompt builder")
    ap.add_argument("--mode", required=True)
    ap.add_argument("--role-file", default="")
    ap.add_argument("--role-text", default="")
    user_source = ap.add_mutually_exclusive_group()
    user_source.add_argument("--user-file", default=None)
    user_source.add_argument("--user-text", default=None)
    ap.add_argument("--facts-file", default="")
    ap.add_argument("--schema-file", default="")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--skip-output-template", action="store_true")
    ap.add_argument("--style", default=STYLE_ADVISER, choices=REVIEW_STYLES,
                    help="adviser: role and five headings (default). assignment: "
                         "the caller's brief sets the role and the answer structure.")
    ap.add_argument("--researcher-channels-file", default="",
                    help="Ways the worker may start read-only researchers, one "
                         "'- ...' line each; without it the worker works alone.")
    ap.add_argument("--provenance", action="store_true")
    ap.add_argument("--check", action="store_true", help="Run isolation assertions")
    args = ap.parse_args()

    def _read(path: str, fallback: str) -> str:
        if path:
            return Path(path).read_text(encoding="utf-8")
        return fallback

    try:
        user = (Path(args.user_file).read_text(encoding="utf-8")
                if args.user_file is not None else args.user_text)
        if user is None:
            user = "" if sys.stdin.isatty() else sys.stdin.read()
    except (OSError, UnicodeError) as error:
        print(f"Error: cannot read user prompt: {error}", file=sys.stderr)
        return 5
    if not user.strip():
        print("Error: no user prompt provided", file=sys.stderr)
        return 5
    role = _read(args.role_file, args.role_text)
    facts = _read(args.facts_file, "")
    schema = _read(args.schema_file, "")
    try:
        channels = _read(args.researcher_channels_file, "")
        built = build_prompt(
            mode=args.mode,
            user_input=user,
            role_text=role,
            repository_facts=facts,
            output_schema=schema,
            raw=args.raw,
            skip_output_template=args.skip_output_template,
            honor_env=True,
            style=args.style,
            researcher_channels=channels,
        )
    except (OSError, UnicodeError, ValueError) as error:
        print(f"Error: cannot build review prompt: {error}", file=sys.stderr)
        return 5
    if args.check:
        assert_layer_order(built)
        assert_raw_purity(built)
        print("ok", file=sys.stderr)
    if args.provenance:
        print(json.dumps(built.provenance(), indent=2), file=sys.stderr)
    sys.stdout.write(built.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
