# 로컬 문서 PDF 변환 API

FastAPI는 **호스트에서 실행**하고, LibreOffice 변환 서버만 **상시 Docker 컨테이너**로 실행합니다.

```text
Swagger / API 호출자
  → 로컬 FastAPI (127.0.0.1:8000)
    → 로컬 HTTP 요청 (127.0.0.1:8001)
      → 상시 LibreOffice 컨테이너
    ← PDF 응답
  ← PDF 다운로드
```

FastAPI에는 Docker 생성·실행·삭제 코드가 없습니다. Docker CLI, Docker 소켓, Windows 하위 프로세스 이벤트 루프에 의존하지 않습니다. 컨테이너 시작과 종료는 운영자가 Compose로 관리합니다.

## 실행

Python 3.12, Docker Desktop의 Linux 컨테이너 모드가 필요합니다. 이미지는 준비 환경에서 처음 한 번 빌드합니다.

```powershell
docker compose build libreoffice
docker compose up -d --no-build --pull never
docker compose ps

# 호스트 Python 환경에 설치하고 호스트에서 FastAPI 실행
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\deploy\run-local.ps1
```

Linux에서는 `sh deploy/run-local.sh`로 FastAPI를 실행합니다. 직접 실행할 수도 있습니다.

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

- Swagger: http://127.0.0.1:8000/swagger-ui
- 변환 API: `POST /api/v1/conversions/pdf`, multipart `file`
- 변환 서버 상태: http://127.0.0.1:8001/health
- 변환 서버 로그: `docker compose logs -f libreoffice`
- 변환 서버 종료: `docker compose down`

Swagger 리소스는 `app/static/swagger-ui/`에서 제공하므로 인터넷이 필요 없습니다. API는 PDF 응답과 `X-Job-ID` 헤더를 반환합니다. `/docs`는 간단한 업로드 폼, `/openapi.json`은 명세입니다.

## 지원 파일과 저장

| 입력 | 처리 |
|---|---|
| PPT, PPTX | LibreOffice Impress PDF 내보내기 |
| DOCX | LibreOffice Writer PDF 내보내기 |
| PDF | 호스트에서 검증 후 바이트 그대로 반환 |
| 구형 HWP 3.x / Hangul WP 97 | 해당 식별 헤더만 허용. 실제 변환 호환성 검증 필요 |

HWP 5.x, HWPX, 암호화 문서는 지원하지 않습니다. PPT 애니메이션은 정적인 PDF로 변환됩니다. 외부 링크의 이미지·데이터는 가져오지 않습니다.

```text
resources/static/<UUID>/
  0/input.<확장자>
  1/input.pdf
  manifest.json
```

원본과 결과는 호스트에 영구 보관합니다. 저장 경로를 웹 정적 파일로 공개하지 않습니다. 유효한 원본은 변환 실패·취소 후에도 보관하고, 잘못된 입력은 제거합니다. 후속 파이프라인은 같은 UUID의 `2/`, `3/` 등으로 확장할 수 있습니다. 컨테이너는 문서 저장 폴더나 Docker 소켓을 마운트하지 않습니다. 전달받은 문서는 요청별 임시 폴더에만 저장하고 응답·오류 후 삭제합니다.

## 설정

호스트 FastAPI는 **프로세스 환경변수**에서 설정을 읽습니다. `.env.example`은 참고용이며 자동 로드되지 않습니다. Compose는 자신의 환경변수 또는 `.env`에서 컨테이너 설정을 읽으므로 변경 시 양쪽에 동일한 제한을 적용하세요.

| 환경변수 | 기본값 |
|---|---|
| CONVERTER_URL | http://127.0.0.1:8001 |
| MAX_FILE_BYTES | 104857600 (100 MiB) |
| MAX_OUTPUT_BYTES | 104857600 (100 MiB) |
| CONVERSION_TIMEOUT_SECONDS | 60 |
| MAX_CONCURRENT_CONVERSIONS | 2 |
| PIPELINE_STORAGE_ROOT | 프로젝트 resources/static |
| CONVERSION_WORK_ROOT | 운영 계정의 로컬 임시 작업 디렉터리 |
| STALE_JOB_SECONDS | 3600 |

