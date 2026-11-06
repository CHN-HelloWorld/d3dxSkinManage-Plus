## Chapter 6: Settings

> [![Plusa thinking](../../images/plusa/face/09-思考.webp)](../../images/plusa/09-思考.webp) **Plusa's tip:** Search settings when you are unsure where an option lives. Your saved configuration determines the current shortcuts and paths.

### 6.1 Sections, search, and saving

The Settings page has six sections across the top: **General**, **Personalization**, **Shortcuts**, **Import and export**, **Online Mods**, and **Update**. Each section also has subpages on the left.

Use the default `Ctrl+K` shortcut to open settings search, or type a setting name in the upper-right corner. Search results can take you across sections and locate the relevant control.

![Searching all settings](../../images/guide-en/26-设置搜索.webp)

*Enter “cache” to find settings for image memory and disk caches.*

The page states **Changes apply immediately and are saved automatically**. Switches and selections usually save immediately, while text fields submit changes when editing finishes. Some sliders preview changes as you drag and save when you release them.

This chapter lists the default settings. Screenshots use example settings; check the values and switch positions shown on your device.

**General → Window behavior** provides a close-action dropdown, defaulting to **Exit application**. With **Hide to system tray**, closing the main window leaves the app and background tasks running. Click the tray icon or choose **Show main window** from its context menu to restore it. **Exit application** in that menu still runs the normal unsaved-change and background-task confirmations. If the tray is unavailable, the app closes normally rather than hiding without a recovery entry.

### 6.2 General → Mod management

| Setting | Default | Description |
| --- | --- | --- |
| Deletion method | Permanent deletion (unrecoverable) | You can also choose the Recycle Bin; this applies only to deleting individual mods or batches of mods |
| Concurrency | Auto recommend | The number of deployment tasks that can run at once; manually adjustable from 1–16 |

Deleting a category, deleting a game, and clearing a deployment folder each have their own scope confirmations. Selecting the Recycle Bin for mod deletion does not make these other operations recoverable.

**Auto recommend** saves automatic mode, allowing the app to recalculate for a different device. Manually changing the number switches to a fixed limit. Higher concurrency is not always faster; lower it for mechanical hard drives, large archives, or limited memory.

### 6.3 General → Network proxy

| Setting | Default | Usage |
| --- | --- | --- |
| Enable proxy | Off | When enabled, application updates can use this proxy configuration |
| Use system proxy | On | Reads the proxy from Windows |
| Protocol | HTTP | Manual mode supports HTTP, HTTPS, and SOCKS5 |
| Proxy address / Port | 127.0.0.1 / 7890 | Example defaults; these must match your proxy software |
| Test connection | Manual action | Checks external network connectivity and latency through the proxy |

Online Mods also requires **Enable proxy** in its own section. If Online Mods is using the proxy when you turn off the main switch, the app asks whether to disable that use as well.

If no configuration is detected in system proxy mode, check Windows settings first. In manual mode, check the address, port, and whether the proxy application is running.

### 6.4 General → Download data

This page shows total package storage, split into installed and uninstalled packages, and provides:

| Control | Purpose |
| --- | --- |
| Delete installer packages automatically | On by default; removes the corresponding source package after the mod is successfully added to the library |
| Clean up installed | Removes downloaded source packages already added to the library, after confirmation |
| Open folder | Opens the actual downloads folder |

Keep downloads that have not yet been installed until you install them or decide how to handle them. These source packages are stored separately from the managed mod contents in the library, so cleaning up installed packages reduces duplicate storage.

### 6.5 General → Images and cache

This page separates the image memory cache from the disk cache, each with its own limit and cleanup controls.

New temporary directories and preview caches inherit normal parent-directory permissions. App startup does not scan or repair historical directory permissions.

