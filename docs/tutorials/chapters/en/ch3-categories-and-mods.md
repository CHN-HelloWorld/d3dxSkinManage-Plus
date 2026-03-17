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

