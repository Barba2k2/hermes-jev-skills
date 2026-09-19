import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("jev_install", Path(__file__).resolve().parents[1] / "install.py")
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)

CONFIG = """model:
  default: some/model   # keep this comment
plugins:
  enabled:
  - coagent-observer
  disabled: []
  entries:
    resource-lifecycle:
      allow_tool_override: false
security:
  redact_secrets: true
"""


def run_installer(argv, home, path="/usr/bin:/bin"):
    """Run the installer in-process against a throwaway HOME and return (exit code, report).

    HERMES_HOME is stripped: on the machine this was written on it points at a real fleet,
    and a test that installs into it would rewrite live config.
    """
    env = {k: v for k, v in os.environ.items() if k != "HERMES_HOME"}
    env["HOME"] = str(home)
    env["PATH"] = path
    out = io.StringIO()
    with mock.patch.dict(os.environ, env, clear=True), \
            mock.patch.object(sys, "argv", ["install.py", *argv]), \
            contextlib.redirect_stdout(out):
        code = install.main()
    return code, json.loads(out.getvalue())


class ConfigEditTests(unittest.TestCase):
    def test_home_warning_flags_a_profile_scoped_install(self):
        self.assertIsNotNone(install.home_warning(Path("/srv/hermes/profiles/devbot")))
        self.assertIsNone(install.home_warning(Path("/srv/hermes")))

    def test_enable_and_disable_touch_only_the_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text(CONFIG)
            first = install.enable_plugins(config, install.PLUGINS, True)
            self.assertEqual(set(first.values()), {"enabled"})
            again = install.enable_plugins(config, install.PLUGINS, True)
            self.assertEqual(set(again.values()), {"already enabled"})
            text = config.read_text()
            self.assertIn("  enabled:\n" + "".join(f"  - {n}\n" for n in install.PLUGINS)
                          + "  - coagent-observer\n", text)
            self.assertIn("# keep this comment", text)
            off = install.enable_plugins(config, install.PLUGINS, False)
            self.assertEqual(set(off.values()), {"disabled"})
            self.assertEqual(config.read_text(), CONFIG)

    def test_unchanged_config_is_not_rewritten_or_backed_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text(CONFIG)
            install.enable_plugins(config, install.PLUGINS, True)
            backups = len(list(Path(tmp).glob("config.yaml.bak-jev-*")))
            install.enable_plugins(config, install.PLUGINS, True)
            self.assertEqual(len(list(Path(tmp).glob("config.yaml.bak-jev-*"))), backups)

    def test_empty_inline_list_and_missing_section(self):
        listed = "".join(f"  - {n}\n" for n in install.PLUGINS)
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text("plugins:\n  enabled: []\nother: 1\n")
            install.enable_plugins(config, install.PLUGINS, True)
            self.assertEqual(config.read_text(), f"plugins:\n  enabled:\n{listed}other: 1\n")
            config.write_text("other: 1\n")
            install.enable_plugins(config, install.PLUGINS, True)
            self.assertEqual(config.read_text(), f"other: 1\nplugins:\n  enabled:\n{listed}")


