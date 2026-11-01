## Chapter 7: Tools, plugins and About

### 7.1 Tools

![Category and maintenance tools](../../images/guide-en/17-工具箱.webp)

Tools is divided into category organization, maintenance and troubleshooting, and tools contributed by enabled plugins.

| Entry | Purpose |
| --- | --- |
| Import category preset | Adds categories and category images to the destination game |
| Export category preset | Shares the current game's complete category tree, optionally with images |
| Rebuild Index | Rescans the file library to restore the index |
| View Logs | Shows runtime records by startup session |
| Plugin tools | Runs features provided by enabled plugins |

Cache cleanup is under **Settings → General → Images and cache**. Downloaded source package cleanup is under **General → Download data**. To export mods, select the items in Category Overview.

### 7.2 Importing a category preset

A category preset uses the **`.d3dxcp`** extension. It contains a category structure and optional category images, without mod contents or a complete preset of enabled mods.

![Category preset controls](../../images/guide-en/10-分类预设.webp)

Choose **Import category preset** in Tools, or **Start with a category preset** on an empty Category Overview page:

1. Select a `.d3dxcp` file.
2. Check the destination game and review the complete category tree.
3. Review the counts for new folders, existing folders, images to add, and path conflicts.
4. Locate and resolve any conflicts, then click **Compare again**.
5. After checking the result, click **Start import**, then **View categories** when it finishes.

If the game suggested by the file differs from the current game, the interface prompts you to check the destination. If every folder already exists and no images need adding, it explains that no import is needed.

Existing categories and category images are handled according to the comparison results. Importing a preset does not mean replacing the entire existing tree. Resolve path or type conflicts before importing.

Cancellation takes effect at a safe boundary. If the app reports that categories have been imported but cleanup is pending, keep the recovery records. Follow the prompt to retry, or restart and wait for cleanup to finish.

### 7.3 Exporting a category preset

![Exporting the complete category tree](../../images/guide-en/25-分类导出.webp)

Activate a game, then click **Export category preset** in Tools:

- Enter a preset name.
- Review the current game's complete category tree and counts.
- Select **Include category images** if needed.
- Repair unreadable images, or deselect those images using the available controls.
- Choose an output location, confirm the output file, and click **Start export**.

The export covers the current game's complete category tree, rather than a single selected mod. Recipients of this file still need to import their own mods.

### 7.4 Rebuilding the index

Use this when source files still exist but the categories or mod list no longer match the disk. First try refreshing with the default `F5` shortcut. Rebuild only when the index needs restoring.

Before starting, check that the resource drive is accessible and the correct game is selected, and finish related file tasks. After confirmation, the app scans the library, reads category and mod markers, and restores the index entries and associations it can recognize.

A normal rebuild mainly handles the current game. **If the app detects corruption of the entire index database at startup, recovery involves all configured games.** Connect each game's resource drive and follow the prompts.

The completion report lists registered entries, classification results, and problems such as invalid metadata. Unreadable files or files with conflicting identities may not be recoverable; keep the report and address the affected items. Rebuilding cannot recover permanently deleted archives and does not replace a backup.

### 7.5 Log Viewer

![Log Viewer](../../images/guide-en/23-日志.webp)

Click **View Logs** in Tools to access:

| Control | Purpose |
| --- | --- |
| Session | Selects a startup session by date and sequence, such as “2026-10-30 #1” |
| Level | All, or one of DEBUG, INFO, WARNING, ERROR, and CRITICAL |
| Auto-scroll to bottom | Moves to the newest entries after a refresh |
| Refresh | Rereads the selected file |
| Open Log Folder | Locates the actual log files |
| Copy | Copies the currently displayed content |

The level filter matches exactly: selecting WARNING does not also include ERROR. Use **All** when you need the context before and after an error.

The window reads only approximately the final 1 MiB of a log. Open the full file from the log folder if you need earlier content. Logs are saved by date and startup sequence and retained for 14 days by default, so save the relevant session promptly when reporting a problem.

Updater logs are also part of the logging system. Keep them alongside the application logs if an application update fails. Remove unrelated personal paths or sensitive information before submitting a report.

### 7.6 Installing and managing plugins

![Plugin list and details](../../images/guide-en/18-插件.webp)

The Plugins page lists plugins on the left and shows details for the selected plugin on the right. Search by name, author, or description, or filter by **All**, **Enabled**, or **Disabled**.

There are two installation methods:

| Method | Steps |
| --- | --- |
| Install an archive | Click **Install plugin**, choose a ZIP, 7z, or RAR file, and follow the result message |
| Add a folder manually | Click **Open plugins folder**, add the complete plugin folder containing `plugin.json`, then click **Rescan** |

