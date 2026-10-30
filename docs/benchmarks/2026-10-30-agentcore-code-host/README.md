# Complete the AgentCore Codex installation

A real development task on native ARM AgentCore exposed a missing code-mode
host: version, subscription login and model communication worked, while both
tool attempts failed before executing code. The worker reported the blocker
without changing files or claiming tests. The image recipe now installs the
matching officially hashed companion binary beside Codex. Its help entrypoint
is checked during build. The ARM image built and its binary smoke passed with
all Linux capabilities dropped; the same official asset enabled a subsequent
Terra/medium worker to edit and test actual source on AgentCore.

Bootstrap had also been reported idle while login and dependency work were
active. The guest now recognizes a caller-owned bootstrap marker. Nine fake
loopback/real Porch tests passed, including busy-before-first-worker and idle
after removal. Independent provider cleanup is still a separate obligation.

`results.json` records the assets, image and exact source hashes. This is a
local source/image qualification, not a plugin release or general cloud pool.
