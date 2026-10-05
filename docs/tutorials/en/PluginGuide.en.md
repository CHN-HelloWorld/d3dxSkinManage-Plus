# Plugin development guide

This guide is for developers who know Python and want to build extension tools for d3dxSkinManage-Plus. The application organizes Mods, categories, combination presets, and appearance variants by game. Plugins can combine these public capabilities into dedicated tools for organizing, checking, and operating on a library.

Plugins access the host through `src.plugin_sdk`, can use multiple Python modules and packages, declare plugin dependencies, activate on demand, and submit background tasks. Third-party Python dependencies are not installed automatically. Users install extensions from archives or plugin folders.

Start with the skeleton in section 1 and confirm that its command works. Then read the three bundled examples in this order. Each includes a manifest, source code, an English language pack, and usage instructions, and can be enabled directly. You do not need to enable them all at once.

| Order | Practical example | Purpose | Main topics |
| --- | --- | --- | --- |
| 1 | [Mod inventory export](../../../plugins/example.mod-inventory/README.md) | Export a CSV inventory for the current game or a category subtree | Multiple modules, commands activated on demand, configuration, background computation, and atomic file saving |
| 2 | [Preset audit](../../../plugins/example.preset-audit/README.md) | Find invalid Mod and variant references in combinations | Unresolved references, startup activation, pages, events, batched queries, and cleanup |
| 3 | [Preset cycling](../../../plugins/example.preset-cycle/README.md) | Switch to the previous or next combination using shortcuts or buttons | Write permissions, asynchronous operations, repeated requests, completion feedback, and cancellation |

Section 10 provides reading and practice steps for each example. Sections 2–9 cover the interfaces, section 11 covers custom interfaces, and section 12 covers development checks and troubleshooting.

## 1. Environment and your first plugin

### 1.1 Prepare the environment

Use Windows 10/11 x64 and Python 3.10–3.14. Developing from the source checkout is recommended. Run these commands at the repository root; skip the first two if your development environment is already set up:

```powershell
python -m venv .venv
& ".venv/Scripts/python.exe" -m pip install -e ".[dev]"
& ".venv/Scripts/python.exe" run_app.py
```

Confirm that the application starts, then add a test game and a few placeholder Mods. The first plugin only reads data. Preset cycling changes deployment, so check successful, failed, and cancelled operations in a test library.

### 1.2 Generate and enable the skeleton

```powershell
& ".venv/Scripts/python.exe" scripts/plugins/new_plugin.py com.example.mod-summary --name "Mod summary" --author "Example author"
```

The generated folder contains:

```text
plugins/com.example.mod-summary/
├── plugin.json
├── entry.py
├── commands.py
├── languages/en-US/strings.json
└── README.md
```

On the application's **Plugins** page, click **Open plugins folder** and confirm that this folder is present. Click **Rescan**, enable the plugin, and read the initial trust and permission prompt. Run **Show mod summary** from Tools or **Plugin commands** in the navigation area. It shows the total and enabled Mod counts for the current game. If no game is selected, it asks you to select one first.

The default shortcut is `Ctrl+Alt+M`, which you can change in **Settings → Shortcuts**. A conflict does not overwrite an existing binding. The command's returned Python dictionary is available to its caller; the plugin displays visible feedback itself by calling `notify()`.

If the target folder already exists, the scaffolding tool stops without overwriting files. `--dir` specifies the parent of the plugin folder: for example, `--dir "$env:TEMP/plugin-demo"` produces `$env:TEMP/plugin-demo/com.example.mod-summary/`. The host scans only its actual plugins folder and does not automatically scan other development folders.

### 1.3 Understand the minimal implementation

The scaffolding tool separates lifecycle code and command logic into `entry.py` and `commands.py`. The following minimal plugin uses a single entry file so that you can also create it manually. Save files as UTF-8 and use standard JSON for the manifest.

`plugin.json`:

```json
{
  "id": "com.example.mod-summary",
  "name": "Mod summary",
  "version": "0.1.0",
  "apiVersion": "2.0",
  "minAppVersion": "0.1.0",
  "author": "Example author",
  "description": "Mod summary plugin",
  "entry": "entry.py",
  "files": ["entry.py"],
  "permissions": ["mods:read", "ui:tools-page", "ui:shortcuts"],
  "contributes": {
    "commands": [
      {"id": "com.example.mod-summary.summary", "title": "Show mod summary"}
    ],
    "toolsPage": [
      {"command": "com.example.mod-summary.summary", "group": "Plugins"}
    ],
    "shortcuts": [
      {"command": "com.example.mod-summary.summary", "default": "Ctrl+Alt+M", "context": "window"}
    ]
  }
}
```

`entry.py`:

```python
"""Plugin entry point using only the public SDK."""

from __future__ import annotations

from src.plugin_sdk import PluginBase, PluginContext


class Plugin(PluginBase):
    """Query the current game through the public SDK and display enabled Mod counts."""

    def activate(self, ctx: PluginContext) -> None:
        """Keep the plugin context and register the summary command."""
        self._ctx = ctx
        ctx.commands.register("com.example.mod-summary.summary", self._summary)
        ctx.logger.info("Plugin activated: %s", ctx.meta.name)

    def _summary(self, **_) -> dict[str, int]:
        """Display total and enabled Mod counts and return the result for other commands."""
        tr = self._ctx.i18n.t
        if not self._ctx.game.current_game_id():
            self._ctx.ui.notify(tr("Select a game first."), "warning")
            return {"mods": 0, "enabled": 0}
        mods = self._ctx.mods.get_mods()
        enabled = sum(mod.enabled for mod in mods)
        self._ctx.ui.notify(tr("{total} Mods in total; {enabled} enabled.", total=len(mods), enabled=enabled))
        return {"mods": len(mods), "enabled": enabled}

    def deactivate(self) -> None:
        """Log plugin deactivation."""
        self._ctx.logger.info("Plugin deactivated: %s", self._ctx.meta.name)
```

The manifest declares the entry points users see; `activate()` binds each command ID to a handler. The declared and registered IDs must match. Query the current game on every execution instead of treating an old snapshot as a live model.

Declared commands automatically become activation triggers, so you do not need to add `onCommand`. A **Ready** status after enabling is normal: the host has registered the buttons and shortcuts and runs the entry point when first used. See section 7.5 for the language pack format.

### 1.4 Continue with the project conventions

Use the following coding conventions when writing plugins. The three bundled examples follow them as well:

- Use four-space indentation, double quotes, a 120-character line width, and LF line endings for Python. The Ruff version is pinned in `pyproject.toml`.
- Use `snake_case` identifiers, modern type annotations, and concise Chinese module, class, and function docstrings. Qt overrides retain the names required by their interfaces.
- Translate user-facing text through `ctx.i18n.t()`. Keep command IDs, setting keys, and persistent identities stable, and describe operations in Chinese in logs.
- Separate pure logic, such as snapshot transformations, reference checks, and candidate selection, from Qt widgets and task callbacks. Avoid duplicating host internals inside a plugin.
- Import only the public SDK when accessing host capabilities. Use standard PySide6 widgets and the host theme, and avoid hard-coded colors.

To build on a practical example, copy its folder and change the `id`, name, and all contribution IDs in `plugin.json`. The three examples construct command or page IDs with `ctx.plugin_id` in code; you must still change the corresponding prefixes in their manifests. Reload after editing and follow section 12 to verify the result.

