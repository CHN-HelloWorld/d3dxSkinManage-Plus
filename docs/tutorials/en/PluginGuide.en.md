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

## 2. Directory and loading contract

- The only loading location is `plugins/` under the application root: the repository root when running from source or the folder containing the executable when running a distribution. A Nuitka one-file build uses the outer launcher's folder.
- **Open plugins folder**, archive installation, rescanning, and reloading all use this location. The list does not invent missing plugins from source packages or saved enabled-state records.
- All plugins, including bundled examples, follow the same trust, permission, and uninstall rules. Initial activation requires trust confirmation. Removing a folder and rescanning disables the plugin and removes its record.
- If the application folder is not writable, the host uses its standard data-folder fallback. Use the actual location shown by **Open plugins folder**.
- One plugin occupies one folder, which **must** contain `plugin.json`.
- The manifest's `entry` points to a `.py` file that **must export a class named `Plugin`** inheriting from `src.plugin_sdk.PluginBase`.
- When accessing host code, **import only** `src.plugin_sdk` and its submodules. The security scan blocks imports from internal modules such as `src.core` and `src.ui`. The Python standard library, third-party libraries already available in the host, and PySide6 widgets remain usable with the required permissions.
- The host creates an isolated package namespace for each load, supporting relative imports such as `from .commands import show_summary`, nested packages, and package resources. Do not import a neighboring file with `import helper` or modify global `sys.path`.
- Reloading reads current source and clears all plugin submodules and import finders. Even edits with unchanged file size and timestamps do not reuse stale bytecode. Reloading a plugin does not remove shared third-party dependencies.

---

## 3. plugin.json fields

| Field | Required | Description |
|------|------|------|
| `id` | ✓ | Globally unique, reverse-domain style, 2–64 characters; only letters, digits, `.`, `_`, and `-` |
| `name` `version` | ✓ | Display name / semantic version |
| `apiVersion` | ✓ | Required SDK version, currently `"2.0"`; the major version must match and the required minor version must not exceed the host's |
| `entry` | ✓ | Relative `.py` path inside the plugin folder, exporting the `Plugin` class; absolute paths, `..`, and links outside the folder are prohibited |
| `minAppVersion` | | Minimum application version, such as `"0.1.0"`; checked before importing code, with loading rejected if unmet |
| `author` `description` `homepage` | | Metadata |
| `permissions` | | See §6 |
| `contributes` | | See §5 |
| `activationEvents` | | Explicit activation conditions, defaulting to an empty array; command and view conditions are inferred automatically; see §4 |
| `dependencies` | | Mapping from plugin IDs to version ranges; see §4.2; dependencies are not installed or authorized automatically |
| `files` | | Relative files or folders to package; explicit declarations are required by the packaging tool; no wildcards |
| `$schema` | | Editor validation location; repository plugins can use `../../docs/plugin.schema.json` |

The [JSON Schema](../../plugin.schema.json) provides editor completion. The host also validates command namespaces, duplicate declarations, menu and shortcut references, setting defaults, entry paths, and dependency ranges. Unknown fields, unknown permissions, and invalid declarations cause errors instead of being silently ignored.

---

## 4. Lifecycle

### 4.1 Discovery and activation

Discovery parses manifests without executing plugin code. Enabled, trusted plugins whose version and dependency requirements are satisfied enter `ready`. Their commands, menus, shortcuts, and settings can appear immediately.

| Activation condition | Trigger |
| --- | --- |
| `onCommand:<command ID>` | First execution of the plugin's command; inferred from `contributes.commands` |
| `onView:<view ID>` | First opening of the corresponding plugin details view; inferred from `contributes.views` |
| `onEvent:<event name>` | Receipt of a host event from §7.3; the host activates the plugin and then delivers that event |
| `onStartup` | Immediate activation after startup or rescanning; used for plugins needing persistent event subscriptions or dynamic navigation pages |

For example, `"activationEvents": ["onEvent:game_changed"]` starts a plugin on the first game switch. Declared commands and views do not need to be repeated in the array. A service used only by other plugins can omit activation events and be started through its consumers' dependencies.

```python
from src.plugin_sdk import PluginBase, PluginContext


class Plugin(PluginBase):
    """Describe the functionality provided by this plugin."""

    def activate(self, ctx: PluginContext) -> None:
        """Keep the context and register commands, events, or UI factories."""
        ...

    def deactivate(self) -> None:
        """Stop plugin-owned tasks; the host cleans up registered resources."""
        ...
```

- An exception triggers **automatic fault isolation**: the host logs the error and disables the plugin while continuing to run.
- Exceptions from command and event handlers trigger the same isolation.
- Runtime states include `discovered` (awaiting trust), `ready`, `activating`, `active`, `blocked` (dependencies unavailable), `disabled`, and `error`. The **Permissions and source** section in plugin details shows dependencies, activation reasons, and loading and activation times.
- Rescanning fully deactivates old instances before discovering plugins and registering contributions again. Reloading or updating a dependency first deactivates its transitive consumers, then recreates previously active consumers. Unrelated plugins continue running.
- Disabling, reloading, or uninstalling cancels unfinished operations and removes task callbacks. The host safely commits completed file operations. `deactivate()` must still release the plugin's own timers, threads, and other resources.
- After deactivation, business writes through the old `ctx` return failed tasks. Queries, storage, configuration, commands, events, UI, and background-task entry points reject further use. Use `deactivate()` to release your own resources; you can no longer query or modify the host at this point. Metadata and logging remain available for diagnostics.

