## Chapter 2: Games and initial setup

> [![Plusa looking surprised](../../images/plusa/face/02-惊讶.webp)](../../images/plusa/02-惊讶.webp) **Plusa's tip:** The loader folder and the Mod folder read by the game are different locations. Check d3dx.ini and fill them in separately; keep the library separate from deployment.

### 2.1 Adding a game and the five fields

Click **Games → Add game** to open a draft in the right panel. Fill it in and click **Confirm**. **Exit** discards the draft, with a confirmation if you have entered anything.

![Draft configuration for a new game](../../images/guide-en/01-添加游戏.webp)

| Field | Purpose and requirements |
| --- | --- |
| **Game name** | Required; a name you can recognize, such as `示例游戏` (Sample Game) |
| **English name** | Used for GameBanana matching and to generate the game's resource folder name under `data/`; use the full English name used by the community |
| **Executable** | The game's exe file. Leaving it blank does not affect management, but disables **Launch Game** |
| **3DMigoto** | The loader's folder; used to launch the loader and locate `d3dx_user.ini` for capturing variants |
| **Mod folder** | The deployment folder for enabled Mods. It must match `d3dx.ini`; Mods cannot be deployed if it is blank |

If **English name** is blank, the resource folder name falls back to **Game name**. A different folder name is generated if the name is already taken. The application generates the internal game ID; you do not need to enter it.

The preview area lets you click, drop, or replace a single game image. Preview changes for an already saved game are persisted separately. If writing fails, a message appears and the change remains unsaved. Other fields require **Save**.

### 2.2 Library and deployment folder

| Location | What it stores | Common example |
| --- | --- | --- |
| Game resource folder | The current game's Mod library, preview cache, and runtime preferences | `<data root>/data/Sample Game/` |
| Library root | Category folders and complete Mod containers | `mods/` inside the folder above |
| **Mod folder** | Enabled copies, parked copies, and management markers read by the loader | `D:/ModLoader/Mods/` |
| **3DMigoto** | Loader program, main configuration, and user configuration | `D:/ModLoader/` |

Importing adds a Mod to the library; enabling publishes its contents to the deployment folder. Do not use the library itself as the deployment folder, or treat the game's installation root as a dedicated Mod folder.

The application does not automatically derive the deployment folder from the loader path. Check `d3dx.ini`, then enter **Mod folder** explicitly.

The data root is normally the application's folder. If the release folder is not writable, a Windows user data folder may be used instead. Confirm the actual location before backing up; see Chapter 8.

### 2.3 Activating and switching games

![An active game](../../images/guide-en/02-游戏配置.webp)

Click once to view the configuration; double-click to activate. The active game's card shows **Current**. You can also click **Activate this game environment** in the details panel.

After activation, the overview, gallery, and Online Mods switch to that game and use its loader and deployment paths. Each game has its own categories, Mods, and Mod presets.

Double-click the current game again to deactivate it. The application only restores a valid previously active game; it does not automatically activate the first newly added game.

### 2.4 Editing and saving

Select a game, edit the fields on the right, and click **Save**. If you switch items or pages with unsaved changes, the application asks whether to save, discard, or cancel leaving.

When you change the English name, the application attempts to migrate the linked resource folder and update related paths. Finish import, update, and deployment tasks first. If the folder cannot move but the settings are saved, a message identifies the retained location. If writing the configuration fails, the application attempts to roll back the move and keeps your edits unsaved. If recovery also fails, check the actual file locations shown in the message before continuing; do not delete the recovery materials yourself.

Game configuration is stored separately in `config/games.json`; application preferences are in `config/settings.json`. **Settings → Reset** preserves the game list and Mod data.

### 2.5 Changing or clearing the deployment path

Before changing a path, make sure both the old and new disks are accessible and deal with any applications holding the affected files open.

| Action | Application behavior |
| --- | --- |
| Change **Mod folder** when the old folder contains files | Asks whether to migrate all files and subfolders in the old folder, including manually added files |
| Choose to migrate | Checks for destination conflicts, then migrates; if it fails, keeps recovery information and reports the failure. Do not delete those records yourself |
| Choose not to migrate | Saves only the new configuration and leaves the old folder's contents in place. You must check which location the game will read afterward |
| Cancel the migration prompt | Exits this save operation |
| Clear **Mod folder** when the old folder still contains files | Asks whether to permanently empty the old folder; declining keeps the original deployment path |

> ⚠️ Emptying the deployment folder affects all files and subfolders inside it, including manually added content, and bypasses the Recycle Bin. Read the full path in the dialog before confirming.

Migration rejects unsafe situations such as nested source and destination folders or content with the same name already at the destination. To move to another disk, use the Games page and confirm afterward that the configuration was saved and enabled states are correct.

Clearing **3DMigoto** also warns that variant state tracking will be affected. The deployment path and loader path are separate fields; clearing either is not a substitute for disabling Mods.

### 2.6 Deleting a game deletes its data

**Deleting a game permanently deletes that game and its data.** The Recycle Bin option under **Mod management → Deletion method** does not apply.

Choose **Delete** in the details panel or context menu. The application scans the resources involved and shows the deletion scope. You can cancel during the scan. Before confirmation, it shows the full resource path and counts of Mods, categories, presets, and deployment copies.

![Confirming the scope of game deletion](../../images/guide-en/20-删除游戏.webp)

Deletion includes:

- The entire game resource folder and its contents, including the Mod library and other files added manually.
- The game's managed deployment data.
- The game's configuration, Mod presets, key or variant preferences, and update records.

Export or back up anything you want to keep to a location outside the deletion scope first. If you delete the current game, its activation state is also cleared.

If some files are in use, **Game data cleanup results** lists the full paths that could not be cleaned up. Keep this report, release the files, and address the remaining items according to the report. A game card disappearing does not mean that every file has been removed.

### 2.7 Search, views, and ordering

The Games page supports searching by name and switching between card and list views. In card view, you can adjust card size and drag games to reorder them. If search filtering is active, clear it before reordering the complete list.

The default `Ctrl+1` returns to the Games page; the default `Ctrl+F` focuses the current page's search box. If launch paths are missing, fill them in and save before using the navigation buttons or `F6` and `F7`.
