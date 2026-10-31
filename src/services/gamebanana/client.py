"""GameBanana apiv11 客户端"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from src.services.gamebanana.cache import GbApiCache
from src.services.gamebanana.constants import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_USER_AGENT_TEMPLATE,
)
from src.services.gamebanana.models import (
    GbCategory,
    GbModel,
    GbModFile,
    GbModProfile,
    GbModRecord,
    GbModUpdateEntry,
)
from src.utils.constants import APP_VERSION
from src.utils.i18n import t as tr
from src.utils.logger import logger

_DEPRECATION_HEADERS = ("deprecation", "sunset", "x-deprecated")

# 将界面排序键映射为 GameBanana apiv11 的 _sSort 参数。
# 未知值、default 和 best_match 均省略该参数，
# 由服务端使用默认排序。
_SORT_ALIASES: dict[str, str] = {
    "latest": "Generic_LatestModified",
    "newest": "Generic_Newest",
    "most_downloaded": "Generic_MostDownloaded",
    "most_viewed": "Generic_MostViewed",
    "most_liked": "Generic_MostLiked",
}


class UpdateCheckError(RuntimeError):
    """批量检查失败；保留已取得的文件，未完成部分不能展开成逐项请求。"""

    def __init__(self, message: str, *, partial_files: dict[int, list[GbModFile]] | None = None) -> None:
        """保存更新检查失败原因，并携带已成功获取的部分文件列表供调用方继续处理。"""
        super().__init__(message)
        self.partial_files = partial_files or {}


class UpdateCheckRateLimited(UpdateCheckError):
    """更新检查命中 429；手动检查也须遵守服务端的静默期。"""

    def __init__(self, seconds: float = 120.0) -> None:
        """将剩余冷却时间向上取整后写入本地化的更新限流异常。"""
        super().__init__(tr("GameBanana 请求过于频繁（429），请在 {value} 秒后重试更新检测", value=math.ceil(seconds)))


def _positive_int(value: Any) -> int | None:
    """只接受可转换为正整数的整数或字符串，拒绝布尔值、小数及非正数。"""
    # 外部响应的缺项、布尔值、小数等均不能变成用于「已是最新」判定的时间戳。
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return number if number > 0 else None


class GameBananaClient:
    """封装在线浏览与更新查询，统一协调连接、请求节流、缓存及限流冷却。"""

    BASE = "https://gamebanana.com/apiv11"
    _UPDATE_FILES_BATCH = 50
    _UPDATE_RATE_LIMIT_COOLDOWN = 120.0

    def __init__(
        self,
        *,
        max_concurrency: int = 8,
        request_interval_ms: int = 200,
        proxy: str | None = None,
        user_agent: str | None = None,
        version: str = APP_VERSION,
        cache: GbApiCache | None = None,
    ) -> None:
        """创建异步 HTTP 连接和并发配额，配置请求间隔、代理、缓存及更新检查冷却状态。"""
        ua = user_agent or DEFAULT_USER_AGENT_TEMPLATE.format(ver=version)
        self._ua = ua
        self._proxy = proxy
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=6, read=30, write=30, pool=30),
            headers={"User-Agent": ua, "Accept": "application/json"},
            proxy=proxy,
            follow_redirects=True,
        )
        self._sem = asyncio.Semaphore(max_concurrency)
        self._interval = request_interval_ms / 1000.0
        self._request_start_lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._update_files_lock = asyncio.Lock()
        self._cache = cache or GbApiCache()
        self._deprecation_msg: str | None = None
        self._update_blocked_until = 0.0

    async def aclose(self) -> None:
        """异步关闭客户端拥有的 HTTP 连接池。"""
        await self._client.aclose()

    def invalidate_game_caches(self, game_id: int) -> int:
        """清除游戏分类及详情、更新缓存，返回清除数量。

        分类缓存按游戏编号精确失效；详情和更新缓存的键不含游戏编号，
        需清除相应前缀，再由后续请求按需获取。
        """
        n = self._cache.invalidate(f"cat:{game_id}")
        n += self._cache.invalidate_prefix("profile:")
        n += self._cache.invalidate_prefix("updates:")
        n += self._cache.invalidate_prefix("update_files:")
        return n

    def invalidate_mod_cache(self, model: GbModel, mod_id: int) -> int:
        """作废单个 Mod 的详情、更新记录和更新文件缓存。"""
        n = self._cache.invalidate(f"profile:{model.value}:{mod_id}")
        n += self._cache.invalidate(f"updates:{model.value}:{mod_id}")
        n += self._cache.invalidate(f"update_files:{model.value}:{mod_id}")
        return n

    @property
    def proxy(self) -> str | None:
        """返回创建请求连接时配置的代理地址，未设置时返回空值。"""
        return self._proxy

    async def update_proxy(self, proxy: str | None) -> None:
        """关闭现有 HTTP 客户端并按新代理配置重新创建。

        在客户端所属的事件循环调用。连接替换可能中断在途请求，调用方须处理资源关闭异常。
        """
        if proxy == self._proxy:
            return
        old = self._client
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=6, read=30, write=30, pool=30),
            headers={"User-Agent": self._ua, "Accept": "application/json"},
            proxy=proxy,
            follow_redirects=True,
        )
        self._proxy = proxy
        try:
            await old.aclose()
        except Exception:
            pass

    def _ensure_update_requests_allowed(self) -> None:
        """共享更新冷却期尚未结束时抛出限流异常，阻止继续发送更新查询。"""
        remaining = self._update_blocked_until - time.monotonic()
        if remaining > 0:
            raise UpdateCheckRateLimited(remaining)

    def _trip_update_rate_limit(self, response: httpx.Response) -> None:
        """根据 Retry-After 秒数或日期延长更新冷却期，并保留至少默认时长的限制。"""
        cooldown = self._UPDATE_RATE_LIMIT_COOLDOWN
        retry_after = response.headers.get("Retry-After", "")
        try:
            seconds = float(retry_after)
        except ValueError:
            try:
                date = parsedate_to_datetime(retry_after)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                seconds = date.timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                seconds = 0.0
        if math.isfinite(seconds):
            cooldown = max(cooldown, seconds)
        self._update_blocked_until = max(self._update_blocked_until, time.monotonic() + cooldown)
        logger.warning("GameBanana 请求被限流（429），更新检测静默 %.0f 秒", cooldown)

    async def _get(
        self,
        path: str,
        *,
        update_check: bool = False,
        **params: Any,
    ) -> Any:
        """执行带并发限额、节流与重试的 JSON 请求；代理连接故障时尝试直连，并记录接口弃用提示。"""
        url = f"{self.BASE}{path}"

        async def call(client: httpx.AsyncClient) -> httpx.Response:
            """在并发配额内安排请求开始时间，发送前复查更新冷却期，并记录新的限流响应。"""
            async with self._sem:
                if update_check:
                    self._ensure_update_requests_allowed()
                if self._interval > 0:
                    # 统一排队分配请求开始时间，避免独立休眠结束后同时发送请求。
                    async with self._request_start_lock:
                        delay = self._next_request_at - time.monotonic()
                        if delay > 0:
                            await asyncio.sleep(delay)
                        self._next_request_at = time.monotonic() + self._interval
                if update_check:
                    # 排队期间其他请求也可能收到 429，发送前再次检查共享静默期。
                    self._ensure_update_requests_allowed()
                resp = await client.get(url, params=params)
                if resp.status_code == 429:
                    # 更新检查可能等待商店发起的共享请求；共享请求收到限流响应后，
                    # 更新检查也须遵守静默期。
                    self._trip_update_rate_limit(resp)
                    if update_check:
                        self._ensure_update_requests_allowed()
                return resp

        def _retry_p(exc: BaseException) -> bool:
            """仅将限流、服务端错误、网络错误及超时判为可重试，其他异常直接传播。"""
            if isinstance(exc, httpx.HTTPStatusError):
                code = exc.response.status_code
                return code == 429 or code >= 500
            return isinstance(exc, (httpx.NetworkError, httpx.TimeoutException))

        async def _attempt(client: httpx.AsyncClient) -> httpx.Response:
            """对给定连接执行至多两次带退避的请求尝试，并将失败状态转换为 HTTP 异常。"""
            r: httpx.Response | None = None
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(2),
                wait=wait_exponential(multiplier=1, min=1, max=4),
                retry=retry_if_exception(_retry_p),
                reraise=True,
            ):
                with attempt:
                    r = await call(client)
                    r.raise_for_status()
            assert r is not None
            return r

        try:
            resp = await _attempt(self._client)
        except (httpx.NetworkError, httpx.TimeoutException) as e:
            # 代理连接失败或超时时尝试一次直连；未配置代理时传播原异常。
            if not self._proxy:
                raise
            logger.warning("GameBanana 经代理失败(%s)，降级直连重试", e)
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=6, read=30, write=30, pool=30),
                headers={"User-Agent": self._ua, "Accept": "application/json"},
                proxy=None,
                follow_redirects=True,
            ) as direct:
                resp = await _attempt(direct)

        for h in _DEPRECATION_HEADERS:
            if h in resp.headers:
                self._deprecation_msg = f"GameBanana API marked {h}: {resp.headers[h]}"
                logger.warning(self._deprecation_msg)
                break

        try:
            return resp.json()
        except ValueError as exc:
            raise httpx.HTTPError(f"Invalid JSON from {url}") from exc

    @staticmethod
    def _extract_records(payload: Any) -> list[dict]:
        """提取列表响应或记录包装对象中的条目，其余响应按空结果处理。"""
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            return payload.get("_aRecords") or []
        return []

    @staticmethod
    def _extract_has_more(payload: Any) -> bool:
        """优先读取服务端完成标记，否则按页码和记录总数判断是否还有下一页。"""
        if isinstance(payload, dict):
            meta = payload.get("_aMetadata") or {}
            if "_bIsComplete" in meta:
                return not bool(meta["_bIsComplete"])
            total = meta.get("_nRecordCount")
            per_page = meta.get("_nPerpage") or DEFAULT_PAGE_SIZE
            page = meta.get("_nPage") or 1
            if total is not None:
                return page * per_page < int(total)
        return False

    async def match_game(self, name: str) -> list[dict]:
        """通过 /Util/Game/NameMatch 按游戏名称查询 GameBanana 候选记录。

        返回包含 _idRow、_sName 和 _sProfileUrl 的原始记录列表，
        由 pick_gb_game_match 选择与用户自定义游戏匹配的记录。
        """
        name = (name or "").strip()
        if not name:
            return []
        payload = await self._get("/Util/Game/NameMatch", _sName=name)
        return self._extract_records(payload)

    # 根分类包含在游戏 ProfilePage 的 _aModRootCategories 字段中。
    # 服务端未提供独立的 Game/Categories 路由。

    async def categories(self, game_id: int) -> list[GbCategory]:
        """通过可持久化缓存获取指定游戏的根分类，并将有效记录转换为分类模型。"""
        key = f"cat:{game_id}"
        # 分类树缓存写入磁盘，重启后可在有效期内复用。
        payload = await self._cache.get_or_fetch(
            key,
            GbApiCache.CAT_TTL_SECONDS,
            lambda: self._get(f"/Game/{game_id}/ProfilePage"),
            persist=True,
        )
        records: list[dict] = []
        if isinstance(payload, dict):
            records = payload.get("_aModRootCategories") or []
        return [GbCategory.from_api(r) for r in records if isinstance(r, dict)]

    async def sub_categories(self, parent_id: int) -> list[GbCategory]:
        """读取 /apiv11/ModCategory/{id}/SubCategories 返回的子分类。

        响应包含名称、图标、URL 和内容数量，能够表示没有 Mod 的子分类。
        接口不提供 _idRow，分类编号从 _sUrl 的末段解析。
        """
        # 请求失败须进入选择器的错误状态；返回空列表会把失败缓存为正常结果。

        payload = await self._get(f"/ModCategory/{parent_id}/SubCategories")
        if not isinstance(payload, list):
            raise ValueError("Invalid category response: expected a list")
        out: list[GbCategory] = []
        for rec in payload:
            if not isinstance(rec, dict):
                continue
            url = rec.get("_sUrl") or rec.get("_sProfileUrl") or ""
            try:
                cid = int(url.rsplit("/", 1)[-1])
            except (ValueError, IndexError):
                continue
            out.append(
                GbCategory(
                    id=cid,
                    name=rec.get("_sName", "") or f"#{cid}",
                    parent_id=parent_id,
                    item_count=int(rec.get("_nItemCount") or 0),
                    icon_url=rec.get("_sIconUrl") or None,
                )
            )
        # 按名称排序，便于在较长的分类列表中定位。
        return sorted(out, key=lambda c: c.name.lower())

    async def mods_by_category(
        self,
        cat_id: int,
        page: int = 1,
        *,
        sort: str = "best_match",
    ) -> tuple[list[GbModRecord], bool]:
        """按分类、页码和受支持排序查询 Mod，返回记录列表及后续分页标记。"""
        params: dict[str, Any] = {
            "_aFilters[Generic_Category]": cat_id,
            "_nPage": page,
            "_nPerpage": DEFAULT_PAGE_SIZE,
        }
        sort_param = _SORT_ALIASES.get(sort)
        if sort_param:
            params["_sSort"] = sort_param
        payload = await self._get("/Mod/Index", **params)
        records = self._extract_records(payload)
        return [GbModRecord.from_api(r) for r in records], self._extract_has_more(payload)

    async def get_profile(self, model: GbModel, mod_id: int) -> GbModProfile:
        """按资源类型与编号获取详情，使用可持久化缓存复用有效期内的响应。"""
        key = f"profile:{model.value}:{mod_id}"
        # 详情缓存写入磁盘，重启后在有效期内无需重复请求。
        payload = await self._cache.get_or_fetch(
            key,
            GbApiCache.MEM_TTL_DEFAULT,
            lambda: self._get(f"/{model.value}/{mod_id}/ProfilePage"),
            persist=True,
        )
        return GbModProfile.from_api(payload)

    @staticmethod
    def _active_update_files(payload: Any) -> list[GbModFile] | None:
        """Multi 包含归档文件；缺项/畸形数据与确实没有活动文件必须区分。"""
        if not isinstance(payload, list):
            return None
        files = []
        for row in payload:
            if not isinstance(row, dict):
                return None
            archived = row.get("_bIsArchived", False)
            if not isinstance(archived, bool):
                return None
            if archived:
                continue
            if _positive_int(row.get("_idRow")) is None or _positive_int(row.get("_tsDateAdded")) is None:
                return None
            if any(
                not isinstance(row.get(key), str) or not row[key].strip()
                for key in ("_sFile", "_sDownloadUrl", "_sMd5Checksum")
            ):
                return None
            files.append(GbModFile.from_api(row))
        return files

    async def bulk_update_files(self, mod_ids: list[int], *, force: bool = False) -> dict[int, list[GbModFile]]:
        """批量读取活动文件，供手动和自动更新检查共用。

        Multi 响应的 _aFiles 包含文件指纹和归档标志；页面更新时间可能早于文件时间，
        因此按文件信息判断更新。自动检查缓存十分钟，force=True 时重新请求。
        批次失败后停止并携带已完成结果；仅对成功批次中的缺失记录进行补充查询。
        """
        ids = list(dict.fromkeys(mid for value in mod_ids if (mid := _positive_int(value)) is not None))
        if not ids:
            return {}
        # 同一 client 的同时自动检测共享缓存结果，避免相同 ID 在缓存填入前重复发批次。
        async with self._update_files_lock:
            self._ensure_update_requests_allowed()
            out: dict[int, list[GbModFile]] = {}
            missing = []
            for mid in ids:
                key = f"update_files:Mod:{mid}"
                if force:
                    self._cache.invalidate(key)
                cached = self._cache.peek(key)
                if cached is None:
                    missing.append(mid)
                else:
                    out[mid] = list(cached)

            for offset in range(0, len(missing), self._UPDATE_FILES_BATCH):
                batch = missing[offset : offset + self._UPDATE_FILES_BATCH]
                try:
                    payload = await self._get(
                        "/Mod/Multi",
                        update_check=True,
                        _csvRowIds=",".join(str(mid) for mid in batch),
                        _csvProperties="_idRow,_aFiles",
                    )
                    rows = payload.get("_aRecords") if isinstance(payload, dict) else payload
                    if not isinstance(rows, list):
                        raise ValueError(tr("GameBanana 未返回有效的批量文件列表"))
                    batch_ids = set(batch)
                    recognized_count = 0
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        mid = _positive_int(row.get("_idRow"))
                        if mid not in batch_ids:
                            continue
                        recognized_count += 1
                        files = self._active_update_files(row.get("_aFiles"))
                        if files is None:
                            continue
                        out[mid] = files
                        self._cache.put(f"update_files:Mod:{mid}", GbApiCache.MEM_TTL_DEFAULT, files)
                    # 合法记录的文件字段可能全为 null（尤其其余项已命中缓存时）。
                    # 这种缺项仍能有界补查，不能与损坏/不匹配的整批响应混为一谈。
                    if not recognized_count:
                        raise ValueError(tr("GameBanana 批次中没有可识别的 Mod 记录"))
                except UpdateCheckError as exc:
                    exc.partial_files = out
                    raise
                except Exception as exc:
                    raise UpdateCheckError(
                        tr("GameBanana 批量文件查询失败：{exc}", exc=exc), partial_files=out
                    ) from exc
            return out

    async def get_download_page(
        self,
        model: GbModel,
        mod_id: int,
        *,
        update_check: bool = False,
    ) -> list[GbModFile]:
        """读取资源文件列表；用于更新检查时严格验证活动文件证据，避免无效响应被当作已是最新。"""
        payload = await self._get(f"/{model.value}/{mod_id}/DownloadPage", update_check=update_check)
        files = payload.get("_aFiles") if isinstance(payload, dict) else None
        if not files and isinstance(payload, list):
            files = payload
        if update_check:
            parsed = self._active_update_files(files)
            if parsed is None:
                raise ValueError(tr("GameBanana 未返回有效的活动文件列表"))
            return parsed
        return [GbModFile.from_api(f) for f in (files or [])]

    async def get_updates(
        self,
        model: GbModel,
        mod_id: int,
        *,
        update_check: bool = False,
        force: bool = False,
    ) -> list[GbModUpdateEntry]:
        """获取并缓存资源更新记录，强制刷新时先使该资源的缓存失效。"""
        # 更新记录使用持久化缓存，在有效期内复用，减少重复请求。
        key = f"updates:{model.value}:{mod_id}"
        if force:
            self._cache.invalidate(key)
        payload = await self._cache.get_or_fetch(
            key,
            GbApiCache.MEM_TTL_DEFAULT,
            lambda: self._get(f"/{model.value}/{mod_id}/Updates", update_check=update_check),
            persist=True,
        )
        records = self._extract_records(payload)
        return [GbModUpdateEntry.from_api(r) for r in records]

    async def search(
        self,
        query: str,
        *,
        game_id: int,
        page: int = 1,
        model: GbModel | None = None,
        sort: str = "best_match",
    ) -> tuple[list[GbModRecord], bool]:
        """在指定游戏和资源类型内分页搜索，返回解析后的记录及下一页可用状态。"""
        params: dict[str, Any] = {
            "_sSearchString": query,
            "_idGameRow": game_id,
            "_nPage": page,
            "_nPerpage": DEFAULT_PAGE_SIZE,
            "_sModelName": (model.value if model is not None else GbModel.MOD.value),
        }
        sort_param = _SORT_ALIASES.get(sort)
        if sort_param:
            params["_sSort"] = sort_param
        payload = await self._get("/Util/Search/Results", **params)
        records = self._extract_records(payload)
        return [GbModRecord.from_api(r) for r in records], self._extract_has_more(payload)

    async def subfeed(
        self,
        game_id: int,
        page: int = 1,
        *,
        sort: str = "default",
    ) -> tuple[list[GbModRecord], bool]:
        """分页读取游戏订阅动态，默认沿用服务端排序，仅传递受支持的自定义排序。"""
        params: dict[str, Any] = {
            "_nPage": page,
            "_nPerpage": DEFAULT_PAGE_SIZE,
        }
        # Subfeed 排序由服务端决定，默认排序不传 _sSort。

        if sort and sort != "default":
            mapped = _SORT_ALIASES.get(sort)
            if mapped:
                params["_sSort"] = mapped
        payload = await self._get(f"/Game/{game_id}/Subfeed", **params)
        records = self._extract_records(payload)
        return [GbModRecord.from_api(r) for r in records], self._extract_has_more(payload)

    # 下载次数
    # apiv11 列表接口不返回 _nDownloadCount，须通过独立请求补全。

    # 通过 Mod/Multi 批量获取下载量，减少逐项请求及限流开销。
    _DL_CACHE_TTL = 3600
    # 每批最多 50 个编号，限制 URL 长度及单批失败的影响范围。
    _DL_BATCH = 50
    # 收到限流响应后暂停下载量请求，避免非关键统计持续触发重试。
    _DL_RATE_LIMIT_COOLDOWN = 120.0

    def _dl_rate_limited(self) -> bool:
        """判断下载量查询是否仍处于独立的限流冷却期。"""
        return time.time() < getattr(self, "_dl_blocked_until", 0.0)

    def _trip_dl_rate_limit(self) -> None:
        """启动下载量查询冷却期，使界面在限流期间保留占位显示。"""
        self._dl_blocked_until = time.time() + self._DL_RATE_LIMIT_COOLDOWN
        logger.warning(
            "下载量接口被限流（429），静默 %.0f 秒；期间卡片下载量显示占位",
            self._DL_RATE_LIMIT_COOLDOWN,
        )

    async def _fetch_download_counts_batch(self, mod_ids: list[int]) -> dict[int, int]:
        """一次请求取多个 Mod 的下载量。返回 {mod_id: count}，整批失败则抛。"""
        payload = await self._get(
            "/Mod/Multi",
            _csvRowIds=",".join(str(m) for m in mod_ids),
            _csvProperties="_idRow,_nDownloadCount",
        )
        out: dict[int, int] = {}
        rows = payload if isinstance(payload, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                mid = int(row.get("_idRow") or 0)
            except (TypeError, ValueError):
                continue
            if mid <= 0:
                continue
            try:
                out[mid] = int(row.get("_nDownloadCount") or 0)
            except (TypeError, ValueError):
                out[mid] = 0
        return out

    async def bulk_download_counts(
        self,
        mod_ids: list[int],
        *,
        timeout: float | None = None,
    ) -> dict[int, int]:
        """批量读取下载量，返回成功获取的 Mod 编号与计数。

        使用 Mod/Multi 按批请求，结果按 dl:{mod_id} 分别缓存一小时，供翻页和重新筛选复用。
        缺失项不填零，由调用方保持待加载状态；超过总体时限时返回已完成的部分结果。
        """
        if not mod_ids:
            return {}

        uniq: list[int] = []
        seen: set[int] = set()
        for m in mod_ids:
            mid = int(m or 0)
            if mid > 0 and mid not in seen:
                seen.add(mid)
                uniq.append(mid)

        # 缓存命中项直接返回，其余项组成网络请求批次。
        out: dict[int, int] = {}
        misses: list[int] = []
        for mid in uniq:
            cached = self._cache.peek(f"dl:{mid}")
            if cached is None:
                misses.append(mid)
            else:
                try:
                    out[mid] = int(cached)
                except (TypeError, ValueError):
                    misses.append(mid)
        if not misses:
            return out
        # 限流静默期内不发送请求，缺失项保持待加载状态。
        if self._dl_rate_limited():
            logger.debug("下载量接口处于限流静默期，跳过 %d 个未缓存项", len(misses))
            return out

        batches = [misses[i : i + self._DL_BATCH] for i in range(0, len(misses), self._DL_BATCH)]

        async def _run_batch(batch: list[int]) -> dict[int, int]:
            """获取一批 Mod 下载量并逐项缓存；失败返回空结果，收到限流响应时启动冷却。"""
            try:
                got = await self._fetch_download_counts_batch(batch)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    self._trip_dl_rate_limit()
                else:
                    logger.debug("批量下载量失败（HTTP %s）", e.response.status_code)
                return {}
            except Exception as e:
                logger.debug("批量下载量失败：%s", e)
                return {}
            # 按 Mod 编号分别缓存结果，供后续页面复用。
            for mid, cnt in got.items():
                self._cache.put(f"dl:{mid}", self._DL_CACHE_TTL, cnt)
            return got

        tasks = [asyncio.ensure_future(_run_batch(b)) for b in batches]
        if timeout is None:
            for got in await asyncio.gather(*tasks, return_exceptions=True):
                if isinstance(got, dict):
                    out.update(got)
            return out

        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for t in done:
            try:
                got = t.result()
            except Exception:
                continue
            if isinstance(got, dict):
                out.update(got)
        return out
