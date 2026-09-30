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

