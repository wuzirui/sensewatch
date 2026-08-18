# Setup

## 1. Prerequisites

- macOS with Keychain and the `security` command
- Python 3.11 or newer
- `git`, `curl`, and `tar`
- a SenseCore subscription, resource group, zone, workspace, and AccessKey pair
- an AFS volume and container image for ACP submission

The HMAC helper reads the AccessKey pair from macOS Keychain. The `sco` CLI
also needs the same pair in its own local profile.

## 2. Install a compatible `sco` CLI

### Current official installer

The current installer is served by SenseCore and supports macOS Intel and
Apple Silicon:

```bash
curl --proto '=https' --tlsv1.2 -sSf \
  https://sco.sensecore.cn/registry/sco/install.sh | sh
export PATH="$HOME/.sco/bin:$PATH"
sco version
```

The installer may update your shell profile. Open a fresh terminal before
continuing if `sco` is not found.

Install the command components used by this package:

```bash
sco components install acp
sco components install aec2
sco components install eip
```

Workspace commands are included in supported `sco` installations. Verify all
required surfaces without changing remote state:

```bash
sco ws instances list
sco aec2 clusters --help
sco acp jobs --help
sco eip dnat --help
```

If an installed component is old, update installed components with:

```bash
sco components upgrade
```

### Separately supplied v1.2.0 compatibility bundle

The current official release may perform stricter subscription/IAM validation
or expose a different command surface than the ACP/WS/AEC2 workflow documented
here. If that occurs, use the separately supplied
`sco-v1.2.0-darwin-arm64-compat-20260818.tar.gz` on an Apple Silicon Mac. The
archive is deliberately not stored in this Git repository.

```bash
tar -xzf sco-v1.2.0-darwin-arm64-compat-20260818.tar.gz
cd sco-v1.2.0-darwin-arm64-compat-20260818
shasum -a 256 -c SHA256SUMS
./install.sh
export PATH="$HOME/.sco/bin:$PATH"
sco version
```

The expected archive SHA-256 is
`85dc9efc93372176c32afd517793874d0acd6cfaecd3a6bdf60fbbd33f556241`.
The installer refuses to overwrite an existing binary; `./install.sh --force`
retains a timestamped backup before replacing it.

This compatibility binary already embeds the command surfaces used here. Do
not run `sco components install` or `sco components upgrade` on the pinned
installation unless you intentionally want to replace it. Its historical
component-registry URL may return HTTP 404 even though the embedded commands
continue to work.

## 3. Store credentials in Keychain

Replace the placeholders locally. Do not paste real values into a tracked
script or documentation file.

```bash
security add-generic-password -U \
  -a "$USER" -s sensecore_access_key_id -w "<ACCESS_KEY_ID>"
security add-generic-password -U \
  -a "$USER" -s sensecore_access_key_secret -w "<ACCESS_KEY_SECRET>"
```

Verify presence without printing either value:

```bash
security find-generic-password -a "$USER" -s sensecore_access_key_id >/dev/null
security find-generic-password -a "$USER" -s sensecore_access_key_secret >/dev/null
```

The Keychain account must match the current macOS account returned by
`python3 -c 'import getpass; print(getpass.getuser())'`.

## 4. Configure `sco`

Create and activate a profile if one does not already exist:

```bash
sco config profiles list
sco config profiles create default
sco config profiles activate default
```

Read the Keychain values into temporary shell variables, configure `sco`, then
unset the variables. These commands do not print the values:

```bash
SENSECORE_AK_ID=$(security find-generic-password \
  -a "$USER" -s sensecore_access_key_id -w)
SENSECORE_AK_SECRET=$(security find-generic-password \
  -a "$USER" -s sensecore_access_key_secret -w)

sco config set access_key_id "$SENSECORE_AK_ID"
sco config set access_key_secret "$SENSECORE_AK_SECRET"
sco config set zone "<ZONE>"
sco config set subscription "<SUBSCRIPTION_UUID>"
sco config set resource_group "<RESOURCE_GROUP>"

unset SENSECORE_AK_ID SENSECORE_AK_SECRET
```

If `default` already exists, skip its create command. Confirm connectivity with
a read-only call:

```bash
sco ws instances list
```

## 5. Install this package

### pipx from GitHub

```bash
pipx install "git+https://github.com/wuzirui/sensewatch.git@agent/acp-cli"
```

### Virtual environment from a checkout

```bash
git clone --branch agent/acp-cli --single-branch \
  https://github.com/wuzirui/sensewatch.git sensecore-acp-cli
cd sensecore-acp-cli
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
```

Verify all entry points:

```bash
acp --help
cci --help
htpc-proxy --help
```

## 6. Create local configuration

From a checkout:

```bash
mkdir -p ~/.config/dreamdojo
cp config.example.toml ~/.config/dreamdojo/acp.toml
chmod 600 ~/.config/dreamdojo/acp.toml
${EDITOR:-vi} ~/.config/dreamdojo/acp.toml
```

For a pipx-only install, download `config.example.toml` from the same branch or
create the file from the schema in the configuration reference. Populate the
workspace, image, AFS mount, IAM identity, and quota fields. DNAT fields are
needed only for `cci doctor`.

See the [configuration reference](configuration.md) for every field.

## 7. Read-only verification

Run these before any submit or lifecycle command:

```bash
acp whoami
acp refresh --workspaces
acp quota
acp list --all --since 1d
cci list
```

Then verify planning without creating a job:

```bash
acp submit \
  --name g1-setup-check \
  --gpus 1 \
  --command "echo setup-check" \
  --dry-run
```

## Updating and uninstalling

```bash
pipx upgrade sensecore-acp-cli
pipx uninstall sensecore-acp-cli
```

A virtual-environment install can be updated with `git pull` followed by
`python3 -m pip install -e .`. Uninstalling the package does not delete
`~/.config/dreamdojo/acp.toml`, `~/.cache/dreamdojo/acp/`, Keychain entries, or
the `sco` profile.
