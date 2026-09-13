from deploy import patch as patch_module


def test_patch_uiautomator2_keeps_v3_entrypoint_without_legacy_installer():
    assert callable(patch_module.patch_uiautomator2)
    assert not hasattr(patch_module, "site_package_file")
    assert not hasattr(patch_module, "patch_apkutils2")

    patch_module.patch_uiautomator2()
