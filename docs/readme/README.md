# README 配图

本目录为根目录的中文 [README](../../README.md) 和英文 [README](../../README.en.md) 提供本地配图。
封面沿用项目原创看板娘普拉莎，界面预览分别来自现有教程的中英文实际程序截图。
平台、Python 版本、发布版本和许可徽章随仓库提供；下载统计由 Shields.io 动态读取 GitHub Releases 数据。

| 文件 | 来源与处理 |
| --- | --- |
| `hero.webp` | [普拉莎封面插画](../tutorials/images/plusa/封面横幅.webp)，按顶部对齐裁剪为 1680 × 700，增加深色遮罩、项目标题与介绍文案 |
| `hero-en.webp` | 同一封面插画、尺寸与排版，使用英文介绍文案 |
| `overview.webp` | [Mod 信息](../tutorials/images/final/04-Mod信息.webp)，保持完整界面与宽高比，缩小至 1600 像素宽 |
| `gallery.webp` | [画廊](../tutorials/images/final/21-画廊.webp)，同上 |
| `online.webp` | [在线详情](../tutorials/images/final/29-在线详情.webp)，同上 |
| `personalization.webp` | [个性化设置](../tutorials/images/final/12-设置个性化.webp)，同上 |
| `overview-en.webp`、`gallery-en.webp`、`online-en.webp`、`personalization-en.webp` | 对应的[英文实际界面截图](../tutorials/images/final-en/)，保持完整界面与宽高比，缩小至 1600 像素宽 |
| `windows.svg`、`python.svg`、`status.svg`、`license.svg` | 本地静态信息徽章，分别标明平台、Python 版本、已发布版本与 PolyForm 非商业许可及署名附加条款；与动态下载统计采用一致的 `flat` 圆角分段样式 |
| `plusa/character-card.png` | 普拉莎角色卡片原图，用于中英文 README 页脚，点击可查看原始尺寸 |
| `plusa/qq-group.png` | QQ 交流群头像，保留原始尺寸并应用圆形透明遮罩；图片与加入群聊文字均链接到 `https://qm.qq.com/q/Ajc0z0K6Kk` |
| `plusa/emote-smile.png`、`plusa/emote-thinking.png` | 从现有教程的 `face/01-自信微笑.webp` 与 `face/09-思考.webp` 制作的圆形透明表情，用于快速上手和常见问题标题 |

角色卡片从项目美术资源中原样复制；群头像在项目内直接处理为圆形，不另存方形副本。
表情保留 128 × 128 像素尺寸，群头像保留 1254 × 1254 像素尺寸，边缘使用抗锯齿透明遮罩。
README 通过图片宽高控制显示尺寸，圆形效果由 PNG 的透明通道提供，不依赖 GitHub 会过滤的自定义 CSS；教程继续使用原有方形表情。

封面以 WebP 质量 92 导出，八张缩小后的界面预览采用无损 WebP 编码。
封面文案使用系统 Segoe UI 与微软雅黑排版，未分发字体文件。预览不替换界面内容；游戏、Mod 名称和封面均为教程演示数据。
README 中的截图链接指向原始 PNG，方便查看细节。更新截图时，同步替换此处缩略图；平台、Python 版本范围、发布版本或许可变化时，同步更新徽章及其替代文本。

全部徽章按 24 像素高显示，统一使用 Verdana 字体、深灰标签区和彩色信息区；本地 SVG 原始高度为 20 像素，圆角半径为 3。
下载徽章使用 `https://img.shields.io/github/downloads/CHN-HelloWorld/d3dxSkinManage-Plus/total?style=flat&label=Downloads&color=3c8da8`，
显示该仓库所有 Release 附件的累计下载次数，不代表独立用户数或安装数。仓库或 Release 尚不可访问时，徽章可能暂时没有统计数据；
离线阅读也无法刷新这一外部徽章。下载按钮统一指向 `https://github.com/CHN-HelloWorld/d3dxSkinManage-Plus/releases/latest`。
