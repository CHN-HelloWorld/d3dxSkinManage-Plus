# 5-minute quick start

Start by adding a game, then follow these six steps to import and enable your first Mod and check it in the game. The time required depends on the Mod's size and your disk speed.

The numbers in the illustrations correspond to the instructions below each image. Click an image to enlarge it. All example names and preview images are placeholders; enter your own game details and paths.

## Before you start

You need Windows 10/11 x64, a working 3DMigoto setup for your target game, and a Mod archive or folder. ZIP, 7z, and RAR archives can all be imported; you only need to install WinRAR separately to create RAR archives.

Extract the entire release package and run `d3dxSkinManage-Plus.exe`. Keep the folders included with the package. This application manages and deploys Mods; 3DMigoto still loads them in the game.

**Local management works offline.** A network connection is only needed when browsing GameBanana, downloading files, or checking for updates.

First, locate these two folders:

| Location | Purpose |
| --- | --- |
| 3DMigoto folder | Contains the loader and `d3dx.ini` |
| Mod folder specified in `d3dx.ini` | The deployment folder the game actually reads, commonly `Mods` inside the 3DMigoto folder |

> ⚠️ The application's **Mod folder** must point to the second location above. The library stores the original Mods, while the deployment folder supplies the files the game reads. Keep these locations separate.

