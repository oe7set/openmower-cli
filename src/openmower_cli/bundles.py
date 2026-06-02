"""Version bundles: save, list and switch between pinned component versions.

A *bundle* pins the versions of the high-level stack components (open_mower_ros
and OpenMowerApp images) plus, optionally, a firmware release. Each component may
come from a different repository/registry. Bundles are stored locally as one JSON
file per bundle under BUNDLES_DIR, and a curated remote catalog can be merged in
for discovery.

Switching applies the ROS + App versions by writing the compose `.env` and letting
`openmower pull` redeploy. Firmware is flashed separately and only on request.

This module deliberately reads the live `.env` straight from disk via
`dotenv_values` instead of `constants.get_env`, because the latter caches the
file at import time and would go stale after we write to it.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import requests
from dotenv import dotenv_values, set_key, unset_key

from openmower_cli.console import warn, error, info
from openmower_cli.constants import (
    ENV_PATH,
    BUNDLES_DIR,
    BUNDLE_CATALOG_URL,
    LAST_FIRMWARE_FILE,
    DEFAULT_ROS_IMAGE,
    DEFAULT_APP_IMAGE,
    ALLOWED_IMAGE_REGISTRIES,
)

# Bundle schema version. Bumped only on incompatible changes; loaders warn and
# skip bundles whose major schema is newer than this.
SCHEMA_VERSION = 1

# Validation patterns. Bundle values flow into `docker pull` and the compose
# `.env`, so they are constrained to safe character sets.
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_NAME_SLUG_RE = re.compile(r"[^a-z0-9._-]+")

# Compose env keys written by apply_env / read by current_versions.
ROS_TAG_KEY = "VERSION"
ROS_IMAGE_KEY = "ROS_IMAGE"
APP_TAG_KEY = "APP_VERSION"
APP_IMAGE_KEY = "APP_IMAGE"


@dataclass
class Component:
    """One stack component: an optional image repo plus a tag."""
    image: Optional[str] = None
    tag: Optional[str] = None

    def to_dict(self) -> dict:
        d: dict = {}
        if self.image:
            d["image"] = self.image
        if self.tag:
            d["tag"] = self.tag
        return d

    def is_empty(self) -> bool:
        return not self.image and not self.tag


@dataclass
class Firmware:
    """A firmware release pin: repo ('owner/name') plus tag."""
    repo: Optional[str] = None
    tag: Optional[str] = None

    def to_dict(self) -> dict:
        d: dict = {}
        if self.repo:
            d["repo"] = self.repo
        if self.tag:
            d["tag"] = self.tag
        return d

    def is_empty(self) -> bool:
        return not self.repo and not self.tag


@dataclass
class Bundle:
    """A named set of pinned component versions with a short description."""
    name: str
    description: str = ""
    source: str = "local"  # local | catalog | backup (forced by the loader)
    created_at: str = ""
    schema: int = SCHEMA_VERSION
    ros: Component = field(default_factory=Component)
    app: Component = field(default_factory=Component)
    firmware: Optional[Firmware] = None

    def to_dict(self) -> dict:
        d = {
            "schema": self.schema,
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "created_at": self.created_at,
            "ros": self.ros.to_dict(),
            "app": self.app.to_dict(),
        }
        if self.firmware is not None and not self.firmware.is_empty():
            d["firmware"] = self.firmware.to_dict()
        return d

    # --- convenience accessors that fall back to the canonical defaults ---

    def ros_image(self) -> str:
        return self.ros.image or DEFAULT_ROS_IMAGE

    def app_image(self) -> str:
        return self.app.image or DEFAULT_APP_IMAGE

    def ros_tag(self) -> str:
        return self.ros.tag or "latest"

    def app_tag(self) -> str:
        return self.app.tag or "latest"


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def slugify(name: str) -> str:
    """Turn a bundle name into a safe filename stem."""
    slug = _NAME_SLUG_RE.sub("-", str(name).strip().lower()).strip("-")
    return slug or "bundle"


def _validate_image(image: Optional[str], *, where: str) -> Optional[str]:
    """Validate an image reference (without tag). Returns None if absent.

    Raises ValueError on an unsafe or non-allowlisted value.
    """
    if image in (None, ""):
        return None
    image = str(image).strip()
    if not _IMAGE_RE.match(image):
        raise ValueError(f"{where}: invalid image reference '{image}'")
    if not any(image.startswith(reg) for reg in ALLOWED_IMAGE_REGISTRIES):
        raise ValueError(f"{where}: image registry not allowed for '{image}'")
    return image


def _validate_tag(tag: Optional[str], *, where: str) -> Optional[str]:
    if tag in (None, ""):
        return None
    tag = str(tag).strip()
    if not _TAG_RE.match(tag):
        raise ValueError(f"{where}: invalid tag '{tag}'")
    return tag


def _validate_repo(repo: Optional[str], *, where: str) -> Optional[str]:
    if repo in (None, ""):
        return None
    repo = str(repo).strip()
    if not _REPO_RE.match(repo):
        raise ValueError(f"{where}: invalid repo '{repo}' (expected 'owner/name')")
    return repo


def validate_bundle(raw: dict, *, force_source: str) -> Optional[Bundle]:
    """Validate and normalize a raw bundle dict.

    Returns a Bundle, or None if the entry is invalid (a warning is emitted and
    the caller should skip it). `source` is always forced to `force_source` so a
    catalog can never claim to be a trusted local bundle.
    """
    try:
        if not isinstance(raw, dict):
            raise ValueError("not an object")

        schema = int(raw.get("schema", SCHEMA_VERSION))
        if schema > SCHEMA_VERSION:
            raise ValueError(f"unsupported schema version {schema}")

        name = str(raw.get("name", "")).strip()
        if not name:
            raise ValueError("missing name")

        ros_raw = raw.get("ros") or {}
        app_raw = raw.get("app") or {}
        ros = Component(
            image=_validate_image(ros_raw.get("image"), where=f"{name}.ros"),
            tag=_validate_tag(ros_raw.get("tag"), where=f"{name}.ros"),
        )
        app = Component(
            image=_validate_image(app_raw.get("image"), where=f"{name}.app"),
            tag=_validate_tag(app_raw.get("tag"), where=f"{name}.app"),
        )

        firmware: Optional[Firmware] = None
        fw_raw = raw.get("firmware")
        if isinstance(fw_raw, dict):
            fw = Firmware(
                repo=_validate_repo(fw_raw.get("repo"), where=f"{name}.firmware"),
                tag=_validate_tag(fw_raw.get("tag"), where=f"{name}.firmware"),
            )
            if not fw.is_empty():
                firmware = fw

        return Bundle(
            name=name,
            description=str(raw.get("description", "")),
            source=force_source,
            created_at=str(raw.get("created_at", "")),
            schema=schema,
            ros=ros,
            app=app,
            firmware=firmware,
        )
    except Exception as e:
        warn(f"Skipping invalid bundle: {e}")
        return None


# --------------------------------------------------------------------------- #
# Local storage
# --------------------------------------------------------------------------- #

def _bundle_path(name: str) -> Path:
    return BUNDLES_DIR / f"{slugify(name)}.json"


def list_local_bundles() -> List[Bundle]:
    """Load all locally stored bundles (including auto-backups)."""
    bundles: List[Bundle] = []
    if not BUNDLES_DIR.exists():
        return bundles
    for path in sorted(BUNDLES_DIR.glob("*.json")):
        try:
            raw = json.loads(path.read_text())
        except Exception:
            warn(f"Skipping unreadable bundle file: {path.name}")
            continue
        # Preserve the stored source for local files (local or backup), but never
        # trust an arbitrary value — coerce anything else to 'local'.
        src = raw.get("source")
        forced = src if src in ("local", "backup") else "local"
        b = validate_bundle(raw, force_source=forced)
        if b is not None:
            bundles.append(b)
    return bundles


def load_local_bundle(name: str) -> Optional[Bundle]:
    path = _bundle_path(name)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return None
    src = raw.get("source")
    forced = src if src in ("local", "backup") else "local"
    return validate_bundle(raw, force_source=forced)


def save_local_bundle(bundle: Bundle, *, overwrite: bool = False) -> Path:
    """Write a bundle to disk. Raises FileExistsError if present and not overwrite."""
    BUNDLES_DIR.mkdir(parents=True, exist_ok=True)
    path = _bundle_path(bundle.name)
    if path.exists() and not overwrite:
        raise FileExistsError(f"A bundle named '{bundle.name}' already exists.")
    path.write_text(json.dumps(bundle.to_dict(), indent=2))
    return path


def delete_local_bundle(name: str) -> bool:
    path = _bundle_path(name)
    if path.exists():
        path.unlink()
        return True
    return False


def prune_backups(keep: int = 10) -> None:
    """Keep only the newest `keep` automatic backups, deleting older ones."""
    backups = [b for b in list_local_bundles() if b.source == "backup"]
    # created_at is ISO-8601, so lexical sort == chronological sort.
    backups.sort(key=lambda b: b.created_at, reverse=True)
    for stale in backups[keep:]:
        delete_local_bundle(stale.name)


# --------------------------------------------------------------------------- #
# Remote catalog
# --------------------------------------------------------------------------- #

def fetch_catalog(url: str = BUNDLE_CATALOG_URL, timeout: int = 15) -> List[Bundle]:
    """Fetch the curated remote catalog. Tolerates being offline (returns [])."""
    try:
        resp = requests.get(url, timeout=timeout)
        if resp.status_code != 200:
            warn(f"Could not fetch bundle catalog (HTTP {resp.status_code}).")
            return []
        data = resp.json()
    except Exception:
        # Offline / unreachable / bad JSON: catalog is optional, degrade quietly.
        warn("Bundle catalog unavailable (offline?). Showing local bundles only.")
        return []

    raw_list = data.get("bundles") if isinstance(data, dict) else None
    if not isinstance(raw_list, list):
        warn("Bundle catalog has an unexpected format; ignoring it.")
        return []

    bundles: List[Bundle] = []
    for raw in raw_list:
        b = validate_bundle(raw, force_source="catalog")
        if b is not None:
            bundles.append(b)
    return bundles


def list_all_bundles() -> List[Bundle]:
    """Merge local bundles, backups and the catalog. Local wins on name clash."""
    local = list_local_bundles()
    local_names = {b.name for b in local}
    catalog = [b for b in fetch_catalog() if b.name not in local_names]
    return local + catalog


def resolve_bundle(name: str) -> Optional[Bundle]:
    """Find a bundle by name: local (incl. backups) first, then the catalog."""
    local = load_local_bundle(name)
    if local is not None:
        return local
    for b in fetch_catalog():
        if b.name == name:
            return b
    return None


# --------------------------------------------------------------------------- #
# Current state / .env application
# --------------------------------------------------------------------------- #

def _read_last_firmware() -> Optional[Firmware]:
    if not LAST_FIRMWARE_FILE.exists():
        return None
    try:
        data = json.loads(LAST_FIRMWARE_FILE.read_text())
    except Exception:
        return None
    fw = Firmware(repo=data.get("repo"), tag=data.get("tag"))
    return None if fw.is_empty() else fw


def current_versions() -> Bundle:
    """Read the versions currently configured on this host (from disk).

    ROS/App come from the compose `.env`; firmware (if known) from the marker
    written by the last flash via this CLI.
    """
    values = dotenv_values(ENV_PATH) if Path(ENV_PATH).exists() else {}

    ros = Component(
        image=(values.get(ROS_IMAGE_KEY) or None),
        tag=(values.get(ROS_TAG_KEY) or None),
    )
    app = Component(
        image=(values.get(APP_IMAGE_KEY) or None),
        tag=(values.get(APP_TAG_KEY) or None),
    )
    return Bundle(
        name="current",
        description="Currently configured versions",
        source="local",
        ros=ros,
        app=app,
        firmware=_read_last_firmware(),
    )


def _ensure_env_file() -> Path:
    """Ensure the compose `.env` exists (set_key/unset_key require the file)."""
    path = Path(ENV_PATH)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    return path


def apply_env(bundle: Bundle) -> None:
    """Write a bundle's ROS + App versions into the compose `.env`.

    Tags are always written. An image key is written whenever the bundle pins
    an image explicitly, so the pinned image wins regardless of what the host's
    compose `${VAR:-default}` fallback happens to be. Only bundles that leave
    the image unset (tag-only) defer to that compose default, in which case the
    key is removed. We never write empty values, because `:-default` only
    triggers when a variable is unset or empty.
    """
    path = str(_ensure_env_file())

    set_key(path, ROS_TAG_KEY, bundle.ros_tag())
    set_key(path, APP_TAG_KEY, bundle.app_tag())

    if bundle.ros.image:
        set_key(path, ROS_IMAGE_KEY, bundle.ros.image)
    else:
        unset_key(path, ROS_IMAGE_KEY)

    if bundle.app.image:
        set_key(path, APP_IMAGE_KEY, bundle.app.image)
    else:
        unset_key(path, APP_IMAGE_KEY)


# --------------------------------------------------------------------------- #
# Compose migration (make an existing host's compose.yaml switch-ready)
# --------------------------------------------------------------------------- #

# Which compose service is the high-level ROS node, and which is the web app.
# The app list is a priority order: the FIRST service that exists in the file is
# the managed one. The real web UI is the Next.js app (service `app` /
# `openmower-app`); the Flutter `OpenMowerApp` is only a last-resort fallback for
# stock OpenMowerOS installs that don't ship the Next.js app.
ROS_SERVICE_NAMES = ("open_mower_ros",)
APP_SERVICE_NAMES = ("app", "openmower-app", "OpenMowerApp")

# Tokens that mark an image line as produced by our migration. Used to detect a
# service that was switch-parameterized by mistake so we can revert it.
_OUR_TOKENS = ("${ROS_IMAGE", "${APP_IMAGE", "${VERSION", "${APP_VERSION")

# A top-level service entry, e.g. "  open_mower_ros:" (exactly 2-space indent).
_SERVICE_RE = re.compile(r"^  (?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*):\s*$")
# An image line inside a service block, e.g. '    image: "ghcr.io/x/y:tag"'.
# Named distinctly from the bundle-validation `_IMAGE_RE` above to avoid shadowing.
_IMAGE_LINE_RE = re.compile(r"^(?P<indent>\s*)image:\s*(?P<ref>.+?)\s*$")
# A "${VAR:-default}" expansion, used when reverting a mistakenly-migrated line.
_EXPANSION_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-(?P<default>[^}]*)\}")


def _split_image(ref: str) -> tuple[str, str]:
    """Split an image reference into (repo, tag).

    Strips surrounding quotes/whitespace. The tag is the part after the last
    ':' only when that part contains no '/' (otherwise the ':' belongs to a
    registry:port, not a tag). Returns ("", "") semantics via empty tag when no
    tag is present.
    """
    ref = ref.strip().strip('"').strip("'").strip()
    if ":" in ref:
        head, _, tail = ref.rpartition(":")
        if "/" not in tail:
            return head, tail
    return ref, ""


def _collapse_expansions(ref: str) -> str:
    """Turn every '${VAR:-default}' in an image ref back into its default."""
    return _EXPANSION_RE.sub(lambda m: m.group("default"), ref)


def _resolve_managed_services(text: str) -> tuple[Optional[str], Optional[str]]:
    """Return the (ros_service, app_service) names actually present in the file."""
    present = set()
    in_services = False
    for line in text.splitlines():
        if line.strip() and not line[0].isspace():
            in_services = line.rstrip().rstrip(":").strip() == "services" or line.strip() == "services:"
            continue
        if not in_services:
            continue
        m = _SERVICE_RE.match(line)
        if m:
            present.add(m.group("name"))
    ros = next((n for n in ROS_SERVICE_NAMES if n in present), None)
    app = next((n for n in APP_SERVICE_NAMES if n in present), None)
    return ros, app


def compose_is_switch_ready(text: str) -> bool:
    """True when the managed ROS and App services both use a switchable image."""
    ros_svc, app_svc = _resolve_managed_services(text)
    if not ros_svc or not app_svc:
        return False
    images = _collect_service_images(text)
    ros_img = images.get(ros_svc, "")
    app_img = images.get(app_svc, "")
    return "${ROS_IMAGE" in ros_img and "${APP_IMAGE" in app_img


def _collect_service_images(text: str) -> dict:
    """Map service name -> its raw image reference (best-effort, first match)."""
    images: dict = {}
    in_services = False
    current: Optional[str] = None
    for line in text.splitlines():
        if line.strip() and not line[0].isspace():
            in_services = line.strip() == "services:"
            current = None
            continue
        if not in_services:
            continue
        m = _SERVICE_RE.match(line)
        if m:
            current = m.group("name")
            continue
        if current is not None:
            mi = _IMAGE_LINE_RE.match(line)
            if mi and current not in images:
                images[current] = mi.group("ref")
    return images


def migrate_compose_text(text: str) -> tuple[str, bool]:
    """Make the managed ROS + App services switch-ready, by service name.

    For the managed ROS service the image line becomes
    `${ROS_IMAGE:-<repo>}:${VERSION:-<tag>}` where <repo>/<tag> are taken from
    the line's CURRENT value, so a fork image and explicit tag are preserved as
    defaults. The managed App service is rewritten the same way with
    APP_IMAGE/APP_VERSION. Any OTHER service whose image was parameterized with
    our variables by a previous (mis-targeted) run is reverted to a concrete
    image. Everything else is left byte-for-byte unchanged.

    Line-oriented on purpose: a YAML round-trip would destroy the file's
    'DO NOT EDIT' header, comments and key order (the file is Dockge-managed).
    Idempotent — a second run changes nothing. Returns (new_text, changed).
    """
    ros_svc, app_svc = _resolve_managed_services(text)

    out: List[str] = []
    changed = False
    in_services = False
    current: Optional[str] = None

    for line in text.splitlines(keepends=True):
        nl = "\n" if line.endswith("\n") else ""
        body = line[:-1] if nl else line

        # Track whether we are inside the top-level `services:` block.
        if body.strip() and not body[0].isspace():
            in_services = body.strip() == "services:"
            current = None
            out.append(line)
            continue

        if in_services:
            m_svc = _SERVICE_RE.match(body)
            if m_svc:
                current = m_svc.group("name")
                out.append(line)
                continue

            mi = _IMAGE_LINE_RE.match(body)
            if mi and current is not None:
                indent = mi.group("indent")
                ref = mi.group("ref")

                if current == ros_svc:
                    new_body = _switchable_image_line(indent, ref, "ROS_IMAGE", "VERSION")
                elif current == app_svc:
                    new_body = _switchable_image_line(indent, ref, "APP_IMAGE", "APP_VERSION")
                elif any(tok in ref for tok in _OUR_TOKENS):
                    # A non-managed service that a previous run wrongly migrated.
                    new_body = f"{indent}image: {_collapse_expansions(ref)}"
                else:
                    new_body = body

                if new_body != body:
                    changed = True
                out.append(new_body + nl)
                continue

        out.append(line)

    return "".join(out), changed


def _switchable_image_line(indent: str, ref: str, image_var: str, tag_var: str) -> str:
    """Build a switch-ready image line, keeping the current repo/tag as defaults.

    Idempotent: if the reference already uses our image variable, the line is
    rebuilt from its existing defaults, yielding an identical string. A repo or
    tag that is itself a bare variable expansion (e.g. the template's
    `${VERSION}`) is collapsed to a literal default first, so we never nest
    `${VERSION:-${VERSION}}`.
    """
    if "${" + image_var in ref:
        # Already switch-ready: keep as-is (rebuilding from defaults is identical).
        return f"{indent}image: {ref.strip()}"
    repo, tag = _split_image(ref)
    # Collapse `${VAR:-default}` to its default; if a value is (or still holds) a
    # bare variable expansion, fall back to a literal so we never nest `${...}`.
    repo = _collapse_expansions(repo)
    tag = _collapse_expansions(tag)
    if not repo or "${" in repo:
        repo = DEFAULT_ROS_IMAGE if image_var == "ROS_IMAGE" else DEFAULT_APP_IMAGE
    if not tag or "${" in tag:
        tag = "latest"
    return f'{indent}image: "${{{image_var}:-{repo}}}:${{{tag_var}:-{tag}}}"'


def make_timestamp() -> str:
    """ISO-8601 timestamp (seconds precision) for created_at / backup names."""
    return datetime.now().replace(microsecond=0).isoformat()
