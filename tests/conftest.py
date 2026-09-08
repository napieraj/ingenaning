"""Fixtures. Nothing here touches real mounts, Ollama, fatrace, or the network."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from ingenaning.config import Settings
from ingenaning.store.db import Database


@pytest.fixture
def branches(tmp_path: Path) -> tuple[Path, Path, Path]:
    """hot, cold, union directories on tmpfs. `union` is a plain directory —
    tests that need mergerfs behaviour must use the fake in fake_union()."""
    hot, cold, union = tmp_path / "hot", tmp_path / "cold", tmp_path / "union"
    for p in (hot, cold, union):
        p.mkdir()
    return hot, cold, union


@pytest.fixture
def settings(branches: tuple[Path, Path, Path], tmp_path: Path) -> Settings:
    hot, cold, union = branches
    return Settings(
        hot_root=hot,
        cold_root=cold,
        union_root=union,
        db_path=tmp_path / "aning.db",
        socket_path=tmp_path / "access.sock",
        ollama_url="http://127.0.0.1:1",  # unroutable on purpose
        dry_run=True,
    )


@pytest.fixture
def db(settings: Settings) -> Database:
    d = Database(settings.db_path)
    d.migrate()
    return d


def touch(root: Path, rel: str, size: int = 1024, mtime: int | None = None) -> Path:
    p = root / rel.lstrip("/")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0" * size)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


@pytest.fixture
def populated(branches: tuple[Path, Path, Path]) -> tuple[Path, Path, Path]:
    hot, cold, _ = branches
    now = int(time.time())
    for i in range(1, 6):
        touch(cold, f"/a/series/S01E0{i}.mkv", 4096, now - 86400 * i)
    for i in range(1, 4):
        touch(hot, f"/b/shoot/IMG_00{i}.dng", 2048, now - 3600 * i)
    touch(cold, "/c/data/shard-0001.parquet", 8192, now - 7200)
    touch(cold, "/c/data/shard-0002.parquet", 8192, now - 7200)
    return branches


class FakeOllama:
    """Records prompts, returns canned JSON. Use as `arms.planner.client`."""

    def __init__(self, responses: list[dict] | None = None):
        self.responses = responses or []
        self.prompts: list[str] = []

    def generate(self, model: str, prompt: str, timeout: float) -> dict:
        self.prompts.append(prompt)
        return self.responses.pop(0) if self.responses else {"proposals": []}


@pytest.fixture
def ollama() -> FakeOllama:
    return FakeOllama()


def access_line(ts: int, client: str, op: str, path: str, tier: str = "cold") -> str:
    import json

    return (
        json.dumps({"ts": ts, "client": client, "op": op, "path": path, "tier": tier, "bytes": 0})
        + "\n"
    )
