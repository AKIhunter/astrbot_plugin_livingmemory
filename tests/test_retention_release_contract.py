"""Release controls that are material to artifact promotion and defaults."""
import json
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_retention_schema_defaults_and_boundaries_match_validator():
    from astrbot_plugin_livingmemory.core.base.config_validator import ForgettingAgentConfig
    schema = json.loads((ROOT/"_conf_schema.json").read_text())["forgetting_agent"]["items"]
    model = ForgettingAgentConfig()
    for name in ("long_term_retention_enabled","retention_recent_days",
                 "retention_sample_days","retention_min_occurrences","retention_atom_days"):
        assert schema[name]["default"] == getattr(model,name)
    assert schema["retention_min_occurrences"]["slider"]["min"] == 3


def test_main_checks_and_draft_release_are_required():
    workflow = yaml.safe_load((ROOT/".github/workflows/test.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert "main" in triggers["push"]["branches"]
    assert "main" in triggers["pull_request"]["branches"]
    commands = [s.get("run","") for s in workflow["jobs"]["test"]["steps"]]
    for expected in ("python -m pytest -q","npm run test:frontend",
                     "npm run check:dashboard","npm run docs:build"):
        assert expected in commands
    release = yaml.safe_load((ROOT/".github/workflows/auto-release.yml").read_text())
    create = next(s for s in release["jobs"]["create-release"]["steps"] if s["name"]=="Create Release")
    assert create["with"]["draft"] is True
    assert create["with"]["target_commitish"]=="${{ github.sha }}"
    assert release["jobs"]["check-version"]["outputs"]["previous_sha"]


def test_plugin_archive_includes_prompts_and_vendor_license_without_development_data():
    import importlib.util
    spec = importlib.util.spec_from_file_location("lm_release_builder", ROOT/"scripts/build_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    prompts = list((ROOT/"core/prompts").glob("*.txt"))
    assert len(prompts) >= 7
    for path in prompts:
        assert module.allowed(path.relative_to(ROOT).as_posix()), path
    assert module.allowed("pages/dashboard/vendor/LUCIDE_LICENSE")
    for name in ("data/memory.db", "tests/test_engine.py", "benchmarks/dataset.json",
                 ".env", "core/prompts/.secret.txt", "../metadata.yaml"):
        assert not module.allowed(name), name