Plugins are discovered in `plugins/` under the application root by default. If that folder is not writable, the app uses its data-folder fallback; use the actual location shown by **Open plugins folder**. Bundled examples and plugins you install use the same folder and management rules.

After installation, check the plugin's status in the list. Select it to review its author, description, permissions, and source, then enable it as needed. The first trust prompt explains that the plugin runs code inside the app and lists its requested capabilities. Confirm only for sources you trust. New permissions discovered after an update or rescan require confirmation again.

**Ready** means the plugin is already available: its tool buttons, menus, and shortcuts are registered, and it starts automatically when you first run a command or open the corresponding view. Declared settings in its details can also be edited and reset to defaults immediately.

If the status is **Dependency blocked**, expand the details and follow the instructions to install and enable dependency plugins with compatible versions. Confirm trust separately for each dependency. Dependencies are not installed or authorized automatically. After resolving the requirements, reload or enable the original plugin again.

Installing a new archive for the same plugin replaces its code folder and preserves private settings data. Before replacement, the installer checks the archive, manifest, version requirements, and code; a failed check keeps the original plugin. Check its status after updating, and confirm any additional permissions.

### 7.7 Plugin permissions, settings, and uninstallation

Expand **Permissions and source** in the details pane to review capabilities such as reading or modifying mods, storing settings, network access, files, processes, background tasks, views, and shortcuts, along with dependencies and activation information.

An enabled, trusted plugin whose dependencies are satisfied displays editable controls for any declared settings, including while its status is **Ready**. Settings are saved, and each can be reset to its default. Some plugins also provide dedicated options on their own pages.

Disabling removes the plugin's commands, pages, menus, shortcuts, and callbacks and cancels its tasks. Plugins that depend on it also stop, while retaining their enabled-state preference. Cancellation takes effect at safe boundaries and does not undo every completed operation; check the final task result.

Use **Reload** after editing plugin code. The app removes old commands and interfaces and cancels tasks, then reads the current code and restarts previously active plugins that depend on it, without restarting the whole app. Use **Rescan** when adding plugins or changing the folder layout.

**Uninstall** first disables the plugin and plugins that depend on it, then deletes its code folder. Check the name before confirming. The plugin's settings and private data are preserved for reuse when you reinstall the same plugin. Keep a separate backup of your own plugin source files.

If the status is **Error**, read the error description and logs. Parsing failures, permission mismatches, code checks, and runtime errors can all prevent activation. Permission declarations and checks do not determine whether a plugin's source is trustworthy for you.

Three optional plugins are bundled; enable each one before first use. Their contributions add the corresponding controls to Tools, Category Overview context menus, navigation pages, and shortcut settings:

- **Mod inventory export** exports a CSV inventory of the current game from Tools. From a category's context menu, it includes that category and its subcategories. Select **Export enabled mods only** in plugin details to limit the output. The inventory is generated in the background, with a notification on completion. It contains text metadata only, without mod files, and is not a restorable preset file.
- **Preset check** adds a navigation page with the same name for checking missing mod and appearance variant references. Return to the main interface to edit affected presets. Choose whether to include the default preset and click **Check again** to refresh. Results reflect references that can currently be resolved; a missing reference does not prove a resource is permanently lost.
- **Preset cycle** switches to the previous or next combination from Tools or **Plugin Commands**. The default shortcuts are `Ctrl+Alt+PgUp` and `Ctrl+Alt+PgDown`, effective only inside the app and adjustable in **Settings → Shortcuts**. The default preset is skipped unless you change the option in plugin details. The host task interface shows progress and failure reasons. Repeated clicks during an unfinished switch do not queue multiple switches.

These plugins' source code and usage instructions are in `plugins/`. To write your own plugin, read this tutorial's [Plugin development guide](../../en/PluginGuide.en.md). Start with the first working example, learn the current SDK's commands, settings, pages, background tasks, and dependencies, then complete testing and packaging.

### 7.8 About and the project film

![About page](../../images/guide-en/19-关于.webp)

<img src="../../images/plusa/立绘.webp" alt="Full portrait of Plusa, the project&#x27;s original mascot" width="280">

*Plusa is the project's original mascot and your companion throughout this guide.*

Open **About** near the bottom of the navigation to read the project introduction, tribute to the original author, project timeline, team and contributor information, and license details.

Watch the project film on the About page using its playback controls. Keep the supplied media folders when moving the application.

The project uses PolyForm Noncommercial 1.0.0 with additional attribution integrity terms. The bundled LICENSE defines the exact rights and conditions. Preserve the required attribution and third-party notices when modifying or distributing the project.

The manager's version and update controls are under **Settings → Update → Application updates**.