Existing Plus users who cannot access older preview caches or temporary directories can install and enable the separately distributed **Directory Permission Repair** ZIP from the Plugins page. Open its window in the toolbox and click **Start repair**; no tool script is required. The plugin checks application data directories in the background and shows repair counts and failed paths. Files and custom restrictions are kept; the whole disk and other applications' temporary directories are not scanned. Repair runs only when clicked, with manual retries for failures. Disable or uninstall the plugin after success; subsequent app launches and software updates never automatically repeat the repair.

![Image memory and disk caches](../../images/guide-en/27-图片与缓存.webp)

| Item | Default or range | Description |
| --- | --- | --- |
| Image memory cache | Auto recommend; manual range 64–8192 MiB | Keeps recently decoded images so returning to them is faster |
| Current image memory | Live statistics | Measures only the image cache, not the whole application's memory use |
| Clear image memory | Button | Releases cache entries that can be freed; images currently on screen remain in memory |
| Disk cache limit | 512 MB by default; 0 means unlimited | Manages online images and other caches that can be regenerated |
| Disk slider range | Maximum 4096 MB by default | Controls how far the slider can go, rather than the effective cache limit |
| Clear disk cache | Requires confirmation | Clears regenerable caches while retaining downloaded mod source packages |

The automatic memory allowance varies with your device and display. Use the recommendation shown on your device. Lowering the allowance may require images to be decoded again when revisited, but does not delete mods or their metadata.

The disk cache is not a backup folder. Do not manually clear `data/`, operation recovery records, or `.d3dxmeta/` as though they were caches.

### 6.6 Personalization → Appearance

![Appearance settings](../../images/guide-en/12-设置个性化.webp)

| Option | Default | Description |
| --- | --- | --- |
| Theme | Dark | Also offers Light and Follow System; the latter follows the Windows app theme |
| Font | System default | Choose and preview a font installed on this computer |
| Language | Simplified Chinese | Options come from language packs in the configuration folder |

Restart the app after switching languages or editing a language pack to refresh the entire interface.

To create another language pack:

1. Click **Open language packs folder**.
2. Copy an existing language folder and name it with a language code, such as `ja-JP`.
3. Edit the UTF-8 `strings.json` file, preserving the source-text keys and dynamic placeholders.
4. Return to Settings, click **Refresh list**, select the language, and restart.

Languages are identified by their folder names. Game names, mod names, author descriptions, and custom tags are user content and are not automatically rewritten when the interface language changes.

For the file format, placeholders, fallback languages, and developer tools, see the [language pack guide](../../en/LanguagePackGuide.en.md).

### 6.7 Personalization → Window background

You can turn the background off or use an image, video, or WE scene.

![Choosing a window background source](../../images/guide-en/28-整窗背景.webp)

| Source | What to select |
| --- | --- |
| Close (off) | Uses the theme's background color |
| Image | A supported local image such as PNG, JPG, JPEG, BMP, or WebP |
| Video | A supported local video such as MP4, WebM, MKV, AVI, or MOV |
| WE scene | The `project.json` from a Wallpaper Engine scene project, together with the local Wallpaper Engine application configuration |

For WE scenes, use **Find automatically** or manually specify an application path such as `wallpaper64.exe`. Keep the complete scene project and its resource files; copying only the JSON file is not enough to play it.

Display settings:

| Setting | Default | Purpose |
| --- | --- | --- |
| Image fit | Fill (crop to cover) | Can be changed to fit; images also support Tile |
| Background overlay | 45 for Dark, 30 for Light | Saved separately for each theme; darkens in Dark mode and softens and brightens in Light mode |
| Background blur | 60 | Range 0–100; 0 turns it off |
| Background frame rate | 30 FPS | Used for animated backgrounds; adjustable through the available options |
| Scene quality | 2× | WE scenes can use 1× to reduce resource use |
| Background volume | Muted | Adjust sound for supported animated backgrounds as needed |

Animated backgrounds provide pause, resume, and retry controls. They pause automatically when the window is minimized and resume when it is restored. Restoring the window does not override a manual pause.

If playback fails, check that the file still exists, its encoding is supported, and the WE path and project are complete. Image backgrounds do not require Wallpaper Engine, nor do ordinary local videos.

