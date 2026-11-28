import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

from tools import install_skill


ROOT = Path(__file__).resolve().parents[1]
ALLOWED_FIELDS = {"name", "description", "license", "allowed-tools", "metadata"}
WORKFLOW_REFS = (
    "content-enrich.md",
    "knowledge-base-management.md",
    "learning-notes-automation.md",
    "industry-intelligence-radar.md",
)


class UnifiedBundleInstallTest(unittest.TestCase):
    def test_bundle_layout_frontmatter_and_references(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            dest = base / "agent-skills"
            exit_code = install_skill.main(["--bundle", "--dest", str(dest)])
            self.assertEqual(exit_code, 0)

            moved = base / "isolated-skills" / "chubby"
            moved.parent.mkdir(parents=True)
            shutil.move(str(dest / "chubby"), moved)

            skill_mds = list(moved.rglob("SKILL.md"))
            self.assertEqual(skill_mds, [moved / "SKILL.md"])

            content = (moved / "SKILL.md").read_text(encoding="utf-8")
            self.assertLess(len(content.splitlines()), 120)
            match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", content, re.DOTALL)
            self.assertIsNotNone(match)
            fields = yaml.safe_load(match.group(1))
            self.assertIsInstance(fields, dict)
            self.assertLessEqual(set(fields), ALLOWED_FIELDS)
            self.assertEqual(fields.get("name"), "chubby")
            desc = fields.get("description")
            self.assertIsInstance(desc, str)
            self.assertTrue(desc.strip())
            self.assertLessEqual(len(desc), 1024)
            self.assertNotRegex(desc, r"[<>]")
            for k, v in fields.get("metadata", {}).items():
                self.assertIsInstance(k, str)
                self.assertIsInstance(v, str)

            for snippet in ("tools/chubby_ingest.py", "--enrich", "CDP", "/question/"):
                self.assertIn(snippet, content)

            refs_dir = moved / "references"
            self.assertEqual(sorted(p.name for p in refs_dir.glob("*.md")), sorted(WORKFLOW_REFS))
            for ref_name in WORKFLOW_REFS:
                self.assertIn(f"references/{ref_name}", content)
                ref_text = (refs_dir / ref_name).read_text(encoding="utf-8")
                self.assertFalse(ref_text.startswith("---\n"))

            for module in (
                "__init__.py",
                "chubby_ingest.py",
                "platform_health.py",
                "source_identity.py",
                "import_document.py",
                "vault_index.py",
                "vault_curator.py",
                "evidence_brief.py",
            ):
                self.assertTrue((moved / "tools" / module).is_file(), module)
            self.assertTrue((moved / "chubby_common" / "__init__.py").is_file())
            self.assertGreaterEqual(len(list((moved / "platforms").glob("*.yaml"))), 9)
            self.assertTrue((moved / "LICENSE").is_file())

            manifest = json.loads((moved / "installation.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["bundle"])
            self.assertEqual(manifest["skill"], "chubby")
            self.assertEqual(set(manifest["skills"]), set(install_skill.SUPPORTED_SKILLS))

    def test_bundled_scripts_isolated_import_and_ingest_from_external_cwd(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            dest = base / "install"
            bundle = install_skill.install_bundle(dest)
            moved = base / "portable" / "chubby"
            moved.parent.mkdir(parents=True)
            shutil.move(str(bundle), moved)

            external_vault = base / "obsidian-vault"
            external_vault.mkdir()

            # python -I isolated import for all bundled scripts and tools
            scripts = sorted(
                p
                for p in list(moved.glob("*/scripts/*.py")) + list((moved / "tools").glob("*.py"))
                if p.name != "__init__.py"
            )
            self.assertGreaterEqual(len(scripts), 20)
            for script in scripts:
                with self.subTest(script=script.relative_to(moved)):
                    code = (
                        "import runpy, sys; "
                        "m = runpy.run_path(sys.argv[1], run_name='bundled_probe'); "
                        "assert 'VAULT_INDEX_ERROR' not in m or m['vault_index'] is not None, "
                        "m.get('VAULT_INDEX_ERROR')"
                    )
                    res = subprocess.run(
                        [sys.executable, "-I", "-c", code, str(script)],
                        cwd=external_vault,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=15,
                    )
                    self.assertEqual(res.returncode, 0, f"{script}: {res.stderr}")

            # Verify detect_skill and platform resolution from moved bundle
            ingest_py = moved / "tools" / "chubby_ingest.py"
            probe_code = (
                "import runpy, sys; "
                "m = runpy.run_path(sys.argv[1]); "
                "assert m['detect_skill']('BV1xx411c7mD') == 'bilibili'; "
                "assert m['detect_skill']('https://www.zhihu.com/question/123') == 'zhihu'; "
                "assert m['ROOT'] == sys.argv[2], (m['ROOT'], sys.argv[2]); "
                "assert m['script_path']('youtube').startswith(sys.argv[2]); "
                "assert m['script_path']('document').startswith(sys.argv[2])"
            )
            res = subprocess.run(
                [sys.executable, "-I", "-c", probe_code, str(ingest_py), str(moved)],
                cwd=external_vault,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(res.returncode, 0, res.stderr)

            # Verify real local document ingest via bundled chubby_ingest.py from external vault cwd
            doc = external_vault / "sample.txt"
            doc.write_text("Isolated bundle document ingest check.", encoding="utf-8")
            out_dir = external_vault / "00_Inbox"
            res = subprocess.run(
                [sys.executable, "-I", str(ingest_py), str(doc), "-o", str(out_dir)],
                cwd=external_vault,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            written = Path(res.stdout.strip().splitlines()[-1])
            self.assertTrue(written.is_file())
            self.assertIn("Isolated bundle document ingest check.", written.read_text(encoding="utf-8"))

            # Verify bundled child script receives chubby_common and runs from external cwd
            x_out = external_vault / "x_out"
            res = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(ingest_py),
                    "https://x.com/example/status/42",
                    "--output",
                    str(x_out),
                    "--fallback-only",
                    "--fallback-text",
                    str(doc),
                ],
                cwd=external_vault,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertGreaterEqual(len(list(x_out.glob("*.md"))), 1)

            # Verify --enrich resolves bundled content-enrich/scripts/enrich.py from moved dir
            enrich_probe = "\n".join(
                [
                    "import os, runpy, sys",
                    "from pathlib import Path",
                    "from unittest.mock import patch",
                    "m = runpy.run_path(sys.argv[1])",
                    "calls = []",
                    "def fake_run(cmd, dry_run=False):",
                    "    calls.append(cmd)",
                    "    if len(cmd) >= 5 and cmd[3] == '--output':",
                    "        p = Path(cmd[4]) / 'note.md'",
                    "        p.write_text('# T\\n', encoding='utf-8')",
                    "        return str(p)",
                    "    return ''",
                    "enrich_py = os.path.join(m['skill_dir']('content-enrich'), 'scripts', 'enrich.py')",
                    "assert os.path.isfile(enrich_py) and enrich_py.startswith(sys.argv[2]), enrich_py",
                    "with patch.dict(m['main'].__globals__, {'run_command': fake_run}), patch.object(",
                    "    sys, 'argv', ['chubby_ingest.py', sys.argv[3], '--enrich', '-o', sys.argv[4]]",
                    "):",
                    "    rc = m['main']()",
                    "assert rc == 0 and len(calls) == 2 and calls[1][1] == enrich_py, (rc, calls)",
                ]
            )
            res = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    enrich_probe,
                    str(ingest_py),
                    str(moved),
                    str(doc),
                    str(external_vault / "enriched_out"),
                ],
                cwd=external_vault,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(res.returncode, 0, res.stderr)

            # Verify Zhihu question-only URL prompts cleanly and writes nothing
            z_out = external_vault / "zhihu_out"
            res = subprocess.run(
                [sys.executable, "-I", str(ingest_py), "https://www.zhihu.com/question/123456", "-o", str(z_out)],
                cwd=external_vault,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("纯问题链接", res.stderr)
            self.assertFalse(z_out.exists())

    def test_clean_legacy_removes_only_known_skills_and_preserves_venvs(self):
        with tempfile.TemporaryDirectory() as temporary:
            dest = Path(temporary) / "skills"
            dest.mkdir()

            # Legacy skills without venv -> completely removed
            plain_legacy = dest / "x-ingest"
            (plain_legacy / "scripts").mkdir(parents=True)
            (plain_legacy / "SKILL.md").write_text("old", encoding="utf-8")
            (plain_legacy / "scripts" / "fetch_tweet.py").write_text("old", encoding="utf-8")

            # Legacy skill with top-level .venv -> skill removed, .venv preserved
            venv_legacy = dest / "douyin-transcribe"
            (venv_legacy / ".venv" / "bin").mkdir(parents=True)
            (venv_legacy / ".venv" / "bin" / "python").write_text("py", encoding="utf-8")
            (venv_legacy / "SKILL.md").write_text("old", encoding="utf-8")

            # Legacy skill with nested venv -> skill removed, nested venv preserved
            nested_legacy = dest / "bilibili-transcribe"
            (nested_legacy / "scripts" / "venv").mkdir(parents=True)
            (nested_legacy / "scripts" / "venv" / "pyvenv.cfg").write_text("home=.", encoding="utf-8")
            (nested_legacy / "SKILL.md").write_text("old", encoding="utf-8")

            # Unrelated directory -> untouched
            unrelated = dest / "custom-user-skill"
            unrelated.mkdir()
            (unrelated / "SKILL.md").write_text("keep me", encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools" / "install_skill.py"),
                    "--bundle",
                    "--clean-legacy",
                    "--dest",
                    str(dest),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertTrue((dest / "chubby" / "SKILL.md").is_file())
            self.assertFalse(plain_legacy.exists())
            self.assertFalse((venv_legacy / "SKILL.md").exists())
            self.assertTrue((venv_legacy / ".venv" / "bin" / "python").is_file())
            self.assertFalse((nested_legacy / "SKILL.md").exists())
            self.assertTrue((nested_legacy / "scripts" / "venv" / "pyvenv.cfg").is_file())
            self.assertEqual((unrelated / "SKILL.md").read_text(encoding="utf-8"), "keep me")
            self.assertIn("Warning: preserved virtual environment", proc.stderr)
            self.assertIn(".venv", proc.stderr)
            self.assertIn("venv", proc.stderr)

    def test_bundle_name_customization_and_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            dest = Path(temporary) / "skills"
            dest.mkdir()
            (dest / "x-ingest").mkdir()
            (dest / "x-ingest" / "SKILL.md").write_text("old", encoding="utf-8")

            bundle = install_skill.install_bundle(dest, bundle_name="my-chubby")
            self.assertEqual(bundle.name, "my-chubby")
            self.assertIn("name: my-chubby\n", (bundle / "SKILL.md").read_text(encoding="utf-8"))

            # Existing target aborts without wiping legacy skills even with clean_legacy=True
            with self.assertRaises(FileExistsError):
                install_skill.install_bundle(dest, bundle_name="my-chubby", clean_legacy=True)
            self.assertTrue((dest / "x-ingest" / "SKILL.md").is_file())

            for bad in ("../bad", "Bad_Name", "a" * 65):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    install_skill.install_bundle(dest, bundle_name=bad)


if __name__ == "__main__":
    unittest.main()
