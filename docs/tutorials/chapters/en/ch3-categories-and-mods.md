## Chapter 3: Category Overview and Mod management

### 3.1 Page structure

**Category Overview** handles categories, imports, enabling Mods, editing information, tags, preview images, and exports. The top toolbar provides Mod presets, batch category creation, views, NSFW display options, and update checks. The breadcrumb shows your current level.

![Category Overview](../../images/guide-en/03-类别总览.webp)

*The illustration uses fictional categories; create your own folders when using the application.*

The search row provides searches by name, author, and tag; tag conditions; type and age-rating filters; Disable All; sorting; and card scaling. You can drag the divider between the content area and the details panel on the right. You can also adjust the heights of the image and text areas within the details panel.

Click once to select an item, double-click a category to enter it, and double-click a Mod to toggle its enabled state. Click **All** in the breadcrumb to return to the category root.

### 3.2 Categories and characters/objects

![Plusa organizing categories](../../images/plusa/supplement/分类整理.webp)

| Type | Enable rule | Suitable uses |
| --- | --- | --- |
| **Category** | Multiple Mods can be enabled at once | Interface Mods, tools, and resources that can be combined |
| **Character/object** | Only one Mod can be enabled across this node and all its subcategories | Mutually exclusive character appearances or object replacements |

Choose **Node type** when creating a category. Character restrictions apply down the hierarchy from parent nodes; creating another subcategory does not bypass them.

The root level is primarily for category navigation. Enter the target category before importing Mods to avoid accumulating uncategorized resources at the root.

### 3.3 Creating, editing, and adding categories in bulk

Right-click a blank part of the content area and choose **New Category**, or use the default `Ctrl+Shift+N`. An empty page also offers **+ New Category** or **+ New subcategory**.

![New Category entry on an empty page](../../images/guide-en/30-新建类别入口.webp)

*① Open **Category Overview**. ② Click **+ New Category** in the center of the empty page to open the creation dialog.*

Enter a name, optionally add an English name and cover image, then confirm. The English name is used for online lookups and category matching. Category names must follow Windows filename rules and be unique at the same level. Characters/objects are also checked for name conflicts throughout the current game.

![Enter a category name, choose a node type, and confirm creation](../../images/guide-en/31-新建类别.webp)

*① Enter a name. ② Choose the node type. ③ Click **Confirm**, then double-click the new category card to enter it.*

**Add multiple** in the toolbar lets you create several categories at once:

![Add multiple categories](../../images/guide-en/22-批量添加.webp)

*Enter a name, English name, image, and node type separately for each item. Categories are created at the current level.*

Batch creation also uses the node type selected for each item. To change an existing category's information, choose **Edit** from its context menu or details panel.

If you have a category preset, build the category tree using **Start with a category preset** on the empty page or **Tools → Import category preset**. See Chapter 7. Category presets contain different data from Mod presets, which save which Mods are enabled.

### 3.4 Importing an archive or folder

Enter the target category first, then use any of these methods:

| Method | Action |
| --- | --- |
| Drag and drop | Drag a ZIP, 7z, RAR, or folder into the content area |
| Context menu | Right-click a blank area → **Import Mod** |
| Shortcut | Default `Ctrl+I` |
| Empty category button | Click **Import Mod** |

![Import dialog](../../images/guide-en/05-导入Mod.webp)

The import dialog offers **Add Files**, **Add Folder**, and **Remove Selected**. Set an age rating and preview image for each source. There is no target category dropdown: the destination is the location you were in before opening the dialog.

The source names in the table identify the current import candidates. You can edit the display name, author, version, source URL, and tags after importing.

Distinguish between not using a preview and deleting it: the former only deselects it, while the latter affects the image file. Check the scope shown in the current confirmation prompt before deleting.

After clicking **Confirm Import**, use the task center at the bottom-right to check progress, duplicates, and failures. Importing normally preserves the source files. When files are discovered directly inside the library, however, adding them to management may organize them in place; these are not identical copying workflows.

### 3.5 Placing files directly in the library

You can also use File Explorer to place archives or folders awaiting organization into an existing category folder, such as `外观` (Appearance):

```text
<data root>/data/Sample Game/mods/外观/
```

