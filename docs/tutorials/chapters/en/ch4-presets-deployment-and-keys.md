## Chapter 4: Presets, deployment, key bindings, and variants

> [![Plusa sharing a useful trick](../../images/plusa/face/06-坏笑.webp)](../../images/plusa/06-坏笑.webp) **Plusa's tip:** A preset remembers a combination of Mods; a named variant remembers one Mod's appearance settings. Use each for its own purpose to avoid repeating setup.

### 4.1 Three different ways to save a setup

<img src="../../images/plusa/supplement/组合变体.webp" alt="Plusa choosing Mod presets and variants" width="280">

| Name | What it saves | Where to find it |
| --- | --- | --- |
| Mod preset | A selection of Mods for the current game, plus references to the named variant selected for each Mod | **Presets** in the overview toolbar |
| Individual Mod variant | Appearance values that a Mod supports saving, such as color schemes or accessory options | The **Variants** tab in Mod details |
| Category preset | The category tree, names, node types, and optional folder images; no Mod files | Category preset actions in Tools |

Create categories and import Mods first, then save frequently used combinations as Mod presets. If one Mod has several internal appearances, also save named variants for your presets to reference.

### 4.2 Mod containers and stable identity

Each Mod in the library has a complete container folder. This example uses the `外观` (Appearance) category and `示例 Mod A` (Sample Mod A):

```text
<data root>/data/Sample Game/mods/外观/示例 Mod A/
├── .d3dxmod
├── 示例 Mod A.zip
└── .d3dxmeta/
    ├── info.json
    ├── previews/
    ├── ini_overrides/
    └── variants/
        └── <variant_id>.json
```

| Content | Purpose |
| --- | --- |
| `.d3dxmod` | Records the stable `mod_id`, resource location, and content verification information |
| ZIP / 7z / RAR archive | The original resource archive |
| `info.json` | Metadata such as name, author, tags, age rating, and source |
| `previews/` | Manually added preview images |
| `ini_overrides/` | Editable INI overrides |
| `variants/` | Separately saved named variants |

**A stable `mod_id` identifies the Mod; content hashes are used for verification.** Two folders sharing a name are not necessarily the same Mod, and external changes are not trusted based on filenames alone.

When backing up or moving a Mod out of the library, copy the entire container. Taking only the resource archive leaves behind its accompanying metadata, key changes, and named variants.

Prefer renaming, moving, and exporting through the application. After manually moving a complete container, refresh and check its category. Do not delete markers, forge identities, or replace only the archive to “fix” a verification failure.

### 4.3 Library formats and import rules

Resources are kept in the library as ZIP, 7z, or RAR archives. Importing a folder compresses it into the format selected under **Mod import format**.

When importing an existing archive:

| **Always use the import format** | Behavior |
| --- | --- |
| Off | Keeps the incoming archive's original format to reduce repeated compression |
| On | Extracts archives in other formats and repackages them in the selected format |

Support for reading ZIP, 7z, and RAR is included with the application. **Creating RAR archives requires WinRAR's compression tool.** Without it, the relevant output options are unavailable. Importing an existing RAR archive and saving resources as a new RAR archive are different operations.

Deployment always uses extracted copies. Progress that does not change continuously does not necessarily mean a task has stalled; archive format, file count, and disk performance all affect the processing stages.

After a large Mod is extracted, file scanning may briefly lock its folder. The manager waits and retries automatically. If a file-access warning remains, close the game and tools viewing the Mod files, then try again and check deployment folder permissions.

For Mods imported by older versions, deployment fills in missing original configuration files while keeping your customized keys. You do not need to delete or reimport the Mod. If another program is editing the configuration, the operation stops to avoid overwriting changes. Wait for editing to finish, then try again; do not manually delete management files.

### 4.4 Enabling, parking, and disabling

Deployment uses the currently configured **Mod folder**. If you change it without migrating files, active and parked copies at the previous location remain there. Enabling Mods and applying presets prepare the required copies in the current folder.

Enabling first checks the source container, metadata, deployment location, and operations in progress. It then prepares resources, applies the effective INI content and configuration, and publishes the finished copy to the deployment folder.

Temporary preparation folders may start with `DISABLED_` and include `.partial`. They are not exposed to the loader as normal enabled copies before completion. After a failure, cancellation, or unexpected application exit, recovery may still need to process these folders.

Disabling first captures the required runtime configuration and synchronizes the INI, then removes the copy currently used by the game:

