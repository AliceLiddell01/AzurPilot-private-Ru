from pathlib import Path
from unittest.mock import patch

import pytest

from deploy.patch import patch_uiautomator2


def test_patch_uiautomator2_fails_closed_without_local_cache(tmp_path: Path):
    missing_cache = tmp_path / "uiautomator2cache" / "cache"
    init_file = tmp_path / "uiautomator2" / "init.py"
    init_file.parent.mkdir(parents=True)
    original = "appdir = '/remote/cache'\nself.minicap_urls = ['remote']\n"
    init_file.write_text(original, encoding="utf-8")

    with patch(
        "deploy.patch.site_package_file",
        side_effect=[str(missing_cache), str(init_file)],
    ):
        with pytest.raises(RuntimeError, match="uiautomator2cache/cache"):
            patch_uiautomator2()

    assert init_file.read_text(encoding="utf-8") == original


def test_patch_uiautomator2_repoints_to_installed_local_cache(tmp_path: Path):
    cache = tmp_path / "uiautomator2cache" / "cache"
    cache.mkdir(parents=True)
    init_file = tmp_path / "uiautomator2" / "init.py"
    init_file.parent.mkdir(parents=True)
    init_file.write_text(
        "appdir = '/remote/cache'\nself.minicap_urls = ['remote']\n",
        encoding="utf-8",
    )

    with patch(
        "deploy.patch.site_package_file",
        side_effect=[str(cache), str(init_file)],
    ):
        patch_uiautomator2()

    result = init_file.read_text(encoding="utf-8")
    assert "self.minicap_urls" not in result
    assert "[] = ['remote']" in result
    assert "../../uiautomator2cache" in result
