"""`openmower version` command group: save, list and switch version bundles.

A bundle pins the open_mower_ros + OpenMowerApp images (and optionally a firmware
release). Switching writes the compose `.env` and redeploys via `openmower pull`;
firmware is flashed separately and only on request. See bundles.py for the model.
"""

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from openmower_cli.console import info, error, success, warn, message
from openmower_cli.constants import COMPOSE_FILE
from openmower_cli import bundles
from openmower_cli.bundles import Bundle, Component, Firmware

openmower_version_app = typer.Typer(
    name="version",
    no_args_is_help=True,
    help="Save, list and switch between pinned component-version bundles.",
)

_console = Console()


def _fmt_component(image: Optional[str], tag: Optional[str], default_image: str) -> str:
    """Render a component as 'tag' or 'tag (custom/image)' for table display."""
    tag = tag or "latest"
    if image and image != default_image:
        return f"{tag} ({image})"
    return tag


def _fmt_firmware(fw: Optional[Firmware]) -> str:
    if fw is None or fw.is_empty():
        return "-"
    if fw.repo:
        return f"{fw.tag or 'latest'} ({fw.repo})"
    return fw.tag or "latest"


def _render_table(items: list[Bundle]) -> None:
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Name")
    table.add_column("Firmware")
    table.add_column("ROS")
    table.add_column("App")
    table.add_column("Source")
    table.add_column("Description")
    for idx, b in enumerate(items, start=1):
        table.add_row(
            str(idx),
            b.name,
            _fmt_firmware(b.firmware),
            _fmt_component(b.ros.image, b.ros.tag, bundles.DEFAULT_ROS_IMAGE),
            _fmt_component(b.app.image, b.app.tag, bundles.DEFAULT_APP_IMAGE),
            b.source,
            b.description or "",
        )
    _console.print(table)


@openmower_version_app.command("list")
def list_bundles():
    """List local bundles, automatic backups and the remote catalog."""
    items = bundles.list_all_bundles()
    if not items:
        info("No version bundles found. Create one with 'openmower version save <name>'.")
        return
    _render_table(items)


@openmower_version_app.command("show")
def show_bundle(name: str = typer.Argument(..., help="Bundle name to show.")):
    """Show the full details of a single bundle."""
    b = bundles.resolve_bundle(name)
    if b is None:
        error(f"No bundle named '{name}' found.")
        raise typer.Exit(code=1)
    message(f"[bold]{b.name}[/bold]  ([italic]{b.source}[/italic])")
    if b.description:
        message(f"  {b.description}")
    if b.created_at:
        message(f"  created: {b.created_at}")
    message(f"  ROS: {b.ros_image()}:{b.ros_tag()}")
    message(f"  App: {b.app_image()}:{b.app_tag()}")
    message(f"  Firmware: {_fmt_firmware(b.firmware)}")


