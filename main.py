import asyncio
import logging
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
import secrets

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.conversions import router
from app.config import Settings
from app.exceptions import ConversionError, conversion_error_handler, error_body
from app.middleware import LocalRequestMiddleware
from app.logging_config import configure_logging
from app.services.conversion_service import ConversionService

logger = logging.getLogger(__name__)

def create_app(settings: Settings | None = None, converter=None):
    configure_logging()
    explicit_settings = settings is not None
    settings = settings or Settings.from_env()
    service = ConversionService(settings, converter)
    if explicit_settings:
        service.prepare_storage()

    @asynccontextmanager
    async def lifespan(application):
        logger.info("server startup log_level=DEBUG max_file_bytes=%d max_body_bytes=%d timeout_seconds=%d concurrency=%d converter_url=%s",
                    settings.max_file_bytes, settings.max_file_bytes + settings.multipart_overhead_bytes,
                    settings.conversion_timeout_seconds, settings.max_concurrent_conversions, settings.converter_url)
        logger.debug("server event_loop=%s", type(asyncio.get_running_loop()).__name__)
        service.prepare_storage()
        # Starlette's multipart spool must use the same private local storage.
        original_tempdir = tempfile.tempdir
        tempfile.tempdir = str(settings.work_root)
        service.cleanup_stale()

        async def sweep():
            while True:
                await asyncio.sleep(60)
                service.cleanup_stale()

        sweeper = asyncio.create_task(sweep())
        try:
            yield
        finally:
            logger.info("server shutdown started active_conversions=%d", service.active)
            tempfile.tempdir = original_tempdir
            sweeper.cancel()
            try:
                await sweeper
            except asyncio.CancelledError:
                pass
            logger.info("server shutdown completed")

    # Serve vendored Swagger assets rather than the default CDN.
    application = FastAPI(
        title="Mobidrill 로컬 문서 PDF 변환 API",
        version="1.0.0",
        description=("같은 장비에서 업로드한 문서를 외부 통신 없이 PDF로 변환합니다. "
                     "PPT, PPTX, DOCX, PDF 및 구형 HWP를 처리하며 HWP 5.x/HWPX는 지원하지 않습니다. "
                     f"입력 제한: {settings.max_file_bytes} bytes, 변환 제한: "
                     f"{settings.conversion_timeout_seconds}초, 동시 처리: {settings.max_concurrent_conversions}개, "
                     "변환은 상시 실행되는 로컬 LibreOffice 서버에 요청합니다."),
        openapi_tags=[{"name": "conversions", "description": "로컬 문서 업로드 및 PDF 다운로드"}],
        lifespan=lifespan, docs_url=None, redoc_url=None,
    )
    application.mount("/static/swagger-ui",
                      StaticFiles(directory=Path(__file__).parent / "app" / "static" / "swagger-ui"),
                      name="swagger-assets")
    application.state.conversion_service = service
    application.add_middleware(LocalRequestMiddleware,
                               max_body_bytes=settings.max_file_bytes + settings.multipart_overhead_bytes,
                               spool_directory=str(settings.work_root))
    application.add_exception_handler(ConversionError, conversion_error_handler)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc):
        logger.warning("request validation rejected code=INVALID_REQUEST status=422")
        return JSONResponse(error_body("INVALID_REQUEST", "A multipart file field named 'file' is required.",
                                       request.state.request_id), status_code=422)

    application.include_router(router)
    application.add_api_route("/", root, methods=["GET"])
    application.add_api_route("/hello/{name}", say_hello, methods=["GET"])

    @application.get("/swagger-ui", response_class=HTMLResponse, include_in_schema=False)
    async def swagger_ui(request: Request):
        root_path = request.scope.get("root_path", "").rstrip("/")
        html = get_swagger_ui_html(
            openapi_url=root_path + application.openapi_url,
            title=application.title + " - Swagger UI",
            swagger_js_url=root_path + "/static/swagger-ui/swagger-ui-bundle.js",
            swagger_css_url=root_path + "/static/swagger-ui/swagger-ui.css",
            swagger_favicon_url=root_path + "/static/swagger-ui/favicon.svg",
            swagger_ui_parameters={"validatorUrl": "none", "queryConfigEnabled": False,
                                   "persistAuthorization": False, "displayRequestDuration": True},
        )
        nonce = secrets.token_urlsafe(24)
        content = html.body.decode("utf-8").replace("<script>", f'<script nonce="{nonce}">')
        # Also block browser-side requests to external validators, images, and
        # any remote server supplied through Swagger configuration.
        return HTMLResponse(content, headers={
            "Content-Security-Policy": (
                "default-src 'none'; "
                f"script-src 'self' 'nonce-{nonce}'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                "font-src 'self' data:; connect-src 'self'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        })

    @application.get("/docs", response_class=HTMLResponse, include_in_schema=False)
    async def local_docs():
        return '''<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>로컬 PDF 변환</title><h1>로컬 PDF 변환 API</h1>
<p>PPT, PPTX, DOCX, PDF 및 지원되는 구형 HWP 파일 1개를 선택하세요.</p>
<form action="/api/v1/conversions/pdf" method="post" enctype="multipart/form-data">
<label>문서 <input type="file" name="file" accept=".ppt,.pptx,.docx,.pdf,.hwp" required></label>
<button type="submit">PDF 변환 및 다운로드</button></form>
<p>HWP 5.x 및 HWPX는 지원하지 않습니다.</p>
<p><a href="/swagger-ui">Swagger UI 문서 및 API 실행</a></p>
<p><a href="/openapi.json">OpenAPI 명세</a></p></html>'''
    return application


async def root():
    return {"message": "Hello World"}


async def say_hello(name: str):
    return {"message": f"Hello {name}"}


app = create_app()
