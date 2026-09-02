"""
HLS Stream Redirect Proxy
==========================
طبقة وسيطة (Middleware Layer) تقوم بجلب صفحة HTML من خادم البث الداخلي
(Self-hosted CDN Node)، استخراج رابط HLS (.m3u8) المحدّث بالتوكن الحيوي منها،
وإرجاع إعادة توجيه HTTP 302 إلى ذلك الرابط للمشغلات القديمة التي لا تدعم
Token Refresh API مباشرة.

تشغيل محلي:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager

import httpx
from cachetools import TTLCache
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# الإعدادات (Configuration)
# --------------------------------------------------------------------------

class Settings:
    # عنوان خادم البث الداخلي الذي يولّد صفحات HTML الحاوية على روابط HLS
    UPSTREAM_BASE_URL: str = "https://internal-media-backend.example.local"

    # مهلة الاتصال بالخادم الداخلي (بالثواني)
    UPSTREAM_TIMEOUT: float = 8.0

    # مدة صلاحية الكاش (بالثواني) — يجب أن تكون أقل من عمر التوكن الفعلي
    CACHE_TTL_SECONDS: int = 45

    # الحد الأقصى لعدد العناصر المخزّنة في الكاش في آن واحد
    CACHE_MAX_SIZE: int = 512

    # الترويسات المرسلة عند الطلب من الخادم الداخلي
    USER_AGENT: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )


settings = Settings()

# --------------------------------------------------------------------------
# التسجيل (Logging)
# --------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("hls_proxy")

# --------------------------------------------------------------------------
# الكاش المشترك (TTL Cache) + عميل HTTP غير المتزامن
# --------------------------------------------------------------------------

# key: channel_id -> value: الرابط النهائي المستخرج
stream_cache: TTLCache[str, str] = TTLCache(
    maxsize=settings.CACHE_MAX_SIZE, ttl=settings.CACHE_TTL_SECONDS
)

http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """إدارة دورة حياة عميل httpx: إنشاء عند الإقلاع، وإغلاق عند الإيقاف."""
    global http_client
    http_client = httpx.AsyncClient(
        timeout=settings.UPSTREAM_TIMEOUT,
        headers={
            "User-Agent": settings.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        },
        follow_redirects=True,
    )
    logger.info("تم تهيئة عميل httpx وبدء تشغيل الخدمة.")
    yield
    await http_client.aclose()
    logger.info("تم إغلاق عميل httpx وإيقاف الخدمة.")


app = FastAPI(
    title="HLS Stream Redirect Proxy",
    description="طبقة وسيطة لإنعاش توكنز HLS الحيوية وإعادة توجيه المشغلات القديمة.",
    version="1.0.0",
    lifespan=lifespan,
)

# --------------------------------------------------------------------------
# نماذج الاستجابة (Response Models)
# --------------------------------------------------------------------------

class ErrorResponse(BaseModel):
    detail: str = Field(..., description="وصف الخطأ")
    channel_id: str | None = None


# --------------------------------------------------------------------------
# دالة استخراج رابط HLS عبر Regex
# --------------------------------------------------------------------------

# يلتقط أي رابط ينتهي بـ .m3u8 (مع أو بدون Query String للتوكن)، سواء كان
# داخل علامتي اقتباس HTML أو ضمن كتلة JavaScript/JSON.
M3U8_PATTERN = re.compile(
    r"""https?://[^\s'"<>\\]+?\.m3u8(?:\?[^\s'"<>\\]*)?""",
    re.IGNORECASE,
)


def extract_stream_url(html_content: str) -> str | None:
    """
    يبحث في نص HTML/JS المرجَع عن أول رابط .m3u8 صالح.

    :param html_content: النص الخام للاستجابة القادمة من الخادم الداخلي.
    :return: الرابط المستخرج أو None في حال عدم العثور على تطابق.
    """
    match = M3U8_PATTERN.search(html_content)
    if not match:
        return None

    url = match.group(0)
    # إزالة أي فواصل زائدة قد تلتصق بنهاية الرابط عن طريق الخطأ
    url = url.rstrip(").,;")
    return url


