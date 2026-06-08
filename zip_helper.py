import io
import asyncio

# Fix Pyrogram event loop crash on Python 3.12/3.14
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import zipfile
import logging
import anyio
from typing import List, Union
from pyrogram.types import Message

logger = logging.getLogger("zip_helper")

VIDEO_EXTENSIONS = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.ts', '.m4v')

def is_video_file(filename: str) -> bool:
    return filename.lower().endswith(VIDEO_EXTENSIONS)

class TelegramSeekableReader:
    def __init__(self, client, messages: Union[Message, List[Message]], block_size=1024*1024):
        self.client = client
        self.messages = messages if isinstance(messages, list) else [messages]
        self.block_size = block_size
        
        self.parts = []
        self.total_size = 0
        for msg in self.messages:
            media = msg.video or msg.document or msg.audio
            if not media:
                continue
            self.parts.append({
                "message": msg,
                "media": media,
                "size": media.file_size,
                "start": self.total_size,
                "end": self.total_size + media.file_size
            })
            self.total_size += media.file_size
            
        self.pos = 0
        self.block_cache = {}

    async def fetch_block(self, part_index: int, block_index: int) -> bytes:
        cache_key = (part_index, block_index)
        if cache_key in self.block_cache:
            return self.block_cache[cache_key]
            
        part = self.parts[part_index]
        media = part["media"]
        
        block = b""
        try:
            # limit=1 will download exactly one block_size (1MB) chunk
            async for chunk in self.client.stream_media(media, offset=block_index, limit=1):
                block = chunk
                break
        except Exception as e:
            logger.error(f"Error fetching block {block_index} for part {part_index}: {e}")
            
        self.block_cache[cache_key] = block
        return block

    async def read_range(self, start: int, end: int) -> bytes:
        if start >= self.total_size or start > end:
            return b""
        end = min(end, self.total_size - 1)
        
        result = bytearray()
        needed_bytes = end - start + 1
        curr_pos = start
        
        while needed_bytes > 0:
            part_idx = -1
            local_offset = -1
            for idx, part in enumerate(self.parts):
                if part["start"] <= curr_pos < part["end"]:
                    part_idx = idx
                    local_offset = curr_pos - part["start"]
                    break
                    
            if part_idx == -1:
                break
                
            block_idx = local_offset // self.block_size
            offset_in_block = local_offset % self.block_size
            
            block_data = await self.fetch_block(part_idx, block_idx)
            if not block_data:
                break
                
            chunk_avail = len(block_data) - offset_in_block
            if chunk_avail <= 0:
                break
                
            chunk_to_read = min(needed_bytes, chunk_avail)
            result.extend(block_data[offset_in_block : offset_in_block + chunk_to_read])
            
            curr_pos += chunk_to_read
            needed_bytes -= chunk_to_read
            
        return bytes(result)


class SyncTelegramFile(io.RawIOBase):
    def __init__(self, reader: TelegramSeekableReader, loop: asyncio.AbstractEventLoop):
        self.reader = reader
        self.loop = loop
        self.pos = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            self.pos = offset
        elif whence == io.SEEK_CUR:
            self.pos += offset
        elif whence == io.SEEK_END:
            self.pos = self.reader.total_size + offset
        self.pos = max(0, min(self.pos, self.reader.total_size))
        return self.pos

    def read(self, size=-1):
        if size == -1 or size is None:
            size = self.reader.total_size - self.pos
        if size <= 0 or self.pos >= self.reader.total_size:
            return b""
            
        fut = asyncio.run_coroutine_threadsafe(
            self.reader.read_range(self.pos, self.pos + size - 1),
            self.loop
        )
        data = fut.result()
        self.pos += len(data)
        return data


def list_zip_files_sync(reader: TelegramSeekableReader, loop: asyncio.AbstractEventLoop) -> list:
    sync_file = SyncTelegramFile(reader, loop)
    try:
        with zipfile.ZipFile(sync_file) as zf:
            return zf.infolist()
    except Exception as e:
        logger.error(f"Failed to list ZIP files: {e}")
        return []


async def list_zip_files(client, messages: Union[Message, List[Message]]) -> list:
    reader = TelegramSeekableReader(client, messages)
    if reader.total_size < 4:
        return []
        
    # Check if first part starts with ZIP signature
    first_block = await reader.fetch_block(0, 0)
    if not first_block.startswith(b"PK\x03\x04"):
        return []
        
    loop = asyncio.get_running_loop()
    return await anyio.to_thread.run_sync(list_zip_files_sync, reader, loop)


async def get_zip_entry_data_offset(reader: TelegramSeekableReader, header_offset: int) -> int:
    # Read local header (30 bytes)
    header = await reader.read_range(header_offset, header_offset + 29)
    if len(header) < 30:
        raise ValueError("Invalid ZIP local header")
    filename_len = int.from_bytes(header[26:28], "little")
    extra_len = int.from_bytes(header[28:30], "little")
    return header_offset + 30 + filename_len + extra_len


async def zip_compressed_generator(reader: TelegramSeekableReader, entry_name: str, start: int, end: int):
    loop = asyncio.get_running_loop()
    
    send_stream, receive_stream = anyio.create_memory_object_stream(max_buffer_size=4)
    
    def thread_worker():
        try:
            sync_file = SyncTelegramFile(reader, loop)
            with zipfile.ZipFile(sync_file) as zf:
                with zf.open(entry_name) as f:
                    if start > 0:
                        remaining = start
                        while remaining > 0:
                            data = f.read(min(remaining, 1024*1024))
                            if not data:
                                break
                            remaining -= len(data)
                    
                    bytes_to_read = end - start + 1
                    while bytes_to_read > 0:
                        data = f.read(min(bytes_to_read, 1024*1024))
                        if not data:
                            break
                        # Send chunk back to the async generator
                        asyncio.run_coroutine_threadsafe(send_stream.send(data), loop).result()
                        bytes_to_read -= len(data)
        except Exception as e:
            logger.error(f"Error reading compressed ZIP entry {entry_name}: {e}")
        finally:
            asyncio.run_coroutine_threadsafe(send_stream.aclose(), loop).result()

    # Start the background thread
    asyncio.create_task(anyio.to_thread.run_sync(thread_worker))
    
    async with receive_stream:
        async for chunk in receive_stream:
            yield chunk
