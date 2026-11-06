"""只读解析旧版用户环境、索引、分类规则及本地图片，不执行旧版代码。"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

from rapidfuzz import fuzz

from src.plugin_sdk.imports import directory_fingerprint

from .sources import IMAGE_SUFFIXES, archive_entries, cache_entries


@dataclass(frozen=True)
class LegacyEnvironment:
    """保存所选安装目录中的直接用户环境，不包含宿主配置。"""

    key: str
    name: str
    path: str
    root: str


@dataclass(frozen=True)
class LegacyCategory:
    """描述目标分类路径；对象节点使用 character 类型。"""

    path: tuple[str, ...]
    node_type: str = "category"
    thumbnail_path: str = ""


@dataclass(frozen=True)
class LegacyMod:
    """冻结可导入的旧版本地条目；分类路径包含最终对象节点。"""

    sha: str
    source_path: str
    name: str
    object_name: str = ""
    category_path: tuple[str, ...] = ()
    author: str = ""
    description: str = ""
    rating: str = "G"
    tags: tuple[str, ...] = ()
    preview_paths: tuple[str, ...] = ()
    source_url: str = ""
    size_bytes: int = 0
    archive_path: str = ""
    cache_path: str = ""
    cache_digest: str = ""
    cache_size_bytes: int = 0
    cache_changed: bool = False
    legacy_key: str = ""


@dataclass(frozen=True)
class ScanIssue:
    """使用稳定中文翻译键说明缺失、无效或部分迁移的信息。"""

    code: str
    message: str
    name: str = ""
    source: str = ""


@dataclass(frozen=True)
class LegacyScan:
    """保存一次扫描的不可变结果，仅包含具有本体的可导入 Mod。"""

    environment: LegacyEnvironment
    categories: tuple[LegacyCategory, ...]
    mods: tuple[LegacyMod, ...]
    issues: tuple[ScanIssue, ...]
    total_bytes: int


@dataclass(frozen=True)
class _IndexRecord:
    """保留索引来源与已展开地址，检测跨文件元数据冲突。"""

    sha: str
    item: dict
    source: Path
    disabled: bool = False


class _UnsafePath(ValueError):
    """表示路径越出安装目录，或经过链接及其他重解析点。"""


class _DuplicateKey(ValueError):
    """阻止 JSON 同名字段在解析期间被静默覆盖。"""


_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
_IMAGE_SUFFIXES = IMAGE_SUFFIXES
_MOD_FIELDS = frozenset({"object", "type", "name", "author", "explain", "grading", "tags", "get"})


def _valid_index_key(key: str) -> bool:
    """接受普通文件名索引键，拒绝路径、设备名、流和 Windows 文件名别名。"""
    if not key or key in {".", ".."} or key.endswith((".", " ")) or re.search(r'[<>:"/\\|?*\x00-\x1f]', key):
        return False
    stem = key.split(".", 1)[0].rstrip(" ").upper()
    return (
        stem not in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
        and re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", stem) is None
    )


def _bounded_path(root: Path, path: Path) -> Path:
    """验证最终位置和每级链接；不存在的普通路径留给调用方报告缺失。"""
    try:
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise _UnsafePath
        for ancestor in (path, *path.parents):
            if ancestor == root:
                break
            try:
                metadata = ancestor.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
                raise _UnsafePath
        return resolved
    except (OSError, RuntimeError, ValueError) as error:
        raise _UnsafePath from error


def _installation(root: str) -> Path:
    """接受包含普通 home 目录的安装根目录，不使用当前目录推测空输入。"""
    try:
        if not root:
            raise ValueError
        directory = Path(root).resolve(strict=True)
        home = _bounded_path(directory, directory / "home")
        if not directory.is_dir() or not home.is_dir():
            raise ValueError
        return directory
    except (OSError, ValueError, RuntimeError) as error:
        raise ValueError("所选目录不是旧版安装目录") from error


def discover_environments(root: str) -> tuple[LegacyEnvironment, ...]:
    """按旧版登录规则列出 home 的直接子目录，排除链接环境。"""
    directory = _installation(root)
    environments = []
    try:
        for path in sorted((directory / "home").iterdir(), key=lambda item: item.name.casefold()):
            try:
                checked = _bounded_path(directory, path)
            except _UnsafePath:
                continue
            if checked.is_dir():
                environments.append(LegacyEnvironment(path.name, path.name, str(checked), str(directory)))
    except OSError as error:
        raise ValueError("无法读取旧版用户环境目录") from error
    return tuple(environments)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    """在对象构造前拒绝重复 JSON 键，保留索引冲突的可见性。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _valid_metadata(item: dict) -> bool:
    """验证会写入宿主的字段，避免把未知分级或非文本信息降为默认值。"""
    if any(not isinstance(item.get(key), str) or not item[key].strip() for key in ("name", "object")):
        return False
    if any(not isinstance(item.get(key, ""), str) for key in ("author", "explain")):
        return False
    if item.get("grading", "G") not in ("G", "P", "R", "X"):
        return False
    tags = item.get("tags", [])
    return isinstance(tags, list) and all(isinstance(tag, str) for tag in tags)