@openmower_version_app.command("save")
def save_bundle(
    name: str = typer.Argument(..., help="Name for the new bundle."),
    description: str = typer.Option("", "--description", "-d", help="Short description of what this version adds."),
    ros_tag: Optional[str] = typer.Option(None, "--ros-tag", help="Override ROS tag (default: current .env VERSION)."),
    ros_image: Optional[str] = typer.Option(None, "--ros-image", help="Override ROS image repository."),
    app_tag: Optional[str] = typer.Option(None, "--app-tag", help="Override App tag (default: current .env APP_VERSION)."),
    app_image: Optional[str] = typer.Option(None, "--app-image", help="Override App image repository."),
    fw_repo: Optional[str] = typer.Option(None, "--fw-repo", help="Firmware repo 'owner/name' to pin."),
    fw_tag: Optional[str] = typer.Option(None, "--fw-tag", help="Firmware release tag to pin."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite an existing bundle with the same name."),
):
    """Save the currently configured versions as a named bundle.

    Without overrides, the snapshot is taken from the current compose `.env`
    (and the firmware marker, if a firmware was flashed via this CLI). Any
    component can be overridden via the options.
    """
    current = bundles.current_versions()

    ros = Component(
        image=ros_image if ros_image is not None else current.ros.image,
        tag=ros_tag if ros_tag is not None else current.ros.tag,
    )
    app = Component(
        image=app_image if app_image is not None else current.app.image,
        tag=app_tag if app_tag is not None else current.app.tag,
    )

    # Firmware: explicit options take precedence; otherwise reuse the marker.
    firmware: Optional[Firmware] = None
    if fw_repo is not None or fw_tag is not None:
        firmware = Firmware(repo=fw_repo, tag=fw_tag)
    elif current.firmware is not None:
        firmware = current.firmware

    bundle = Bundle(
        name=name,
        description=description,
        source="local",
        created_at=bundles.make_timestamp(),
        ros=ros,
        app=app,
        firmware=firmware,
    )

    # Validate the assembled bundle through the same path as loaded ones.
    validated = bundles.validate_bundle(bundle.to_dict(), force_source="local")
    if validated is None:
        error("Refusing to save: the resulting bundle is invalid.")
        raise typer.Exit(code=2)

    try:
        path = bundles.save_local_bundle(validated, overwrite=overwrite)
    except FileExistsError as e:
        error(f"{e} Use --overwrite to replace it.")
        raise typer.Exit(code=1)

    success(f"Saved bundle '{name}' -> {path}")


@openmower_version_app.command("delete")
def delete_bundle(name: str = typer.Argument(..., help="Bundle name to delete.")):
    """Delete a locally stored bundle."""
    if bundles.delete_local_bundle(name):
        success(f"Deleted bundle '{name}'.")
    else:
        error(f"No local bundle named '{name}' found.")
        raise typer.Exit(code=1)


def _read_compose_text() -> Optional[str]:
    path = Path(COMPOSE_FILE)
    if not path.exists():
        return None
    try:
        return path.read_text()
    except Exception:
        return None


@openmower_version_app.command("migrate-compose")
def migrate_compose(
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
):
    """Make the host's compose.yaml switch-ready for App version switching.

    Rewrites the two hardcoded image lines to reference ROS_IMAGE/APP_IMAGE/
    APP_VERSION (with safe defaults), after writing a timestamped .bak backup.
    Idempotent.
    """
    path = Path(COMPOSE_FILE)
    text = _read_compose_text()
    if text is None:
        error(f"Compose file not found or unreadable: {COMPOSE_FILE}")
        raise typer.Exit(code=1)

    if bundles.compose_is_switch_ready(text):
        info("Compose file is already switch-ready. Nothing to do.")
        return

    new_text, changed = bundles.migrate_compose_text(text)
    if not changed:
        warn(
            "Could not find the expected image lines in the compose file. "
            "It may have been customized; migrate it manually so that the "
            "open_mower_ros and OpenMowerApp images use ${ROS_IMAGE}/${VERSION} "
            "and ${APP_IMAGE}/${APP_VERSION}."
        )
        raise typer.Exit(code=1)

    if not yes:
        confirm = typer.confirm(f"Rewrite {COMPOSE_FILE} (a .bak backup will be created)?", default=True)
        if not confirm:
            info("Aborted.")
            raise typer.Exit(code=0)

    backup = path.with_name(path.name + ".bak-" + bundles.make_timestamp().replace(":", ""))
    try:
        backup.write_text(text)
        path.write_text(new_text)
    except Exception as e:
        error(f"Failed to migrate compose file: {e}")
        raise typer.Exit(code=1)
    success(f"Compose file is now switch-ready. Backup: {backup}")


def _select_bundle_interactively() -> Optional[Bundle]:
    """Render the table and prompt for an index. Returns the chosen bundle."""
    items = bundles.list_all_bundles()
    if not items:
        error("No version bundles found. Create one with 'openmower version save <name>'.")
        raise typer.Exit(code=1)
    _render_table(items)
    choice = typer.prompt("Select a bundle by number")
    try:
        idx = int(str(choice).strip())
    except ValueError:
        error("Please enter a valid number.")
        raise typer.Exit(code=2)
    if idx < 1 or idx > len(items):
        error(f"Selection out of range (1-{len(items)}).")
        raise typer.Exit(code=2)
    return items[idx - 1]


@openmower_version_app.command("apply")
def apply_bundle(
    name: Optional[str] = typer.Argument(None, help="Bundle name to apply. Omit to pick from a list."),
    with_firmware: Optional[bool] = typer.Option(
        None,
        "--with-firmware/--no-firmware",
        help="Also flash the bundle's firmware. Default: ask if the bundle pins firmware.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation (non-interactive)."),
):
    """Switch to a version bundle (ROS + App), optionally flashing its firmware.

    Steps: back up the current versions, write ROS/App into the compose `.env`,
    redeploy via `openmower pull`, then flash firmware if requested. A firmware
    flash failure does not undo the already-applied ROS/App switch.
    """
    # Resolve the bundle (explicit name, else interactive selection).
    if name:
        bundle = bundles.resolve_bundle(name)
        if bundle is None:
            error(f"No bundle named '{name}' found.")
            raise typer.Exit(code=1)
    else:
        bundle = _select_bundle_interactively()

    info(
        f"Selected '{bundle.name}': "
        f"ROS {bundle.ros_image()}:{bundle.ros_tag()}, "
        f"App {bundle.app_image()}:{bundle.app_tag()}, "
        f"Firmware {_fmt_firmware(bundle.firmware)}"
    )

    # Warn (and offer migration) if the host compose can't switch the App image.
    compose_text = _read_compose_text()
    if compose_text is not None and not bundles.compose_is_switch_ready(compose_text):
        warn(
            "The compose file is not switch-ready: App version switching will "
            "have no effect until you run 'openmower version migrate-compose'. "
            "ROS switching via ${VERSION} works regardless."
        )
        if not yes and typer.confirm("Run migrate-compose now?", default=True):
            migrate_compose(yes=True)

    if not yes and not typer.confirm(f"Apply bundle '{bundle.name}' and redeploy the stack?", default=True):
        info("Aborted.")
        raise typer.Exit(code=0)

    # Auto-backup the current state so the switch is reversible.
    try:
        current = bundles.current_versions()
        backup = Bundle(
            name=f"backup-{bundles.make_timestamp().replace(':', '')}",
            description=f"Auto-backup before applying '{bundle.name}'",
            source="backup",
            created_at=bundles.make_timestamp(),
            ros=current.ros,
            app=current.app,
            firmware=current.firmware,
        )
        bundles.save_local_bundle(backup, overwrite=True)
        bundles.prune_backups(keep=10)
        info(f"Saved backup of current versions as '{backup.name}'.")
    except Exception as e:
        warn(f"Could not create a backup of the current versions: {e}")

    # Write the new ROS/App versions and redeploy.
    bundles.apply_env(bundle)
    info("Updated compose .env. Redeploying stack ...")
    from openmower_cli.openmower_common_commands import pull
    pull()
    success(f"Switched ROS/App to bundle '{bundle.name}'.")

    # Optional firmware flash (last, so a failure doesn't leave the stack down).
    should_flash = False
    has_fw = bundle.firmware is not None and bundle.firmware.tag is not None
    if with_firmware is True:
        if not has_fw:
            warn("Bundle does not pin a firmware version; skipping firmware flash.")
        else:
            should_flash = True
    elif with_firmware is None and has_fw and not yes:
        should_flash = typer.confirm(
            f"Also flash firmware {_fmt_firmware(bundle.firmware)}?", default=False
        )

    if should_flash:
        from openmower_cli.openmower_commands import flash_firmware_core
        info("Flashing firmware ...")
        try:
            flash_firmware_core(repo=bundle.firmware.repo, tag=bundle.firmware.tag)
        except typer.Exit:
            warn("Firmware flash failed. ROS/App were switched successfully.")
