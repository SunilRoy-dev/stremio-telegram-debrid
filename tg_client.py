import time
import logging
import asyncio
import functools
import inspect
from hashlib import sha256
from typing import Callable, Optional, AsyncGenerator, Union

from pyrogram import Client, raw, utils
from pyrogram.types import Message
from pyrogram.session.auth import Auth
from pyrogram.session import Session
from pyrogram.file_id import FileId, FileType, ThumbnailSource
from pyrogram.errors import VolumeLocNotFound, CDNFileHashMismatch
from pyrogram.crypto import aes
import pyrogram
from config import Config

logger = logging.getLogger("tg_client")

# Monkey-patch to cache auth keys across media sessions
_original_auth_create = Auth.create
_auth_key_cache = {}

async def _patched_auth_create(self):
    if self.dc_id in _auth_key_cache:
        logger.info(f"Reusing cached auth key for DC{self.dc_id}")
        return _auth_key_cache[self.dc_id]
    
    logger.info(f"Generating new auth key for DC{self.dc_id}...")
    key = await _original_auth_create(self)
    _auth_key_cache[self.dc_id] = key
    return key

Auth.create = _patched_auth_create


# Monkey-patch Client.get_file to reuse media sessions and avoid connection overhead
async def _patched_get_file(
    self: Client,
    file_id: FileId,
    file_size: int = 0,
    limit: int = 0,
    offset: int = 0,
    progress: Callable = None,
    progress_args: tuple = ()
) -> Optional[AsyncGenerator[bytes, None]]:
    async with self.get_file_semaphore:
        file_type = file_id.file_type

        if file_type == FileType.CHAT_PHOTO:
            if file_id.chat_id > 0:
                peer = raw.types.InputPeerUser(
                    user_id=file_id.chat_id,
                    access_hash=file_id.chat_access_hash
                )
            else:
                if file_id.chat_access_hash == 0:
                    peer = raw.types.InputPeerChat(
                        chat_id=-file_id.chat_id
                    )
                else:
                    peer = raw.types.InputPeerChannel(
                        channel_id=utils.get_channel_id(file_id.chat_id),
                        access_hash=file_id.chat_access_hash
                    )

            location = raw.types.InputPeerPhotoFileLocation(
                peer=peer,
                photo_id=file_id.media_id,
                big=file_id.thumbnail_source == ThumbnailSource.CHAT_PHOTO_BIG
            )
        elif file_type == FileType.PHOTO:
            location = raw.types.InputPhotoFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size
            )
        else:
            location = raw.types.InputDocumentFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size
            )

        current = 0
        total = abs(limit) or (1 << 31) - 1
        chunk_size = 1024 * 1024
        offset_bytes = abs(offset) * chunk_size

        dc_id = file_id.dc_id

        async with self.media_sessions_lock:
            session = self.media_sessions.get(dc_id)
            if session is None:
                logger.info(f"Creating new media session for DC{dc_id}...")
                session = Session(
                    self, dc_id,
                    await Auth(self, dc_id, await self.storage.test_mode()).create()
                    if dc_id != await self.storage.dc_id()
                    else await self.storage.auth_key(),
                    await self.storage.test_mode(),
                    is_media=True
                )
                await session.start()

                if dc_id != await self.storage.dc_id():
                    exported_auth = await self.invoke(
                        raw.functions.auth.ExportAuthorization(
                            dc_id=dc_id
                        )
                    )

                    await session.invoke(
                        raw.functions.auth.ImportAuthorization(
                            id=exported_auth.id,
                            bytes=exported_auth.bytes
                        )
                    )
                self.media_sessions[dc_id] = session
            else:
                logger.info(f"Reusing cached media session for DC{dc_id}")

        try:
            r = await session.invoke(
                raw.functions.upload.GetFile(
                    location=location,
                    offset=offset_bytes,
                    limit=chunk_size
                ),
                sleep_threshold=30
            )

            if isinstance(r, raw.types.upload.File):
                while True:
                    chunk = r.bytes

                    yield chunk

                    current += 1
                    offset_bytes += chunk_size

                    if progress:
                        func = functools.partial(
                            progress,
                            min(offset_bytes, file_size)
                            if file_size != 0
                            else offset_bytes,
                            file_size,
                            *progress_args
                        )

                        if inspect.iscoroutinefunction(progress):
                            await func()
                        else:
                            await self.loop.run_in_executor(self.executor, func)

                    if len(chunk) < chunk_size or current >= total:
                        break

                    r = await session.invoke(
                        raw.functions.upload.GetFile(
                            location=location,
                            offset=offset_bytes,
                            limit=chunk_size
                        ),
                        sleep_threshold=30
                    )

            elif isinstance(r, raw.types.upload.FileCdnRedirect):
                cdn_session = Session(
                    self, r.dc_id, await Auth(self, r.dc_id, await self.storage.test_mode()).create(),
                    await self.storage.test_mode(), is_media=True, is_cdn=True
                )

                try:
                    await cdn_session.start()

                    while True:
                        r2 = await cdn_session.invoke(
                            raw.functions.upload.GetCdnFile(
                                file_token=r.file_token,
                                offset=offset_bytes,
                                limit=chunk_size
                            )
                        )

                        if isinstance(r2, raw.types.upload.CdnFileReuploadNeeded):
                            try:
                                await session.invoke(
                                    raw.functions.upload.ReuploadCdnFile(
                                        file_token=r.file_token,
                                        request_token=r2.request_token
                                    )
                                )
                            except VolumeLocNotFound:
                                break
                            else:
                                continue

                        chunk = r2.bytes

                        decrypted_chunk = aes.ctr256_decrypt(
                            chunk,
                            r.encryption_key,
                            bytearray(
                                r.encryption_iv[:-4]
                                + (offset_bytes // 16).to_bytes(4, "big")
                            )
                        )

                        hashes = await session.invoke(
                            raw.functions.upload.GetCdnFileHashes(
                                file_token=r.file_token,
                                offset=offset_bytes
                            )
                        )

                        for i, h in enumerate(hashes):
                            cdn_chunk = decrypted_chunk[h.limit * i: h.limit * (i + 1)]
                            CDNFileHashMismatch.check(
                                h.hash == sha256(cdn_chunk).digest(),
                                "h.hash == sha256(cdn_chunk).digest()"
                            )

                        yield decrypted_chunk

                        current += 1
                        offset_bytes += chunk_size

                        if progress:
                            func = functools.partial(
                                progress,
                                min(offset_bytes, file_size) if file_size != 0 else offset_bytes,
                                file_size,
                                *progress_args
                            )

                            if inspect.iscoroutinefunction(progress):
                                await func()
                            else:
                                await self.loop.run_in_executor(self.executor, func)

                        if len(chunk) < chunk_size or current >= total:
                            break
                finally:
                    await cdn_session.stop()
        except Exception as e:
            if not isinstance(e, (pyrogram.StopTransmission, asyncio.CancelledError)):
                logger.warning(f"Error in media session for DC{dc_id}, discarding from cache: {e}")
                async with self.media_sessions_lock:
                    if self.media_sessions.get(dc_id) is session:
                        self.media_sessions.pop(dc_id, None)
                try:
                    await session.stop()
                except Exception:
                    pass
            raise e

Client.get_file = _patched_get_file


class TelegramClientManager:
    def __init__(self):
        self.client = None
        self.is_running = False
        self._search_cache = {}
        self._message_cache = {}
        self._log_cache = {}

    def initialize(self):
        Config.validate()
        
        if Config.USER_SESSION_STRING:
            logger.info("Initializing User Client...")
            self.client = Client(
                name="tg_stremio_user",
                api_id=Config.API_ID,
                api_hash=Config.API_HASH,
                session_string=Config.USER_SESSION_STRING,
                no_updates=True
            )
        elif Config.BOT_TOKEN:
            logger.info("Initializing Bot Client...")
            self.client = Client(
                name="tg_stremio_bot",
                api_id=Config.API_ID,
                api_hash=Config.API_HASH,
                bot_token=Config.BOT_TOKEN,
                no_updates=True
            )
        else:
            raise ValueError("Neither USER_SESSION_STRING nor BOT_TOKEN is configured!")

    def get_channel_ids(self) -> list:
        val = Config.TELEGRAM_CHANNEL_ID
        if not val:
            return []
        if isinstance(val, int):
            return [val]
        parts = [p.strip() for p in str(val).split(",")]
        ids = []
        for p in parts:
            if p.startswith("-") or p.isdigit():
                try:
                    ids.append(int(p))
                except ValueError:
                    ids.append(p)
            else:
                ids.append(p)
        return ids

    async def start(self):
        if not self.client:
            self.initialize()
        
        if not self.is_running:
            logger.info("Starting Pyrogram client...")
            await self.client.start()
            self.is_running = True
            
            # Resolve target channels on startup to avoid PeerIdInvalid errors
            try:
                chat_ids = self.get_channel_ids()
                
                if Config.USER_SESSION_STRING:
                    cached_count = 0
                    async for dialog in self.client.get_dialogs(limit=400):
                        if dialog.chat.id in chat_ids:
                            logger.info(f"Resolved channel: {dialog.chat.title} ({dialog.chat.id})")
                            cached_count += 1
                            if cached_count >= len(chat_ids):
                                break
                
                for chat_id in chat_ids:
                    try:
                        await self.client.get_chat(chat_id)
                    except Exception as e:
                        logger.warning(f"Failed to cache channel {chat_id}: {e}")
                        
                if Config.LOG_CHANNEL_ID:
                    try:
                        await self.client.get_chat(Config.LOG_CHANNEL_ID)
                    except Exception as e:
                        logger.warning(f"Failed to cache log channel {Config.LOG_CHANNEL_ID}: {e}")
            except Exception as e:
                logger.warning(f"Failed to resolve target channels on startup: {e}")

    async def stop(self):
        if self.is_running and self.client:
            logger.info("Stopping Pyrogram client...")
            try:
                await asyncio.wait_for(self.client.stop(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("Pyrogram client stop timed out, skipping...")
            except Exception as e:
                logger.warning(f"Error stopping Pyrogram client: {e}")
            self.is_running = False

    async def send_play_log(self, filename: str, chat_id: Union[str, int], message_id: int):
        if not Config.LOG_CHANNEL_ID:
            return
            
        key = (chat_id, message_id)
        now = time.time()
        
        # Avoid duplicate logs for the same file within 15 mins
        if key in self._log_cache and now - self._log_cache[key] < 900:
            return
                
        self._log_cache[key] = now
        
        try:
            import datetime
            from datetime import timezone, timedelta
            
            tz_str = getattr(Config, "TIMEZONE", "UTC") or "UTC"
            local_dt = None
            
            try:
                from zoneinfo import ZoneInfo
                local_dt = datetime.datetime.now(ZoneInfo(tz_str))
            except Exception:
                pass
                
            if local_dt is None:
                try:
                    tz_clean = tz_str.upper().replace("UTC", "").replace("GMT", "").strip()
                    if tz_clean and tz_clean[0] in ("+", "-"):
                        sign = 1 if tz_clean[0] == "+" else -1
                        time_parts = tz_clean[1:].split(":")
                        hours = int(time_parts[0])
                        minutes = int(time_parts[1]) if len(time_parts) > 1 else 0
                        td = timedelta(hours=hours, minutes=minutes)
                        local_dt = datetime.datetime.now(timezone(sign * td))
                except Exception:
                    pass
            
            if local_dt is None:
                local_dt = datetime.datetime.now(timezone.utc)
                
            time_str = local_dt.strftime("%Y-%m-%d %H:%M:%S")
            year_str = local_dt.strftime("%Y")
            
            message_text = (
                f"🎬 **Media Stream Log**\n\n"
                f"📁 **File Name:** `{filename}`\n"
                f"📅 **Date & Time:** `{time_str}`\n"
                f"📆 **Year:** `{year_str}`\n"
                f"💬 **Source Channel:** `{chat_id}`\n"
                f"🆔 **Message ID:** `{message_id}`"
            )
            
            await self.client.send_message(
                chat_id=Config.LOG_CHANNEL_ID,
                text=message_text
            )
        except Exception as e:
            logger.error(f"Failed to send log to log channel: {e}")

    async def search_messages(self, query: str = "", limit: int = 50):
        if not self.is_running:
            await self.start()
        
        query_str = str(query).strip() if query else ""
        
        cache_key = f"{query_str}:{limit}"
        now = time.time()
        if cache_key in self._search_cache:
            cached_time, cached_results = self._search_cache[cache_key]
            if now - cached_time < Config.CACHE_TTL:
                return cached_results

        chat_ids = self.get_channel_ids()
        results = []
        per_channel_limit = max(100, limit)
        
        for chat_id in chat_ids:
            try:
                if query_str:
                    async for msg in self.client.search_messages(chat_id=chat_id, query=query_str, limit=per_channel_limit):
                        if self._has_media(msg):
                            results.append(msg)
                else:
                    async for msg in self.client.get_chat_history(chat_id=chat_id, limit=per_channel_limit):
                        if self._has_media(msg):
                            results.append(msg)
            except Exception as e:
                logger.warning(f"Telegram query failed for {chat_id}: {e}")
        
        results.sort(key=lambda m: m.date, reverse=True)
        final_results = results[:limit]
        self._search_cache[cache_key] = (now, final_results)
        return final_results

    async def get_message(self, message_id: int, chat_id: int = None) -> Message:
        if not self.is_running:
            await self.start()
            
        target_chat = chat_id if chat_id is not None else self.get_channel_ids()[0]
        
        cache_key = f"{target_chat}:{message_id}"
        now = time.time()
        if cache_key in self._message_cache:
            cached_time, cached_msg = self._message_cache[cache_key]
            if now - cached_time < Config.CACHE_TTL:
                return cached_msg

        try:
            msg = await self.client.get_messages(chat_id=target_chat, message_ids=message_id)
            self._message_cache[cache_key] = (now, msg)
            return msg
        except Exception as e:
            logger.error(f"Failed to fetch message {message_id} in channel {target_chat}: {e}")
            raise e

    def _has_media(self, msg: Message) -> bool:
        return bool(msg.video or msg.document or msg.audio)

tg_client_manager = TelegramClientManager()
