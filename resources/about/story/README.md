# 项目短片资源

封面和 `story.json` 随源码管理，`film.mp4` 由独立资源包 `d3dxSkinManage-Plus-story-media.zip` 提供。
该视频不进入 Git；现有本地视频无需移动。制作完整发行包前，必须将资源包解压到项目根目录，
使视频位于 `resources/about/story/film.mp4`。原有打包程序会读取这个路径。

## 获取与验证

资源包由项目维护者另行分发，当前没有配置在线下载地址。资源包内保留上述相对路径。
取得资源包后，在项目根目录执行：

```powershell
Expand-Archive -LiteralPath '路径/d3dxSkinManage-Plus-story-media.zip' -DestinationPath .
(Get-FileHash -Algorithm SHA256 -LiteralPath 'resources/about/story/film.mp4').Hash
```

视频 SHA-256：`8928bc52314ba4ef69aa7843419b4cd87839f0de62e1fb89d51db8331b2ea1ee`。
视频字节数：`24635970`。
机器可读信息见[资源清单](media-manifest.json)。本地仍可替换短片；发布新资源包时同步更新清单。
