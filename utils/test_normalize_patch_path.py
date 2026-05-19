import os
import tempfile
import unittest
# python -m unittest test_normalize_patch_path
# Assume normalize_patch_path is located in agent_tools.py or utils/path_utils.py
from agent_tools import normalize_patch_path

class TestNormalizePatchPath(unittest.TestCase):
    """Cross-host path normalization test: does not depend on any local absolute paths"""

    def setUp(self):
        # 1. Create a temporary directory as the simulated project root to fully isolate the host environment
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = self.temp_dir.name
        
        # 2. Dynamically build the standard project structure
        self.oss_fuzz_dir = os.path.join(self.base_dir, "oss-fuzz/projects/cert-manager")
        self.src_dir = os.path.join(self.base_dir, "process/project/cert-manager")
        os.makedirs(self.oss_fuzz_dir, exist_ok=True)
        os.makedirs(self.src_dir, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_absolute_to_relative(self):
        """Absolute path → relative path (based on dynamic base_dir)"""
        abs_path = os.path.join(self.oss_fuzz_dir, "build.sh")
        result = normalize_patch_path(abs_path, base_dir=self.base_dir)
        self.assertEqual(result, "oss-fuzz/projects/cert-manager/build.sh")

    def test_relative_path_passthrough(self):
        """Already a relative path → keep as is (no redundant prefix added)"""
        rel_path = "process/project/cert-manager/go.mod"
        result = normalize_patch_path(rel_path, base_dir=self.base_dir)
        self.assertEqual(result, rel_path)

    def test_cross_platform_slash_normalization(self):
        """Windows-style backslashes → uniformly converted to forward slashes"""
        win_path = "oss-fuzz\\projects\\cert-manager\\build.sh"
        result = normalize_patch_path(win_path, base_dir=self.base_dir)
        self.assertEqual(result, "oss-fuzz/projects/cert-manager/build.sh")
        self.assertNotIn("\\", result)

    def test_depth_traversal_cleanup(self):
        """Clean up redundant ../ and ./ symbols"""
        messy_path = os.path.join(self.base_dir, "process/project", "..", "process/project/cert-manager", "./go.mod")
        result = normalize_patch_path(messy_path, base_dir=self.base_dir)
        self.assertEqual(result, "process/project/cert-manager/go.mod")

    def test_empty_path_handling(self):
        """Empty string or blank path → return safely"""
        self.assertEqual(normalize_patch_path("", base_dir=self.base_dir), "")
        self.assertEqual(normalize_patch_path("   ", base_dir=self.base_dir), "   ")

if __name__ == "__main__":
    unittest.main()
