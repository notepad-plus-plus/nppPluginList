import re
import unittest
from pathlib import Path

import validator


ROOT = Path(__file__).resolve().parents[1]


class RepositoryPolicyTests(unittest.TestCase):
    def test_all_catalogs_pass_offline_validation(self):
        reporter = validator.Reporter(api_url="")
        catalogs = validator.validate_local_catalogs(validator.ARCHITECTURES, reporter)
        self.assertEqual(set(validator.ARCHITECTURES), set(catalogs), reporter.errors)
        self.assertFalse(reporter.errors)

    def test_generated_documents_match_catalogs(self):
        reporter = validator.Reporter(api_url="")
        catalogs = validator.validate_local_catalogs(validator.ARCHITECTURES, reporter)
        for architecture, catalog in catalogs.items():
            validator.check_generated_document(
                catalog, validator.ARCHITECTURES[architecture].doc_path, reporter
            )
        self.assertFalse(reporter.errors)

    def test_workflow_actions_are_pinned_to_full_commit_hashes(self):
        for workflow in (ROOT / ".github/workflows").glob("*.yml"):
            text = workflow.read_text(encoding="utf-8")
            references = re.findall(
                r"^\s*uses:\s*([^\s@]+)@([^\s#]+)", text, re.MULTILINE
            )
            self.assertTrue(references, workflow)
            for action, reference in references:
                if action.startswith("./"):
                    continue
                self.assertRegex(
                    reference, r"^[0-9a-f]{40}$", f"{workflow}: {action}@{reference}"
                )

    def test_workflows_have_bounded_jobs(self):
        for workflow in (ROOT / ".github/workflows").glob("*.yml"):
            text = workflow.read_text(encoding="utf-8")
            jobs_section = text.split("\njobs:\n", 1)[1]
            job_count = len(
                re.findall(r"^  [A-Za-z0-9_-]+:\s*$", jobs_section, re.MULTILINE)
            )
            timeout_count = len(
                re.findall(
                    r"^    timeout-minutes:\s*\d+\s*$", jobs_section, re.MULTILINE
                )
            )
            self.assertEqual(job_count, timeout_count, workflow)

    def test_release_signing_is_exact_and_not_floating(self):
        release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertNotIn("--prerelease", release)
        self.assertNotIn("**/*.dll", release)
        self.assertIn("--version $env:SIGN_CLI_VERSION", release)
        self.assertIn("$env:SIGN_TARGET", release)

    def test_release_oidc_permission_is_isolated_from_build_and_validation(self):
        release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        build_job, sign_job = release.split("\n  sign:\n", 1)
        self.assertNotIn("id-token: write", build_job)
        self.assertIn("id-token: write", sign_job)
        self.assertIn("needs: build", sign_job)
        self.assertIn("actions/download-artifact@", sign_job)

    def test_locked_requirements_have_hashes_and_cover_direct_requirements(self):
        direct = {
            line.split("==", 1)[0].lower()
            for line in (ROOT / "requirements.txt")
            .read_text(encoding="utf-8")
            .splitlines()
            if line and not line.startswith("#")
        }
        lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
        blocks = re.split(r"(?m)(?=^[A-Za-z0-9][A-Za-z0-9_.-]*==)", lock)
        locked = set()
        for block in blocks:
            match = re.match(r"([A-Za-z0-9][A-Za-z0-9_.-]*)==", block)
            if not match:
                continue
            locked.add(match.group(1).lower())
            self.assertIn("--hash=sha256:", block, match.group(1))
        self.assertLessEqual(direct, locked)

    def test_resource_identifies_the_output_as_a_dll(self):
        resource = (ROOT / "src/nppPluginList.rc").read_text(encoding="utf-8")
        self.assertIn("FILETYPE VFT_DLL", resource)


if __name__ == "__main__":
    unittest.main()