변환 서버 URL은 숫자로 표시한 루프백 IP와 포트만 허용하며, 프록시 환경변수와 HTTP 리다이렉트를 사용하지 않습니다. 포트 변경 시 Compose의 호스트 포트와 `CONVERTER_URL`을 함께 변경하세요. 컨테이너 총 메모리는 Compose의 `mem_limit: 3g`, CPU는 2, 임시 파일 tmpfs는 1 GiB입니다. API worker는 1개로 실행합니다.

## 오프라인 배포와 원본 보호

초기 이미지 빌드와 패키지 다운로드만 인터넷이 가능한 준비 장비에서 수행합니다. 빌드 컨텍스트는 Dockerfile과 변환 서버 소스만 허용하므로 원본 문서는 이미지에 포함되지 않습니다.

```powershell
# 준비 장비
docker compose build libreoffice
docker save -o libreoffice.image.tar mobidrill-libreoffice:local
.\.venv\Scripts\python.exe -m pip download -r requirements.lock.txt --dest wheelhouse
Get-FileHash libreoffice.image.tar -Algorithm SHA256

# 오프라인 장비: 반입 파일의 해시를 먼저 확인
docker load -i libreoffice.image.tar
.\.venv\Scripts\python.exe -m pip install --no-index --find-links=wheelhouse -r requirements.lock.txt
docker compose up -d --no-build --pull never
.\deploy\run-local.ps1
```

Compose의 수신 포트는 호스트 `127.0.0.1`에만 게시합니다. HTTP 변환 서버는 수신 소켓을 준비한 직후 Linux seccomp 필터를 적용해 새로운 IPv4·IPv6 소켓 생성을 차단합니다. 이미 생성한 수신 소켓의 요청 수락과 응답은 가능하며, 이후의 작업 스레드와 LibreOffice 자식 프로세스는 동일한 제한을 상속합니다. Unix 소켓만 허용하고, 필터가 실제로 네트워크 소켓을 차단하는지 시작 시 검증합니다. 적용할 수 없으면 서버가 시작되지 않습니다. `/health`의 `network_blocked: true`로 검증 상태를 확인할 수 있습니다. 별도 운영자 `docker exec` 명령은 이 프로세스 필터를 상속하지 않습니다. 프로세스는 비관리자 계정이며, 매크로 실행을 차단하고 작업별 사용자 프로필을 사용합니다.

FastAPI와 변환 서버는 DEBUG 로그를 출력하되 파일명·문서 본문·파서 원시 출력·URL 파라미터는 기록하지 않습니다. 원본·결과·임시 폴더는 로컬 디스크에 두고 클라우드 동기화·외부 백업·원격 로그 수집에서 제외하세요. 영구 자료의 보관 용량과 삭제 정책은 운영자가 관리합니다.

호스트 요청이 취소되면 HTTP 연결은 닫히고 호스트 임시 파일은 제거됩니다. 컨테이너 작업은 응답 실패 또는 최대 변환 제한 시간까지 종료된 뒤 임시 파일과 처리 슬롯을 정리합니다. 프로세스 타임아웃 시 LibreOffice 프로세스 그룹 전체를 종료합니다.

## 검증

```powershell
.\.venv\Scripts\python.exe -m pytest -q
# Compose 변환 서버를 실행한 뒤 실제 변환·네트워크 차단 검증
$env:RUN_OFFLINE_INTEGRATION = '1'
.\.venv\Scripts\python.exe -m pytest -q
```

실제 파일을 로컬에서 검증하려면 `python tools/verify_local_file.py <파일 경로>`를 실행합니다. 상태·작업 UUID·PDF 크기·페이지 수만 출력합니다.
