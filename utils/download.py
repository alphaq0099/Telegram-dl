import os
import json
import re
import sys
import asyncio
import time
import inspect
import shutil
from datetime import datetime
from tqdm import tqdm
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageService, MessageActionTopicCreate
from utils.archive import is_archived, add_to_archive

from utils.auth import PROJECT_ROOT

def get_config():
    """Reads configuration from config.json, fallback to defaults if error."""
    config_path = os.path.join(PROJECT_ROOT, 'config.json')
    defaults = {
        "download_dir": "./downloads",
        "media_types": ["photo", "video", "document", "audio", "voice", "sticker"],
        "chunk_size_kb": 1024,
        "max_concurrent_downloads": 3,
        "max_download_speed_kbps": 0,
        "file_name_template": "{chat_id}_{message_id}_{date}_{filename}",
        "filter_min_size_mb": 0,
        "filter_max_size_mb": 0,
        "filter_after_date": "",
        "filter_before_date": "",
        "filter_filename_regex": "",
        "filter_caption_regex": "",
        "filter_allowed_extensions": [],
        "monitored_chats": [],
        "enable_gallery": True,
        "cloud_upload_enabled": False,
        "cloud_delete_local_after_upload": False,
        "cloud_rclone_remote": "",
        "cloud_remote_path": "telegram-dl/backups",
        "tg_mirror_enabled": False,
        "tg_mirror_target": "",
        "monitor_date_organization": "none",
        "monitor_bot_session_grouping": True,
        "monitor_bot_session_gap_seconds": 60
    }
    if not os.path.exists(config_path):
        try:
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(defaults, f, indent=4)
        except Exception:
            pass
        return defaults
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            user_config = json.load(f)
        merged = defaults.copy()
        merged.update(user_config)
        return merged
    except Exception:
        return defaults

def get_media_type(message):
    """Determines the category/type of the media in a message."""
    if not message or not message.media:
        return None
    
    # Check specifically for voice/audio
    if getattr(message, 'voice', None):
        return "voice"
    if getattr(message, 'audio', None):
        return "audio"
    if getattr(message, 'video', None) or getattr(message, 'gif', None):
        return "video"
    if getattr(message, 'photo', None):
        return "photo"
    if getattr(message, 'sticker', None):
        return "sticker"
    if getattr(message, 'document', None):
        return "document"
    
    return "document" # Fallback

def clean_filename(filename: str) -> str:
    """Removes invalid characters for OS filenames."""
    return re.sub(r'[\\/*?:"<>|]', "_", filename)

def get_media_filename(message, chat_id: str) -> str:
    """Generates a filename based on config.json template."""
    config = get_config()
    template = config.get("file_name_template", "{chat_id}_{message_id}_{date}_{filename}")
    
    # Try to extract the original filename
    orig_filename = None
    if message.file and message.file.name:
        orig_filename = message.file.name
    
    if not orig_filename:
        # Fallback names based on media type
        mtype = get_media_type(message)
        ext = message.file.ext if (message.file and message.file.ext) else ""
        if not ext:
            if mtype == "photo":
                ext = ".jpg"
            elif mtype == "voice":
                ext = ".ogg"
            elif mtype == "video":
                ext = ".mp4"
            elif mtype == "audio":
                ext = ".mp3"
            elif mtype == "sticker":
                ext = ".webp"
            else:
                ext = ".bin"
        orig_filename = f"media_{message.id}{ext}"

    date_str = message.date.strftime("%Y%m%d_%H%M%S") if message.date else "unknown"
    
    formatted_name = template.format(
        chat_id=str(chat_id),
        message_id=str(message.id),
        date=date_str,
        filename=orig_filename
    )
    
    return clean_filename(formatted_name)

class TokenBucketLimiter:
    """Token Bucket rate limiter to cap network download speeds globally across tasks."""
    def __init__(self, rate_kbps):
        self.rate = rate_kbps * 1024  # bytes per second
        self.capacity = max(self.rate * 2, 1024 * 1024)  # burst capacity: 2s of transfer or 1MB min
        self.tokens = self.capacity
        self.last_update = time.time()
        self.lock = asyncio.Lock()

    def set_rate(self, rate_kbps):
        new_rate = rate_kbps * 1024
        if self.rate != new_rate:
            self.rate = new_rate
            self.capacity = max(self.rate * 2, 1024 * 1024)
            self.tokens = min(self.tokens, self.capacity)

    async def consume(self, amount):
        if self.rate <= 0:
            return
        
        async with self.lock:
            now = time.time()
            if now > self.last_update:
                elapsed = now - self.last_update
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.last_update = now
            
            if self.tokens >= amount:
                self.tokens -= amount
                return
            
            needed = amount - self.tokens
            self.tokens = 0
            
            wait_time = (self.last_update - now) + (needed / self.rate)
            self.last_update = self.last_update + (needed / self.rate)
            
        await asyncio.sleep(wait_time)

