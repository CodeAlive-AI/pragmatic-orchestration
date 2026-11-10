#!/usr/bin/env python3
"""Coordinator Mode: remember per session where the skill and journal are, and
remind the session after compaction (hook). Standard library only."""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile

SELF = os.path.abspath(__file__)
SCRIPT = os.path.basename(SELF)
CAPSULE = """COORDINATOR MODE ACTIVE (this session only; workers do not inherit it)
Skill: {skill}
Journal: {journal}
Executors: {executors}
After any compaction, reset or resume: reread the skill and the journal before anything else."""
SESSION_START = """COORDINATOR MODE ACTIVE (this session only; workers do not inherit it)
Skill: {skill}
Journal: {journal}
Executors: {executors}
Your context was just compacted, cleared or resumed. Before any other action, reread the skill file and the journal in full, then check the workers the journal lists before starting or repeating work. You plan, design verification, delegate and accept; implementation normally stays with executors."""
PRE_COMPACT = """This session is in Coordinator Mode. Keep the following five lines verbatim at the top of the summary:
""" + CAPSULE + """
Also keep: the user's current goal and latest instructions, decisions and approvals not yet written to the journal, and the ids of workers that are still running."""
TEMPLATE = """# Coordinator journal: {title}

{capsule}
Project: {project}
Started: {date}

## Goal and constraints

## Decisions and approvals

## Invariants

## Verification plan

## Work items

## Workers

## Accepted results and evidence

## Open questions and blockers

## Next action

## Log
"""


def die(msg, code=2):
    sys.stderr.write(msg.rstrip("\n") + "\n")
    sys.exit(code)


def state_root():
    if os.environ.get("COORDINATOR_MODE_HOME"):
        return os.environ["COORDINATOR_MODE_HOME"]
    if os.environ.get("XDG_STATE_HOME"):
        return os.path.join(os.environ["XDG_STATE_HOME"], "coordinator-mode")
    return os.path.join(os.path.expanduser("~"), ".local", "state", "coordinator-mode")


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pointer_path(session):
    return os.path.join(state_root(), "sessions", sha(session) + ".json")


def load_pointer(session):
    """Return the pointer dict, None if absent; ValueError if unreadable."""
    path = pointer_path(session)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise ValueError("cannot read session pointer %s (%s)" % (path, e))
    if not isinstance(data, dict) or not all(
            isinstance(data.get(k), str) for k in ("skill", "journal", "executors")):
        raise ValueError("session pointer %s is corrupt" % path)
    return data


def write_atomic(path, text):
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def resolve_session(arg):
    if arg:
        return arg
    claude = os.environ.get("CLAUDE_CODE_SESSION_ID")
    codex = os.environ.get("CODEX_THREAD_ID")
    if claude and codex and claude != codex:
        die("Both CLAUDE_CODE_SESSION_ID (%s) and CODEX_THREAD_ID (%s) are set and differ.\n"
            "Rerun with the id of this session: %s <command> --session <id>" % (claude, codex, SCRIPT))
    if claude or codex:
        return claude or codex
    die("No session id: neither CLAUDE_CODE_SESSION_ID nor CODEX_THREAD_ID is set.\n"
        "Rerun with the id of this session: %s <command> --session <id>" % SCRIPT)


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48].strip("-")


def cmd_on(args):
    session = resolve_session(args.session)
    project = None
    if args.project:
        project = os.path.abspath(os.path.expanduser(args.project))
        if not os.path.isdir(project):
            die("--project %s is not an existing directory.\nRerun with an existing directory: "
                "%s on --project <dir> --task <slug>" % (args.project, SCRIPT))
    slug = None
    if args.task is not None:
        slug = slugify(args.task)
        if not slug:
            die("--task %r has no letters or digits.\nRerun with a slug such as: "
                "%s on --project <dir> --task my-task" % (args.task, SCRIPT))
    if args.journal:
        journal = os.path.expanduser(args.journal)
        if not os.path.isabs(journal):
            die("--journal must be an absolute path, got %s.\nRerun with: %s on --journal %s"
                % (args.journal, SCRIPT, os.path.abspath(journal)))
    elif project and slug:
        folder = "%s-%s" % (os.path.basename(project) or "root", sha(project)[:8])
        name = "%s-%s.md" % (datetime.date.today().isoformat(), slug)
        journal = os.path.join(state_root(), "journals", folder, name)
    else:
        die("Say where the journal is.\nRun either: %s on --project <dir> --task <slug>\n"
            "or: %s on --journal <absolute path>" % (SCRIPT, SCRIPT))
    skill = os.path.abspath(os.path.expanduser(args.skill)) if args.skill else os.path.join(
        os.path.dirname(os.path.dirname(SELF)), "SKILL.md")
    data = {"session": session, "skill": skill, "journal": journal,
            "executors": args.executors, "activated_at": datetime.datetime.now().astimezone().isoformat()}
    if not os.path.exists(journal):
        title = args.title or slug or os.path.splitext(os.path.basename(journal))[0]
        capsule = CAPSULE.format(skill=skill, journal=journal, executors=args.executors)
        write_atomic(journal, TEMPLATE.format(
            title=title, capsule=capsule, project=project or "not recorded",
            date=datetime.date.today().isoformat()))
    write_atomic(pointer_path(session), json.dumps(data, indent=2) + "\n")
    print(CAPSULE.format(**data))


def cmd_off(args):
    session = resolve_session(args.session)
    try:
        pointer = load_pointer(session)
    except ValueError:
        pointer = {"journal": "unknown (pointer was unreadable)"}
    if pointer is None:
        print("Coordinator Mode was not active for this session.")
        return
    os.remove(pointer_path(session))
    print("Coordinator Mode off for this session. Journal kept: %s" % pointer["journal"])


def cmd_status(args):
    session = resolve_session(args.session)
    try:
        pointer = load_pointer(session)
    except ValueError as e:
        die("%s\nRemove it with: %s off --session %s" % (e, SCRIPT, session), 1)
    print(CAPSULE.format(**pointer) if pointer else "Coordinator Mode is not active for this session.")


def cmd_hook(_args):
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError("expected a JSON object")
        agent_id, session = event.get("agent_id"), event.get("session_id")
        if (isinstance(agent_id, str) and agent_id) or not isinstance(session, str) or not session:
            return
        name = event.get("hook_event_name")
        if name not in ("PreCompact", "SessionStart"):
            return
        pointer = load_pointer(session)
        if pointer is None:
            return
        text = (PRE_COMPACT if name == "PreCompact" else SESSION_START).format(**pointer)
    except ValueError as e:
        die("coordinator-mode hook: %s" % e, 1)
    print(text)


def main(argv=None):
    parser = argparse.ArgumentParser(prog=SCRIPT, description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    on = sub.add_parser("on", help="activate for this session and print the capsule")
    on.add_argument("--journal")
    on.add_argument("--project")
    on.add_argument("--task")
    on.add_argument("--executors", default="not chosen yet")
    on.add_argument("--title")
    on.add_argument("--skill")
    for name, helptext in (("off", "deactivate"), ("status", "show state")):
        sub.add_parser(name, help=helptext).add_argument("--session")
    on.add_argument("--session")
    sub.add_parser("hook", help="hook entry: JSON event on stdin")
    args = parser.parse_args(argv)
    {"on": cmd_on, "off": cmd_off, "status": cmd_status, "hook": cmd_hook}[args.cmd](args)


if __name__ == "__main__":
    main()
