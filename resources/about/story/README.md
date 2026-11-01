# 项目短片资源

封面和 `story.json` 随源码管理，`film.mp4` 由独立资源包 `d3dxSkinManage-Plus-story-media.zip` 提供。
该视频不进入 Git。制作完整发行包时，将资源包解压到项目根目录，
确保 `resources/about/story/film.mp4` 存在且通过下述校验；构建脚本从该路径读取视频。

## 获取与验证

资源包由项目维护者另行分发，源码不固定下载仓库。云端工作流默认从当前 GitHub 仓库的
`build-resources` 预发布下载此附件，首次上传方法见[构建指南的首次配置](../../../docs/构建指南.md#首次配置)。
在有本地视频的环境中执行 `python scripts/release/github_release.py pack-media` 可校验并制作资源包。

资源包内保留上述相对路径。取得资源包后，在项目根目录执行：

```powershell
Expand-Archive -LiteralPath '路径/d3dxSkinManage-Plus-story-media.zip' -DestinationPath .
(Get-FileHash -Algorithm SHA256 -LiteralPath 'resources/about/story/film.mp4').Hash
```

视频 SHA-256：`8928bc52314ba4ef69aa7843419b4cd87839f0de62e1fb89d51db8331b2ea1ee`。
视频字节数：`24635970`。
机器可读信息见[资源清单](media-manifest.json)。本地仍可替换短片；发布新资源包时同步更新清单。