- A deployment still referenced by other Mod presets may be parked as `DISABLED_...` for faster restoration next time.
- Copies that no longer need to be retained may be cleaned up.
- The source container remains in the library.

3DMigoto normally excludes parked folders using `exclude_recursive = DISABLED*`. If you changed the loader configuration yourself, make sure these folders are still excluded.

Do not enable a parked copy by renaming its prefix in File Explorer. Use Enable or Apply Preset in the application so that the actual files remain consistent with the records.

### 4.5 Deployment folder and external changes

**Mod folder** must match the location read by the loader. It is configured separately from **3DMigoto**; actual Mod deployment is unavailable if it is not set.

The application monitors managed source files and deployment folders for changes. If a deployment folder is moved externally, a source container changes, a disk goes offline, or identity markers conflict, address the report in the interface before resuming operations.

Use the migration workflow on the Games page to change the deployment location. If the application reports changed files, uncertain ownership, or unfinished recovery, do not delete the database, transaction records, or markers to force it to continue. They may contain information needed for rollback and recovery.

### 4.6 Creating and using Mod presets

Click the current preset name in the overview toolbar to open the preset list:

![Mod preset management](../../images/guide-en/08-组合预设.webp)

*Click once to select a preset for management. Only **Apply** on the right switches the setup. The preset in use has a status marker.*

| Action | How to use it |
| --- | --- |
| Create | Click **New Preset** to create an empty setup, then name it |
| Apply | Click **Apply** on the target row and wait for deployment to finish |
| Rename | Select a preset and click **Rename**, or double-click its name |
| Copy | Select a preset and click **Copy**, or use the default `Ctrl+C` and `Ctrl+V` |
| Select multiple | Use `Ctrl` to add selections or `Shift` to select a range; you can also select all within the list |
| Delete | Select presets and click **Delete**, then confirm the scope |

Each game has its own default preset, which is retained and cannot be deleted. When deleting a user preset that is currently in use, the application switches to the default preset before completing deletion. Check the dialog and task results.

**A new preset starts empty.** To keep an existing setup and make small changes, copy that preset before applying it. Applying an empty preset means that Mods from other presets will no longer be enabled.

The current preset updates its selections as you enable or disable Mods. When you save or switch a named variant for an individual Mod, the current preset also records the selected variant reference.

### 4.7 How switching presets affects files

Switching is based on the differences between the two presets:

| Difference | Handling |
| --- | --- |
| A Mod is no longer used by the target preset | Parks or removes its deployment |
| The target needs a Mod with a usable parked copy | Verifies and restores the copy |
| The target needs a Mod with no usable copy | Extracts and deploys it for the first time |
| Both presets use the same Mod | Keeps reusable deployment files and handles configuration according to the target preset |
| The same Mod references different variants | Applies the target variant's configuration; INI and user configuration files may still be written |

Sharing a Mod therefore does not guarantee that no file operations will occur. It also depends on the variants, overrides, and whether the deployment is still valid.

Switching shows progress and a cancel action, while restricting conflicting operations such as enabling/disabling, deletion, and switching games. Cancellation or failure attempts to roll back completed steps; unfinished rollbacks retain records for recovery. Wait for the task to finish handling its result before modifying resources again.

### 4.8 Editing a Mod's in-game keys

Select the Mod and open the **Keys** tab in the details panel:

![INI key bindings](../../images/guide-en/06-按键设置.webp)

Keys are grouped by INI file. Some bindings provide both a forward and a backward key. Names come from the Mod's own section definitions.

There are two editing methods:

- Click a key field and press the key or Ctrl / Alt / Shift combination you want to use. `Esc` cancels recording.
- Open **Manual input** and enter 3DMigoto key tokens as instructed. Special keys and controller tokens depend on loader support.

**Key changes save directly; no additional overall Save button is needed.** Changes are written to the container's INI override layer. For an enabled Mod, they are also synchronized to its deployed copy. If the game is running, reload as required by the loader for the changes to take effect.

An option to open the source INI appears where supported. If saving is rejected because the file changed externally, reselect the Mod, check the latest contents, and then edit again.

These settings change the Mod's in-game keys. **Settings → Shortcuts** changes the manager's controls; the two are saved separately.

### 4.9 Saving an appearance variant for a Mod

Variants record numerical options that the Mod author declares as persistent, such as color schemes or accessory toggles. They cannot add appearances to a Mod that has no such variables.