class ProgressPositionManager:
    """Tracks and assigns screen positions for concurrent tqdm progress bars."""
    def __init__(self, max_concurrent):
        self.max_concurrent = max_concurrent
        self.available_positions = list(range(1, max_concurrent + 1))
        self.lock = asyncio.Lock()

    async def acquire(self):
        async with self.lock:
            if self.available_positions:
                return self.available_positions.pop(0)
            return None

    async def release(self, position):
        if position is not None:
            async with self.lock:
                self.available_positions.append(position)
                self.available_positions.sort()

_rate_limiter = None
_rate_limiter_lock = asyncio.Lock()

async def get_rate_limiter():
    global _rate_limiter
    config = get_config()
    rate_kbps = config.get("max_download_speed_kbps", 0)
    async with _rate_limiter_lock:
        if _rate_limiter is None:
            _rate_limiter = TokenBucketLimiter(rate_kbps)
        else:
            _rate_limiter.set_rate(rate_kbps)
    return _rate_limiter

_position_manager = None
_pos_mgr_lock = asyncio.Lock()

async def get_position_manager():
    global _position_manager
    config = get_config()
    max_concurrent = config.get("max_concurrent_downloads", 3)
    async with _pos_mgr_lock:
        if _position_manager is None or _position_manager.max_concurrent != max_concurrent:
            _position_manager = ProgressPositionManager(max_concurrent)
    return _position_manager

class TqdmDownloadProgress:
    """Helper class to track download progress using tqdm with rate limiting."""
    gui_callback = None  # Class-level hook to receive progress updates in GUI: (filename, received, total)
    is_canceled = False

    def __init__(self, filename, total_bytes, position=None):
        self.filename = filename
        self.pbar = tqdm(
            total=total_bytes,
            unit='B',
            unit_scale=True,
            desc=filename[:30].ljust(30),
            leave=False,
            dynamic_ncols=True,
            position=position
        )
        self.last_bytes = 0

    async def callback(self, received, total):
        if total is None:
            self.pbar.total = received
        else:
            self.pbar.total = total
            
        diff = received - self.last_bytes
        if diff > 0:
            self.pbar.update(diff)
            self.last_bytes = received
            
            limiter = await get_rate_limiter()
            await limiter.consume(diff)
            
        if TqdmDownloadProgress.gui_callback:
            try:
                TqdmDownloadProgress.gui_callback(self.filename, received, total)
            except Exception:
                pass

    def close(self):
        self.pbar.close()

async def get_safe_chat_name(client: TelegramClient, message, chat_id) -> str:
    """
    Attempts to retrieve the clean, human-readable name of the chat/group/channel/bot.
    Falls back to chat_id if retrieval fails.
    """
    try:
        chat = await message.get_chat()
        if not chat:
            return str(chat_id)
            
        # Determine appropriate name field
        name = getattr(chat, 'title', None)  # For channels, groups, chats
        if not name:
            # For users/bots
            first_name = getattr(chat, 'first_name', None)
            last_name = getattr(chat, 'last_name', None)
            if first_name:
                name = first_name
                if last_name:
                    name = f"{first_name} {last_name}"
            else:
                name = getattr(chat, 'username', None)
                
        if not name:
            name = str(chat_id)
            
        # Clean folder name of invalid filesystem characters
        cleaned = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
        # Prevent empty or dot-only directory names
        if not cleaned or cleaned in ('.', '..'):
            return str(chat_id)
        return cleaned
    except Exception:
        return str(chat_id)

# Global cache for forum topic names: {(chat_id, topic_id): "Topic Name"}
_topic_name_cache = {}
_topic_cache_lock = asyncio.Lock()