### 6.8 Shortcuts → Key bindings

![Shortcut settings](../../images/guide-en/13-快捷键.webp)

Commands are grouped by where they are used. Click the corresponding key binding to record a shortcut, then save it. Combinations of Ctrl / Alt / Shift with one primary key are supported.

Mouse side button 1 (BackButton / XButton1) and side button 2 (ForwardButton / XButton2) can also be recorded, either alone or with Ctrl / Alt / Shift. They use the same conflict checks and focus rules as keyboard shortcuts and only operate within the manager.

When a conflict is found, the app identifies the conflicting command. Confirming replacement may leave the previously assigned command unbound; canceling keeps the original binding. System-reserved combinations cannot be assigned to ordinary commands.

Shortcuts depend on focus: editing in a text box, working with items in Category Overview, and copying in the presets list each have their own context. The manager does not provide global hotkeys that work in other applications.

Commands registered by plugins can also appear in the relevant groups. See Appendix A for the complete default shortcut table.

### 6.9 Import and export

![Import and export settings](../../images/guide-en/14-导入与导出.webp)

The **Import** subpage:

| Option | Default | Description |
| --- | --- | --- |
| Mod import format | 7z | Folder inputs are compressed to this format; archives keep their original format when conversion is not forced |
| Always use the import format | Off | Extracts and recompresses when needed |
| Import concurrency | Auto recommend | Manually adjustable from 1–16 tasks |

The **Export** subpage:

| Option | Default | Description |
| --- | --- | --- |
| Mod export format | ZIP | Also offers 7z, RAR, or a folder |
| Always use the export format | Off | When off, the source archive format may be retained; this option is unnecessary for folder exports |
| Preserve the category folder structure | On | Organizes output by category path |
| Include metadata and previews | Off | Enable to share tags, additional previews, and named variants |
| Export concurrency | Auto recommend | Manually adjustable from 1–16 tasks |

Creating RAR archives requires WinRAR. Reading RAR archives does not require a separate archiver installation. Read the explanations for unavailable options before choosing an output format.

The current category determines the destination for a local import. For online installation, check the category in the installation destination selection flow.

### 6.10 Online Mods

The **Online Mod proxy** subpage provides a switch and **Open proxy settings**. It uses the proxy configured in General. You cannot enable the online proxy separately while the main switch is off.

The **Mod updates** subpage provides automatic checks, startup checks, scheduled frequency, notification style, and check concurrency. Automatic and startup checks are on by default, scheduled checks are off, and notifications use **Badge only**. Concurrency uses **Auto recommend**, with a manual range of 1–16.

See Chapter 5 for pausing, resuming, background tasks, and file selection in the update window.

### 6.11 Update → Application updates

| Option | Default | Description |
| --- | --- | --- |
| Check application updates | Manual control | Displays the current version and checks the update source configured by the publisher |
| Check on startup | On | Checks in the background, with a 24-hour minimum interval |
| Back up the previous version | On | Backs up application files before updating the manager itself |
| Update package source | Automatic | Compares download speeds and switches after sustained low speed or failure; you can also choose a specific source |

Manual **Check application updates** works regardless of the **Check on startup** setting or the 24-hour interval. The supplied updater installs application updates. If no update source is configured, obtain the new version through the software publisher's download channel.

### 6.12 Restoring defaults

**Reset**, in the lower-right corner, first opens a confirmation dialog with Cancel selected by default. Confirming resets all application preferences, including:

- Appearance, language, font, and background.
- Customized shortcuts.
- Import, export, deletion, network, and update preferences.
- Settings that support automatic recommendations, which return to recommendations based on the device.
- Options such as interface layout stored in application preferences.

**Game configurations and imported mods are retained.** The game list, active game, and current preset selection are saved separately in `config/games.json`.

If the reset fails, the app keeps the original settings and displays a message. To revert just one option, adjust it directly without resetting all preferences.
