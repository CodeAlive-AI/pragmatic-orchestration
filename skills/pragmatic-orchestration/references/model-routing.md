# Model routing and economic execution

Choose a worker for the assignment and its consequences. A lower price per token
helps only if the whole job still ends in an acceptable result sooner or cheaper.

## Contents

- [Count the whole job](#count-the-whole-job)
- [Route by uncertainty](#route-by-uncertainty)
- [Harness for a named model](#harness-for-a-named-model)
- [Effort and limits](#effort-and-limits)
- [Escalate for a diagnosed reason](#escalate-for-a-diagnosed-reason)
- [Parallelism](#parallelism)
- [Context handed to a worker](#context-handed-to-a-worker)

## Count the whole job

The cost of a delegation is the worker's run plus everything around it: writing
the brief, transferring context, supervising, the likely retries, integrating the
result and verifying it. A frontier model's output tokens cost several times a
workhorse model's, so a long brief written by the orchestrator can cost more than
the work it describes.

- A task that takes longer to explain than to do stays with you. So does a
  short sequential standalone task you would finish in roughly 10-12 turns with
  no independent parts: the brief, the wait and the acceptance pass cost about as
  much as the work, and elapsed time roughly doubles. Delegate when the piece is
  larger, when independent parts can run in parallel, or when it would fill your
  context with output you do not need. In Coordinator Mode, batch small items
  into one assignment instead of doing them one by one yourself; this threshold
  does not change that mode's default of delegating implementation.
- A brief that points to files and artifacts is cheaper than one that retells
  them.
- Subscription quota is a real budget. Check it with `"$PORCH" quota` when it
  affects the choice, and do not treat an expired promotion or a cached price as
  proof that a run is free.
- Report savings only when measured data supports them.

## Route by uncertainty

| Assignment | Starting point |
| --- | --- |
| Mechanical edits, extraction, bounded searches, straightforward checks | A small or fast model with explicit output criteria |
| Typical implementation, debugging, test writing, integration | A workhorse model with a coherent task and its context |
| Ambiguous architecture, cross-system invariants, hard failure analysis | Strong reasoning, on a bounded question grounded in evidence |
| Independent acceptance evidence | A reviewer or runner suited to the specific remaining risk, preferably from another model family |

The actual mapping comes from the configured profiles (`"$PORCH" --list-agents`),
the harness's built-in workers, the user's stated preferences, and results
observed in this project. A profile label does not prove the model version,
effort, capability or price behind it; inspect the mapping before claiming that a
specific model ran.

The user's routing decisions win over an estimated saving. In Coordinator Mode
the executor chosen on entry is the default for implementation; use other models
for research and review within the user's preferences, and change the primary
executor only with a reason you can state.

## Harness for a named model

When the user names a model without naming a harness, run it through the
model's own harness. Pick the profile from `"$PORCH" --list-agents` whose
`model` is that model; never pick a profile only because its label sounds close.

| Model family | Default harness |
| --- | --- |
| GPT (Sol, Terra, Luna, Astra) | Codex (`codex-cli`) |
| Claude (Fable, Opus, Sonnet, Haiku) | Claude Code (`claude-code`) |
| Gemini 4 (Argon, Hydrogen, Neon, Carbon), Gemini 3.x | Antigravity (`antigravity-cli`, `agy-*`) |
| Grok | Grok Build (`grok-build`) |
| Kimi, GLM, DeepSeek, Qwen, MiniMax, Muse Spark and other open or third-party models | OpenCode (`opencode-go-*`) |
| SWE-2.1, SWE-2, SWE-2 Flash | Devin (`devin-cli`): `devin`, `devin-swe-2-1-ultrafast`, `devin-swe-2` |

Devin is the default harness only for the SWE family: SWE-2.1 (free on the
owner's paid plan through 2026-12-08, then discounted) and SWE-2 / SWE-2 Flash
(free on the owner's plan). Its other models (`devin-gemini-4-*`, or any model chosen with
`DEVIN_MODEL`) are billed or limited and run only when the user names Devin
for them, for example "run Argon through Devin". "Run Hydrogen" means
`agy-gemini-4-hydrogen`, not Devin. If the native harness has no profile for the
model, say so and ask rather than switching harness silently.

## Effort and limits

Set reasoning effort through the profile or a supported override, not through
prose such as "think harder". Maximum effort is rarely the economical choice for
an executor with a clear brief. A budget written into a prompt is guidance, not a
limit: Porch imposes no deadline or spending cap, so watch long runs and cancel
them explicitly. An orchestrator's effort raises the price of every one of its
turns, routine supervision included: raise it for the design and acceptance
decisions that need it, not for the whole session.

## Escalate for a diagnosed reason

When a worker misses a requirement, find the cause before changing anything:

| Cause | Response |
| --- | --- |
| The brief was incomplete or ambiguous | One focused correction to the same worker |
| Missing access or evidence | Supply it, or report the blocker |
| The assignment was too large or cut across owners | Split or re-cut the assignment |
| The worker cannot hold the invariant (repeated failure on the same point, supervision cost growing) | A stronger model or higher effort for this assignment |

Do not run a chain of cheap retries, and do not accept a weaker result because it
was cheaper. Escalating one assignment does not change the default for the rest.

A less capable or unfamiliar worker gets the stronger protocol from
[delegate.md](delegate.md#delegating-to-a-less-capable-model): bounded plan,
named pitfalls, deviation record, inspection of the actual work. It is mandatory
for the user-designated pairs listed there.

## Parallelism

Run work in parallel when the time saved exceeds the cost of coordinating and
integrating it:

- Independent investigations and separately owned changes parallelize well.
  Dependent steps stay in sequence.
- Each parallel writer gets its own workspace under the project's rules. Two
  writers never share files.
- Give a shared contract (an interface, a schema, a migration) one owner, and
  land it before the work that depends on it. Several workers redesigning the
  same interface independently is the usual way parallel work is lost.
- Integrate finished work in dependency order, and bring workspaces that are
  still running onto a contract once it lands. Parts that each pass their own
  checks have not been run together: have the required checks run on the
  combined revision before calling the whole accepted.
- Choose a finite number of workers for the machine and the quotas at hand, and
  ask from time to time whether everything that could run in parallel is running.
- Additional reviewers must answer different questions. Repeating a whole-repository
  review, or letting workers start their own workers, multiplies cost without
  adding evidence.

## Context handed to a worker

Prefer a fresh worker with a self-contained brief when the conversation history is
not needed: links to the relevant artifacts, the decision already taken, the exact
scope. Keep a continuing worker when its accumulated context is still useful;
after changing the task or the model, restate the decisions and the remaining
work explicitly rather than assuming they carried over.

Give reviewers the requirements and the raw artifacts, not the verdict you hope
for or another model's confidence.
