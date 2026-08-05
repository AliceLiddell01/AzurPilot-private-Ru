"""Global/EN game-server and package contract."""

server = "en"

VALID_SERVER = ("en",)
GLOBAL_PACKAGE = "com.YoStarEN.AzurLane"
VALID_PACKAGE = {
    GLOBAL_PACKAGE: "en",
}
VALID_CHANNEL_PACKAGE = {}
DICT_PACKAGE_TO_ACTIVITY = {
    GLOBAL_PACKAGE: "com.manjuu.azurlane.PrePermissionActivity",
}
VALID_SERVER_LIST = {
    "en": [
        "Avrora",
        "Lexington",
        "Sandy",
        "Washington",
        "Amagi",
        "Little Enterprise",
    ],
}


def to_server(package_or_server: str) -> str:
    """Return EN for the supported server or package; reject everything else."""
    if package_or_server == "en":
        return "en"
    if package_or_server in (GLOBAL_PACKAGE, "auto"):
        return "en"
    raise ValueError(f"Unsupported Global/EN package or server: {package_or_server}")


def to_package(package_or_server: str) -> str:
    """Return the only supported Global package; reject everything else."""
    if package_or_server in ("en", GLOBAL_PACKAGE, "auto"):
        return GLOBAL_PACKAGE
    raise ValueError(f"Unsupported Global/EN package or server: {package_or_server}")


def set_server(package_or_server: str) -> None:
    """Validate before changing global state or releasing resources."""
    global server
    validated = to_server(package_or_server)
    server = validated

    from module.base.resource import release_resources

    release_resources()