class HermesInstallTests(unittest.TestCase):
    def _fleet(self, tmp):
        root = Path(tmp) / "hermes"
        for home in (root, root / "profiles" / "alpha"):
            home.mkdir(parents=True)
            (home / "config.yaml").write_text(CONFIG)
        return root

    def test_full_install_links_every_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fleet(tmp)
            report = install.install_hermes(root, "alpha", check=False)
            self.assertTrue((root / "plugins" / "hermes-jev" / "jevkit" / "client.py").is_file())
            self.assertTrue((root / "profiles" / "alpha" / "plugins" / "hermes-jev").is_symlink())
            self.assertTrue((root / "profiles" / "alpha" / "skills" / "jev" / "jev-setup" / "SKILL.md").is_file())
            self.assertEqual(list(report["enabled_in"]), ["alpha"])
            self.assertNotIn("hermes-jev", (root / "config.yaml").read_text())
            install.uninstall_hermes(root)
            self.assertFalse((root / "profiles" / "alpha" / "plugins" / "hermes-jev").exists())
            self.assertEqual((root / "profiles" / "alpha" / "config.yaml").read_text(), CONFIG)

    def test_every_shipped_plugin_is_installed_and_enabled(self):
        # The installer named one plugin, so hermes-handoff was unreachable however
        # faithfully you followed the docs.
        self.assertIn("hermes-jev", install.PLUGINS)
        self.assertIn("hermes-handoff", install.PLUGINS)
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fleet(tmp)
            report = install.install_hermes(root, "all", check=False)
            for name in install.PLUGINS:
                plugin_dir = root / "plugins" / name
                self.assertTrue((plugin_dir / "plugin.yaml").is_file(), name)
                self.assertTrue((plugin_dir / "jevkit" / "compact.py").is_file(), name)
                self.assertTrue((root / "profiles" / "alpha" / "plugins" / name).is_symlink(), name)
                self.assertEqual(report["enabled_in"]["default"][name], "enabled")
                self.assertEqual(report["enabled_in"]["alpha"][name], "enabled")
                self.assertIn(f"- {name}", (root / "config.yaml").read_text())

    def test_nightly_script_is_installed_and_stays_runnable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fleet(tmp)
            install.install_hermes(root, "none", check=False)
            script = root / "scripts" / "nightly-handoff.py"
            self.assertTrue(script.is_file())
            self.assertTrue(os.access(script, os.X_OK))
            # It defaults --plugin to <home>/plugins/hermes-handoff, so the pair has to land together.
            self.assertTrue((root / "plugins" / "hermes-handoff" / "handoff.py").is_file())

    def test_uninstall_removes_what_was_installed_and_leaves_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fleet(tmp)
            install.install_hermes(root, "all", check=False)
            theirs = root / "scripts" / "backup-nightly.sh"
            theirs.write_text("# someone else's cron job\n")
            install.uninstall_hermes(root)
            for name in install.PLUGINS:
                self.assertFalse((root / "plugins" / name).exists(), name)
                self.assertFalse((root / "profiles" / "alpha" / "plugins" / name).exists(), name)
            self.assertFalse((root / "skills" / "jev").exists())
            self.assertFalse((root / "scripts" / "nightly-handoff.py").exists())
            self.assertTrue(theirs.is_file())
            self.assertEqual((root / "config.yaml").read_text(), CONFIG)

    def test_uninstall_leaves_no_empty_scripts_directory_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fleet(tmp)
            install.install_hermes(root, "none", check=False)
            install.uninstall_hermes(root)
            self.assertFalse((root / "scripts").exists())


class ReportWarningTests(unittest.TestCase):
    def test_path_warning_is_top_level_when_local_bin_is_not_on_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, report = run_installer(["--check", "--skills-dir", f"{tmp}/agent"], tmp)
            self.assertEqual(code, 0)
            self.assertFalse(report["cli"]["on_path"])
            warning = report["warning"]
            self.assertIn(str(Path(tmp) / ".local" / "bin"), warning)      # add this to PATH
            self.assertIn(str(install.REPO / "bin" / "jev"), warning)      # or call this instead
            self.assertIn("setup-key", warning)
            self.assertNotIn("No agent was found", warning)                # a skills folder was given

    def test_no_path_warning_when_local_bin_is_on_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            on_path = f"{tmp}/.local/bin:/usr/bin:/bin"
            code, report = run_installer(["--check", "--skills-dir", f"{tmp}/agent"], tmp, path=on_path)
            self.assertEqual(code, 0)
            self.assertTrue(report["cli"]["on_path"])
            self.assertNotIn("warning", report)

    def test_a_machine_with_no_agent_warns_instead_of_reporting_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, report = run_installer(["--check"], tmp)
            self.assertEqual(code, 0)
            self.assertEqual(report["skill_folders"], [])
            self.assertNotIn("hermes", report)
            self.assertIn("--skills-dir", report["warning"])
            self.assertFalse([step for step in report["next"] if "Hermes" in step])

    def test_hermes_next_step_survives_when_a_hermes_home_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes = Path(tmp) / ".hermes"
            hermes.mkdir()
            (hermes / "config.yaml").write_text(CONFIG)
            code, report = run_installer(["--check"], tmp)
            self.assertEqual(code, 0)
            self.assertIn("hermes", report)
            self.assertTrue([step for step in report["next"] if "Hermes" in step])
            self.assertNotIn("--skills-dir", report.get("warning", ""))


if __name__ == "__main__":
    unittest.main()