# --------------------------------------------------------------------------
# منطق الجلب والتخزين المؤقت
# --------------------------------------------------------------------------

async def fetch_and_resolve_stream(channel_id: str) -> str:
    """
    يعيد الرابط من الكاش إن وُجد، وإلا يجلب صفحة القناة من الخادم الداخلي
    ويستخرج رابط HLS منها ثم يخزّنه مؤقتًا.

    :raises HTTPException: 404 إن لم يُعثر على رابط، 502 عند فشل الاتصال بالمصدر.
    """
    cached_url = stream_cache.get(channel_id)
    if cached_url:
        logger.info("Cache hit لقناة %s", channel_id)
        return cached_url

    logger.info("Cache miss لقناة %s — جاري الجلب من المصدر الداخلي.", channel_id)

    upstream_url = f"{settings.UPSTREAM_BASE_URL}/channel/{channel_id}"

    assert http_client is not None, "لم يتم تهيئة عميل httpx بعد."

    try:
        response = await http_client.get(upstream_url)
        response.raise_for_status()
    except httpx.TimeoutException as exc:
        logger.error("انتهت مهلة الاتصال بالمصدر الداخلي لقناة %s: %s", channel_id, exc)
        raise HTTPException(
            status_code=502, detail="انتهت مهلة الاتصال بخادم البث الداخلي."
        ) from exc
    except httpx.HTTPStatusError as exc:
        logger.error(
            "استجابة خطأ من المصدر الداخلي لقناة %s: %s", channel_id, exc.response.status_code
        )
        raise HTTPException(
            status_code=502,
            detail=f"خادم البث الداخلي أعاد الحالة {exc.response.status_code}.",
        ) from exc
    except httpx.RequestError as exc:
        logger.error("فشل الاتصال بالمصدر الداخلي لقناة %s: %s", channel_id, exc)
        raise HTTPException(
            status_code=502, detail="تعذّر الاتصال بخادم البث الداخلي."
        ) from exc

    resolved_url = extract_stream_url(response.text)

    if not resolved_url:
        logger.warning("لم يُعثر على رابط HLS صالح لقناة %s.", channel_id)
        raise HTTPException(
            status_code=404, detail="لم يتم العثور على رابط بث فعّال لهذه القناة."
        )

    stream_cache[channel_id] = resolved_url
    logger.info("تم استخراج وتخزين رابط جديد لقناة %s.", channel_id)
    return resolved_url


# --------------------------------------------------------------------------
# نقاط النهاية (Endpoints)
# --------------------------------------------------------------------------

@app.get("/health", tags=["monitoring"])
async def health_check() -> dict:
    """فحص صحة الخدمة — مفيد لأدوات المراقبة و Docker healthcheck."""
    return {"status": "ok", "cached_channels": len(stream_cache)}


@app.get(
    "/stream/{channel_id}",
    responses={404: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
    tags=["stream"],
)
async def redirect_to_stream(channel_id: str, request: Request) -> RedirectResponse:
    """
    يستقبل معرّف القناة، يحل رابط الـ HLS الحالي (من الكاش أو بالجلب الفعلي)،
    ثم يرجع HTTP 302 Redirect إلى ذلك الرابط ليتبعه المشغل مباشرة.
    """
    logger.info("طلب بث وارد لقناة %s من %s", channel_id, request.client.host if request.client else "unknown")

    resolved_url = await fetch_and_resolve_stream(channel_id)
    return RedirectResponse(url=resolved_url, status_code=302)


# --------------------------------------------------------------------------
# معالج استثناءات موحّد لإرجاع JSON منسّق دائمًا
# --------------------------------------------------------------------------

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    channel_id = request.path_params.get("channel_id")
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(detail=exc.detail, channel_id=channel_id).model_dump(),
    )
