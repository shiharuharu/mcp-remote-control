# Harness scripts

Canonical green path: **`./ci.sh`** (from repo root).

See also the [Harness / CI](../../README.md#harness--ci) section in the package README.

| Script | Role |
|--------|------|
| `ci.sh` | Full default gate (no integration) |
| `smoke_local.sh` | CLI local: endpoint / exec / fs + in-process screen |
| `smoke_mcp.sh` | 7 tools (host 5 + console + config) + MCPServer local calls |
| `local_screen_smoke.py` | Process-local screen open→send→close Core loop |
| `smoke_winrm.sh` / `smoke_winrm.py` | WinRM open/exec/timeout/fs/ps (needs WinRM profile; `--mock` offline) |
| `docker_shell_matrix.sh` | shell dialect matrix (bash/zsh/dash/**busybox**) |

```bash
export MRC_HOME="$(pwd)/tests/fixtures/config"
./scripts/harness/ci.sh
```

### Docker shell matrix

Requires Docker daemon. **Not** part of default `ci.sh`.

```bash
# full matrix (busybox + bash required; zsh/debian may skip on pull fail)
MRC_DOCKER=1 ./scripts/harness/docker_shell_matrix.sh

# only busybox cell
MRC_DOCKER=1 MATRIX_ONLY=busybox ./scripts/harness/docker_shell_matrix.sh
```

Validates silent-cwd probe strings run under target `sh`/`bash`/`zsh` and that
busybox probes never contain `fc` / `set +o history` / `history -d`.

Optional real hosts (never part of `ci.sh`):

```bash
export MRC_INTEGRATION=1
pytest -q tests/integration
```

### WinRM smoke (needs WinRM profile)

Not part of default `ci.sh`. Single-process Python (ps registry is process-local).

```bash
# offline mock (fixtures lab-win; asserts timeout=1 wall < 2s vs Start-Sleep 3)
./scripts/harness/smoke_winrm.sh --mock

# true machine
MRC_HOME=~/.config/mcp-remote-control ./scripts/harness/smoke_winrm.sh --profile buildbox-210-winrm
```

Screen registries are process-local; do not chain `mcp-remote-control-cli screen` across processes.
