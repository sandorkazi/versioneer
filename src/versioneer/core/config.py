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


def config_dir() -> Path:
    override = os.environ.get("VERSIONEER_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "versioneer"


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


@dataclass
class Target:
    path: str = ""
    abs_path: str = ""
    kind: str = "text"
    glob: str = ""
    ignore: list[str] = field(default_factory=list)
    symlink: str = "preserve"
    flex: str = "fixed"
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
            if age is not None and not _valid_interval(str(age).replace(" ", "")) \
                    and not re.match(r"^\d+\s*d$", str(age).strip()):
                # age like "30d" allowed in addition to intervals
                errors.append(f"invalid retention.age {age!r}: e.g. 30d")
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
        }

    @classmethod
    def from_dict(cls, data: dict) -> Target:
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


def load(name: str) -> Config:
    """Load config by name, raising FileNotFoundError / ValueError."""
    import tomllib

    path = config_path(name)
    with path.open("rb") as f:
        data = tomllib.load(f)
    meta_data = data.get("meta", {})
    meta = Meta(
        name=meta_data.get("name", name),
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


def save(config: Config) -> Path:
    """Write config to disk, creating the config dir. Returns path."""
    import tomli_w

    errors = config.validate()
    if errors:
        raise ValueError("; ".join(errors))
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = config_path(config.meta.name)
    targets_payload = [
        t.to_dict() if isinstance(t, Target) else dict(t)  # type: ignore[union-attr]
        for t in config.targets
    ]
    payload = {
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
    with path.open("wb") as f:
        tomli_w.dump(payload, f)
    return path


def list_configs() -> list[str]:
    d = config_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.toml"))


def store_dir(config: Config) -> Path:
    import os as _os

    raw = config.meta.storage or f"~/versioneer-store/{config.meta.name}"
    return Path(_os.path.expandvars(raw)).expanduser()


def find_target(config: Config, key: str) -> Target | None:
    for t in config.targets:
        assert isinstance(t, Target)
        if t.path == key or t.abs_path == key:
            return t
    return None
