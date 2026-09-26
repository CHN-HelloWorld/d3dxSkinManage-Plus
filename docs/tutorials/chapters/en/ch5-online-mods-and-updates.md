## Chapter 5: Online mods and updates

### 5.1 Connecting to GameBanana

![Plusa organizing downloads](../../images/plusa/supplement/下载更新.webp)

Activate a game, then open **Online Mods**. The app uses the English name in the game's configuration to find a match on GameBanana and saves the result for that game. Changing the English name triggers a new match.

If you see **GameBanana has no entry for this game**, first check the English name on the Games page. You can also use the website search link provided on the page. A network failure and a game that is not listed have different causes: check the connection for the former, and check the game name and the site's listings for the latter.

Configure the proxy in two steps:

1. Under **Settings → General → Network proxy**, turn on the main switch, then use the system proxy or enter a manual address and port.
2. Under **Settings → Online Mods → Online Mod proxy**, turn on **Enable proxy**.

![Online Mods settings](../../images/guide-en/15-在线设置.webp)

*Online browsing, images, downloads, and mod updates all use the Online Mods proxy policy.*

Local categories, importing, and deployment do not require a connection to GameBanana.

### 5.2 Browsing, searching, and details

The online page lets you browse by category, search for mods, change the sort order, and refresh the list. Sort options include **Default**, **Newest**, **Recently updated**, **Most liked**, **Most downloaded**, and **Most viewed**.

![Online browsing with placeholder content](../../images/guide-en/24-在线Mod.webp)

*The names, covers, authors, and statistics are tutorial placeholders used to show the browsing controls in the real interface.*

**Hide NSFW**, **Blur NSFW**, and **Show NSFW** control online content previews; the default is to blur them. This preference is saved separately from the display preference for local X-rated mods.

Click a card to open its details, where you can view images, **Overview**, **Description**, **Update**, and **Available files**. An entry with multiple files often offers different versions, optional content, or attachments. Read the author's instructions before choosing a file.

![Online details and file selection](../../images/guide-en/29-在线详情.webp)

*The filenames and sizes shown here are also placeholders. Read each file's description when using real entries.*

Author descriptions and downloadable files come from an external site. This tutorial uses placeholder content to explain the controls; actual entries depend on the site's content.

### 5.3 Downloading and choosing an installation destination

Select an available file in the details view and click **Download**. When the download finishes, the app prepares to import it into a destination category.

Installation can proceed when there is one unambiguous category match. Otherwise, **Choose installation destination** appears:

- Select an existing category in the tree. You can search by category name.
- Check the destination game and category to avoid installing into another game.
- If the interface offers to save the site's category name as the English name, you can do so to help future matching.
- If the destination folder already has an English name, follow the overwrite confirmation prompt.

Online installation still checks the archive, metadata, and destination path. A finished download does not necessarily mean the mod has been added to the library; wait for the installation result.

If you switch to another game during a download, the task still belongs to the original game. Once downloaded, it may wait for you to switch back before installing. If the new mod is missing from the current page, do not immediately import it again into a different game.

### 5.4 Managing downloads

Open **Downloads** from the online page to see queued, downloading, paused, completed, and failed tasks, along with their progress, speed, and size.

| Control or menu item | Purpose |
| --- | --- |
| Pause / Resume | Control downloads that support pausing |
| Cancel | Stop the current task |
| Retry | Retry a failed download |
| Open Mod category | Locate an installed mod |
| Show in File Explorer | Locate a retained download file |
| Remove from list | Remove the record |
| Delete, including local files | Also handle that download's local files; follow the confirmation prompt |
| Clear completed / Clear all | Process download list entries in bulk; check the scope in the prompt before proceeding |

Downloaded source packages are stored in `<data-root>/data/downloads/`. They are not disposable image caches.

**Delete installer packages automatically**, under **Settings → General → Download data**, is on by default. **The corresponding source package is deleted only after the mod has been successfully added to the library.** Turn this off to retain packages for an offline archive.

**Clean up installed** removes source packages that have been confirmed as added to the library, while keeping downloads that have not yet been installed. Deleting a source package does not delete the imported mod from the library, but retaining source packages is not a substitute for a complete backup.

### 5.5 Checking installed mods for updates

Click **Check for updates** in **Category Overview** to open the mod updates window. Checks depend on saved GameBanana source information. Mods installed from the online page have the relevant records; other local packages may not.

The update window separates check progress from the update list. You can search mod names and view local and new version information, update notes, file dates, and estimated download sizes.

If a check fails or covers only some items, the interface reports that the check is incomplete. This does not mean that all mods are up to date. Use **Check again** once the network connection is restored.

Automatic checks are configured under **Settings → Online Mods → Mod updates**:

| Setting | Default and behavior |
| --- | --- |
| Check Mod updates automatically | On |
| Check on startup | On, with a minimum interval between checks |
| Scheduled check frequency | Off by default; also offers Every 6 hours, Every 12 hours, and Daily |
| Notification style | Badge only by default; a status bar notice can also be added |
| Check concurrency | Auto recommend by default; lets you adjust concurrency in the update workflow |

Manual and automatic checks have different triggers. For everyday use, refer to the latest check results shown in the update window.