Folder monitoring, entering the relevant category, switching games, and manual refreshes discover changes that need processing. The default `F5` requests a refresh of the current game's library, with progress in the task center. Large folders and archives that are still being written may take time.

Discovered content is identified, verified, and brought under management to form complete Mod containers. The application reports duplicates, invalid containers, files in use, and interrupted items. If leftover items are found, inspect the original files first, then choose an available action such as keeping them, moving them to the Recycle Bin, or deleting them.

**Do not edit or remove a container's identity marker.** The application uses these markers to identify Mods and checks the related files. Keep each container complete; see Chapter 4.

### 3.6 Enabling, disabling, and conflicts

Any of these actions can toggle a single Mod:

- Double-click its card or list row.
- Click **Enable** / **Disable** in the details panel on the right.
- Choose **Enable** / **Disable** from its context menu.
- Select a single Mod in the overview list and press the default `Space`.

After a Mod is enabled successfully, its status changes and the card shows a green marker. The first activation may require extraction; wait for that item's progress to finish. A parked copy that meets the requirements can be restored more quickly when enabled again.

If another Mod is already enabled under a character/object, the application asks whether to disable the conflicting item and enable the new one. If you choose not to be prompted again, similar conflicts will follow that preference.

**Disable All** applies to the current folder and its subfolders. At the root level, it covers the current game. An active search does not mean that only search results will be disabled; read the confirmation carefully.

Related write operations may be temporarily unavailable during deployment, preset switching, or file recovery. Let the current task finish before continuing, rather than repeatedly clicking Enable or Delete.

### 3.7 Details panel: information, keys, and variants

![Mod details](../../images/guide-en/04-Mod信息.webp)

*The example Mod uses a geometric placeholder cover. Separate details tabs appear beneath it.*

| Tab | Contents |
| --- | --- |
| **Information** | Available information such as author, version, age rating, tags, description, source, and path |
| **Keys: N** | Bindings detected in INI files, grouped by file; record a key or enter it manually |
| **Variants: N** | The Mod's default configuration and saved named appearances, with controls to switch and manage them |

If there are no keys or variants, the corresponding empty state appears. This does not mean the Mod is unusable. The variant count reflects saved named configurations, and support also depends on whether the Mod declares variables that can be saved.

Selecting a category shows category information and actions. Selecting multiple items shows the selection count instead of combining fields from several Mods for editing.

### 3.8 Editing Mods and tags

Select a single Mod and click **Edit**, or use the default `F2`.

![Edit Mod](../../images/guide-en/09-编辑Mod.webp)

You can edit the display name, author, version, source URL, description, age rating, and tags. Expand **File Info** to see the path, SHA-1, and import time, and to copy the path or hash.

**Also rename the Mod file or folder** controls whether the name on disk changes too. If unchecked, only the display name changes. If checked, the application handles name conflicts and related paths; check the result after saving.

**Click Save changes to commit the information and tag edits from this session.** The dialog also supports `Ctrl+Enter` to save. If you close it with changes, you can choose to continue editing or discard them.

There are three kinds of tag operations:

| Operation | Effect |
| --- | --- |
| Add or remove tags on the current Mod | Changes the association between the current Mod and those tags |
| Edit a shared tag's name or color | After saving, every Mod referencing that tag in the same game displays the change |
| Delete a tag and all its references | Removes it from the current game's tag library and all Mods referencing it; after confirmation, the change still needs to be saved through the main edit dialog |

You can reuse tags, choose preset colors, or enter custom colors. Click tag chips to filter by them. Deleting an entire shared tag is different from removing one tag from the current Mod.

### 3.9 Search, tag filters, and sorting

The search box searches names, authors, and tags. With a search term entered, it finds matches throughout the current category's subtree. Clearing the search returns to browsing the current level.

Click **Tags** to expand the tags used in the current game. Two combination modes are available:

| Mode | Example: selecting both `配色` (Color scheme) and `日常` (Everyday) |
| --- | --- |
| **Match all** | Shows only Mods with both tags |
| **Match any** | Matches Mods with either tag |

Selected conditions appear as chips. Remove them individually, or click **Clear** to clear only the tags. Tag filtering is temporarily unavailable when the type filter is set to categories or characters/objects; switch back to all types or Mods to use it again.