class _Scanner:
    """管理单次只读扫描的取消检查、路径边界与结构化诊断。"""

    def __init__(
        self,
        environment: LegacyEnvironment,
        should_cancel: Callable[[], bool] | None,
        progress: Callable[[int, int, str], None] | None,
    ) -> None:
        """冻结回调与已发现的环境；不信任外部构造的环境路径。"""
        self.environment = environment
        self.should_cancel = should_cancel
        self.progress = progress
        self.issues: list[ScanIssue] = []
        self._reported: set[ScanIssue] = set()
        self.check_cancel()
        self.root = _installation(environment.root)
        try:
            self.profile = _bounded_path(self.root, Path(environment.path))
            if self.profile.parent != self.root / "home" or self.profile.name != environment.key:
                raise _UnsafePath
            if not self.profile.is_dir():
                raise _UnsafePath
        except _UnsafePath as error:
            raise ValueError("旧版用户环境路径无效") from error
        self.thumbnails: dict[str, Path] = {}
        self.approximate_thumbnails = False
        self.thumbnail_mode = "similarity/key-in"
        self.classifications: dict[str, tuple[str, ...]] = {}
        self.classifications_complete = True
        self.categories: dict[tuple[str, ...], LegacyCategory] = {}

    def check_cancel(self) -> None:
        """在目录、文件和 Mod 处理边界响应宿主取消。"""
        if self.should_cancel is not None and self.should_cancel():
            raise InterruptedError("旧版扫描已取消")

    def issue(self, code: str, message: str, name: str = "", source: Path | str = "") -> None:
        """按完整诊断去重，路径与对象名称不混入翻译键。"""
        issue = ScanIssue(code, message, name, str(source))
        if issue not in self._reported:
            self._reported.add(issue)
            self.issues.append(issue)

    def safe(self, path: Path) -> bool:
        """在读取任何内容前检查链接与根目录边界，并记录拒绝原因。"""
        self.check_cancel()
        try:
            _bounded_path(self.root, path)
            return True
        except _UnsafePath:
            self.issue("invalid", "路径越过旧版目录或包含链接，已跳过", source=path)
            return False

    def children(self, directory: Path) -> tuple[Path, ...]:
        """读取一层普通目录；可选资源目录不存在时不创建目录。"""
        if not self.safe(directory):
            return ()
        try:
            if not directory.exists():
                return ()
            return tuple(sorted(directory.iterdir(), key=lambda item: item.name.casefold()))
        except OSError:
            self.issue("invalid", "无法读取旧版文件或目录", source=directory)
            return ()

    def text(self, path: Path) -> str | None:
        """只读 UTF-8 文本，单个资源异常不终止其他索引的解析。"""
        if not self.safe(path):
            return None
        try:
            return path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            self.issue("invalid", "无法读取旧版文件或目录", source=path)
            return None

    def document(self, path: Path) -> dict | None:
        """解析普通 JSON 对象并拒绝重复键及损坏内容。"""
        content = self.text(path)
        if content is None:
            return None
        try:
            document = json.loads(content, object_pairs_hook=_unique_object)
            if not isinstance(document, dict):
                raise ValueError
            return document
        except _DuplicateKey:
            self.issue("invalid", "旧版 JSON 含重复字段，已跳过文件", source=path)
        except (ValueError, RecursionError):
            self.issue("invalid", "旧版索引或配置格式无效，已跳过文件", source=path)
        return None

    def index_files(self, directory: Path) -> Iterator[Path]:
        """递归所有索引；停用状态在原包和元数据优先级检查时处理。"""
        pending = [directory]
        while pending:
            self.check_cancel()
            for path in self.children(pending.pop()):
                if not self.safe(path):
                    continue
                if path.is_dir():
                    pending.append(path)
                elif path.is_file() and path.suffix.casefold() == ".json":
                    yield path

    def records(self) -> dict[str, list[_IndexRecord]]:
        """收集文件名索引键；键可为 SHA 或可读名称，但不允许作为任意路径。"""
        directory = self.profile / "modsIndex"
        if self.safe(directory) and not directory.exists():
            self.issue("invalid", "旧版索引目录不存在", source=directory)
        records: dict[str, list[_IndexRecord]] = {}
        for path in self.index_files(directory):
            document = self.document(path)
            if document is None:
                continue
            mods = document.get("mods")
            if not isinstance(mods, dict):
                self.issue("invalid", "旧版索引或配置格式无效，已跳过文件", source=path)
                continue
            variables = document.get("variable", {})
            if not isinstance(variables, dict) or any(not isinstance(value, str) for value in variables.values()):
                self.issue("warning", "获取地址格式或变量无效，已跳过该地址", source=path)
                variables = {}
            for sha, item in mods.items():
                self.check_cancel()
                if not _valid_index_key(sha):
                    self.issue("invalid", "旧版索引键无效，已跳过", sha, path)
                    continue
                if not isinstance(item, dict):
                    self.issue("invalid", "旧版 Mod 元数据无效，已跳过", sha, path)
                    continue
                expanded = dict(item)
                if isinstance(item.get("get"), list):
                    expanded["get"] = [self.expand_address(address, variables) for address in item["get"]]
                disabled = any(part.casefold().startswith("disabled") for part in path.relative_to(directory).parts)
                key = sha.lower() if _SHA.fullmatch(sha) else sha
                records.setdefault(key, []).append(_IndexRecord(key, expanded, path, disabled))
        return records

    @staticmethod
    def expand_address(address: object, variables: dict) -> object:
        """只按旧版字符串变量语法展开地址，不计算表达式或执行配置。"""
        if not isinstance(address, dict) or not isinstance(address.get("url"), str):
            return address
        result = dict(address)
        for key, value in variables.items():
            result["url"] = result["url"].replace("($." + key + ")", value)
        return result

    def read_classifications(self) -> None:
        """分类文件名整体作为一层名称，每个非空行都是通配规则。"""
        issue_start = len(self.issues)
        for path in self.children(self.profile / "classification"):
            if not self.safe(path) or not path.is_file():
                continue
            content = self.text(path)
            if content is not None:
                self.classifications[path.name] = tuple(line.strip() for line in content.splitlines() if line.strip())
                category = LegacyCategory((path.name,), thumbnail_path=self.thumbnail(path.name))
                self.categories[category.path] = category
        self.classifications_complete = not any(issue.code == "invalid" for issue in self.issues[issue_start:])

    def thumbnail_directory(self, directory: Path) -> dict[str, Path]:
        """读取单层 PNG/JPG；同批同名图片的优先级无法确定时报告歧义。"""
        images: dict[str, Path] = {}
        ambiguous: set[str] = set()
        for path in self.children(directory):
            if path.suffix.casefold() not in _IMAGE_SUFFIXES or not self.safe(path) or not path.is_file():
                continue
            if path.stem in images:
                ambiguous.add(path.stem)
                self.issue("warning", "同名缩略图存在歧义，已跳过", path.stem, directory)
            images[path.stem] = path
        return {name: path for name, path in images.items() if name not in ambiguous}

    def reference_path(self, directory: Path, value: str) -> Path | None:
        """解析 Windows 风格图片路径，外部绝对路径和越界相对路径均拒绝。"""
        path = Path(value.replace("\\", "/"))
        if PureWindowsPath(value).drive and not path.is_absolute():
            self.issue("invalid", "路径越过旧版目录或包含链接，已跳过", source=value)
            return None
        path = directory / path
        return path if self.safe(path) else None

    def read_thumbnails(self) -> None:
        """保留精确命名及配置覆盖顺序；近似匹配配置不用于推测迁移目标。"""
        configuration = self.root / "local/configuration"
        if self.safe(configuration) and configuration.is_file():
            data = self.document(configuration)
            if data is not None:
                self.thumbnail_mode = data.get("thumbnail_approximate_algorithm", "similarity/key-in")
                self.approximate_thumbnails = bool(self.thumbnail_mode)
        else:
            self.approximate_thumbnails = True
        directory = self.profile / "thumbnail"
        self.thumbnails.update(self.thumbnail_directory(directory))
        configuration = directory / "_redirection.ini"
        if not self.safe(configuration) or not configuration.exists():
            return
        content = self.text(configuration)
        if content is None:
            return
        for raw_line in content.splitlines():
            self.check_cancel()
            line = raw_line.strip()
            if not line or line.startswith((";", "/", "\\")):
                continue
            if "=" in line:
                name, value = (part.strip() for part in line.split("=", 1))
                if not name or not value:
                    self.issue("warning", "无法识别旧版缩略图指令，已跳过", line, configuration)
                    continue
                target = self.reference_path(directory, value)
                if target is not None:
                    if target.is_file() and target.suffix.casefold() in _IMAGE_SUFFIXES:
                        self.thumbnails[name] = target.resolve()
                    else:
                        self.issue("warning", "旧版缩略图资源不存在或格式不受支持", name, target)
            elif line.startswith("[*] ") and line.endswith(("\\*", "/*")):
                target = self.reference_path(directory, line[4:-2].strip())
                if target is not None:
                    if target.is_dir():
                        self.thumbnails.update(self.thumbnail_directory(target))
                    else:
                        self.issue("warning", "旧版缩略图资源不存在或格式不受支持", source=target)
            else:
                self.issue("warning", "无法识别旧版缩略图指令，已跳过", line, configuration)

    def thumbnail(self, name: str) -> str:
        """优先精确匹配，再复现旧版四种近似规则并保留可见提示。"""
        path = self.thumbnails.get(name)
        if path is not None:
            return str(path)
        if self.approximate_thumbnails and self.thumbnails:
            mode = self.thumbnail_mode
            if not isinstance(mode, str) or mode not in {
                "key-in only",
                "similarity only",
                "similarity threshold",
                "similarity/key-in",
            }:
                self.issue("warning", "旧版缩略图匹配方式不受支持，仅使用精确匹配", name)
                return ""
            best = None
            score = 0.0
            for candidate, image in self.thumbnails.items():
                similarity = fuzz.ratio(name, candidate)
                if mode == "key-in only":
                    if candidate in name:
                        best = image
                        break
                elif similarity > score and (
                    mode == "similarity only" or similarity >= 50 or mode == "similarity/key-in" and candidate in name
                ):
                    best, score = image, similarity
            if best is not None:
                self.issue("warning", "已按旧版规则近似匹配缩略图，请检查结果", name, best)
                return str(best)
        return ""

    def category_path(self, name: str) -> tuple[str, ...] | None:
        """按旧版 Windows 通配规则归类，多命中时保留诊断并拒绝任选一项。"""
        matches = [
            category
            for category, patterns in self.classifications.items()
            if any(fnmatch.fnmatchcase(name.lower(), pattern.lower()) for pattern in patterns)
        ]
        if len(matches) > 1:
            sources = "\n".join(str(self.profile / "classification" / category) for category in matches)
            self.issue("invalid", "对象匹配多个旧版分类，已跳过相关 Mod", name, sources)
            return None
        parent = (matches[0] if matches else "未分类",)
        if parent not in self.categories:
            self.categories[parent] = LegacyCategory(parent, thumbnail_path=self.thumbnail(parent[0]))
        path = (*parent, name)
        self.categories[path] = LegacyCategory(path, "character", self.thumbnail(name))
        return path

    def source_url(self, record: _IndexRecord) -> str:
        """保存首个有效 HTTP(S) 来源，报告多地址及无法解析的变量。"""
        addresses = record.item.get("get", [])
        if not isinstance(addresses, list):
            self.issue("warning", "获取地址格式或变量无效，已跳过该地址", record.sha, record.source)
            return ""
        if len(addresses) > 1:
            self.issue("warning", "旧版包含多个获取地址，仅保留首个有效地址", record.sha, record.source)
        result = ""
        for address in addresses:
            self.check_cancel()
            if isinstance(address, dict) and (
                address.keys() - {"url", "mode"} or address.get("mode") not in (None, "", "get", "lanzou")
            ):
                self.issue("warning", "获取配置包含不支持的方式或参数，仅保留地址", record.sha, record.source)
            url = address.get("url") if isinstance(address, dict) else None
            try:
                parsed = urlsplit(url) if isinstance(url, str) else None
                valid = parsed is not None and parsed.scheme in ("http", "https") and parsed.netloc and "($." not in url
            except ValueError:
                valid = False
            if not valid:
                self.issue("warning", "获取地址格式或变量无效，已跳过该地址", record.sha, record.source)
            elif not result:
                result = url
        return result

    def cache_paths(self, sha: str) -> tuple[Path, ...]:
        """由文件系统解析启用与停用缓存路径，不折叠可读索引键。"""
        candidates = (self.profile / "work/Mods" / sha, self.profile / "work/Mods" / ("disabled-" + sha))
        return tuple(path for path in candidates if self.safe(path) and path.is_dir())

    def archive_sha1(self, source: Path) -> str:
        """为可读索引键的原包计算实际摘要，并检查读取期间文件身份未变化。"""
        before = source.stat()
        digest = hashlib.sha1()
        with source.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                self.check_cancel()
                digest.update(chunk)
        after = source.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("Mod 来源已变化，请重新扫描")
        self.check_cancel()
        return digest.hexdigest()

    def previews(self, sha: str) -> tuple[str, ...]:
        """读取已知预览布局，按图片字节去重，不把普通纹理当作预览。"""
        images = []
        digests = set()

        def add(path: Path) -> None:
            """加入安全且可读取的图片路径，保持默认预览的优先顺序。"""
            if path.suffix.casefold() not in _IMAGE_SUFFIXES or not self.safe(path) or not path.is_file():
                return
            try:
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        self.check_cancel()
                        digest.update(chunk)
                key = digest.digest()
                if key not in digests:
                    images.append(str(path))
                    digests.add(key)
            except OSError:
                self.issue("warning", "无法读取旧版预览图，已跳过", source=path)

        def directory(path: Path, all_images: bool = False) -> None:
            """只在专用预览目录中放宽文件名，递归仍拒绝链接和越界路径。"""
            pending = [(path, all_images)]
            while pending:
                parent, dedicated = pending.pop()
                for image in self.children(parent):
                    if not self.safe(image):
                        continue
                    if image.is_dir():
                        pending.append((image, dedicated or image.name.casefold() in {"preview", "preview_screen"}))
                    elif dedicated or image.stem.casefold().startswith("preview"):
                        add(image)

        for kind in ("preview", "preview_screen"):
            shared = self.root / "resources" / kind
            for suffix in _IMAGE_SUFFIXES:
                add(shared / (sha + suffix))
            directory(shared / sha, True)
        for cache in self.cache_paths(sha):
            directory(cache)
        return tuple(images)

    def mod(self, sha: str, records: list[_IndexRecord]) -> LegacyMod | None:
        """验证索引交集、元数据和本体后生成可交给宿主验证的导入条目。"""
        records = sorted(records, key=lambda value: (value.disabled, str(value.source).casefold()))
        record = records[0]
        sha = record.sha
        active = [value for value in records if not value.disabled]
        if active and len(active) != len(records):
            self.issue(
                "warning",
                "相同 Mod 的停用索引已跳过，保留启用索引信息",
                sha,
                "\n".join(str(value.source) for value in records if value.disabled),
            )
            records = active
        if len(records) > 1:
            sources = "\n".join(str(item.source) for item in records)
            if any(item.item != record.item for item in records[1:]):
                self.issue("invalid", "同一索引键的元数据冲突，已跳过", sha, sources)
                return None
            self.issue("warning", "多个索引包含相同 Mod，已合并重复项", sha, sources)
        item = record.item
        if not _valid_metadata(item):
            self.issue("invalid", "旧版 Mod 元数据无效，已跳过", sha, record.source)
            return None
        if not self.classifications_complete:
            self.issue(
                "invalid", "旧版分类规则无法完整读取，已跳过相关 Mod", item["name"], self.profile / "classification"
            )
            return None
        if item.keys() - _MOD_FIELDS:
            self.issue("warning", "索引包含无法迁移的扩展字段", item["name"], record.source)
        source = self.root / "resources/mods" / sha
        if not self.safe(source):
            return None
        try:
            has_archive = source.is_file()
            if not has_archive and record.disabled:
                self.issue("missing", "停用索引没有本地原包，已跳过", item["name"], source)
                return None
            size = source.stat().st_size if has_archive else 0
            archive_sha = sha if _SHA.fullmatch(sha) else ""
            if has_archive and not archive_sha:
                archive_sha = self.archive_sha1(source)
        except InterruptedError:
            raise
        except ValueError as error:
            self.issue("invalid", str(error), item["name"], source)
            return None
        except OSError:
            self.issue("invalid", "无法读取旧版文件或目录", item["name"], source)
            return None
        cache_path = ""
        cache_digest = ""
        cache_size = 0
        changed = False
        caches = self.cache_paths(sha)
        if len(caches) > 1:
            self.issue("warning", "存在多个解压缓存，无法确定恢复来源，已忽略缓存", item["name"])
        elif caches:
            cache = caches[0]
            try:

                def cancelled():
                    """给目录比较提供不触碰界面对象的取消检查。"""
                    return self.should_cancel is not None and self.should_cancel()

                fingerprint = directory_fingerprint(str(cache), cancelled)
                entries, cache_size = cache_entries(cache, cancelled)
                if directory_fingerprint(str(cache), cancelled) != fingerprint:
                    raise ValueError
                if entries:
                    cache_path, cache_digest = str(cache), fingerprint
                    if has_archive:
                        try:
                            changed = entries != archive_entries(source)
                        except Exception:
                            changed = True
                        if changed:
                            self.issue(
                                "warning", "缓存与原包不同或无法确认一致，可选择恢复缓存修改", item["name"], cache
                            )
            except (OSError, ValueError, RuntimeError):
                self.issue("warning", "解压缓存无效或包含链接，已忽略缓存", item["name"], cache)
        if not has_archive:
            if not cache_path:
                self.issue("missing", "旧版 Mod 本体缺失，仅有索引或解压缓存", item["name"], source)
                return None
            self.issue("warning", "原包缺失，将恢复解压缓存；缓存可能不完整，请检查结果", item["name"], cache_path)
        elif record.disabled:
            self.issue("warning", "停用索引的本地原包已纳入迁移", item["name"], record.source)
        category_path = self.category_path(item["object"])
        if category_path is None:
            return None
        return LegacyMod(
            sha=archive_sha,
            source_path=str(source) if has_archive else cache_path,
            name=item["name"],
            object_name=item["object"],
            category_path=category_path,
            author=item.get("author", ""),
            description=item.get("explain", ""),
            rating=item.get("grading", "G"),
            tags=tuple(item.get("tags", [])),
            preview_paths=self.previews(sha),
            source_url=self.source_url(record),
            size_bytes=size if has_archive else cache_size,
            archive_path=str(source) if has_archive else "",
            cache_path=cache_path,
            cache_digest=cache_digest,
            cache_size_bytes=cache_size,
            cache_changed=changed,
            legacy_key=sha,
        )

    def scan(self) -> LegacyScan:
        """读取规则后逐项报告进度，总字节数仅统计可导入本体一次。"""
        self.read_thumbnails()
        self.read_classifications()
        records = self.records()
        mods = []
        total = len(records)
        if self.progress is not None:
            self.progress(0, total, "正在扫描旧版 Mod")
        for completed, (sha, entries) in enumerate(sorted(records.items()), 1):
            self.check_cancel()
            mod = self.mod(sha, entries)
            if mod is not None:
                mods.append(mod)
            if self.progress is not None:
                self.progress(completed, total, "正在扫描旧版 Mod")
        self.check_cancel()
        categories = tuple(
            self.categories[path] for path in sorted(self.categories, key=lambda path: (len(path), path))
        )
        return LegacyScan(
            self.environment, categories, tuple(mods), tuple(self.issues), sum(mod.size_bytes for mod in mods)
        )


def scan_environment(
    environment: LegacyEnvironment,
    *,
    should_cancel: Callable[[], bool] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> LegacyScan:
    """扫描一个已发现的环境；取消抛出 InterruptedError，来源始终保持只读。"""
    return _Scanner(environment, should_cancel, progress).scan()
