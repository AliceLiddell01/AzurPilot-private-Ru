from unittest.mock import patch

from deploy import patch as patch_module
from deploy.Windows import patch as windows_patch


def test_pre_checks_do_not_require_legacy_uiautomator2_installer():
    assert not hasattr(patch_module, "patch_uiautomator2")
    assert not hasattr(windows_patch, "patch_uiautomator2")
    assert not hasattr(patch_module, "site_package_file")
    assert not hasattr(patch_module, "patch_apkutils2")

    with patch.object(patch_module, "check_running_directory") as check:
        patch_module.pre_checks()
    check.assert_called_once_with()

    with patch.object(windows_patch, "check_running_directory") as check:
        windows_patch.pre_checks()
    check.assert_called_once_with()
