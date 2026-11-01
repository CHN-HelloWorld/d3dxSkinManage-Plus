## Chapter 8: Data, backups and troubleshooting

### 8.1 Finding the actual data root

<img src="../../images/plusa/supplement/备份排障.webp" alt="Plusa helping with backups and troubleshooting" width="280">

The application first tries to store personal data beside the application. If that folder is not writable, it uses the Windows user data directory, usually:

```text
%LOCALAPPDATA%/d3dxSkinManage-Plus/
```

Use **Open language packs folder**, **Open plugins folder**, or **Open Log Folder** to check the location currently in use, then identify `config/` and `data/` under the same data root.

A single-file release also includes folders for languages, plugins, tutorials, and media. Move the complete distribution instead of keeping only the executable.

### 8.2 Files to keep

| Location | Contents | Backup recommendation |
| --- | --- | --- |
| `config/settings.json` | Application preferences and shortcut overrides | Keep |
| `config/games.json` | Game list, paths, active game, current preset, and community site matches | Keep |
| `config/plugin_state.json` | Plugin activation, trust, and related state | Keep |
| `config/languages/` | Default and custom language files | Keep, especially your own changes |
| `data/mod_index.db` | Current resource and deployment index | Back up after closing the app |
| `data/presets.json` | Presets for all games | Keep |
| `data/<game-resource-folder>/mods/` | Category folders and complete mod containers | Essential |
| Preferences and other records in `data/<game-resource-folder>/` | State specific to the game, variant working state, previews, and other records | Back up the entire folder together |
| `data/downloads/` | Downloaded source packages and queue information | Keep if packages are awaiting installation or needed for an archive |
| `plugins/` and plugins' personal storage | Installed plugins and their data | Keep if you intend to continue using them |
| `cache/` | Caches that can be regenerated | Do not use as your only backup source |
| `logs/` | Troubleshooting logs | Keep relevant sessions when a problem occurs |
| External loaders and deployment folders | Loader configuration, managed deployments, and manually added resources | Back up separately if they contain custom or manual content |

The `.d3dxmod` and `.d3dxmeta/` entries inside a complete container are essential data. Keep the `.d3dxcategory` markers in category folders as well.

Named variants are stored in `.d3dxmeta/variants/`. Back up the complete mod containers together with `presets.json` to restore both presets and the appearance configurations they reference.

### 8.3 Recommended full backup procedure

1. Wait for import, export, deployment, update, and recovery tasks to finish.
2. Exit the application normally. If the game or loader is still writing configuration, finish those operations too.
3. Copy `config/`, `data/`, and `plugins/` from the actual data root, along with any external assets you need.
4. Save custom loader configuration and manually added resources in deployment folders separately.
5. Store the backup in a different location from the source data, and spot-check containers, metadata, and preset files.

While the application is running, the database may also use `-wal` and `-shm` files. Close the app normally before copying to avoid an inconsistent snapshot. Do not copy just one database file while it is being written.

To share an individual mod, export it from Category Overview and include metadata. To share a category structure, use `.d3dxcp`. Neither replaces a full backup of your data.

### 8.4 Moving to another folder, disk, or computer

To move the entire manager, first back up as described above and exit the app, then copy the complete distribution and personal data. After starting from the new location, check each of the following:

- Game executable and loader paths.
- The actual deployment location specified by **Mod folder**.
- External background images, videos, or WE scenes, and the WE application path.
- Availability of fonts you installed, language packs, and plugins.
- Whether the games' resource drives are connected.

Some paths managed by the app can be stored relative to the data root. Paths to external games, loaders, and media may still need to be entered again.

When changing the deployment location, prefer the migration confirmation provided on the **Games** page. Do not reimport an incomplete deployment folder as a new mod library, especially after discarding identity markers and while still using the old index.

### 8.5 File identity, verification, and recovery messages

> [![Plusa helping with a setback](../../images/plusa/face/05-哭泣.webp)](../../images/plusa/05-哭泣.webp) **Plusa's tip:** Keep the complete original package and records when verification or recovery needs attention. Do not remove identity markers or the database to dismiss a warning; check the causes below.

The app checks managed source files, metadata, and deployment records before an operation. The following changes can block writes:

| Message type | First action |
| --- | --- |
| File missing or disk offline | Reconnect the disk and check the actual path |
| File in use | Close the relevant editor or preview, or finish related game operations, then retry |
| Source file or metadata changed | Select the item again, refresh, and check where the change came from |
| Identity mismatch or ownership cannot be determined | Keep the complete container and records; check whether only some files were copied or replaced |
| Verification mismatch | Keep the original package and error details; check whether the file is damaged or was modified |
| Unfinished transaction, rollback, or cleanup | Wait for recovery, or restart and retry as instructed |

These messages prevent operations from continuing on the wrong item. **Do not delete markers, the database, or transaction records to dismiss the message.** Those records may contain the file contents and path information needed for recovery.

### 8.6 Common problems

> [![Plusa checking a puzzling result](../../images/plusa/face/03-无语滴汗.webp)](../../images/plusa/03-无语滴汗.webp) **Plusa's tip:** Start with the current game, actual paths, and task results. Change one thing at a time and keep the original error text so you can identify which step went wrong.