Basic workflow:

1. Enable the target Mod and switch to the appearance you want in the game.
2. Use the loader's action for saving configuration. Common 3DMigoto setups write `d3dx_user.ini` on an F10 reload or a normal game exit.
3. Return to the manager, select the Mod, and open **Variants**.
4. Click **Save current state as a new variant**, enter a name, and confirm with **Save and use**.
5. The new configuration appears in the list, and the current preset references it.

![Named variants](../../images/guide-en/07-变体管理.webp)

***Mod defaults** and named configurations are listed separately. The selected option is marked **Currently in use** when the Mod is enabled, or **Use next time** when it is disabled.*

Names must be unique within the same Mod and contain no more than 40 characters. Saving requires the Mod to be enabled and valid configuration to be readable.

If the configuration is reported as incomplete, return to the game, save it, and read it again. Choose **Fill with defaults and save** only when you intend to do so. This fills any unread values with defaults, which may not exactly match the appearance currently shown in the game.

**No variants can be saved** usually means that no appearance variables suitable for saving were found. The Mod can still be enabled and disabled normally.

Drive letters or spaces in an author's namespace do not require manual editing. Legacy names beginning with DISABLED receive a usable deployment folder name, while their library names stay the same. Backup INIs excluded by the loader are kept but left out of appearance analysis; explicitly included files still follow the loader's rules.

Appearance restoration, variant capture, and monitoring all use the current **Mod folder**. The app checks loader include configurations above the deployment folder and uses the corresponding user configuration file. An old saved loader launch path is not used for these operations, even if it still exists.

If the current deployment folder has no identifiable runtime environment, or appearance settings cannot currently be identified, the Mod still enables using its existing files, with a notice that appearance preferences were not restored. Saved preferences and variant selections are kept and cannot be overwritten by data from the wrong location or an incomplete capture. Independent test folders can also deploy Mods. After confirming the loading configuration for the current deployment folder, enable the Mod again or apply a saved variant. An INI inventory change during enabling triggers at most one fresh read and retry; continuing changes or concurrent preset edits are not overwritten.

### 4.10 Switching, updating, copying, and deleting variants

| Action | Result |
| --- | --- |
| Click **Mod defaults** | Uses the Mod's default configuration |
| Click a named configuration | Applies it if the Mod is enabled, or selects it for next time if disabled |
| Save current state as a new variant | Creates a separate configuration and switches the current preset to use it |
| Update an existing variant | Writes the captured current state into that configuration. Other presets referencing it use the new contents the next time they are applied |
| Rename | Keeps the variant's identity and references; changes only its display name |
| Create copy | Creates a new variant that can be adjusted independently |
| Delete | Changes all preset references to that variant to Mod defaults; also restores defaults if the variant is currently in use |

Management actions are in the more menu on each configuration row. Before updating a shared configuration, the application lists the presets referencing it. If you want to change only one setup, create a copy first.

**Temporary in-game changes do not automatically overwrite named configurations.** When **Unsaved appearance changes** appears, save a new configuration or update the existing one as needed.

Multiple presets can reference the same named variant. Copying a preset preserves these references; it does not automatically create separate variant files for each preset. Copy the variant separately when you need independent settings.

After restoring a variant, reload through the loader if the game is running. A successful save in the application means the configuration has been written; it does not mean the running game has already refreshed its appearance.

### 4.11 Sharing variants with others

Named configurations are stored in the Mod container's `.d3dxmeta/variants/`. For sharing, use **Export** with **Include metadata and previews** enabled. You can also back up the entire container.

Import checks the variant format and whether its variables can be matched to the Mod. Mismatched content, duplicate identities, damaged files, and similar issues may be rejected. You cannot apply an arbitrary variant JSON from another Mod directly.

A game's private working state records serve a different purpose from named variants. For a full backup on your machine, retain `config/`, `data/`, and external resources. An export package is sufficient for sharing a single Mod.

### 4.12 Archives containing multiple manager containers

If an archive contains several complete manager Mod containers, deployment expands each body and keeps its configuration. Manual unpacking is not required. Previously prepared deployments are upgraded automatically when enabled; the original folder is retained if upgrading fails. Ordinary archive attachments are not unpacked recursively.

A configuration inventory mismatch does not imply that another program has locked the files. The manager distinguishes configuration mismatches, write-permission failures, and file-use errors explicitly reported by Windows, and records the actual inventory mismatch in the logs.
