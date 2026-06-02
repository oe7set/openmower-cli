# OpenMower CLI

OpenMower CLI is a Python command line tool to manage and interact with the OpenMower software stack. It provides commands for day‑to‑day operations (pull, start, stop, restart, logs, shell/exec), legacy utilities, and a self‑update mechanism for the distributed zipapp binary.


## Features
- Manage OpenMower Docker Compose stack:
  - pull, start, stop, restart, status, logs
  - exec/shell into services
- Legacy commands for hardware/firmware workflows (subject to change)
- Self-update from GitHub Releases when using the zipapp distribution
- Rich, user-friendly console output

## Requirements
- Python 3.10+
- Docker with Docker Compose plugin (for stack management commands)
- Access to the OpenMower compose file (default: `/opt/stacks/openmower/compose.yaml`)

## Installation
Prebuilt zipapp artifacts may be published on GitHub Releases for convenient distribution. Download the `openmower` artifact from the repo releases page, make it executable and run it directly:
```bash
chmod +x ./openmower
./openmower --help
```
You can then keep it up to date via the built-in self-update command described below.

## Usage
Once installed, the main entry point is the `openmower` command.

```bash
openmower --help
openmower --version
```

The CLI selects command groups based on the environment variable `HARDWARE_PLATFORM`:
- If `HARDWARE_PLATFORM` is set to `2` commands under the newer `openmower` group are enabled.
- Otherwise, legacy commands are used. If the variable is not set, a warning is printed and legacy commands are used by default.

### Common stack commands
These commands manage the Docker Compose stack referenced by `/opt/stacks/openmower/compose.yaml`.

- Pull images:
  ```bash
  openmower pull
  ```
- Start services (detached):
  ```bash
  openmower start
  ```
- Stop services:
  ```bash
  openmower stop
  ```
- Restart services:
  ```bash
  openmower restart
  ```
- Status (compose ps):
  ```bash
  openmower status
  ```
- Logs (follow, last 100 lines by default):
  ```bash
  openmower logs
  # or specify services
  openmower logs openmower another-service
  ```
- Exec/Shell into a service (defaults to service `openmower` if none provided):
  ```bash
  # Interactive shell
  openmower shell

  # Shell into specific service
  openmower shell ros

  # Execute a command inside a service
  openmower exec ros bash -lc 'echo Hello && env | sort'
  ```

Notes:
- The CLI uses `/usr/bin/docker compose -f /opt/stacks/openmower/compose.yaml ...` under the hood.
- Ensure your user can run Docker commands (e.g., part of the `docker` group) or run with appropriate privileges.

### Version bundles

A *version bundle* pins the versions of the high-level stack components — the
`open_mower_ros` image, the `OpenMowerApp` image, and (optionally) a firmware
release — together with a short description. You can save the versions you are
currently running as a bundle (a backup), and switch between bundles later.
Each component may come from a different repository/registry, so bundles work
across forks.

Bundles come from two sources, merged into one list:
- **Local** bundles you saved (JSON files under `~/.config/openmower-cli/bundles/`),
  including automatic backups created before each switch.
- A curated **remote catalog** (configurable via `OPENMOWER_BUNDLE_CATALOG_URL`).
  When offline, the catalog is simply skipped.

```bash
# List local bundles, backups and the catalog
openmower version list

# Save the currently configured versions as a bundle
openmower version save my-stable -d "Known-good setup"
# ...or pin specific versions explicitly
openmower version save heatmap -d "Heatmap + scheduler" \
  --ros-tag v1.2.25-dev --app-tag v0.4.34-dev \
  --fw-repo oe7set/fw-openmower-v2 --fw-tag v0.0.8-dev

# Show one bundle in detail
openmower version show heatmap

# Switch to a bundle (writes the compose .env and redeploys the stack).
# Omit the name to pick interactively from the list.
openmower version apply heatmap
openmower version apply                       # interactive selection
openmower version apply heatmap --with-firmware   # also flash the pinned firmware
openmower version apply heatmap -y --no-firmware  # non-interactive, ROS/App only

# Remove a saved bundle
openmower version delete heatmap
```

Notes:
- **Switching backs up the current versions automatically** (as a `backup-<timestamp>`
  bundle) before applying, so you can always switch back. The newest 10 backups are kept.
- **Switching requires a one-time `migrate-compose` per host.** Bundles set image
  versions through `.env` variables, so the `compose.yaml` image lines must
  reference them first. `migrate-compose` does this by service name — it
  parameterizes the `open_mower_ros` service and the web-app service (the first of
  `app`, `openmower-app`, `OpenMowerApp` present in the file), keeping whatever
  image and tag they currently use as the defaults. It also reverts any other
  service that a previous run parameterized by mistake.
  ```bash
  openmower version migrate-compose      # writes a .bak backup, then rewrites the image lines
  ```
  `apply` detects a non-migrated compose file and offers to run this for you.
- **Tag conventions:** ROS/App **image** tags follow the GHCR images, which drop the
  leading `v` (e.g. `1.2.26-dev`, `edge`). Firmware tags are GitHub release tags and
  keep the `v` (e.g. `v0.0.8-dev`).
- **Firmware** is flashed separately (the firmware is not a Docker image). Bundles
  pin a firmware repo+tag; switching only flashes it when you pass `--with-firmware`
  (or confirm the prompt). The firmware version of a bundle saved from the current
  state is captured only if you flashed firmware through this CLI (it records the
  repo+tag in `~/.config/openmower-cli/last_firmware.json`) or if you pass
  `--fw-repo`/`--fw-tag` explicitly.

### Self-update (zipapp distribution)
If you run the zipapp build (a single-file `openmower` executable), you can self-update from GitHub releases:
```bash
openmower update-self                  # update to latest release
openmower update-self -v v1.2.3        # update to specific tag
openmower update-self --repo owner/repo # override repo (defaults to ClemensElflein/openmower-cli)
openmower update-self --dry-run        # show what would be done
```
The command replaces the currently running zipapp with the downloaded version atomically.

## Development
Clone and install in editable mode:
```bash
git clone https://github.com/ClemensElflein/openmower-cli.git
cd openmower-cli
python -m venv .venv
source .venv/bin/activate
pip install -U pip setuptools wheel
pip install -r requirements.txt
pip install -e .
```
Run the CLI from source:
```bash
openmower --help
```

Linting and formatting are managed via pre-commit hooks (a `pre-commit` helper script is present). You may install and run them locally if desired.

## Troubleshooting
- Docker not found: ensure `/usr/bin/docker` exists or adjust your environment to provide Docker with the compose plugin.
- Permission errors with Docker: add your user to the `docker` group or run with sufficient privileges.
- V2 hardware commands missing: set `HARDWARE_PLATFORM=2` to enable the new command group.
- Self-update says executable is not a zipapp: the feature is only for the packaged zipapp artifact; when running from source or pip install, use your package manager to update instead.

## License
This project is licensed under the terms of the LICENSE file included in this repository.

## Acknowledgements
Built with:
- Typer for CLI composition
- Rich for styled console output