| Symptom | What to check |
| --- | --- |
| The game list is empty after startup | Check that the correct data root is in use; the list is expected to be empty on a first installation |
| An activation prompt covers the page | Double-click the target game on the **Games** page |
| Cannot import at the root level | Open a category first |
| Cannot find an imported mod | Check the current game, category, search text, type, rating, and tag filters |
| Enabling a mod has no effect in the game | Check the deployment location, whether the loader is running, mod compatibility with the game version, and whether you have reloaded |
| Space, F2, or another shortcut does nothing | Select a single item in Category Overview and move focus out of text boxes, then check custom bindings |
| The key bindings page has no bindings | The mod's INI may not contain recognizable bindings, or loading may not have finished |
| A variant cannot be saved | Enable the mod, confirm that it supports persistent variables, and let the loader write the current configuration first |
| A variant's state is shown as unconfirmed | Save the configuration in the game and read it again, or explicitly choose to fill missing values with defaults |
| The game still shows the old appearance after changing a variant | Reload as configured in the loader, and avoid letting unwritten game state overwrite the saved selection |
| The online page says the game is not listed | Check the game's English name and the site's listings |
| A download fails or is slow | Test the network and proxy, check free disk space, then retry |
| A download is complete but not installed | Check the pending installation destination, switch back to the game it belongs to, and review the task result |
| An update check is incomplete | Resolve network or source-information errors, then check again |
| No application update is available | Check whether an update source is configured and whether the publisher has released a new package |
| RAR can be read but cannot be exported | Creating RAR archives requires WinRAR |
| Image memory use is high | Adjust the image memory allowance or clear the image cache; do not clear resource data |
| A video or WE scene does not play | Check the asset path, format, complete scene folder, and local WE application |
| Items are still missing after rebuilding | Review the report for invalid metadata, identity conflicts, or missing source files, and address each item |

### 8.7 Reporting a problem

<img src="../../images/plusa/排障.webp" alt="Plusa holding logs to help prepare a useful report" width="280">

*Keep the steps, error text, and logs from the same time period together to help reproduce and locate the problem.*

Use **Tools → View Logs** to find the startup session in which the problem occurred, and retain the records before and after the operation. Include:

- Application and Windows versions.
- The page you started on and the sequence of actions.
- The expected result, actual result, and complete error text.
- Logs for the relevant period; include updater logs for updater problems.
- Whether files were moved or renamed outside the app, disks were disconnected, or custom plugins were involved.

You can replace or obscure game names, mod names, and personal paths in screenshots while keeping buttons, statuses, and error messages visible. Check logs for personal paths or credentials before sharing them too.

---

## Appendix A: Default keyboard shortcuts

These are the application's default bindings. If you have customized them, or plugin conflict resolution has changed them, refer to **Settings → Shortcuts**.

| Default shortcut | Action | Where it works |
| --- | --- | --- |
| `Ctrl+C` | Copy | Text areas; copies a preset in the presets list |
| `Ctrl+X` | Cut | Text areas; prepares to move selected items in Category Overview |
| `Ctrl+V` | Paste | Text areas, Category Overview, or the presets list |
| `Ctrl+A` | Select all | The current text area, Category Overview, or presets list |
| `Delete` | Delete | Text or selected items in Category Overview; deleting items still requires the applicable confirmation |
| `F5` | Refresh the current game library | Main window |
| `Ctrl+I` | Import mods | A game is active and the destination category is open |
| `Ctrl+F` | Search this page | The current page, if it supports search |
| `Ctrl+K` | Search all settings | Main window |
| `Ctrl+,` | Open Settings | Main window |
| `Ctrl+1` | Games | Main window |
| `Ctrl+2` | Category Overview | Main window |
| `Ctrl+3` | Gallery | Main window |
| `Ctrl+4` | Online Mods | Main window |
| `Alt+Left` | Go up one level / close online details | The corresponding page |
| `Ctrl+Shift+N` | New Category | Category Overview |
| `F2` | Edit the selected item | A single mod or category selected in Category Overview |
| `Space` | Enable or disable | A single mod selected in Category Overview |
| `Ctrl+Shift+E` | Export selected items | Category Overview |
| `F6` | Launch Mod loader | A valid loader is configured |
| `F7` | Launch Game | A game executable is configured |

`Esc`, `Ctrl+Enter` in edit windows, and similar keys are contextual actions outside the customizable command set listed above. In-game F10 is configured by 3DMigoto.

---

## Appendix B: Operation scope reference

| What you want to do | Where to do it | Key point |
| --- | --- | --- |
| Stop using a mod temporarily | Disable | Keeps the source container in the library |
| Delete a mod | Delete in Category Overview | Check whether it uses the Recycle Bin or permanent deletion |
| Delete all data for a game | Delete on the Games page | Permanently removes resources, configuration, and managed deployments; back up first |
| Switch to another set of enabled mods | Presets | Includes both mod selections and variant references |
| Change the appearance within one mod | Variants | Requires the mod to support the corresponding variables |
| Share an empty category structure | Export category preset | Produces a `.d3dxcp` file without mods |
| Share a mod and its named variants | Export the mod with metadata | Check the output package and keep all associated content |
| Clear regenerable images | Images and cache | Distinguish memory cleanup from disk cleanup |
| Remove duplicate downloaded source packages | Download data → Clean up installed | Keeps downloads that have not yet been installed |
| Reset application preferences | Reset | Requires confirmation; preserves game configurations and imported mods |
| Repair the index | Tools → Rebuild Index | Requires intact source files; cannot restore deleted resources |
