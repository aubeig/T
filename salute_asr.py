"""
SaluteSpeech ASR (распознавание речи, GigaAM/Sber Salute) — расшифровка
голосовых сообщений и видеокружков, отправленных через спонсорские ссылки.

Настройка (переменные окружения):
  SALUTE_CLIENT_SECRET — client_secret приложения SaluteSpeech (обязательно,
                         без него модуль считается выключенным).
  SALUTE_CLIENT_ID     — client_id (по умолчанию совпадает с client_secret,
                         если не задан).
  SALUTE_SCOPE         — scope токена (по умолчанию SALUTE_SPEECH_PERS).
  SALUTE_AUTH_URL      — OAuth-эндпоинт (по умолчанию ngw.devices.sberbank.ru).
  SALUTE_RECOGNIZE_URL — эндпоинт распознавания (по умолчанию smartspeech.sber.ru).

Для видеокружков (mp4 без отдельной аудиодорожки) нужен ffmpeg. Берём
первый найденный: 1) системный (apt: `ffmpeg` в PATH), 2) бинарник из
pip-пакета imageio-ffmpeg (перечислен в requirements.txt — ставится
автоматически, там, где системные пакеты ставить нельзя). Если ffmpeg
нет совсем, кружок просто не расшифровывается (отправляется как есть),
ничего не падает.
"""
import asyncio
import base64
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid

import httpx

CLIENT_SECRET = os.environ.get("SALUTE_CLIENT_SECRET")
CLIENT_ID = os.environ.get("SALUTE_CLIENT_ID") or CLIENT_SECRET
SCOPE = os.environ.get("SALUTE_SCOPE", "SALUTE_SPEECH_PERS")
AUTH_URL = os.environ.get("SALUTE_AUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth")
RECOGNIZE_URL = os.environ.get("SALUTE_RECOGNIZE_URL", "https://smartspeech.sber.ru/rest/v1/speech:recognize")
TASK_URL = os.environ.get("SALUTE_TASK_URL", "https://smartspeech.sber.ru/rest/v1/task:get")

_token = None
_token_expires = 0.0

# ── Поиск ffmpeg ────────────────────────────────────────────────────
_FFMPEG_PATH = None
_FFMPEG_RESOLVED = False


def _ffmpeg() -> str | None:
    """
    Путь к ffmpeg (кэшируется на весь процесс): сначала системный бинарник
    (PATH), потом бинарник из pip-пакета imageio-ffmpeg. None — нет нигде.
    """
    global _FFMPEG_PATH, _FFMPEG_RESOLVED
    if not _FFMPEG_RESOLVED:
        _FFMPEG_RESOLVED = True
        _FFMPEG_PATH = shutil.which("ffmpeg")
        if not _FFMPEG_PATH:
            try:
                import imageio_ffmpeg
                _FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                _FFMPEG_PATH = None
        if _FFMPEG_PATH:
            logging.info(f"ffmpeg для расшифровки кружков: {_FFMPEG_PATH}")
        else:
            logging.warning(
                "ffmpeg не найден (ни в системе, ни в imageio-ffmpeg) — "
                "видеокружки уйдут без расшифровки"
            )
    return _FFMPEG_PATH


def enabled() -> bool:
    """True, если настроен client_secret и расшифровка вообще возможна."""
    return bool(CLIENT_SECRET)


async def _obtain_token() -> str | None:
    """Получает/обновляет OAuth-токен SaluteSpeech (кэшируется)."""
    global _token, _token_expires
    if not CLIENT_SECRET:
        return None
    if _token and time.time() < _token_expires - 60:
        return _token
    auth = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "RqUID": str(uuid.uuid4()),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(AUTH_URL, data={"scope": SCOPE}, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    _token = data.get("access_token")
    expires_at = data.get("expires_at")
    if isinstance(expires_at, (int, float)):
        # Salute возвращает expires_at в миллисекундах от эпохи.
        if expires_at > 10 ** 12:
            expires_at /= 1000.0
        _token_expires = float(expires_at)
    else:
        _token_expires = time.time() + 1800
    return _token


async def _recognize(data: bytes, filename: str, content_type: str) -> str | None:
    token = await _obtain_token()
    if not token:
        return None
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            RECOGNIZE_URL,
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (filename, data, content_type)},
        )
        resp.raise_for_status()
        js = resp.json()

    result = js.get("result")
    if result:
        text = " ".join(str(seg.get("text", "")) for seg in result if isinstance(seg, dict)).strip()
        return text or None

    task_id = js.get("task_id")
    if task_id:
        return await _poll_task(task_id)
    return None


async def _poll_task(task_id: str) -> str | None:
    token = await _obtain_token()
    if not token:
        return None
    for _ in range(120):
        await asyncio.sleep(1.0)
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    TASK_URL,
                    params={"task_id": task_id},
                    headers={"Authorization": f"Bearer {token}"},
                )
                resp.raise_for_status()
                js = resp.json()
        except Exception as e:
            logging.warning(f"Salute task:get ошибка: {e}")
            return None
        status = js.get("status")
        if status == "done":
            result = (js.get("response") or {}).get("result")
            if result:
                return " ".join(str(seg.get("text", "")) for seg in result if isinstance(seg, dict)).strip() or None
            return None
        if status in ("error", "failed", "canceled", "cancelled"):
            return None
    return None


def _extract_audio_from_video_note(data: bytes) -> bytes | None:
    """
    Извлекает аудиодорожку из видеокружка (mp4) в OGG/Opus 16 кГц через
    ffmpeg (системный или из imageio-ffmpeg). Возвращает bytes или None.
    """
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        return None
    fin_path = fout_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fin, \
             tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as fout:
            fin.write(data)
            fin.flush()
            fin_path, fout_path = fin.name, fout.name
        # libopus есть в большинстве сборок; если в этой сборке его нет —
        # повторяем со встроенным кодеком `opus` (есть всегда)
        for codec in ("libopus", "opus"):
            proc = subprocess.run(
                [
                    ffmpeg, "-y", "-loglevel", "error",
                    "-i", fin_path,
                    "-vn", "-c:a", codec, "-b:a", "32k",
                    "-ar", "16000", "-ac", "1",
                    fout_path,
                ],
                capture_output=True, timeout=60,
            )
            if proc.returncode == 0:
                break
        else:
            logging.warning(
                f"ffmpeg: не удалось кодировать в Opus: {proc.stderr[:200]!r}"
            )
            return None
        with open(fout_path, "rb") as f:
            return f.read()
    except Exception as e:
        logging.warning(f"Не удалось извлечь аудио из кружка через ffmpeg: {e}")
        return None
    finally:
        for p in (fin_path, fout_path):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


async def transcribe_media(data: bytes, kind: str) -> str | None:
    """
    Распознаёт голосовое (kind='voice', OGG/Opus) или видеокружок
    (kind='video_note', mp4). Возвращает текст или None, если распознать
    не удалось (нет настроек, нет сети, нет ffmpeg для кружка и т.п.).
    """
    if not data or not enabled():
        return None
    if kind == "video_note":
        audio = _extract_audio_from_video_note(data)
        if not audio:
            return None
        return await _recognize(audio, "audio.ogg", "audio/ogg")
    return await _recognize(data, "audio.ogg", "audio/ogg")
