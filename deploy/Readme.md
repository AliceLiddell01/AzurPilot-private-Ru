# Deploy

Этот каталог содержит материалы Docker deployment для AzurPilot.

Prepare AzurPilot by running `azur build` in the repository root, then use
`azur start` for the WebUI lifecycle.

This entry point bootstraps the project-local `.venv` with `uv` and syncs
dependencies from `pyproject.toml` and `uv.lock` before continuing. It does not
install packages into the system Python environment.


# Launcher

The supported launcher is the installed `azur` console command. There is no
second batch launcher or hidden installer path.