async def get_forum_topic_name(client: TelegramClient, chat_id, topic_id: int) -> str:
    global _topic_name_cache
    
    try:
        chat_id_val = int(chat_id)
    except ValueError:
        chat_id_val = chat_id
        
    cache_key = (chat_id_val, topic_id)
    async with _topic_cache_lock:
        if cache_key in _topic_name_cache:
            return _topic_name_cache[cache_key]
            
    # If not in cache, fetch it from Telegram
    try:
        # Fetch the service message at topic_id (which is the creation message)
        topic_msg = await client.get_messages(chat_id, ids=topic_id)
        if topic_msg and isinstance(topic_msg, MessageService) and isinstance(topic_msg.action, MessageActionTopicCreate):
            title = topic_msg.action.title
            cleaned_title = re.sub(r'[\\/*?:"<>|]', "_", title).strip()
            if cleaned_title:
                async with _topic_cache_lock:
                    _topic_name_cache[cache_key] = cleaned_title
                return cleaned_title
    except Exception:
        # Fallback if request fails
        pass
        
    fallback = f"Topic_{topic_id}"
    async with _topic_cache_lock:
        _topic_name_cache[cache_key] = fallback
    return fallback

_bot_sessions = {}
_download_semaphore = None
_download_semaphore_lock = asyncio.Lock()

async def get_download_semaphore():
    global _download_semaphore
    async with _download_semaphore_lock:
        config = get_config()
        max_concurrent = config.get("max_concurrent_downloads", 3)
        if _download_semaphore is None or _download_semaphore._value != max_concurrent:
            _download_semaphore = asyncio.Semaphore(max_concurrent)
    return _download_semaphore

