## Chapter 6: Settings

### 6.1 Sections, search, and saving

The Settings page has six sections across the top: **General**, **Personalization**, **Shortcuts**, **Import and export**, **Online Mods**, and **Update**. Each section also has subpages on the left.

Use the default `Ctrl+K` shortcut to open settings search, or type a setting name in the upper-right corner. Search results can take you across sections and locate the relevant control.

![Searching all settings](../../images/guide-en/26-设置搜索.webp)

*Enter “cache” to find settings for image memory and disk caches.*

The page states **Changes apply immediately and are saved automatically**. Switches and selections usually save immediately, while text fields submit changes when editing finishes. Some sliders preview changes as you drag and save when you release them.

This chapter lists the default settings. Screenshots use example settings; check the values and switch positions shown on your device.

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

