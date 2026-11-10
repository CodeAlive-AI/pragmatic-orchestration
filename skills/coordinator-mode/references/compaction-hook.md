# Automatic reminder after compaction

The capsule in the summary and the journal on disk already carry the mode across
a compaction. The hook makes that independent of how well the summary was
written: after every compaction the harness itself puts the capsule back into
context.

It is worth installing for work that runs for days. Claude Code restores invoked
skills after compaction only within a budget (5,000 tokens per skill, 25,000 in
total, oldest dropped first), and a mode entered last week is the oldest skill in
the session.

## What it does

`scripts/coordinator_mode.py hook` reads the hook event from stdin and looks up
the event's `session_id` among the sessions recorded by `coordinator_mode.py on`.

| Event | Output when the session is a coordinator |
| --- | --- |
| `SessionStart` (after compaction, resume) | The capsule and the instruction to reread the skill and the journal. The harness adds it to context. |
| `PreCompact` (Claude Code) | An instruction for the summary to keep the capsule verbatim. |

For every other session it prints nothing, so it can be registered globally:
ordinary sessions and delegated workers are unaffected. Events from subagents
(`agent_id` set) are ignored. The hook does no task work, starts no workers and
schedules nothing. On a malformed event or a corrupt record it prints nothing,
explains on stderr and exits 1, which a harness treats as a non-blocking error.

## Installing

This changes the user's harness configuration. Do it only when the user agrees,
and merge the entries into the existing hooks instead of replacing them (use the
harness's hook-management tooling when there is one). Use absolute paths.

Claude Code, `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "python3 /ABSOLUTE/coordinator-mode/scripts/coordinator_mode.py hook", "timeout": 5 } ] }
    ],
    "PreCompact": [
      { "hooks": [ { "type": "command", "command": "python3 /ABSOLUTE/coordinator-mode/scripts/coordinator_mode.py hook", "timeout": 5 } ] }
    ]
  }
}
```

Codex, `~/.codex/hooks.json`: the same `SessionStart` entry. Codex runs a new or
edited hook only after the user reviews and trusts it in `/hooks`.

## Checking

With the mode on, this prints the capsule (use `$CODEX_THREAD_ID` in Codex):

```bash
python3 /ABSOLUTE/coordinator-mode/scripts/coordinator_mode.py status
printf '{"hook_event_name":"SessionStart","session_id":"%s"}' "$CLAUDE_CODE_SESSION_ID" \
  | python3 /ABSOLUTE/coordinator-mode/scripts/coordinator_mode.py hook
```

## Limits

- The record is keyed by the session id the harness gives to shell commands
  (`CLAUDE_CODE_SESSION_ID`, `CODEX_THREAD_ID`) and matched against `session_id`
  in the hook event. For Codex the equality of the two is assumed, not verified.
  If no reminder appears after a compaction there, the ids differ and the hook
  adds nothing; the capsule and the journal still carry the mode.
- A cleared or forked session gets a new id. Turn the mode on again there with
  the same `--journal`.
- The harness caps injected hook output (about 10,000 characters in Claude Code);
  the capsule is far below that.

## Removing

Delete the two entries. `coordinator_mode.py off` removes the session record;
journals are never deleted by the script.
