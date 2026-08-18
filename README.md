# SenseCore ACP + CCI CLI

Standalone operator commands for SenseCore training jobs and CCI debug
containers. This code lives on the orphan `agent/acp-cli` branch of
`wuzirui/sensewatch`; it is intentionally independent from the macOS
SenseWatch application on `main`.

The package installs three commands:

- `acp` plans, submits, lists, stops, switches, and inspects ACP jobs.
- `cci` lists and manages existing CCI apps and diagnoses SSH/DNAT access.
- `htpc-proxy` launches an isolated Chrome profile through an SSH SOCKS5
  tunnel.

The bundled DreamDojo defaults current on 2026-08-05 use `p18-eacv` with a
64-GPU regular quota cap. The retired
`p1-video-world-model-for-robot-learning` workspace is removed from discovery
and quota fallbacks and is rejected as a new submission target.

## Quick start

Prerequisites are macOS, Python 3.11+, SenseCore credentials, and a compatible
`sco` CLI. Complete the [setup guide](docs/setup.md) before running commands.
The repository does not redistribute SCO. A separately supplied v1.2.0 offline
bundle can be used when the current official release rejects the required
ACP/WS/AEC2 workflow.

Install directly from this branch with pipx:

```bash
pipx install "git+https://github.com/wuzirui/sensewatch.git@agent/acp-cli"
```

Create the local configuration:

```bash
mkdir -p ~/.config/dreamdojo
cp config.example.toml ~/.config/dreamdojo/acp.toml
${EDITOR:-vi} ~/.config/dreamdojo/acp.toml
```

Verify the read-only paths first:

```bash
acp whoami
acp refresh --workspaces
acp quota
acp list --all --since 1d
cci list
```

Preview a submission without creating a job:

```bash
acp submit \
  --name g8-example \
  --gpus 8 \
  --command "bash /mnt/afs/PROJECT/launch.sh" \
  --dry-run
```

## Documentation

- [Installation and credential setup](docs/setup.md)
- [Configuration reference](docs/configuration.md)
- [ACP command reference](docs/acp.md)
- [CCI command reference](docs/cci.md)
- [Isolated Chrome proxy](docs/htpc-proxy.md)
- [Troubleshooting](docs/troubleshooting.md)

## Safety model

- `acp stop` accepts one canonical `pt-...` job ID; it never accepts bulk name
  patterns and suspends rather than deletes.
- `acp switch` creates the replacement first and stops the source only after
  replacement submission succeeds.
- `acp submit` rejects referenced startup scripts containing `pip install`
  unless `--force` is explicitly supplied.
- `cci doctor` reads DNAT rules but never creates, deletes, or rewrites them.
- Credentials remain in macOS Keychain and the local `sco` profile. Do not put
  AccessKeys, tokens, or cookies in this repository or its TOML file.

Run the test suite with:

```bash
python3 -m pip install -e ".[dev]"
pytest -q
```

No software license is declared on this branch. All rights remain with the
repository owner until a license is explicitly added.