Search text, type, age rating, and tags can all affect results at the same time. If there are no results, check the conditions before rebuilding the index.

Sorting supports name, import time, author, age rating, enabled state, and other criteria. Chinese names are sorted by pinyin.

The NSFW dropdown controls whether local X-rated preview images are shown normally, hidden, or blurred. It does not automatically delete or disable Mods. The local overview and gallery share this display preference; the online page has its own NSFW policy.

### 3.10 Adding, removing, and deleting preview images

Drag in an image, click **Add preview image**, or paste an image from the clipboard into a supported preview area. Mods support multiple images; categories and games each use a single cover image.

Use the arrows to browse multiple images. Changing the displayed image affects the card preview. The image viewer supports zooming in and out and dragging to pan; follow the window's hints for its controls.

- **Remove / do not use**: removes the reference and keeps the corresponding file.
- **Delete**: deletes the corresponding image file. The relevant interface explains the scope before the operation.
- **Delete all preview images**: affects multiple images for this Mod. Check the target before confirming.

Manually added Mod previews are normally copied into the container's `.d3dxmeta/previews/`. Previews inside the archive may be extracted to the cache. When backing up a Mod, keep the complete container, not just cached images.

### 3.11 Moving items and selecting in bulk

| Action | Default method |
| --- | --- |
| Add to / remove from selection | `Ctrl` + click |
| Select a range | `Shift` + click |
| Select the entire current list | `Ctrl+A` |
| Cut selected items | `Ctrl+X` |
| Move to the target category | Enter the target category, then press `Ctrl+V` |
| Cancel cutting | `Esc` |
| Delete | `Delete` or the context menu |
| Export | `Ctrl+Shift+E` or **Export** in the context menu |

Cut items are marked in orange while awaiting a move. Pasting at the original level does not move them, and you cannot paste a parent category into its own subtree. The edit dialog has no category field for moving an item; use the move operations above to change its category.

### 3.12 Deletion and the Recycle Bin

Disabling only withdraws the copy used by the game; the Mod remains in the library. Deleting handles the source container and its associated deployment data.

**Settings → General → Mod management → Deletion method** offers **Recycle Bin (recoverable)** and **Permanent deletion (unrecoverable)**. Permanent deletion is the default. This setting applies to deleting individual Mods or Mods in bulk. **Category deletion and game deletion do not use this Recycle Bin setting.**

Deleting a category affects its subcategories and Mods. With multiple items selected, the confirmation summarizes the scope. A batch task may also ask whether to keep enabled Mods.

If the system Recycle Bin does not support the current location or path, the application asks again whether to delete permanently instead. If you decline, check the interface for which items remain unfinished; do not assume they reached the Recycle Bin.

When files are in use or a disk is offline, deletion may retain cleanup records and retry. Release the files or reconnect the disk, then check the result. Canceling a batch task stops only subsequent processing; it cannot restore files already deleted permanently.

### 3.13 Exporting and sharing

Select Mods or categories, right-click **Export**, and choose a destination folder. Exporting a category includes the Mods in its subtree. Select multiple items to export in bulk.

Under **Settings → Import and export → Export**, choose:

- Whether to package as ZIP, 7z, RAR, or a folder.
- Whether to force conversion to the selected format; when off, archives may keep their original format.
- Whether to preserve the character/category folder structure.
- Whether to include metadata and preview images.
- How many export tasks to process at the same time.

To share named variants, tags, and additional previews, enable **Include metadata and previews**. Named variants are included through supported metadata export workflows. A game-wide Mod preset is different from an exported single Mod.

The export window shows progress and offers pause, resume, and stop actions. After completion, check the success, failure, and missing-source counts, then open the output folder to confirm the files.

### 3.14 Gallery

![Gallery with placeholder data](../../images/guide-en/21-画廊.webp)

The gallery displays the current game's Mods through their preview images. Click once to locate the corresponding Mod in the overview; double-click to toggle its enabled state. The same rules apply as in the overview.

Return to **Category Overview** for categories, tags, sorting, editing, or batch operations. A placeholder is used when there is no preview image; this does not affect whether the Mod itself can be enabled.
