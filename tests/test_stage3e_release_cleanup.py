import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LegacyReleaseInfrastructureTests(unittest.TestCase):
    def test_product_branch_has_no_git_over_cdn_release_code(self):
        removed_paths = (
            '.github/workflows/git-over-cdn-ssh.yml',
            '.github/workflows/git-over-cdn-123pan.yml',
            '.github/workflows/cloudflare-pages-git-over-cdn.sh',
            '.github/scripts/build_git_over_cdn.py',
            '.github/scripts/build_git_over_cdn_eo_esa.mjs',
            '.github/scripts/upload_123pan.py',
            '.github/scripts/package.json',
        )

        for relative_path in removed_paths:
            with self.subTest(relative_path=relative_path):
                self.assertFalse((ROOT / relative_path).exists())


if __name__ == '__main__':
    unittest.main()
