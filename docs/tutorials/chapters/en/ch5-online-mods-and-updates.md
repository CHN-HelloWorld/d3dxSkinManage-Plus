## Chapter 5: Online mods and updates

### 5.1 Connecting to GameBanana

<img src="../../images/plusa/supplement/下载更新.webp" alt="Plusa organizing downloads" width="280">

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

The category dropdown's filter supports input methods such as Chinese and matches both original category names and their translations in the current language. Text entered while categories are loading remains active when they arrive. Reopening the dropdown starts with an empty filter.

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

> [![Plusa waiting patiently](../../images/plusa/face/07-困倦.webp)](../../images/plusa/07-困倦.webp) **Plusa's tip:** Large files can take time. Check the status, speed, and error details in download management, and use its pause or cancel controls when you need to stop.

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

### 5.6 Updating one or more mods

Click **Update** for an individual item or **Update all** for a batch. If an entry offers multiple files, **Choose update files** appears first. You must select one file for each relevant mod before starting.

Tasks may pass through preparation, download, verification, extraction, and installation stages. Use the **Updated** status or the results summary to confirm completion.

| Action | Behavior |
| --- | --- |
| Pause / Resume for one item | Controls the task at stages that currently allow pausing |
| Pause all / Resume all / Stop all | Applies to all unfinished updates for the current game, including tasks hidden by the search filter |
| Close update window | If tasks are running, you can return to them, stop unfinished updates, or continue in the background |
| Stop | Unfinished items keep their old versions; completed updates are not undone. You may need to wait during installation commit or final cleanup |

If preparing the new content fails, check the specific file or verification error. The app retains the materials needed for recovery. Do not manually delete source containers, working folders, or transaction records during an update.

Export or back up important custom changes beforehand. Updating a mod and **Back up the previous version** for application updates are separate features; the latter does not replace a mod backup.

### 5.7 Ignoring updates and restoring notifications

An item's More menu offers:

- **View on GameBanana**: open the source page in a browser.
- **Ignore this version**: skip notifications for this version.
- **Ignore this Mod**: stop notifications for future updates to this mod as well.

After ignoring an update, you can use **Undo** in the window's current notification. Undo requires the mod and game to remain valid. After closing the window, do not assume this temporary Undo control will still be available.

### 5.8 Updating the application

Application version updates are under **Settings → Update → Application updates**. Use **Check application updates**.

![Application update settings](../../images/guide-en/16-程序更新.webp)

Update availability depends on whether the distribution channel provides a valid update source and a matching package. If the app reports that no update source is configured, obtain the new version through the software publisher's download channel.

When a new version is found, you can read its release notes and release history, then update, ask to be reminded later, or skip that version.

The one-click update process downloads, checks, and extracts the package, then makes a backup according to your settings. A separate updater replaces the application and restarts it. Keep the `Updater/` folder supplied with the distribution; you do not need to launch the updater directly.

- Finish active tasks before installing an update, then close the application when prompted.
- Updates preserve your mods, settings, and plugins. Regular backups of personal data are still recommended.
- Choose automatic selection or a specific source. Automatic mode compares routes using small downloads and switches after sustained low speed or a connection failure. The window shows the current source and proxy status.
- The current task can resume after a connection interruption or a switch from a slow source. A source without resume support restarts the download. Canceling, exiting the application, or a final download failure removes unfinished data; the next update attempt starts over.
- Installation requires an exact file size and matching SHA-256. Missing or conflicting verification information stops the update.
- If the updater reports an error, retain both application and updater logs, check paths and files in use, then retry.

Application updates and GameBanana mod updates serve different purposes. The former upgrades the manager; the latter replaces resources in the library.
