# SenseCore ACP + CCI CLI

Standalone operator commands for SenseCore training jobs and CCI debug
containers. This code lives on the orphan `agent/acp-cli` branch of
`wuzirui/sensewatch`; it is intentionally independent from the macOS
SenseWatch application on `main`.

The package installs two commands:

- `acp` plans, submits, lists, stops, switches, and inspects ACP jobs.
- `cci` lists and manages existing CCI apps and diagnoses SSH/DNAT access.

## Quick start

Prerequisites are macOS, Python 3.11+, SenseCore credentials, and the official
`sco` CLI. Complete the [setup guide](docs/setup.md) before running commands.

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