async def download_message_media(client: TelegramClient, message, chat_id, override_types=None, is_monitor=False):
    """
    Downloads media from a given message if it satisfies filters.
    Handles rate-limits (FloodWaitError) and creates necessary directories.
    """
    if not message or not message.media:
        return False, "No media found in message."
        
    if not getattr(message, 'file', None):
        return True, "Filtered (Skipped): Message has no downloadable file media."
    
    media_type = get_media_type(message)
    config = get_config()
    allowed_types = override_types if override_types is not None else config.get("media_types", [])
    
    if media_type not in allowed_types:
        return True, f"Filtered (Skipped): Media type '{media_type}' is filtered out."

    # Check if already archived in SQLite3 database
    if is_archived(chat_id, message.id):
        return True, "File already archived (Skipped)"

    # Get file size
    file_size = message.file.size if message.file else 0

    # 1. Size Filters
    min_size_mb = config.get("filter_min_size_mb", 0)
    max_size_mb = config.get("filter_max_size_mb", 0)
    if min_size_mb > 0 and file_size < min_size_mb * 1024 * 1024:
        return True, f"Filtered (Skipped): File size ({file_size / (1024*1024):.2f} MB) is below minimum of {min_size_mb} MB."
    if max_size_mb > 0 and file_size > max_size_mb * 1024 * 1024:
        return True, f"Filtered (Skipped): File size ({file_size / (1024*1024):.2f} MB) exceeds maximum of {max_size_mb} MB."

    # 2. Date Filters
    after_date_str = config.get("filter_after_date", "")
    before_date_str = config.get("filter_before_date", "")
    if after_date_str and message.date:
        try:
            after_date = datetime.strptime(after_date_str.strip(), "%Y-%m-%d").date()
            if message.date.date() < after_date:
                return True, f"Filtered (Skipped): Message date ({message.date.date()}) is before allowed start date ({after_date_str})."
        except ValueError:
            pass
    if before_date_str and message.date:
        try:
            before_date = datetime.strptime(before_date_str.strip(), "%Y-%m-%d").date()
            if message.date.date() > before_date:
                return True, f"Filtered (Skipped): Message date ({message.date.date()}) is after allowed end date ({before_date_str})."
        except ValueError:
            pass

    # Determine filename and path
    filename = get_media_filename(message, str(chat_id))

    # 3. Extension Filter
    allowed_exts = config.get("filter_allowed_extensions", [])
    if allowed_exts:
        _, ext = os.path.splitext(filename.lower())
        ext = ext.lstrip('.')
        if ext not in [e.lower().lstrip('.') for e in allowed_exts]:
            return True, f"Filtered (Skipped): File extension '.{ext}' is not in allowed extensions: {allowed_exts}."

    # 4. Filename Regex Filter
    fn_regex_str = config.get("filter_filename_regex", "")
    if fn_regex_str:
        try:
            if not re.search(fn_regex_str, filename, re.IGNORECASE):
                return True, f"Filtered (Skipped): Filename '{filename}' does not match regex pattern '{fn_regex_str}'."
        except re.error:
            pass

    # 5. Caption Regex Filter
    caption_regex_str = config.get("filter_caption_regex", "")
    if caption_regex_str:
        caption = message.message or ""
        try:
            if not re.search(caption_regex_str, caption, re.IGNORECASE):
                return True, f"Filtered (Skipped): Message caption does not match regex pattern '{caption_regex_str}'."
        except re.error:
            pass

    # Resolve download directory
    download_dir = config.get("download_dir", "./downloads")
    if not os.path.isabs(download_dir):
        download_dir = os.path.join(PROJECT_ROOT, download_dir)
        
    # Get safe chat name to build the subfolder
    chat_folder = await get_safe_chat_name(client, message, chat_id)
    download_dir = os.path.join(download_dir, chat_folder)
    
    # Check if the message belongs to a forum topic/thread
    topic_id = None
    topic_name = None
    if message.reply_to and getattr(message.reply_to, 'forum_topic', False):
        topic_id = getattr(message.reply_to, 'reply_to_top_id', None)
        if topic_id is None:
            topic_id = getattr(message.reply_to, 'reply_to_msg_id', None)
            
    if topic_id is not None:
        topic_name = await get_forum_topic_name(client, chat_id, topic_id)
        download_dir = os.path.join(download_dir, topic_name)
        
    # Organize live monitoring files
    if is_monitor:
        # Check if chat is a Bot
        is_bot = False
        try:
            chat = await client.get_entity(chat_id)
            from telethon.tl.types import User
            if isinstance(chat, User) and chat.bot:
                is_bot = True
        except Exception:
            pass
            
        if is_bot and config.get("monitor_bot_session_grouping", True):
            now_ts = time.time()
            gap = config.get("monitor_bot_session_gap_seconds", 60)
            session_info = _bot_sessions.get(chat_id)
            
            if session_info is None or (now_ts - session_info["last_timestamp"]) > gap:
                # Start new session folder
                session_time_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                session_folder = f"Session_{session_time_str}"
                _bot_sessions[chat_id] = {
                    "session_folder": session_folder,
                    "last_timestamp": now_ts
                }
            else:
                # Extend current session
                _bot_sessions[chat_id]["last_timestamp"] = now_ts
                
            session_folder = _bot_sessions[chat_id]["session_folder"]
            download_dir = os.path.join(download_dir, session_folder)
        elif message.date:
            date_org = config.get("monitor_date_organization", "none")
            if date_org == "daily":
                download_dir = os.path.join(download_dir, message.date.strftime("%Y-%m-%d"))
            elif date_org == "monthly":
                download_dir = os.path.join(download_dir, message.date.strftime("%Y-%m"))
        
    os.makedirs(download_dir, exist_ok=True)
    target_path = os.path.join(download_dir, filename)

    # If file already exists on disk, skip and add to archive (sync database with disk)
    if os.path.exists(target_path):
        add_to_archive(chat_id, message.id, filename, file_size)
        await save_metadata_and_regenerate_gallery(
            download_dir, 
            chat_folder if topic_id is None else f"{chat_folder} - {topic_name}", 
            message, 
            filename, 
            file_size, 
            media_type,
            topic_name
        )
        return True, f"File already exists (Skipped): {filename}"

    temp_path = target_path + ".part"
    
    sem = await get_download_semaphore()
    await sem.acquire()
    
    # Acquire a progress bar position
    pos_mgr = await get_position_manager()
    position = await pos_mgr.acquire()
    
    progress = TqdmDownloadProgress(filename, file_size, position)
    
    retry_count = 5
    try:
        for attempt in range(retry_count):
            try:
                # Resolve the actual file location to download to prevent webpage casting errors
                file_to_download = message
                if message.media:
                    from telethon.tl.types import MessageMediaWebPage
                    if isinstance(message.media, MessageMediaWebPage):
                        webpage = getattr(message.media, 'webpage', None)
                        if webpage:
                            if getattr(webpage, 'document', None):
                                file_to_download = webpage.document
                            elif getattr(webpage, 'photo', None):
                                file_to_download = webpage.photo
                    else:
                        if getattr(message.media, 'document', None):
                            file_to_download = message.media.document
                        elif getattr(message.media, 'photo', None):
                            file_to_download = message.media.photo

                if isinstance(file_to_download, MessageMediaWebPage):
                    return True, "Filtered (Skipped): WebPage link preview has no direct downloadable document/photo."

                # Calculate current offset for resumable files (only large files > 1MB)
                offset = 0
                if os.path.exists(temp_path) and file_size and file_size > 1024 * 1024:
                    local_size = os.path.getsize(temp_path)
                    # Telegram requires offsets divisible by 4096 bytes
                    offset = (local_size // 4096) * 4096
                    if offset < 4096:
                        offset = 0
                
                # Check if already fully downloaded
                if file_size and offset >= file_size:
                    if os.path.exists(temp_path):
                        os.replace(temp_path, target_path)
                else:
                    # Setup progress bar starting position
                    progress.last_bytes = offset
                    progress.pbar.n = offset
                    progress.pbar.refresh()
                    
                    limiter = await get_rate_limiter()
                    
                    # Choose write mode
                    mode = "r+b" if offset > 0 else "wb"
                    
                    with open(temp_path, mode) as f_out:
                        if offset > 0:
                            f_out.seek(offset)
                            f_out.truncate(offset) # ensure file size is precisely offset
                            
                        # Download chunk by chunk
                        async for chunk in client.iter_download(
                            file=file_to_download,
                            offset=offset,
                            request_size=512 * 1024, # 512KB chunks (Telegram maximum limit for speed)
                            file_size=file_size
                        ):
                            if getattr(TqdmDownloadProgress, 'is_canceled', False):
                                raise asyncio.CancelledError("Download cancelled by user.")
                                
                            f_out.write(chunk)
                            chunk_len = len(chunk)
                            
                            # Update progress bar
                            progress.pbar.update(chunk_len)
                            
                            # Trigger GUI progress callback
                            if TqdmDownloadProgress.gui_callback:
                                try:
                                    TqdmDownloadProgress.gui_callback(progress.filename, progress.pbar.n, file_size)
                                except Exception:
                                    pass
                                    
                            # Apply bandwidth throttling
                            if limiter:
                                await limiter.consume(chunk_len)
                                
                    progress.close()
                    
                    # Rename temp file to final target path
                    if os.path.exists(temp_path):
                        os.replace(temp_path, target_path)
                
                # Successfully downloaded -> Add to SQLite3 archive database
                add_to_archive(
                    chat_id, 
                    message.id, 
                    filename, 
                    file_size, 
                    chat_name=chat_folder, 
                    caption=message.message or "", 
                    media_type=media_type
                )
                
                # Write metadata & update gallery
                await save_metadata_and_regenerate_gallery(
                    download_dir, 
                    chat_folder if topic_id is None else f"{chat_folder} - {topic_name}", 
                    message, 
                    filename, 
                    file_size, 
                    media_type,
                    topic_name
                )
                
                # Auto-extract audio if enabled in config
                if config.get("auto_extract_audio", False) and media_type == "video" and target_path.lower().endswith(".mp4"):
                    if shutil.which("ffmpeg") is not None:
                        audio_path = os.path.splitext(target_path)[0] + ".mp3"
                        try:
                            cmd = [
                                "ffmpeg", "-y", "-i", target_path,
                                "-vn", "-acodec", "libmp3lame", "-q:a", "2",
                                audio_path
                            ]
                            proc = await asyncio.create_subprocess_exec(
                                *cmd,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE
                            )
                            await proc.communicate()
                            if proc.returncode == 0:
                                tqdm.write(f"   ↳ 🔊 [Auto-Extract] Extracted audio to {os.path.basename(audio_path)}")
                        except Exception as e:
                            tqdm.write(f"   ↳ ⚠️ [Auto-Extract] Failed to extract audio: {str(e)}")
                
                # Trigger post-download backups (Cloud Sync & TG Mirroring)
                try:
                    from modules.cloud import process_post_download_backups
                    asyncio.create_task(
                        process_post_download_backups(
                            client, 
                            target_path, 
                            chat_folder, 
                            message.message or ""
                        )
                    )
                except Exception:
                    pass
                
                return True, target_path
                
            except FloodWaitError as e:
                progress.close()
                tqdm.write(f"\n⏳ \033[1;33m[FLOOD WAIT] Rate limited by Telegram. Must sleep for {e.seconds} seconds before retrying...\033[0m")
                await asyncio.sleep(e.seconds)
                progress = TqdmDownloadProgress(filename, file_size, position)
            except asyncio.CancelledError:
                try:
                    progress.close()
                except Exception:
                    pass
                raise
            except Exception as e:
                progress.close()
                if attempt == retry_count - 1:
                    # Final attempt failed - keep the part file so it can be resumed next time!
                    return False, f"Failed to download after {retry_count} attempts. Error: {str(e)}"
                tqdm.write(f"\n⚠️ \033[1;33m[WARNING] Attempt {attempt+1} failed: {str(e)}. Retrying in 3 seconds...\033[0m")
                await asyncio.sleep(3)
                progress = TqdmDownloadProgress(filename, file_size, position)
    finally:
        try:
            progress.close()
        except Exception:
            pass
        # Always release the position and semaphore
        await pos_mgr.release(position)
        sem.release()
            
    return False, "Unknown download failure."

_metadata_write_lock = asyncio.Lock()

async def save_metadata_and_regenerate_gallery(chat_dir, chat_title, message, filename, file_size, media_type, topic_name=None):
    """
    Extracts metadata from a downloaded message, appends it safely to metadata.json,
    and triggers a clean regeneration of gallery.html.
    If topic_name is provided, it updates both the topic gallery and the parent group gallery.
    """
    caption = message.message or ""
    date_str = message.date.strftime("%Y-%m-%d %H:%M:%S") if message.date else "Unknown"
    sender_id = message.sender_id or 0
    
    # 1. Update the local (topic or direct chat) gallery
    metadata_obj = {
        "message_id": message.id,
        "sender_id": sender_id,
        "date": date_str,
        "caption": caption,
        "filename": filename,
        "file_size": file_size,
        "media_type": media_type,
        "topic_name": topic_name
    }
    
    await update_gallery_files(chat_dir, chat_title, metadata_obj)
    
    # 2. If it's in a topic, also update the parent chat gallery
    if topic_name:
        parent_dir = os.path.dirname(chat_dir)
        parent_metadata_obj = metadata_obj.copy()
        # File path relative to the parent directory: e.g. "General/image.jpg"
        parent_metadata_obj["filename"] = f"{topic_name}/{filename}"
        
        # Get the group name (which is the directory name of the parent)
        group_title = os.path.basename(parent_dir)
        
        await update_gallery_files(parent_dir, group_title, parent_metadata_obj)

async def update_gallery_files(directory, title, metadata_obj):
    """Appends metadata to metadata.json and regenerates gallery.html inside a directory."""
    metadata_path = os.path.join(directory, "metadata.json")
    async with _metadata_write_lock:
        data = []
        if os.path.exists(metadata_path):
            try:
                with open(metadata_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if not isinstance(data, list):
                        data = []
            except Exception:
                data = []
                
        # Remove duplicate entry if exists
        data = [item for item in data if item.get("message_id") != metadata_obj["message_id"]]
        data.append(metadata_obj)
        
        try:
            with open(metadata_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            tqdm.write(f"⚠️ [WARNING] Failed to write metadata: {str(e)}")
            return
            
        # Re-generate gallery.html only if enabled
        config = get_config()
        if config.get("enable_gallery", True):
            generate_gallery_html(directory, title, data)

def generate_gallery_html(chat_dir, chat_title, data):
    """
    Generates a highly-stylized, responsive HTML gallery based on the downloaded media.
    Delegates visual rendering templates to utils/gallery_template.py.
    """
    from utils.gallery_template import build_gallery_html
    html_content = build_gallery_html(chat_title, data)
    
    gallery_path = os.path.join(chat_dir, "gallery.html")
    try:
        with open(gallery_path, "w", encoding="utf-8") as f:
            f.write(html_content)
    except Exception as e:
        tqdm.write(f"⚠️ [WARNING] Failed to write gallery.html: {str(e)}")

def rebuild_all_galleries():
    """Iterates through all download folders and regenerates gallery.html if metadata.json exists."""
    config = get_config()
    download_dir = config.get("download_dir", "./downloads")
    if not os.path.isabs(download_dir):
        download_dir = os.path.join(PROJECT_ROOT, download_dir)
        
    if not os.path.exists(download_dir):
        return 0
        
    rebuild_count = 0
    # Walk through download directory
    for root, dirs, files in os.walk(download_dir):
        if "metadata.json" in files:
            metadata_path = os.path.join(root, "metadata.json")
            try:
                with open(metadata_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list) and data:
                    chat_title = os.path.basename(root)
                    generate_gallery_html(root, chat_title, data)
                    rebuild_count += 1
            except Exception:
                pass
    return rebuild_count
