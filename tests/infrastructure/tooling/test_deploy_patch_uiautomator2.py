from deploy.patch import patch_uiautomator2


def test_patch_uiautomator2_keeps_v3_entrypoint_without_legacy_installer():
    patch_uiautomator2()