### 4.2 Plugin dependencies and public APIs

A consumer declares `"dependencies": {"com.example.provider": "^1.2.0"}` in its manifest. The host validates the entire dependency graph before activating dependencies ahead of consumers. Missing, disabled, untrusted, incompatible, or cyclic dependencies prevent the consumer from being imported and show a specific reason. Dependency failures stop consumers but preserve their enabled-state preference; consumers can be used again after the dependency recovers.

| Constraint | Accepted versions |
| --- | --- |
| `1.2.3` | The same semantic version, ignoring `+build` metadata |
| `*` | Any stable release |
| `^1.2.3` | `>=1.2.3 <2.0.0` |
| `^0.2.3` / `^0.0.3` | Up to, but excluding, the next minor / patch version, respectively |
| `~1.2.3` | `>=1.2.3 <1.3.0` |
| `>=1.2.0 <2.0.0` | All space-separated comparisons must be satisfied |

The operators `= > >= < <=` are supported. `||`, wildcard version components, and hyphen ranges are not supported. A prerelease is accepted only when the range explicitly includes a prerelease boundary with the same three-part version, such as `>=1.1.0-beta.1 <1.1.0`.

A service plugin can return its API object from `activate()`. Consumers retrieve it in their own `activate()` with `ctx.extensions.get_exports("com.example.provider")`. Requesting an undeclared dependency raises `PluginError`. An exported object is valid only for the current activation cycle; consumers are recreated when the dependency reloads. Do not save API objects in persistent storage. The two plugins agree on interface signatures, and the version range expresses that agreement. Cross-plugin commands can also be composed with `ctx.commands.execute()`.

### 4.3 Resource scopes

`ctx.events.on()`, `ctx.commands.register()`, `ctx.ui.register_view()`, and `ctx.ui.register_page()` return a `Disposable`, with ownership also registered automatically by the host. Call the handle's `dispose()` to unregister early; repeated disposal has no side effects. After replacing a command, its old handle cannot remove the new handler.

Manage your own resources with `ctx.subscriptions.add(cleanup_callback)` or `add(an_object_implementing_dispose)`. The collection disposes resources in reverse order. If one cleanup fails, it logs the failure and continues with the others. `DisposableStore` supports nesting and `with`, making it suitable for subscriptions scoped to a page or operation. Background-task and configuration subscriptions also return handles that can be added to the collection.

```python
from src.plugin_sdk import DisposableStore
from src.plugin_sdk.events import GAME_CHANGED


def observe_game(ctx, callback):
    """Create an independently disposable scope, with plugin cleanup as a fallback."""
    scope = ctx.subscriptions.add(DisposableStore())
    scope.add(ctx.events.on(GAME_CHANGED, callback))
    return scope
```

---

## 5. Contribution points (contributes)

```jsonc
"contributes": {
  "commands":  [{ "id": "myplugin.scan", "title": "Scan" }, { "id": "myplugin.inspect", "title": "Inspect" }],
  "shortcuts": [{ "command": "myplugin.scan", "default": "Ctrl+Alt+S", "context": "window" }],
  "toolsPage": [{ "command": "myplugin.scan", "group": "Plugins", "icon": "navigation/tools" }],
  "menus":     { "mod-context": [{ "command": "myplugin.inspect", "when": "modSelected" }] },
  "settings":  [{ "key": "threshold", "type": "int", "default": 2, "title": "Threshold" }],
  "views":     [{ "id": "myplugin.panel", "title": "My panel" }]
}
```

| Contribution | Location | Description |
|--------|----------|------|
| `commands` | — | Executable unit for contributions; implement with `ctx.commands.register(id, handler)` |
| `toolsPage` | Buttons in the Tools page's **Plugins** group | Clicking executes `command` |
| `menus."mod-context"` | Mod card context menu | Receives the `mod_id` keyword argument; `when` supports `modSelected` / `always` |
| `menus."character-context"` | Character card context menu | Receives the `node_id` keyword argument; `when` supports `characterSelected` / `always` |
| Global commands | **Plugin commands** button in navigation | All plugin `commands` appear in this panel automatically, with no additional declaration; clicking executes them directly |
| `settings` | Plugin details | Types `bool` / `int` / `float` / `str`, with range, length, and enum constraints; use `ctx.configuration` |
| `views` | Inside the plugin's card on the Plugins page | Supply a QWidget factory with `ctx.ui.register_view(view_id, factory)` |
| `shortcuts` | Plugin group in **Settings → Shortcuts** | Declare default keys and focus context for commands; requires `ui:shortcuts` |

Command IDs must start with the complete plugin ID followed by `.`, such as `com.example.mod-summary.summary`. A plugin can update its own handlers but cannot overwrite another plugin's commands. Menus, Tools buttons, and shortcuts must reference commands declared by the same plugin. Every declared command must be registered before `activate()` returns; a missing handler causes activation to fail and all contributions to be rolled back.

`context` defaults to `window` and can also be `overview` (category overview list) or `preset` (preset list). Shortcuts use the host's conflict checks, text-editing priority, input-method composition rules, and dialog rules. System-reserved keys cannot be bound. Plugin shortcuts work only inside the application. A default key never overwrites an existing binding; the new command's unbound state is saved, and restarting or reloading does not change assigned keys. Users can reassign shortcuts or restore defaults in Settings. Changes take effect immediately. Disabling, reloading, fault isolation, and uninstalling remove active bindings while preserving user preferences for the next activation.

---

