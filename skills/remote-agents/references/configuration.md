# Configuration

Every script resolves host identity from `config.json` in the skill directory
(gitignored; copy `config.example.json`). `REMOTE_AGENTS_CONFIG` overrides the
file location; `REMOTE_AGENTS_HOST` and `--host <id>` select the host. A host is
resolved by flag, then env, then `defaultHost`, then "only defined host".

## Schema

```json
{
  "defaultHost": "windows-dev",
  "hosts": {
    "<id>": {
      "os": "linux | windows",
      "sshAlias": "<ssh config Host alias>",
      "workRoot": "<closed work root; see work-storage.md>",
      "aws": {
        "profile": "<AWS CLI profile allowed to control this instance>",
        "region": "<region>",
        "instanceId": "i-...",
        "configFile": null
      },
      "desktop": { ... },   // windows only
      "bridge":  { ... },   // windows + WireGuard/SMB mount only
      "qa":      { ... }    // windows visual-QA worker only
    }
  }
}
```

`aws` may be omitted on hosts you reach only by SSH (no lifecycle commands).
`aws.configFile` selects a dedicated non-shared `AWS_CONFIG_FILE` when the
shared `~/.aws/config` carries a conflicting or obsolete stanza.

### `desktop` (windows)

Per-dev-OS behavior is documented in [local-platforms.md](local-platforms.md).

| Field | Meaning |
|---|---|
| `rdpBookmark` | Saved Windows App bookmark name; its credential is read from the macOS Keychain at start |
| `rdpAppBundleId` | RDP client bundle id for `desktop-open` (default `com.microsoft.rdc.macos`, used by Microsoft Remote Desktop and Windows App on macOS) |
| `clientBinary` | FreeRDP binary override for the headless session holder (auto-detected per dev OS) |
| `passwordCommand` | Shell command printing the RDP password — the credential path on Linux, escape hatch elsewhere |
| `passwordEnv` | Environment variable holding the RDP password (checked before passwordCommand) |
| `credTarget` | Windows Credential Manager target override (default tries `TERMSRV/<addr>` with and without port) |
| `openCommand` | Custom interactive-client launcher for `desktop-open`; `{addr}`/`{bookmark}` placeholders |
| `windowsUser` | Interactive account (default `Administrator`) |
| `localPort` / `remotePort` | SSM port-forward pair (default `13389`/`3389`) |
| `viewerLocalPort` / `viewerRemotePort` | Observer tunnel pair (default `16080`/`16081`) |
| `size` | Headless desktop size, e.g. `1600x1000` |
| `helperDir` | Work-root subdir for probe/observer (`desktop-helpers`) |
| `remotePython` | `pythonw.exe` with Pillow, used by probe/observer |
| `taskPrefix` | Scheduled-task prefix (default `RemoteAgents-Desktop`) |

### `bridge` (windows + SMB mount)

| Field | Meaning |
|---|---|
| `shareName` | SMB share exported by the host (e.g. `Work`) |
| `subnetPrefix` | WireGuard /24 prefix; `.1` = Windows, `.2` = Mac |
| `listenPort` | UDP port the security group allows (e.g. `51820`) |
| `mountRoot` | Local mount root; mount lands at `<mountRoot>/<host-id>` (ignored on Windows — UNC access) |
| `launchdLabel` | Fixed tunnel service label (macOS launchd) |
| `serviceName` | Linux systemd unit name (default `remote-agents-work-bridge-<id>`) |
| `appSupportDir` | Service install dir (e.g. `/Library/Application Support/RemoteAgents-WorkBridge`) |
| `sudoersFile` | `/etc/sudoers.d/<name>` for password-free kickstart/kill |
| `logFile` | Service log path (default `/var/log/remote-agents-work-bridge.log`) |
| `stateDir` | Override for `.bridge-state` (default: inside the skill, gitignored) |

### `qa` (windows visual QA)

| Field | Meaning |
|---|---|
| `agentBin` | Agent CLI path on the host (tested: Grok) |
| `agentModel` | Model id passed to the CLI |
| `python` | Interpreter for `run-visual-qa.py` |
| `mcpServer` | QA MCP server script path on the host |
| `mcpName` | MCP server name / tool prefix (`windows-qa`) |
| `artifactDir` | Screenshot artifact directory the driver writes |

## SSH-over-SSM config

Each `sshAlias` is a `~/.ssh/config` entry whose `ProxyCommand` runs
`aws ssm start-session --document-name AWS-StartSSHSession` — see
`examples/ssh-config-ssm`. One dedicated ed25519 key per host; `IdentitiesOnly
yes`; `ControlMaster auto` + `ControlPersist` amortize SSM session setup.

## Helper CLI

```bash
scripts/lib/config.py hosts                    # defined host ids
scripts/lib/config.py resolve [--host id]      # effective host id
scripts/lib/config.py host [id]                # host object as JSON
scripts/lib/config.py field <dotted.path>      # single field
scripts/lib/config.py aws-env [--host id]      # AWS_* exports for the host
```
