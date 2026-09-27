"""End-to-end tests for hooks/sagittarius/install.sh.

Covers:

* `--dry-run` is fully side-effect free.
* A real install copies the scripts and merges the four events into
  settings.json without touching unrelated settings (mcpServers, …).
* Retired predecessors (mempal-autosave.sh, mempal-drain.sh, the upstream
  precompact/session-end scripts) are replaced, not duplicated.
* Re-running the installer is idempotent (no duplicate entries).
* `--variant minimal` wires AfterAgent only.
* `--uninstall` removes our entries and preserves unrelated hooks.
* `--uninstall` with no settings file is a no-op.
* Malformed existing settings.json → exit 2, file untouched.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "hooks" / "sagittarius" / "install.sh"

# Skip on Windows — install.sh is bash and uses POSIX path semantics.
pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="install.sh is a bash script; Windows users use a separate code path.",
)

EXPECTED_SCRIPTS = (
    "mempal_save_hook_sagittarius.sh",
    "mempal_drain_hook_sagittarius.sh",
    "mempal_precompress_hook_sagittarius.sh",
    "mempal_session_end_hook_sagittarius.sh",
    "lib/common.sh",
)


def _run_install(
    *args: str,
    home: Path,
    cwd: Path | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(home)
    return subprocess.run(
        ["bash", str(INSTALL_SH), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd or REPO_ROOT),
        timeout=timeout,
    )


def _hook_names(cfg: dict, event: str) -> list[str]:
    names = []
    for group in cfg.get("hooks", {}).get(event, []):
        for defn in group.get("hooks", []):
            names.append(defn.get("name"))
    return names


def _all_commands(cfg: dict) -> list[str]:
    cmds = []
    for groups in cfg.get("hooks", {}).values():
        for group in groups:
            for defn in group.get("hooks", []):
                cmds.append(defn.get("command", ""))
    return cmds


def _write_settings(home: Path, cfg: dict) -> Path:
    target = home / ".sagittarius" / "settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(cfg, indent=2))
    return target


# ── --dry-run ─────────────────────────────────────────────────────────


def test_dry_run_is_side_effect_free(tmp_path: Path) -> None:
    """--dry-run must not copy scripts or write settings."""
    home = tmp_path / "home"
    proc = _run_install("--dry-run", home=home)
    assert proc.returncode == 0
    assert not (home / ".sagittarius" / "hooks").exists()
    assert not (home / ".sagittarius" / "settings.json").exists()
    # ... but it prints the would-be JSON to stdout.
    cfg = json.loads(proc.stdout)
    assert "AfterAgent" in cfg["hooks"]


# ── real install ──────────────────────────────────────────────────────


def test_install_copies_scripts_and_merges_settings(tmp_path: Path) -> None:
    home = tmp_path / "home"
    install_dir = home / ".sagittarius" / "hooks"
    _write_settings(
        home,
        {
            "mcpServers": {"mempalace": {"command": "mempalace-mcp"}},
            "providers": {"active": "gemini-apikey"},
        },
    )
    proc = _run_install(home=home)
    assert proc.returncode == 0, proc.stderr
    for rel in EXPECTED_SCRIPTS:
        assert (install_dir / rel).is_file(), f"missing after install: {rel}"
    cfg = json.loads((home / ".sagittarius" / "settings.json").read_text())
    # Unrelated settings preserved.
    assert cfg["mcpServers"]["mempalace"]["command"] == "mempalace-mcp"
    assert cfg["providers"]["active"] == "gemini-apikey"
    # All four events wired.
    assert _hook_names(cfg, "AfterAgent") == ["mempalace-autosave"]
    assert _hook_names(cfg, "SessionStart") == ["mempalace-drain"]
    assert _hook_names(cfg, "PreCompress") == ["mempalace-precompress"]
    assert _hook_names(cfg, "SessionEnd") == ["mempalace-session-end"]
    # Commands are absolute paths into the install dir.
    for cmd in _all_commands(cfg):
        assert cmd.startswith(str(install_dir)), cmd


def test_install_replaces_retired_predecessors(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_settings(
        home,
        {
            "hooks": {
                "AfterAgent": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "name": "mempalace-autosave",
                                "command": "$HOME/.sagittarius/hooks/mempal-autosave.sh",
                                "timeout": 5,
                            }
                        ]
                    }
                ],
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "name": "mempalace-drain",
                                "command": "$HOME/.sagittarius/hooks/mempal-drain.sh",
                                "timeout": 10,
                            }
                        ]
                    }
                ],
                "PreCompress": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "name": "mempalace-precompact",
                                "command": "$HOME/src/mempalace/hooks/mempal_precompact_hook.sh",
                                "timeout": 60,
                            }
                        ]
                    }
                ],
                "SessionEnd": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "name": "mempalace-session-end",
                                "command": "$HOME/src/mempalace/hooks/mempal_session_end_hook.sh",
                                "timeout": 10,
                            }
                        ]
                    }
                ],
            }
        },
    )
    proc = _run_install(home=home)
    assert proc.returncode == 0, proc.stderr
    cfg = json.loads((home / ".sagittarius" / "settings.json").read_text())
    cmds = _all_commands(cfg)
    assert not any("mempal-autosave.sh" in c for c in cmds)
    assert not any("mempal-drain.sh" in c for c in cmds)
    assert not any("mempal_precompact_hook.sh" in c for c in cmds)
    assert not any("mempal_session_end_hook.sh" in c for c in cmds)
    assert sum("mempalace-autosave" in n for n in _hook_names(cfg, "AfterAgent")) == 1


def test_reinstall_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert _run_install(home=home).returncode == 0
    first = (home / ".sagittarius" / "settings.json").read_text()
    assert _run_install(home=home).returncode == 0
    second = (home / ".sagittarius" / "settings.json").read_text()
    assert json.loads(first)["hooks"] == json.loads(second)["hooks"]
    cfg = json.loads(second)
    assert _hook_names(cfg, "AfterAgent") == ["mempalace-autosave"]


def test_minimal_variant_wires_afteragent_only(tmp_path: Path) -> None:
    home = tmp_path / "home"
    proc = _run_install("--variant", "minimal", home=home)
    assert proc.returncode == 0, proc.stderr
    cfg = json.loads((home / ".sagittarius" / "settings.json").read_text())
    assert "AfterAgent" in cfg["hooks"]
    assert "SessionStart" not in cfg["hooks"]
    assert "PreCompress" not in cfg["hooks"]
    assert "SessionEnd" not in cfg["hooks"]


def test_unrelated_hooks_preserved(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_settings(
        home,
        {
            "hooks": {
                "BeforeTool": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "name": "my-guard",
                                "command": "/opt/guard.sh",
                                "timeout": 5,
                            }
                        ]
                    }
                ]
            }
        },
    )
    assert _run_install(home=home).returncode == 0
    cfg = json.loads((home / ".sagittarius" / "settings.json").read_text())
    assert cfg["hooks"]["BeforeTool"][0]["hooks"][0]["name"] == "my-guard"
    # ... and uninstall keeps them too.
    assert _run_install("--uninstall", home=home).returncode == 0
    cfg = json.loads((home / ".sagittarius" / "settings.json").read_text())
    assert cfg["hooks"]["BeforeTool"][0]["hooks"][0]["command"] == "/opt/guard.sh"
    assert "AfterAgent" not in cfg["hooks"]


# ── --uninstall ───────────────────────────────────────────────────────


def test_uninstall_without_settings_is_noop(tmp_path: Path) -> None:
    home = tmp_path / "home"
    proc = _run_install("--uninstall", home=home)
    assert proc.returncode == 0
    assert "nothing to uninstall" in proc.stderr


def test_malformed_settings_refuses_to_overwrite(tmp_path: Path) -> None:
    home = tmp_path / "home"
    target = home / ".sagittarius" / "settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{ not valid json")
    proc = _run_install(home=home)
    assert proc.returncode == 2
    assert target.read_text() == "{ not valid json"
