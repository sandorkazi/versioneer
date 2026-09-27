"""TOML config load/save + validation (Phase 0).

Location: ~/.config/versioneer/<name>.toml
Override for tests: VERSIONEER_CONFIG_DIR.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

VALID_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
VALID_INTERVAL = re.compile(r"^(\d+[smh]|inotify)$|^\d+$")

#: Store-side snapshot of the TOML, committed alongside artifacts so that
#: `bootstrap <url>` can recreate exact TOML(s) on a fresh machine.
SNAPSHOT_NAME = ".versioneer.toml"


def config_dir() -> Path:
    override = os.environ.get("VERSIONEER_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(os.path.expandvars(xdg)).expanduser() / "versioneer"
    # Sudo-aware: `sudo vers ...` must read the invoking user's configs,
    # not /root/.config/versioneer.
    from versioneer.core import elevate as _elev

    return _elev.effective_home() / ".config" / "versioneer"


def config_path(name: str) -> Path:
    return config_dir() / f"{name}.toml"


@dataclass
class Meta:
    name: str
    upstream: str = ""
    root: str = ""
    storage: str = ""
    notify: bool = True
    auto_commit: bool = False
    auto_push: bool = False
    check_interval: str = "3h"
    encrypt: bool = False
    large_file_warn_mb: int = 10

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not VALID_NAME.match(self.name):
            errors.append(f"invalid name {self.name!r}: use [a-zA-Z0-9_-]")
        if self.auto_push and not self.auto_commit:
            errors.append("auto_push=true requires auto_commit=true")
        if self.check_interval and not _valid_interval(self.check_interval):
            errors.append(
                f"invalid check_interval {self.check_interval!r}: e.g. 10s, 5m, 3h, inotify"
            )
        return errors


def _valid_interval(v: str) -> bool:
    v = v.strip()
    if v in ("inotify",):
        return True
    if VALID_INTERVAL.match(v):
        return True
    # allow "3h", "5m", "10s" explicitly (covered) plus plain "3h" style
    return bool(re.match(r"^\d+\s*[smh]$", v))


#: Retention age forms accepted by store.parse_retention_age: 30d/5m/3h/10s/2w
#: (case-insensitive), bare seconds, or positive ints. "inotify" is NOT valid here.
_VALID_RETENTION_AGE = re.compile(r"^\s*(\d+)\s*([smhdwSMHDW])?\s*$")


def _valid_retention_age(age: object) -> bool:
    if isinstance(age, bool):
        return False
    if isinstance(age, (int, float)):
        try:
            return int(age) >= 1
        except (ValueError, OverflowError):
            return False
    m = _VALID_RETENTION_AGE.match(str(age))
    return bool(m) and int(m.group(1)) >= 1


#: Warning shown whenever per-target auto-commit is enabled or fires:
#: frequent silent commits could take up space quickly.
AUTO_COMMIT_SPACE_WARNING = (
    "auto-commit enabled: frequent silent commits could take up space quickly "
    "— keep retention.count limited (history kept; prune with "
    "`git lfs prune` or `versioneer commit --prune-retention`)"
)


def has_limited_retention(retention: object) -> bool:
    """True when retention limits kept revisions (count >= 1).

    Per-target ``auto_commit`` is only available with limited retention.
    An age-only retention does not bound revision count, so it alone
    is not sufficient.
    """
    if not isinstance(retention, dict):
        return False
    count = retention.get("count")
    return isinstance(count, int) and not isinstance(count, bool) and count >= 1


@dataclass
class Target:
    path: str = ""
    abs_path: str = ""
    kind: str = "text"
    glob: str = ""
    ignore: list[str] = field(default_factory=list)
    symlink: str = "preserve"
    flex: str = "fixed"
    # interest: "state" = whole snapshot, "diff" = what changed. v1 deploys
    # state in both cases; diff only adds unified-diff previews in
    # status/diff/dry-run (true patch-apply is v2).
    interest: str = "state"
    deploy_path: str = ""
    owner: str = ""
    group: str = ""
    mode: str = "0644"
    hash: str = ""
    machines: list[str] = field(default_factory=list)
    check_interval: str = ""
    retention: dict = field(default_factory=dict)
    template: bool = False
    on_deploy: str = ""
    encrypt: bool = False
    # Per-target auto-commit opt-in (daemon commits drift silently).
    # Only available with limited retention (retention.count >= 1) since
    # frequent auto-commits could take up space quickly.
    auto_commit: bool = False
    # Manifest subtable ([targets.manifest] in TOML): only for kind="manifest".
    # {type: packages|wine|systemd|env, source: builtin id/cmd, output: fname}.
    manifest: dict = field(default_factory=dict)
    # Auto-add glob for dir targets (snippet/savegame style): untracked files
    # matching this glob are candidates for `watch --auto-add` / bulk add.
    # "" = disabled. Accepts True ("*") / False ("") for legacy bools.
    auto_add_glob: str = ""

    def validate(self, meta_root: str = "") -> list[str]:
        errors: list[str] = []
        if not self.path:
            errors.append("target path is required")
        if self.kind not in ("text", "binary", "dir", "manifest"):
            errors.append(f"invalid kind {self.kind!r}: text|binary|dir|manifest")
        if self.flex not in ("fixed", "user", "flexi"):
            errors.append(f"invalid flex {self.flex!r}: fixed|user|flexi")
        if self.symlink not in ("preserve", "follow"):
            errors.append(f"invalid symlink {self.symlink!r}: preserve|follow")
        if self.interest not in ("state", "diff"):
            errors.append(f"invalid interest {self.interest!r}: state|diff")
        if self.flex == "user" and meta_root:
            errors.append("root must not be set for user targets (flex=user)")
        if self.retention:
            count = self.retention.get("count")
            if count is not None and (not isinstance(count, int) or count < 1):
                errors.append(f"invalid retention.count {count!r}: must be int >= 1")
            age = self.retention.get("age")
            if age is not None and not _valid_retention_age(age):
                errors.append(f"invalid retention.age {age!r}: e.g. 30d, 5m, 3h, 2w")
        if self.manifest:
            if not isinstance(self.manifest, dict):
                errors.append(f"invalid manifest {self.manifest!r}: must be a table")
            elif self.kind != "manifest":
                errors.append("manifest subtable requires kind='manifest'")
            else:
                mtype = self.manifest.get("type", "")
                if mtype and mtype not in ("packages", "wine", "systemd", "env"):
                    errors.append(f"invalid manifest.type {mtype!r}: packages|wine|systemd|env")
        if self.auto_add_glob is not None and not isinstance(self.auto_add_glob, str):
            errors.append(f"invalid auto_add_glob {self.auto_add_glob!r}: must be a glob string")
        if self.auto_commit:
            if not has_limited_retention(self.retention):
                errors.append(
                    "auto_commit=true requires limited retention "
                    "(set retention.count >= 1, e.g. --retention-count N)"
                )
        return errors

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "abs_path": self.abs_path,
            "kind": self.kind,
            "glob": self.glob,
            "ignore": list(self.ignore),
            "symlink": self.symlink,
            "flex": self.flex,
            "interest": self.interest,
            "deploy_path": self.deploy_path,
            "owner": self.owner,
            "group": self.group,
            "mode": self.mode,
            "hash": self.hash,
            "machines": list(self.machines),
            "check_interval": self.check_interval,
            "retention": dict(self.retention),
            "template": self.template,
            "on_deploy": self.on_deploy,
            "encrypt": self.encrypt,
            "manifest": dict(self.manifest),
            "auto_add_glob": self.auto_add_glob,
            "auto_commit": self.auto_commit,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Target:
        # [targets.manifest] subtable compat: tomllib nests it as
        # {"manifest": {...}} under the last [[targets]] entry. Older/flat
        # TOMLs may use manifest_type/manifest_kind strings or
        # manifest_source/manifest_output keys — fold them in.
        manifest: dict = {}
        raw_manifest = data.get("manifest", {})
        if isinstance(raw_manifest, dict):
            manifest = dict(raw_manifest)
        elif isinstance(raw_manifest, str) and raw_manifest.strip():
            manifest = {"type": raw_manifest.strip()}
        for flat_key, sub_key in (
            ("manifest_type", "type"),
            ("manifest_kind", "type"),
            ("manifest_source", "source"),
            ("manifest_output", "output"),
        ):
            if data.get(flat_key) and sub_key not in manifest:
                manifest[sub_key] = data[flat_key]
        # auto_add_glob compat: legacy bool True/False -> "*"/"".
        raw_glob = data.get("auto_add_glob", "")
        if isinstance(raw_glob, bool):
            auto_add_glob = "*" if raw_glob else ""
        elif raw_glob is None:
            auto_add_glob = ""
        else:
            auto_add_glob = str(raw_glob)
        return cls(
            path=data.get("path", ""),
            abs_path=data.get("abs_path", ""),
            kind=data.get("kind", "text"),
            glob=data.get("glob", ""),
            ignore=list(data.get("ignore", [])),
            symlink=data.get("symlink", "preserve"),
            flex=data.get("flex", "fixed"),
            interest=data.get("interest", "state"),
            deploy_path=data.get("deploy_path", ""),
            owner=data.get("owner", ""),
            group=data.get("group", ""),
            mode=data.get("mode", "0644"),
            hash=data.get("hash", ""),
            machines=list(data.get("machines", [])),
            check_interval=data.get("check_interval", ""),
            retention=dict(data.get("retention", {})),
            template=bool(data.get("template", False)),
            on_deploy=data.get("on_deploy", ""),
            encrypt=bool(data.get("encrypt", False)),
            manifest=manifest,
            auto_add_glob=auto_add_glob,
            auto_commit=bool(data.get("auto_commit", False)),
        )


@dataclass
class Config:
    meta: Meta
    targets: list[Target] = field(default_factory=list)

    def validate(self) -> list[str]:
        errors = self.meta.validate()
        seen: set[str] = set()
        for t in self.targets:
            tlist = t if isinstance(t, Target) else Target.from_dict(t)  # type: ignore[union-attr]
            errors.extend(tlist.validate(self.meta.root))
            key = tlist.path or tlist.abs_path
            if key in seen:
                errors.append(f"duplicate target {key!r}")
            seen.add(key)
        return errors


def _config_from_data(data: dict, default_name: str) -> Config:
    """Build a Config from parsed TOML data (shared by load/load_snapshot)."""
    meta_data = data.get("meta", {})
    meta = Meta(
        name=meta_data.get("name", default_name),
        upstream=meta_data.get("upstream", ""),
        root=meta_data.get("root", ""),
        storage=meta_data.get("storage", ""),
        notify=meta_data.get("notify", True),
        auto_commit=meta_data.get("auto_commit", False),
        auto_push=meta_data.get("auto_push", False),
        check_interval=meta_data.get("check_interval", "3h"),
        encrypt=meta_data.get("encrypt", False),
        large_file_warn_mb=meta_data.get("large_file_warn_mb", 10),
    )
    errors = meta.validate()
    if errors:
        raise ValueError("; ".join(errors))
    targets_raw = data.get("targets", [])
    targets = [Target.from_dict(t) if isinstance(t, dict) else t for t in targets_raw]
    return Config(meta=meta, targets=targets)


def _payload_for(config: Config) -> dict:
    targets_payload = []
    for t in config.targets:
        d = t.to_dict() if isinstance(t, Target) else dict(t)  # type: ignore[union-attr]
        # Omit empty manifest subtables: tomli_w renders {} as an empty
        # [targets.manifest] block on every target, which is noisy and
        # drifts from the docs (subtable only for kind="manifest").
        if not d.get("manifest"):
            d.pop("manifest", None)
        targets_payload.append(d)
    return {
        "meta": {
            "name": config.meta.name,
            "upstream": config.meta.upstream,
            "root": config.meta.root,
            "storage": config.meta.storage,
            "notify": config.meta.notify,
            "auto_commit": config.meta.auto_commit,
            "auto_push": config.meta.auto_push,
            "check_interval": config.meta.check_interval,
            "encrypt": config.meta.encrypt,
            "large_file_warn_mb": config.meta.large_file_warn_mb,
        },
        "targets": targets_payload,
    }


def load(name: str) -> Config:
    """Load config by name, raising FileNotFoundError / ValueError."""
    import tomllib

    path = config_path(name)
    with path.open("rb") as f:
        data = tomllib.load(f)
    return _config_from_data(data, name)


def snapshot_path(store: Path) -> Path:
    """Path of the TOML snapshot inside a store repo."""
    return Path(store) / SNAPSHOT_NAME


def export_snapshot(config: Config, store: Path) -> Path:
    """Write the full TOML payload into the store (for bootstrap).

    Raises ValueError (validation) / OSError (I/O). Callers commit the
    returned path and treat failures as warn-only where appropriate.
    """
    import tomli_w

    errors = config.validate()
    if errors:
        raise ValueError("; ".join(errors))
    store = Path(store)
    store.mkdir(parents=True, exist_ok=True)
    path = snapshot_path(store)
    with path.open("wb") as f:
        tomli_w.dump(_payload_for(config), f)
    try:
        from versioneer.core import elevate as _elev

        _elev.fix_store_after_write(store, [SNAPSHOT_NAME])
    except (OSError, ImportError):
        pass
    return path


def load_snapshot(store: Path, default_name: str = "") -> Config | None:
    """Read the store-side TOML snapshot, or None when absent/unreadable."""
    import tomllib

    path = snapshot_path(store)
    if not path.is_file():
        return None
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
        return _config_from_data(data, default_name or path.parent.name)
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return None


def save(config: Config) -> Path:
    """Write config to disk, creating the config dir. Returns path."""
    import tomli_w

    errors = config.validate()
    if errors:
        raise ValueError("; ".join(errors))
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = config_path(config.meta.name)
    payload = _payload_for(config)
    with path.open("wb") as f:
        tomli_w.dump(payload, f)
    try:
        from versioneer.core import elevate as _elev

        _elev.fix_ownership(d, recursive=False)
        _elev.fix_ownership(path, recursive=False)
    except (OSError, ImportError):
        pass
    return path


def list_configs() -> list[str]:
    d = config_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.toml"))


def store_dir(config: Config) -> Path:
    import os as _os

    raw = config.meta.storage or f"~/versioneer-store/{config.meta.name}"
    # Sudo-aware ~ expansion: bare ~/... follows the invoking user,
    # not /root, so `sudo vers` uses the user's stores.
    from versioneer.core import elevate as _elev

    return _elev.expand_user(_os.path.expandvars(raw))


def find_target(config: Config, key: str) -> Target | None:
    for t in config.targets:
        assert isinstance(t, Target)
        if t.path == key or t.abs_path == key:
            return t
    return None
