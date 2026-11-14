import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ATLAS_HOME", str(ROOT))

# Frozen "now": Friday 2026-11-13 09:00 America/New_York. Every date test is relative to this.
FROZEN_NOW = "2026-11-13T09:00:00"


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    monkeypatch.setenv("ATLAS_NOW", FROZEN_NOW)
    monkeypatch.setenv("ATLAS_HOME", str(ROOT))


@pytest.fixture
def engine():
    from atlas.store import make_engine

    return make_engine("sqlite:///:memory:")


@pytest.fixture
def demo_engine(engine):
    from atlas.evaluation.demo_data import seed_demo

    seed_demo(engine)
    return engine


@pytest.fixture
def policy():
    from atlas.config import load_policy

    return load_policy(ROOT / "config" / "policy.yaml")


@pytest.fixture
def make_assistant(demo_engine, policy):
    from fakes import ScriptedModel

    from atlas.agent import Assistant
    from atlas.config import Settings
    from atlas.hooks import AuditLog

    def _make(turns, pol=None, engine=None):
        model = ScriptedModel(turns)
        a = Assistant(engine or demo_engine, Settings(), pol or policy, model=model, audit=AuditLog(None))
        return a, model

    return _make
