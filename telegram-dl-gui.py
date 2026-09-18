import flet as ft
import asyncio
import os
import sys
import json
import logging
import traceback
from datetime import datetime
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from dotenv import load_dotenv
import pyperclip
import threading
import time

# Import core fixes from project root
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from utils.auth import PROJECT_ROOT
from utils.download import get_config, TqdmDownloadProgress, download_message_media
from utils.parser import parse_telegram_link, resolve_chat_entity
from modules.range import download_range
from modules.full import download_full
from modules.queue_manager import load_queue, save_queue
from utils.archive import DB_PATH
from modules.search import find_file_on_disk, open_file_folder, format_size

# Handle windowed execution where stdout/stderr are None
is_windowed = sys.stdout is None
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w', encoding='utf-8')

# Setup logging to console and file
log_handlers = [
    logging.FileHandler(os.path.join(PROJECT_ROOT, 'gui_debug.log'), encoding='utf-8', mode='w')
]
if not is_windowed:
    log_handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=log_handlers
)
logger = logging.getLogger("GUI_Debug")

# Global Telethon Client Reference
client = None

def save_config(new_config):
    """Saves updated config dictionary back to config.json."""
    logger.debug("Saving updated config.json...")
    config_path = os.path.join(PROJECT_ROOT, 'config.json')
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(new_config, f, indent=4)
        logger.info("config.json successfully saved.")
        return True
    except Exception as e:
        logger.error(f"Error saving config.json: {str(e)}")
        return False

async def main(page: ft.Page):
    global client
    logger.debug("Initializing GUI Main Window...")
    
    def handle_time_picker_change(e):
        if e.control.value:
            selected_time = e.control.value
            scheduler_start_time.current.value = selected_time.strftime("%H:%M:%S")
            scheduler_start_time.current.update()

    scheduler_time_picker = ft.TimePicker(
        confirm_text="Confirm",
        help_text="Select download start time"
    )
    scheduler_time_picker.on_change = handle_time_picker_change
    page.overlay.append(scheduler_time_picker)
    
    # Page Window Configuration
    page.title = "Telegram Downloader & Media Manager (v3.9.0)"
    page.theme_mode = ft.ThemeMode.DARK
    page.window.width = 1150
    page.window.height = 750
    page.window.min_width = 1000
    page.window.min_height = 650
    page.bgcolor = "#121214"
    page.padding = 0

    focused_textfield = None
    scheduler_armed = False
    gui_scheduler_task = None
    scheduler_range_start_temp = None
    monitor_handler = None
    monitor_active = False
    active_gui_monitor_tasks = set()

    def on_focus_field(e):
        nonlocal focused_textfield
        focused_textfield = e.control
        logger.debug(f"TextField focused: {getattr(e.control, 'label', 'unnamed')}")

    def on_blur_field(e):
        nonlocal focused_textfield
        if focused_textfield == e.control:
            focused_textfield = None
            logger.debug("TextField blurred")

    async def get_clipboard_text():
        # Try pyperclip first
        try:
            val = pyperclip.paste()
            if val:
                logger.debug(f"Clipboard read via pyperclip: '{val}'")
                return val.strip()
        except Exception as ex:
            logger.warning(f"pyperclip failed to read clipboard: {ex}")
            
        # Fall back to native Flet Clipboard
        try:
            val = await ft.Clipboard().get()
            if val:
                logger.debug(f"Clipboard read via native Flet Clipboard: '{val}'")
                return val.strip()
        except Exception as ex:
            logger.error(f"Native Flet Clipboard failed: {ex}")
            
        return ""

    async def on_keyboard_event(e: ft.KeyboardEvent):
        is_ctrl_down = e.ctrl
        is_meta_down = e.meta
        
        # Windows-specific physical key state check to bypass Flet's modifier key bugs
        try:
            import ctypes
            if sys.platform == "win32":
                is_ctrl_down = is_ctrl_down or ((ctypes.windll.user32.GetKeyState(0x11) & 0x8000) != 0)
                is_meta_down = is_meta_down or ((ctypes.windll.user32.GetKeyState(0x5B) & 0x8000) != 0) or ((ctypes.windll.user32.GetKeyState(0x5C) & 0x8000) != 0)
        except Exception:
            pass
            
        is_paste = (is_ctrl_down or is_meta_down) and e.key.lower() == "v"
        
        if is_paste:
            logger.debug(f"Global Ctrl+V shortcut intercepted. Focused control: {focused_textfield}")
            if focused_textfield:
                val = await get_clipboard_text()
                if val:
                    focused_textfield.value = val
                    focused_textfield.update()
                    logger.debug(f"Pasted text via global shortcut: '{val}'")

    page.on_keyboard_event = on_keyboard_event



    # ----------------------------------------------------
    # State Management & Helpers
    # ----------------------------------------------------
    config = get_config()
    logger.debug(f"Loaded config: {config}")
    
    # Ref handles for active downloader indicators
    active_download_progress = ft.Ref[ft.ProgressBar]()
    active_download_status = ft.Ref[ft.Text]()
    active_download_details = ft.Ref[ft.Text]()
    active_download_speed = ft.Ref[ft.Text]()
    active_download_cancel_btn = ft.Ref[ft.IconButton]()
    
    # Ref handles for single download fields
    single_link_input = ft.Ref[ft.TextField]()
    
    # Ref handles for range and full download fields
    range_start_link = ft.Ref[ft.TextField]()
    range_end_link = ft.Ref[ft.TextField]()
    full_chat_input = ft.Ref[ft.TextField]()
    full_limit_input = ft.Ref[ft.TextField]()
    
    # Ref handles for Scheduler fields
    scheduler_job_mode = ft.Ref[ft.Dropdown]()
    scheduler_input_link = ft.Ref[ft.TextField]()
    scheduler_queue_table = ft.Ref[ft.DataTable]()
    scheduler_start_time = ft.Ref[ft.TextField]()
    scheduler_enable_btn = ft.Ref[ft.Button]()
    scheduler_status_text = ft.Ref[ft.Text]()
    scheduler_add_btn = ft.Ref[ft.FilledButton]()
    
    # Ref handles for Monitor fields
    monitor_status_title = ft.Ref[ft.Text]()
    monitor_status_desc = ft.Ref[ft.Text]()
    monitor_switch = ft.Ref[ft.Switch]()
    monitor_log_field = ft.Ref[ft.TextField]()
    monitor_add_input = ft.Ref[ft.TextField]()
    
    # Ref handles for Settings fields
    settings_api_id = ft.Ref[ft.TextField]()
    settings_api_hash = ft.Ref[ft.TextField]()
    settings_download_dir = ft.Ref[ft.TextField]()
    settings_speed_limit = ft.Ref[ft.TextField]()
    settings_chunk_size = ft.Ref[ft.Dropdown]()
    settings_max_concurrent = ft.Ref[ft.TextField]()
    settings_filename_template = ft.Ref[ft.TextField]()
    
    settings_min_size = ft.Ref[ft.TextField]()
    settings_max_size = ft.Ref[ft.TextField]()
    settings_after_date = ft.Ref[ft.TextField]()
    settings_before_date = ft.Ref[ft.TextField]()
    settings_filename_regex = ft.Ref[ft.TextField]()
    settings_caption_regex = ft.Ref[ft.TextField]()
    settings_allowed_extensions = ft.Ref[ft.TextField]()
    
    settings_media_photo = ft.Ref[ft.Checkbox]()
    settings_media_video = ft.Ref[ft.Checkbox]()
    settings_media_document = ft.Ref[ft.Checkbox]()
    settings_media_sticker = ft.Ref[ft.Checkbox]()
    
    settings_monitor_date_org = ft.Ref[ft.Dropdown]()
    settings_monitor_session_grouping = ft.Ref[ft.Checkbox]()
    settings_monitor_session_gap = ft.Ref[ft.TextField]()
    
    settings_gallery_enabled = ft.Ref[ft.Checkbox]()
    settings_cloud_enabled = ft.Ref[ft.Checkbox]()
    settings_cloud_delete_local = ft.Ref[ft.Checkbox]()
    settings_cloud_rclone_remote = ft.Ref[ft.TextField]()
    settings_cloud_remote_path = ft.Ref[ft.TextField]()
    
    settings_mirror_enabled = ft.Ref[ft.Checkbox]()
    settings_mirror_target = ft.Ref[ft.TextField]()
    
    settings_status = ft.Ref[ft.Text]()
    settings_add_monitored = ft.Ref[ft.TextField]()
    settings_monitored_status = ft.Ref[ft.Text]()
    
    # Ref handles for Statistics fields
    stats_total_storage_text = ft.Ref[ft.Text]()
    stats_total_files_text = ft.Ref[ft.Text]()
    stats_avg_size_text = ft.Ref[ft.Text]()
    stats_search_input = ft.Ref[ft.TextField]()
    stats_results_table = ft.Ref[ft.DataTable]()
    stats_search_button = ft.Ref[ft.IconButton]()
    stats_status_text = ft.Ref[ft.Text]()
    stats_breakdown_col = ft.Ref[ft.Column]()
    stats_chats_col = ft.Ref[ft.Column]()
    stats_largest_col = ft.Ref[ft.Column]()
    # Ref handles for Auth Onboarding fields
    auth_api_id = ft.Ref[ft.TextField]()
    auth_api_hash = ft.Ref[ft.TextField]()
    auth_phone = ft.Ref[ft.TextField]()
    auth_code = ft.Ref[ft.TextField]()
    auth_password = ft.Ref[ft.TextField]()
    auth_status = ft.Ref[ft.Text]()
    
    # Layout panels
    content_lv = ft.ListView(expand=True, spacing=12, padding=20)
    auth_panel = ft.Container(expand=True, bgcolor="#121214")
    main_layout = ft.Row(expand=True, spacing=0)
    
    async def paste_single(e):
        try:
            val = await get_clipboard_text()
            logger.debug(f"paste_single clicked, clipboard text: '{val}'")
            if val:
                single_link_input.current.value = val
                single_link_input.current.update()
        except Exception as ex:
            logger.error(f"Error in paste_single: {ex}")

    async def paste_range_start(e):
        try:
            val = await get_clipboard_text()
            logger.debug(f"paste_range_start clicked, clipboard text: '{val}'")
            if val:
                range_start_link.current.value = val
                range_start_link.current.update()
        except Exception as ex:
            logger.error(f"Error in paste_range_start: {ex}")

    async def paste_range_end(e):
        try:
            val = await get_clipboard_text()
            logger.debug(f"paste_range_end clicked, clipboard text: '{val}'")
            if val:
                range_end_link.current.value = val
                range_end_link.current.update()
        except Exception as ex:
            logger.error(f"Error in paste_range_end: {ex}")

    async def paste_full_chat(e):
        try:
            val = await get_clipboard_text()
            logger.debug(f"paste_full_chat clicked, clipboard text: '{val}'")
            if val:
                full_chat_input.current.value = val
                full_chat_input.current.update()
        except Exception as ex:
            logger.error(f"Error in paste_full_chat: {ex}")
            
    async def paste_scheduler_link(e):
        try:
            val = await get_clipboard_text()
            logger.debug(f"paste_scheduler_link clicked, clipboard text: '{val}'")
            if val:
                scheduler_input_link.current.value = val
                scheduler_input_link.current.update()
        except Exception as ex:
            logger.error(f"Error in paste_scheduler_link: {ex}")


    
    # Telethon Login State
    phone_code_hash = None
    sent_code_obj = None

    # Global dictionary to track speed and elapsed time
    download_tracker = {
        "current_file": None,
        "start_time": None,
        "last_received": 0,
        "last_update_time": None,
        "speed": 0.0,
    }

    def format_size(bytes_val):
        if bytes_val is None:
            return "Unknown"
        bytes_val = float(bytes_val)
        for unit in ['B', 'KB', 'MB', 'GB']:
            if bytes_val < 1024.0:
                return f"{bytes_val:.1f} {unit}"
            bytes_val /= 1024.0
        return f"{bytes_val:.1f} TB"

    def update_progress_status(status_text, color=None, progress_val=0.0, show_cancel=False):
        if active_download_status.current:
            active_download_status.current.value = status_text
            if color:
                active_download_status.current.color = color
            else:
                active_download_status.current.color = "#9CA3AF"
        if active_download_progress.current:
            active_download_progress.current.value = progress_val
        if active_download_details.current:
            active_download_details.current.value = ""
        if active_download_speed.current:
            active_download_speed.current.value = ""
        if active_download_cancel_btn.current:
            active_download_cancel_btn.current.visible = show_cancel
        page.update()

    def handle_cancel_download(e=None):
        logger.info("User requested to cancel current download.")
        TqdmDownloadProgress.is_canceled = True
        update_progress_status("Download canceling...", "amber", None, show_cancel=False)

    # Wire the global downloader callback hook to update Flet's UI progress bar
    def on_download_progress(filename, received, total):
        logger.debug(f"Progress Callback: {filename} -> {received}/{total}")
        if not active_download_progress.current or not active_download_status.current:
            return
            
        now = time.time()
        if download_tracker["current_file"] != filename:
            download_tracker["current_file"] = filename
            download_tracker["start_time"] = now
            download_tracker["last_received"] = received
            download_tracker["last_update_time"] = now
            download_tracker["speed"] = 0.0
            
        elapsed = now - download_tracker["start_time"]
        
        # Calculate instant speed every 0.5 seconds
        time_diff = now - download_tracker["last_update_time"]
        if time_diff >= 0.5:
            bytes_diff = received - download_tracker["last_received"]
            download_tracker["speed"] = bytes_diff / time_diff
            download_tracker["last_received"] = received
            download_tracker["last_update_time"] = now
            
        current_speed = download_tracker["speed"]
        if current_speed <= 0 and elapsed > 0:
            current_speed = received / elapsed
            
        formatted_received = format_size(received)
        formatted_total = format_size(total) if total else "Unknown Size"
        formatted_speed = format_size(current_speed) + "/s" if current_speed > 0 else "0.0 B/s"
        
        if total:
            percent = received / total
            active_download_progress.current.value = percent
            active_download_status.current.value = f"Downloading: {filename}"
            active_download_status.current.color = "#EC4899"  # vibrant color for active downloading
            if active_download_details.current:
                active_download_details.current.value = f"{formatted_received} / {formatted_total} ({percent:.1%})"
        else:
            active_download_progress.current.value = None  # Indeterminate progress
            active_download_status.current.value = f"Downloading: {filename}"
            active_download_status.current.color = "#EC4899"
            if active_download_details.current:
                active_download_details.current.value = f"{formatted_received} (Stream)"
                
        if active_download_speed.current:
            active_download_speed.current.value = formatted_speed
            
        if active_download_cancel_btn.current and not active_download_cancel_btn.current.visible:
            active_download_cancel_btn.current.visible = True
            
        page.update()

    TqdmDownloadProgress.gui_callback = on_download_progress

    # ----------------------------------------------------
    # TAB 1: Core Downloader Layout
    # ----------------------------------------------------
    async def handle_single_download(e=None):
        logger.debug("Starting single download process...")
        TqdmDownloadProgress.is_canceled = False
        link = single_link_input.current.value.strip()
        if not link:
            logger.warning("Empty link submitted.")
            update_progress_status("Error: Link cannot be empty.", "red", 0.0, show_cancel=False)
            return
            
        update_progress_status("Resolving Telegram link...", "#A855F7", None, show_cancel=True)
        
        try:
            chat_id, message_id, _ = parse_telegram_link(link)
            if not chat_id or not message_id:
                raise ValueError("Invalid Telegram message link.")
                
            update_progress_status(f"Fetching message ID {message_id} from chat {chat_id}...", None, None, show_cancel=True)
            
            message = await client.get_messages(chat_id, ids=message_id)
            if not message or not message.media:
                raise ValueError("No media found in the resolved message link.")
                
            update_progress_status("Starting media download...", None, None, show_cancel=True)
            
            success, msg = await download_message_media(client, message, chat_id)
            if success:
                update_progress_status(f"Success: {msg}", "#10B981", 1.0, show_cancel=False)
            else:
                update_progress_status(f"Failed: {msg}", "red", 0.0, show_cancel=False)
                
        except Exception as err:
            logger.error(f"Error in handle_single_download: {traceback.format_exc()}")
            update_progress_status(f"Error: {str(err)}", "red", 0.0, show_cancel=False)

    async def handle_range_download(e=None):
        logger.debug("Starting range download process...")
        TqdmDownloadProgress.is_canceled = False
        start = range_start_link.current.value.strip()
        end = range_end_link.current.value.strip()
        if not start or not end:
            update_progress_status("Error: Both start and end links are required.", "red", 0.0, show_cancel=False)
            return
            
        update_progress_status("Initializing range download...", "#3B82F6", None, show_cancel=True)
        
        try:
            success = await download_range(client, start, end)
            if success:
                update_progress_status("Success: Range download completed!", "#10B981", 1.0, show_cancel=False)
            else:
                update_progress_status("Failed: Range download failed or was incomplete.", "red", 0.0, show_cancel=False)
        except Exception as err:
            logger.error(f"Error in handle_range_download: {traceback.format_exc()}")
            update_progress_status(f"Error: {str(err)}", "red", 0.0, show_cancel=False)

    async def handle_full_download(e=None):
        logger.debug("Starting full history download process...")
        TqdmDownloadProgress.is_canceled = False
        chat = full_chat_input.current.value.strip()
        limit_str = full_limit_input.current.value.strip()
        if not chat:
            update_progress_status("Error: Chat username/link cannot be empty.", "red", 0.0, show_cancel=False)
            return
            
        try:
            limit = int(limit_str) if limit_str else 0
        except ValueError:
            limit = 0
            
        update_progress_status(f"Resolving chat history for '{chat}'...", "#10B981", None, show_cancel=True)
        
        try:
            success = await download_full(client, chat, limit=limit)
            if success:
                update_progress_status("Success: Full chat history download completed!", "#10B981", 1.0, show_cancel=False)
            else:
                update_progress_status("Failed: Full chat download failed or was incomplete.", "red", 0.0, show_cancel=False)
        except Exception as err:
            logger.error(f"Error in handle_full_download: {traceback.format_exc()}")
            update_progress_status(f"Error: {str(err)}", "red", 0.0, show_cancel=False)

    # ----------------------------------------------------
    # Scheduler & Queue Manager Helper Functions
    # ----------------------------------------------------
    def refresh_queue_table():
        if not scheduler_queue_table.current:
            return
        
        try:
            queue = load_queue()
            pending = [j for j in queue if j.get("status") == "pending"]
            
            rows = []
            for job in pending:
                t = job["type"].upper()
                added = job.get("added_at", "-")
                if job["type"] == "single":
                    desc = job["payload"].get("link", "")
                    mode_str = "Single Message"
                elif job["type"] == "range":
                    desc = f"{job['payload'].get('start_link', '')} ➜ {job['payload'].get('end_link', '')}"
                    mode_str = "Range Download"
                else:
                    desc = f"{job['payload'].get('chat_input', '')} (limit: {job['payload'].get('limit')})"
                    mode_str = "Full Chat History"
                
                # Truncate long descriptions
                if len(desc) > 50:
                    desc = desc[:47] + "..."
                    
                job_id = job["id"]
                rows.append(
                    ft.DataRow(
                        cells=[
                            ft.DataCell(ft.Container(ft.Text(desc, color="#E5E7EB", size=13), width=380)),
                            ft.DataCell(ft.Container(ft.Text(mode_str, color="#3B82F6", size=13), width=150)),
                            ft.DataCell(ft.Container(ft.Text(added, color="#9CA3AF", size=13), width=180)),
                            ft.DataCell(ft.Container(ft.Text(f"Pending", color="#F59E0B", size=13), width=100)),
                            ft.DataCell(
                                ft.Container(
                                    ft.IconButton(
                                        icon=ft.Icons.DELETE_OUTLINE,
                                        icon_color="red",
                                        icon_size=18,
                                        tooltip="Remove from Queue",
                                        on_click=lambda e, j_id=job_id: handle_delete_queue_job(j_id)
                                    ),
                                    width=80,
                                    alignment=ft.alignment.Alignment(0, 0)
                                )
                            )
                        ]
                    )
                )
                
            if not rows:
                rows.append(
                    ft.DataRow(
                        cells=[
                            ft.DataCell(ft.Container(ft.Text("No pending jobs in queue.", color="#9CA3AF", italic=True), width=380)),
                            ft.DataCell(ft.Container(ft.Text("-"), width=150)),
                            ft.DataCell(ft.Container(ft.Text("-"), width=180)),
                            ft.DataCell(ft.Container(ft.Text("-"), width=100)),
                            ft.DataCell(ft.Container(ft.Text("-"), width=80, alignment=ft.alignment.Alignment(0, 0)))
                        ]
                    )
                )
                
            scheduler_queue_table.current.rows = rows
            try:
                scheduler_queue_table.current.update()
            except RuntimeError:
                pass
        except Exception as ex:
            logger.error(f"Error in refresh_queue_table: {traceback.format_exc()}")

    def handle_delete_queue_job(job_id):
        logger.debug(f"Deleting job {job_id} from queue...")
        try:
            queue = load_queue()
            queue = [j for j in queue if j["id"] != job_id]
            save_queue(queue)
            refresh_queue_table()
        except Exception as ex:
            logger.error(f"Error in handle_delete_queue_job: {ex}")

    def handle_scheduler_mode_change(e):
        nonlocal scheduler_range_start_temp
        scheduler_range_start_temp = None  # Reset temporary start link
        mode = scheduler_job_mode.current.value
        
        if mode == "range":
            scheduler_input_link.current.label = "Start Message Link (First link)"
            scheduler_input_link.current.hint_text = "https://t.me/c/123456/10"
            if scheduler_add_btn.current:
                scheduler_add_btn.current.text = "Next ➜"
                scheduler_add_btn.current.icon = ft.Icons.ARROW_FORWARD
        else:
            scheduler_input_link.current.label = "Link / Chat Username / ID"
            scheduler_input_link.current.hint_text = "https://t.me/c/123456/10 or @username"
            if scheduler_add_btn.current:
                scheduler_add_btn.current.text = "Add to Queue"
                scheduler_add_btn.current.icon = ft.Icons.ADD
            
        scheduler_input_link.current.value = ""
        scheduler_input_link.current.update()
        if scheduler_add_btn.current:
            scheduler_add_btn.current.update()

    def handle_add_to_queue(e):
        nonlocal scheduler_range_start_temp
        mode = scheduler_job_mode.current.value
        input_val = scheduler_input_link.current.value.strip()
        
        if not input_val:
            update_progress_status("Error: Input link/chat username is required.", "red", 0.0, show_cancel=False)
            return
            
        if mode == "range":
            if scheduler_range_start_temp is None:
                # This is the first link step
                scheduler_range_start_temp = input_val
                
                # Update UI for second step
                scheduler_input_link.current.label = "End Message Link (Last link)"
                scheduler_input_link.current.hint_text = "https://t.me/c/123456/20"
                scheduler_input_link.current.value = ""
                scheduler_input_link.current.update()
                
                if scheduler_add_btn.current:
                    scheduler_add_btn.current.text = "Add to Queue"
                    scheduler_add_btn.current.icon = ft.Icons.ADD
                    scheduler_add_btn.current.update()
                return
            else:
                # This is the second link step
                start_val = scheduler_range_start_temp
                end_val = input_val
                
                payload = {
                    "start_link": start_val,
                    "end_link": end_val
                }
                new_job = {
                    "id": f"job_{int(time.time())}_range",
                    "type": "range",
                    "payload": payload,
                    "status": "pending",
                    "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                
                try:
                    queue = load_queue()
                    queue.append(new_job)
                    save_queue(queue)
                    refresh_queue_table()
                    
                    # Reset flow state
                    scheduler_range_start_temp = None
                    scheduler_input_link.current.label = "Start Message Link (First link)"
                    scheduler_input_link.current.hint_text = "https://t.me/c/123456/10"
                    scheduler_input_link.current.value = ""
                    scheduler_input_link.current.update()
                    
                    if scheduler_add_btn.current:
                        scheduler_add_btn.current.text = "Next ➜"
                        scheduler_add_btn.current.icon = ft.Icons.ARROW_FORWARD
                        scheduler_add_btn.current.update()
                    
                    update_progress_status("Range job added to download queue.", "#10B981", 1.0, show_cancel=False)
                except Exception as ex:
                    logger.error(f"Error saving range queue job: {ex}")
                    update_progress_status(f"Error: {ex}", "red", 0.0, show_cancel=False)
        else:
            # Single or Full Download mode
            payload = {}
            if "t.me/" in input_val and not input_val.endswith("?single") and any(char.isdigit() for char in input_val.split("/")[-1]):
                payload["link"] = input_val
                type_key = "single"
            else:
                payload["chat_input"] = input_val
                payload["limit"] = 0
                type_key = "full"
                
            try:
                queue = load_queue()
                new_job = {
                    "id": f"job_{int(time.time())}_{type_key}",
                    "type": type_key,
                    "payload": payload,
                    "status": "pending",
                    "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                queue.append(new_job)
                save_queue(queue)
                
                # Clear field
                scheduler_input_link.current.value = ""
                scheduler_input_link.current.update()
                
                refresh_queue_table()
                update_progress_status("Job added to download queue.", "#10B981", 1.0, show_cancel=False)
            except Exception as ex:
                logger.error(f"Error in handle_add_to_queue: {ex}")
                update_progress_status(f"Error: {ex}", "red", 0.0, show_cancel=False)

    def handle_process_queue(e):
        page.run_task(process_gui_queue)

    async def process_gui_queue(client_instance=None):
        logger.info("Starting GUI queue processing task...")
        TqdmDownloadProgress.is_canceled = False
        queue = load_queue()
        pending = [j for j in queue if j.get("status") == "pending"]
        
        if not pending:
            update_progress_status("No pending jobs in the queue to process.", "#F59E0B", 0.0, show_cancel=False)
            return
            
        update_progress_status(f"Starting processing of {len(pending)} jobs...", "#3B82F6", None, show_cancel=True)
        
        for idx, job in enumerate(pending, start=1):
            if TqdmDownloadProgress.is_canceled:
                update_progress_status("Queue processing canceled by user.", "red", 0.0, show_cancel=False)
                break
                
            job_desc = job["type"].upper()
            update_progress_status(f"Running Job {idx}/{len(pending)}: {job_desc}...", "#A855F7", None, show_cancel=True)
            
            success = False
            try:
                if job["type"] == "single":
                    chat_id, message_id, _ = parse_telegram_link(job["payload"]["link"])
                    if chat_id and message_id:
                        message = await client.get_messages(chat_id, ids=message_id)
                        if message and message.media:
                            success, msg = await download_message_media(client, message, chat_id)
                elif job["type"] == "range":
                    success = await download_range(client, job["payload"]["start_link"], job["payload"]["end_link"])
                elif job["type"] == "full":
                    success = await download_full(client, job["payload"]["chat_input"], limit=job["payload"].get("limit", 0))
            except Exception as e:
                logger.error(f"Error processing job {job['id']}: {traceback.format_exc()}")
                
            # Update job status
            queue = load_queue()
            for q_job in queue:
                if q_job["id"] == job["id"]:
                    q_job["status"] = "completed" if success else "failed"
                    q_job["processed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            save_queue(queue)
            
            refresh_queue_table()
            
        if not TqdmDownloadProgress.is_canceled:
            update_progress_status("Queue processing completed successfully!", "#10B981", 1.0, show_cancel=False)

    def handle_toggle_scheduler(e):
        nonlocal scheduler_armed, gui_scheduler_task
        if not scheduler_armed:
            time_str = scheduler_start_time.current.value.strip()
            if not time_str:
                update_progress_status("Error: Please set a start time.", "red", 0.0, show_cancel=False)
                return
            try:
                parts = time_str.split(":")
                h = int(parts[0])
                m = int(parts[1])
                s = int(parts[2]) if len(parts) > 2 else 0
                if not (0 <= h <= 23) or not (0 <= m <= 59) or not (0 <= s <= 59):
                    raise ValueError()
            except Exception:
                update_progress_status("Error: Invalid time format. Use HH:MM or HH:MM:SS.", "red", 0.0, show_cancel=False)
                return
                
            scheduler_armed = True
            scheduler_enable_btn.current.text = "Cancel Scheduler"
            scheduler_enable_btn.current.bgcolor = "red"
            scheduler_enable_btn.current.icon = ft.Icons.CANCEL
            scheduler_start_time.current.disabled = True
            
            scheduler_enable_btn.current.update()
            scheduler_start_time.current.update()
            
            gui_scheduler_task = page.run_task(background_gui_scheduler_loop, h, m, s)
            update_progress_status(f"Scheduler armed for {h:02d}:{m:02d}:{s:02d}.", "#3B82F6", 0.0, show_cancel=False)
        else:
            scheduler_armed = False
            if gui_scheduler_task:
                gui_scheduler_task.cancel()
                gui_scheduler_task = None
                
            scheduler_enable_btn.current.text = "Enable Scheduler"
            scheduler_enable_btn.current.bgcolor = "#3B82F6"
            scheduler_enable_btn.current.icon = ft.Icons.SCHEDULE
            scheduler_start_time.current.disabled = False
            scheduler_status_text.current.value = "Scheduler: Idle"
            scheduler_status_text.current.color = "#9CA3AF"
            
            scheduler_enable_btn.current.update()
            scheduler_start_time.current.update()
            scheduler_status_text.current.update()
            update_progress_status("Scheduler disabled.", "#9CA3AF", 0.0, show_cancel=False)

    async def background_gui_scheduler_loop(target_h, target_m, target_s):
        nonlocal scheduler_armed, gui_scheduler_task
        logger.info(f"Armed GUI scheduler loop for {target_h:02d}:{target_m:02d}:{target_s:02d}...")
        try:
            from datetime import timedelta
            while scheduler_armed:
                now = datetime.now()
                target_time = now.replace(hour=target_h, minute=target_m, second=target_s, microsecond=0)
                if target_time < now:
                    target_time = target_time + timedelta(days=1)
                    
                delta = target_time - now
                seconds_left = int(delta.total_seconds())
                
                if seconds_left <= 0:
                    logger.info("Scheduled target time reached! Triggering queue download...")
                    scheduler_status_text.current.value = "Executing scheduled queue..."
                    scheduler_status_text.current.color = "#10B981"
                    scheduler_status_text.current.update()
                    
                    await process_gui_queue(client)
                    
                    scheduler_armed = False
                    gui_scheduler_task = None
                    scheduler_enable_btn.current.text = "Enable Scheduler"
                    scheduler_enable_btn.current.bgcolor = "#3B82F6"
                    scheduler_enable_btn.current.icon = ft.Icons.SCHEDULE
                    scheduler_start_time.current.disabled = False
                    scheduler_status_text.current.value = "Scheduler: Idle"
                    scheduler_status_text.current.color = "#9CA3AF"
                    
                    scheduler_enable_btn.current.update()
                    scheduler_start_time.current.update()
                    scheduler_status_text.current.update()
                    break
                    
                h_left, remainder = divmod(seconds_left, 3600)
                m_left, s_left = divmod(remainder, 60)
                countdown_str = f"Armed: ⌛ {h_left:02d}:{m_left:02d}:{s_left:02d} remaining..."
                
                scheduler_status_text.current.value = countdown_str
                scheduler_status_text.current.color = "#3B82F6"
                scheduler_status_text.current.update()
                
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            logger.info("Armed GUI scheduler loop cancelled.")
        except Exception as e:
            logger.error(f"Error in GUI scheduler loop: {traceback.format_exc()}")

    def log_to_monitor_feed(text):
        if monitor_log_field.current:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # If current value is default mock text, overwrite it
            if "Live daemon monitoring is currently supported" in monitor_log_field.current.value:
                monitor_log_field.current.value = f"[{timestamp}] {text}"
            else:
                monitor_log_field.current.value += f"\n[{timestamp}] {text}"
            
            # Keep log text limited to last 200 lines
            lines = monitor_log_field.current.value.split("\n")
            if len(lines) > 200:
                monitor_log_field.current.value = "\n".join(lines[-200:])
            monitor_log_field.current.update()

    def handle_toggle_monitor(e):
        nonlocal monitor_handler, monitor_active
        is_active = monitor_switch.current.value
        
        if is_active:
            # Start Live Monitoring
            config = get_config()
            monitored = config.get("monitored_chats", [])
            
            if not monitored:
                monitor_switch.current.value = False
                monitor_switch.current.update()
                
                def close_dialog(ev):
                    page.dialog.open = False
                    page.update()
                    
                page.dialog = ft.AlertDialog(
                    title=ft.Text("No Chats Monitored"),
                    content=ft.Text("Please add at least one channel, group, or bot username/ID to monitor in the Settings tab first."),
                    actions=[ft.TextButton("OK", on_click=close_dialog)]
                )
                page.dialog.open = True
                page.update()
                return
                
            chat_ids = []
            for c in monitored:
                if isinstance(c, dict):
                    chat_ids.append(c["id"])
                else:
                    chat_ids.append(c)
                    
            log_to_monitor_feed(f"[SYSTEM] Starting Live Monitor Daemon for {len(chat_ids)} chats...")
            
            async def gui_new_message_callback(event):
                if not monitor_active:
                    return
                if not event.message.media:
                    return
                current_task = asyncio.current_task()
                if current_task:
                    active_gui_monitor_tasks.add(current_task)
                try:
                    chat = await event.get_chat()
                    chat_title = getattr(chat, 'title', None)
                    if not chat_title:
                        first_name = getattr(chat, 'first_name', None)
                        last_name = getattr(chat, 'last_name', None)
                        if first_name:
                            chat_title = f"{first_name} {last_name}".strip() if last_name else first_name
                        else:
                            chat_title = getattr(chat, 'username', None)
                    if not chat_title:
                        chat_title = "Unknown Chat"
                        
                    message = event.message
                    log_to_monitor_feed(f"[NEW MEDIA] {chat_title}: Found media in message {message.id}")
                    
                    success, result = await download_message_media(client, message, event.chat_id, is_monitor=True)
                    if success:
                        if "already exists" in result or "already archived" in result:
                            log_to_monitor_feed(f"   ↳ 🔄 {result}")
                        elif "Filtered" in result:
                            log_to_monitor_feed(f"   ↳ ⏳ {result}")
                        else:
                            log_to_monitor_feed(f"   ↳ ✅ Downloaded: {os.path.basename(result)}")
                    else:
                        log_to_monitor_feed(f"   ↳ ❌ Failed: {result}")
                except asyncio.CancelledError:
                    log_to_monitor_feed(f"   ↳ ⏹️ Cancelled download for message {getattr(event.message, 'id', 'unknown')}")
                    raise
                except Exception as err:
                    log_to_monitor_feed(f"   ↳ ❌ Error: {str(err)}")
                finally:
                    if current_task:
                        active_gui_monitor_tasks.discard(current_task)
                    
            try:
                # Add Telethon event handler
                client.add_event_handler(gui_new_message_callback, events.NewMessage(chats=chat_ids))
                monitor_handler = gui_new_message_callback
                monitor_active = True
                
                # Update UI elements
                monitor_status_title.current.value = f"Daemon Status: Active ({len(chat_ids)} chats)"
                monitor_status_title.current.color = "#10B981"
                monitor_status_desc.current.value = "Daemon listening for incoming channel messages and auto-downloading files..."
                monitor_status_title.current.update()
                monitor_status_desc.current.update()
                
                log_to_monitor_feed("[SYSTEM] Daemon successfully registered. Listening...")
            except Exception as ex:
                logger.error(f"Error starting monitor daemon: {ex}")
                monitor_switch.current.value = False
                monitor_switch.current.update()
                log_to_monitor_feed(f"[ERROR] Failed to start daemon: {ex}")
        else:
            # Stop Live Monitoring
            monitor_active = False
            if monitor_handler:
                try:
                    client.remove_event_handler(monitor_handler)
                except Exception as ex:
                    logger.error(f"Error removing monitor event handler: {ex}")
                monitor_handler = None
            
            # Cancel any active in-flight download tasks
            if active_gui_monitor_tasks:
                cancel_count = len(active_gui_monitor_tasks)
                log_to_monitor_feed(f"[SYSTEM] Cancelling {cancel_count} active monitor download(s)...")
                for t in list(active_gui_monitor_tasks):
                    t.cancel()
                active_gui_monitor_tasks.clear()
            
            # Update UI elements
            monitor_status_title.current.value = "Daemon Status: Stopped"
            monitor_status_title.current.color = "#9CA3AF"
            monitor_status_desc.current.value = "Start the monitor daemon to begin watching target channels."
            monitor_status_title.current.update()
            monitor_status_desc.current.update()
            
            log_to_monitor_feed("[SYSTEM] Live Monitor Daemon stopped.")

    async def handle_monitor_add_chat(e=None):
        logger.debug("Starting monitor tab add chat...")
        if not monitor_add_input.current:
            return
        chat_input = monitor_add_input.current.value.strip()
        if not chat_input:
            return
            
        log_to_monitor_feed(f"[SYSTEM] Resolving chat entity: {chat_input}...")
        
        try:
            entity = await resolve_chat_entity(client, chat_input)
            entity_id = entity.id
            
            # Resolve the title robustly
            title = getattr(entity, 'title', None)
            if not title:
                first_name = getattr(entity, 'first_name', None)
                last_name = getattr(entity, 'last_name', None)
                if first_name:
                    title = f"{first_name} {last_name}".strip() if last_name else first_name
                else:
                    title = getattr(entity, 'username', None)
            if not title:
                title = str(entity_id)
                
            # Append type label
            from telethon.tl.types import Channel, Chat, User
            label = "Chat"
            if isinstance(entity, User):
                label = "Bot" if entity.bot else "User"
            elif isinstance(entity, Chat):
                label = "Group"
            elif isinstance(entity, Channel):
                label = "Group" if getattr(entity, 'megagroup', False) else "Channel"
                
            title = f"{title} ({label})"
            
            identifier = getattr(entity, 'username', None)
            if not identifier:
                identifier = entity_id
                
            config_data = get_config()
            monitored = config_data.get("monitored_chats", [])
            
            # Check for duplicates
            is_dup = False
            for c in monitored:
                c_id = c.get('id') if isinstance(c, dict) else c
                if str(c_id) == str(identifier):
                    is_dup = True
                    break
                    
            if is_dup:
                log_to_monitor_feed(f"[SYSTEM] '{title}' is already in your monitor list.")
            else:
                monitored.append({"id": identifier, "title": title})
                config_data["monitored_chats"] = monitored
                save_config(config_data)
                
                log_to_monitor_feed(f"[SYSTEM] Successfully added '{title}' to monitor list.")
                
                # Clear input field
                monitor_add_input.current.value = ""
                
                # Re-render monitor view
                content_lv.controls = build_monitor_view()
                page.update()
                
        except Exception as ex:
            logger.error(f"Error resolving chat in monitor tab: {ex}")
            log_to_monitor_feed(f"[SYSTEM] [ERROR] Failed to resolve chat: {ex}")

    def handle_remove_monitored_chat_from_monitor_view(chat_id):
        logger.debug(f"Removing monitored chat from monitor view: {chat_id}")
        config_data = get_config()
        monitored = config_data.get("monitored_chats", [])
        
        # Remove matching ID/username
        for val in list(monitored):
            match = False
            if isinstance(val, dict):
                if str(val.get("id")) == str(chat_id):
                    match = True
            else:
                if str(val) == str(chat_id):
                    match = True
            if match:
                monitored.remove(val)
                break
                
        config_data["monitored_chats"] = monitored
        save_config(config_data)
        
        log_to_monitor_feed(f"[SYSTEM] Removed chat from monitor list: {chat_id}")
        
        # Re-render monitor view
        content_lv.controls = build_monitor_view()
        page.update()

    def build_downloader_view():
        logger.debug("Building build_downloader_view controls...")
        return [
                ft.Text("Media Downloader", theme_style=ft.TextThemeStyle.HEADLINE_MEDIUM, weight=ft.FontWeight.BOLD, color="#A855F7"),
                ft.Text("Download single files, message ranges, or complete chat histories directly.", color="#9CA3AF", size=13),
                
                ft.Row(
                    controls=[
                        # Range Download Card
                        ft.Container(
                            content=ft.Column([
                                ft.Row([ft.Icon(ft.Icons.FORMAT_LIST_NUMBERED, color="#3B82F6"), ft.Text("Range Download", weight=ft.FontWeight.BOLD, size=16)]),
                                ft.TextField(
                                    ref=range_start_link,
                                    label="Start Message Link",
                                    hint_text="https://t.me/c/123456/10",
                                    border_color="#374151",
                                    border_radius=8,
                                    filled=True,
                                    bgcolor="#24242B",
                                    focused_border_color="#3B82F6",
                                    label_style=ft.TextStyle(color="#6B7280"),
                                    hint_style=ft.TextStyle(color="#4B5563"),
                                    color="#E5E7EB",
                                    content_padding=ft.Padding(left=12, right=12, top=14, bottom=14),
                                    on_focus=on_focus_field,
                                    on_blur=on_blur_field,
                                    suffix=ft.IconButton(
                                        icon=ft.Icons.CONTENT_PASTE,
                                        icon_size=16,
                                        icon_color="#3B82F6",
                                        tooltip="Paste from Clipboard",
                                        on_click=paste_range_start
                                    )
                                ),
                                ft.TextField(
                                    ref=range_end_link,
                                    label="End Message Link",
                                    hint_text="https://t.me/c/123456/20",
                                    border_color="#374151",
                                    border_radius=8,
                                    filled=True,
                                    bgcolor="#24242B",
                                    focused_border_color="#3B82F6",
                                    label_style=ft.TextStyle(color="#6B7280"),
                                    hint_style=ft.TextStyle(color="#4B5563"),
                                    color="#E5E7EB",
                                    content_padding=ft.Padding(left=12, right=12, top=14, bottom=14),
                                    on_focus=on_focus_field,
                                    on_blur=on_blur_field,
                                    suffix=ft.IconButton(
                                        icon=ft.Icons.CONTENT_PASTE,
                                        icon_size=16,
                                        icon_color="#3B82F6",
                                        tooltip="Paste from Clipboard",
                                        on_click=paste_range_end
                                    )
                                ),
                                ft.FilledButton(
                                    "Download Range",
                                    icon=ft.Icons.PLAY_ARROW,
                                    bgcolor="#3B82F6",
                                    color="white",
                                    style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
                                    on_click=lambda e: page.run_task(handle_range_download)
                                )
                            ], spacing=12, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                            bgcolor="#1A1A1E",
                            padding=18,
                            border_radius=12,
                            border=ft.Border.all(1, "#374151"),
                            expand=True,
                            height=270
                        ),
                        
                        # Full Chat History Download Card
                        ft.Container(
                            content=ft.Column([
                                ft.Row([ft.Icon(ft.Icons.ALL_INBOX, color="#10B981"), ft.Text("Full Chat History Download", weight=ft.FontWeight.BOLD, size=16)]),
                                ft.TextField(
                                    ref=full_chat_input,
                                    label="Chat ID / Username / Link",
                                    hint_text="Enter chat username or private link",
                                    border_color="#374151",
                                    border_radius=8,
                                    filled=True,
                                    bgcolor="#24242B",
                                    focused_border_color="#10B981",
                                    label_style=ft.TextStyle(color="#6B7280"),
                                    hint_style=ft.TextStyle(color="#4B5563"),
                                    color="#E5E7EB",
                                    content_padding=ft.Padding(left=12, right=12, top=14, bottom=14),
                                    on_focus=on_focus_field,
                                    on_blur=on_blur_field,
                                    suffix=ft.IconButton(
                                        icon=ft.Icons.CONTENT_PASTE,
                                        icon_size=16,
                                        icon_color="#10B981",
                                        tooltip="Paste from Clipboard",
                                        on_click=paste_full_chat
                                    )
                                ),
                                ft.TextField(
                                    ref=full_limit_input,
                                    label="Limit (0 for all)",
                                    value="0",
                                    border_color="#374151",
                                    border_radius=8,
                                    filled=True,
                                    bgcolor="#24242B",
                                    focused_border_color="#10B981",
                                    label_style=ft.TextStyle(color="#6B7280"),
                                    hint_style=ft.TextStyle(color="#4B5563"),
                                    color="#E5E7EB",
                                    content_padding=ft.Padding(left=12, right=12, top=14, bottom=14),
                                    on_focus=on_focus_field,
                                    on_blur=on_blur_field
                                ),
                                ft.FilledButton(
                                    "Download Full History",
                                    icon=ft.Icons.PLAY_ARROW,
                                    bgcolor="#10B981",
                                    color="white",
                                    style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
                                    on_click=lambda e: page.run_task(handle_full_download)
                                )
                            ], spacing=12, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                            bgcolor="#1A1A1E",
                            padding=18,
                            border_radius=12,
                            border=ft.Border.all(1, "#374151"),
                            expand=True,
                            height=270
                        ),
                    ],
                    spacing=20
                ),
                
                # Row 2: Single Download Card (Full width horizontal layout)
                ft.Container(
                    content=ft.Column([
                        ft.Row([ft.Icon(ft.Icons.DOWNLOAD, color="#A855F7"), ft.Text("Single Download", weight=ft.FontWeight.BOLD, size=16)]),
                        ft.Row([
                            ft.TextField(
                                ref=single_link_input,
                                label="Telegram Link / Message Link",
                                hint_text="https://t.me/c/123456/789",
                                border_color="#374151",
                                border_radius=8,
                                filled=True,
                                bgcolor="#24242B",
                                focused_border_color="#A855F7",
                                label_style=ft.TextStyle(color="#6B7280"),
                                hint_style=ft.TextStyle(color="#4B5563"),
                                color="#E5E7EB",
                                content_padding=ft.Padding(left=12, right=12, top=14, bottom=14),
                                expand=True,
                                on_focus=on_focus_field,
                                on_blur=on_blur_field,
                                suffix=ft.IconButton(
                                    icon=ft.Icons.CONTENT_PASTE,
                                    icon_size=16,
                                    icon_color="#A855F7",
                                    tooltip="Paste from Clipboard",
                                    on_click=paste_single
                                )
                            ),
                            ft.FilledButton(
                                "Download Link",
                                icon=ft.Icons.PLAY_ARROW,
                                bgcolor="#A855F7",
                                color="white",
                                style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
                                on_click=lambda e: page.run_task(handle_single_download)
                            )
                        ], spacing=15, vertical_alignment=ft.CrossAxisAlignment.CENTER)
                    ], spacing=12),
                    bgcolor="#1A1A1E",
                    padding=18,
                    border_radius=12,
                    border=ft.Border.all(1, "#374151")
                ),
                
                # Active download status bar
                ft.Container(
                    content=ft.Column([
                        ft.Row([
                            ft.Icon(ft.Icons.DOWNLOAD_ROUNDED, color="#EC4899", size=18),
                            ft.Text("Ready", ref=active_download_status, color="#9CA3AF", weight=ft.FontWeight.BOLD, size=14, expand=True),
                            ft.IconButton(
                                ref=active_download_cancel_btn,
                                icon=ft.Icons.CANCEL_ROUNDED,
                                icon_color="red",
                                tooltip="Cancel Download",
                                on_click=handle_cancel_download,
                                icon_size=20,
                                visible=False
                            )
                        ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        ft.ProgressBar(ref=active_download_progress, value=0.0, color="#EC4899", bgcolor="#2D2D35", height=6, border_radius=3),
                        ft.Row([
                            ft.Text("", ref=active_download_details, color="#9CA3AF", size=12),
                            ft.Text("", ref=active_download_speed, color="#10B981", weight=ft.FontWeight.BOLD, size=12)
                        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
                    ], spacing=8),
                    bgcolor="#1A1A1E",
                    padding=ft.Padding(left=18, right=18, top=12, bottom=12),
                    border_radius=10,
                    border=ft.Border.all(1, "#1F2937")
                )
        ]

    # ----------------------------------------------------
    # TAB 2: Queue & Scheduler Layout
    # ----------------------------------------------------
    def build_scheduler_view():
        logger.debug("Building build_scheduler_view controls...")
        
        is_armed = scheduler_armed
        btn_text = "Cancel Scheduler" if is_armed else "Enable Scheduler"
        btn_color = "red" if is_armed else "#3B82F6"
        btn_icon = ft.Icons.CANCEL if is_armed else ft.Icons.SCHEDULE
        status_val = scheduler_status_text.current.value if (scheduler_status_text.current and scheduler_status_text.current.value) else "Scheduler: Idle"
        status_color = scheduler_status_text.current.color if (scheduler_status_text.current and scheduler_status_text.current.color) else "#9CA3AF"
        
        job_mode_dd = ft.Dropdown(
            ref=scheduler_job_mode,
            value="single",
            options=[
                ft.dropdown.Option("single", "Single / Full Chat"),
                ft.dropdown.Option("range", "Range Download")
            ],
            width=180,
            border_color="#374151",
            bgcolor="#24242B",
            color="#E5E7EB"
        )
        job_mode_dd.on_change = handle_scheduler_mode_change
        
        return [
            ft.Text("Scheduled Queue Manager", theme_style=ft.TextThemeStyle.HEADLINE_MEDIUM, weight=ft.FontWeight.BOLD, color="#EC4899"),
            ft.Text("Queue multiple chat archives and schedule them to run at a specific time in the background.", color="#9CA3AF", size=13),
            
            # Interactive Input Card
            ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Icon(ft.Icons.ADD_TASK, color="#EC4899"),
                        ft.Text("Add Job to Download Queue", weight=ft.FontWeight.BOLD, size=16)
                    ]),
                    ft.Row([
                        job_mode_dd,
                        ft.TextField(
                            ref=scheduler_input_link,
                            label="Link / Chat Username / ID",
                            hint_text="https://t.me/c/123456/10 or @username",
                            border_color="#374151",
                            border_radius=8,
                            filled=True,
                            bgcolor="#24242B",
                            focused_border_color="#EC4899",
                            label_style=ft.TextStyle(color="#6B7280"),
                            hint_style=ft.TextStyle(color="#4B5563"),
                            color="#E5E7EB",
                            content_padding=ft.Padding(left=12, right=12, top=14, bottom=14),
                            on_focus=on_focus_field,
                            on_blur=on_blur_field,
                            expand=True,
                            suffix=ft.IconButton(
                                icon=ft.Icons.CONTENT_PASTE,
                                icon_size=16,
                                icon_color="#EC4899",
                                tooltip="Paste from Clipboard",
                                on_click=paste_scheduler_link
                            )
                        ),
                        ft.FilledButton(
                            "Add to Queue",
                            ref=scheduler_add_btn,
                            bgcolor="#EC4899",
                            color="white",
                            icon=ft.Icons.ADD,
                            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
                            on_click=handle_add_to_queue
                        )
                    ], spacing=15, vertical_alignment=ft.CrossAxisAlignment.CENTER)
                ], spacing=12),
                bgcolor="#1A1A1E",
                padding=18,
                border_radius=12,
                border=ft.Border.all(1, "#374151")
            ),
            
            # Queue Table Card
            ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Icon(ft.Icons.LIST_ALT_ROUNDED, color="#EC4899"),
                        ft.Text("Pending Queue", weight=ft.FontWeight.BOLD, size=16),
                        ft.VerticalDivider(width=10),
                        ft.FilledButton(
                            "Process Queue Now",
                            icon=ft.Icons.PLAY_ARROW,
                            bgcolor="#10B981",
                            color="white",
                            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=6)),
                            on_click=handle_process_queue
                        )
                    ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    ft.Column([
                        ft.DataTable(
                            ref=scheduler_queue_table,
                            border=ft.Border.all(1, "#374151"),
                            horizontal_lines=ft.BorderSide(1, "#2D2D35"),
                            vertical_lines=ft.BorderSide(1, "#2D2D35"),
                            columns=[
                                ft.DataColumn(ft.Container(ft.Text("Target Link/Chat", weight=ft.FontWeight.BOLD), width=380)),
                                ft.DataColumn(ft.Container(ft.Text("Mode", weight=ft.FontWeight.BOLD), width=150)),
                                ft.DataColumn(ft.Container(ft.Text("Added At", weight=ft.FontWeight.BOLD), width=180)),
                                ft.DataColumn(ft.Container(ft.Text("Status", weight=ft.FontWeight.BOLD), width=100)),
                                ft.DataColumn(ft.Container(ft.Text("Actions", weight=ft.FontWeight.BOLD), width=80, alignment=ft.alignment.Alignment(0, 0))),
                            ],
                            rows=[
                                ft.DataRow(cells=[
                                    ft.DataCell(ft.Container(ft.Text("Loading queue jobs...", italic=True, color="#9CA3AF"), width=380)),
                                    ft.DataCell(ft.Container(ft.Text("-"), width=150)),
                                    ft.DataCell(ft.Container(ft.Text("-"), width=180)),
                                    ft.DataCell(ft.Container(ft.Text("-"), width=100)),
                                    ft.DataCell(ft.Container(ft.Text("-"), width=80, alignment=ft.alignment.Alignment(0, 0)))
                                ])
                            ],
                            column_spacing=10,
                            expand=True
                        )
                    ], scroll=ft.ScrollMode.AUTO, height=200, expand=True)
                ], spacing=12),
                bgcolor="#1A1A1E",
                padding=18,
                border_radius=12,
                border=ft.Border.all(1, "#374151")
            ),
            
            # Set Schedule Section
            ft.Container(
                content=ft.Row([
                    ft.Icon(ft.Icons.ALARM, color="#3B82F6"),
                    ft.Text("Set Download Start Time:", weight=ft.FontWeight.BOLD),
                    ft.TextField(
                        ref=scheduler_start_time,
                        label="Start Time (HH:MM:SS)",
                        value="23:30:00",
                        border_color="#374151",
                        border_radius=8,
                        filled=True,
                        bgcolor="#24242B",
                        focused_border_color="#3B82F6",
                        label_style=ft.TextStyle(color="#6B7280"),
                        color="#E5E7EB",
                        width=180,
                        content_padding=ft.Padding(left=12, right=12, top=14, bottom=14),
                        disabled=is_armed,
                        suffix=ft.IconButton(
                            icon=ft.Icons.ACCESS_TIME,
                            icon_size=16,
                            icon_color="#3B82F6",
                            tooltip="Select Time Picker",
                            on_click=lambda e: page.show_dialog(scheduler_time_picker)
                        )
                    ),
                    ft.FilledButton(
                        btn_text,
                        ref=scheduler_enable_btn,
                        bgcolor=btn_color,
                        color="white",
                        icon=btn_icon,
                        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
                        on_click=handle_toggle_scheduler
                    ),
                    ft.VerticalDivider(width=20),
                    ft.Text(
                        ref=scheduler_status_text,
                        value=status_val,
                        color=status_color,
                        weight=ft.FontWeight.BOLD,
                        size=14
                    )
                ], spacing=15, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                bgcolor="#1A1A1E",
                padding=15,
                border_radius=12,
                border=ft.Border.all(1, "#374151")
            ),
            
            # Progress Card (aligned and matching downloader tab)
            ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.Row([
                            ft.Icon(ft.Icons.DOWNLOAD_ROUNDED, color="#EC4899", size=18),
                            ft.Text("Ready", ref=active_download_status, color="#9CA3AF", weight=ft.FontWeight.BOLD, size=14, expand=True),
                            ft.IconButton(
                                ref=active_download_cancel_btn,
                                icon=ft.Icons.CANCEL_ROUNDED,
                                icon_color="red",
                                tooltip="Cancel Download",
                                on_click=handle_cancel_download,
                                icon_size=20,
                                visible=False
                            )
                        ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER, expand=True)
                    ]),
                    ft.ProgressBar(ref=active_download_progress, value=0.0, color="#EC4899", bgcolor="#2D2D35", height=6, border_radius=3),
                    ft.Row([
                        ft.Text("", ref=active_download_details, color="#9CA3AF", size=12),
                        ft.Text("", ref=active_download_speed, color="#10B981", weight=ft.FontWeight.BOLD, size=12)
                    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
                ], spacing=8),
                bgcolor="#1A1A1E",
                padding=ft.Padding(left=18, right=18, top=12, bottom=12),
                border_radius=10,
                border=ft.Border.all(1, "#1F2937")
            )
        ]

    # ----------------------------------------------------
    # TAB 3: Live Monitor Layout
    # ----------------------------------------------------
    def build_monitor_view():
        logger.debug("Building build_monitor_view controls...")
        
        # Read monitored chats from config
        config = get_config()
        monitored = config.get("monitored_chats", [])
        
        chips = []
        for c in monitored:
            c_id = c.get("id") if isinstance(c, dict) else c
            title = c.get("title") if isinstance(c, dict) else str(c)
            chips.append(
                ft.Container(
                    content=ft.Chip(
                        label=ft.Text(title, size=12, color="#10B981"),
                        bgcolor="#1E291B",
                        border_side=ft.BorderSide(1, "#10B981"),
                        on_delete=lambda e, chat_id=c_id: handle_remove_monitored_chat_from_monitor_view(chat_id)
                    ),
                    padding=ft.Padding(0, 0, 0, 10)
                )
            )
            
        status_val = "Daemon Status: Active" if monitor_active else "Daemon Status: Stopped"
        status_color = "#10B981" if monitor_active else "#9CA3AF"
        status_desc = "Daemon listening for incoming channel messages and auto-downloading files..." if monitor_active else "Start the monitor daemon to begin watching target channels."
        
        # We want to preserve the logs in the TextField if we switch tabs and come back
        log_feed_val = monitor_log_field.current.value if (monitor_log_field.current and monitor_log_field.current.value) else "[DAEMON] Waiting to start daemon..."
        
        return [
                ft.Text("Live Monitor Daemon", theme_style=ft.TextThemeStyle.HEADLINE_MEDIUM, weight=ft.FontWeight.BOLD, color="#10B981"),
                ft.Text("Watch specific channels/groups and download new files automatically in real-time.", color="#9CA3AF"),
                ft.Divider(height=10, color="#1F2937"),
                
                # Daemon Controller Card
                ft.Container(
                    content=ft.Row([
                        ft.Column([
                            ft.Text(
                                status_val,
                                ref=monitor_status_title,
                                weight=ft.FontWeight.BOLD,
                                size=16,
                                color=status_color
                            ),
                            ft.Text(
                                status_desc,
                                ref=monitor_status_desc,
                                color="#9CA3AF"
                            )
                        ], expand=True),
                        ft.Switch(
                            ref=monitor_switch,
                            label="Start Daemon",
                            value=monitor_active,
                            active_color="#10B981",
                            on_change=handle_toggle_monitor
                        )
                    ]),
                    bgcolor="#1A1A1E",
                    padding=20,
                    border_radius=12,
                    border=ft.Border.all(1, "#374151")
                ),
                
                # Monitored Chats list Card with inline Add controls
                ft.Container(
                    content=ft.Column([
                        ft.Row([
                            ft.Row([
                                ft.Icon(ft.Icons.REMOVE_RED_EYE, color="#10B981", size=20),
                                ft.Text("Monitored Channels & Bots", weight=ft.FontWeight.BOLD, size=16),
                            ], spacing=8),
                            ft.Row([
                                ft.TextField(
                                    ref=monitor_add_input,
                                    hint_text="link/id/chat",
                                    border_color="#374151",
                                    width=220,
                                    content_padding=ft.Padding(12, 10, 12, 10),
                                    height=38,
                                    text_size=13,
                                    color="#E5E7EB"
                                ),
                                ft.ElevatedButton(
                                    "Add to list",
                                    bgcolor="#10B981",
                                    color="white",
                                    height=38,
                                    on_click=lambda e: page.run_task(handle_monitor_add_chat)
                                )
                            ], spacing=10)
                        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, wrap=True),
                        ft.Row(
                            chips,
                            scroll=ft.ScrollMode.AUTO,
                            spacing=8,
                            wrap=False,
                            height=52,
                            vertical_alignment=ft.CrossAxisAlignment.START
                        ) if chips else ft.Text("No chats monitored yet. Add them above.", color="#9CA3AF", italic=True)
                    ], spacing=15),
                    bgcolor="#1A1A1E",
                    padding=20,
                    border_radius=12,
                    border=ft.Border.all(1, "#374151")
                ),
                
                # Live Log View
                ft.Container(
                    content=ft.Column([
                        ft.Row([ft.Icon(ft.Icons.TERMINAL, color="#10B981"), ft.Text("Monitor Log Feed", weight=ft.FontWeight.BOLD, size=16)]),
                        ft.TextField(
                            ref=monitor_log_field,
                            multiline=True,
                            read_only=True,
                            value=log_feed_val,
                            border_color="#1F2937",
                            text_style=ft.TextStyle(font_family="Consolas", color="#10B981"),
                            expand=True
                        )
                    ], spacing=12),
                    bgcolor="#0D0D11",
                    padding=16,
                    border_radius=12,
                    border=ft.Border.all(1, "#374151"),
                    height=270
                )
        ]

    # ----------------------------------------------------
    # TAB 4: History & Statistics Layout
    # ----------------------------------------------------
    def load_statistics_data():
        """Loads and calculates download stats from archive.db."""
        if not os.path.exists(DB_PATH):
            return {
                "total_files": 0,
                "total_size": 0,
                "avg_size": 0,
                "media_breakdown": [],
                "top_chats": [],
                "largest_files": []
            }
            
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        try:
            # Create table if not exists just in case
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS archive (
                    chat_id TEXT,
                    message_id INTEGER,
                    filename TEXT,
                    file_size INTEGER,
                    downloaded_at TEXT,
                    chat_name TEXT,
                    caption TEXT,
                    media_type TEXT,
                    PRIMARY KEY (chat_id, message_id)
                )
            ''')
            
            # 1. Overall stats
            cursor.execute("SELECT COUNT(*), SUM(file_size) FROM archive")
            total_files, total_size = cursor.fetchone()
            total_files = total_files or 0
            total_size = total_size or 0
            avg_size = total_size / total_files if total_files > 0 else 0
            
            # 2. Media type breakdown
            cursor.execute("SELECT media_type, COUNT(*), SUM(file_size) FROM archive GROUP BY media_type ORDER BY SUM(file_size) DESC")
            media_breakdown = cursor.fetchall()
            
            # 3. Top 5 chats
            cursor.execute("SELECT chat_name, COUNT(*), SUM(file_size) FROM archive GROUP BY chat_name ORDER BY SUM(file_size) DESC LIMIT 5")
            top_chats = cursor.fetchall()
            
            # 4. Top 5 largest files that exist on disk
            cursor.execute("SELECT filename, file_size, chat_name, chat_id FROM archive ORDER BY file_size DESC")
            candidates = cursor.fetchall()
            largest_files = []
            for fname, size, cname, cid in candidates:
                if len(largest_files) >= 5:
                    break
                path = find_file_on_disk(cid, cname, fname)
                if path and os.path.exists(path):
                    largest_files.append((fname, size, cname, cid))
            
            conn.close()
            return {
                "total_files": total_files,
                "total_size": total_size,
                "avg_size": avg_size,
                "media_breakdown": media_breakdown,
                "top_chats": top_chats,
                "largest_files": largest_files
            }
        except Exception as ex:
            logger.error(f"Error loading stats from database: {ex}")
            try:
                conn.close()
            except Exception:
                pass
            return {
                "total_files": 0,
                "total_size": 0,
                "avg_size": 0,
                "media_breakdown": [],
                "top_chats": [],
                "largest_files": []
            }

    async def handle_stats_search(e=None):
        """Searches the download archive database and updates the DataTable."""
        if not stats_search_input.current or not stats_results_table.current:
            return
            
        keyword = stats_search_input.current.value.strip()
        logger.debug(f"Searching database with keyword: '{keyword}'")
        
        if stats_status_text.current:
            stats_status_text.current.value = "Searching..."
            stats_status_text.current.color = "#3B82F6"
            stats_status_text.current.update()
            
        # Perform query
        import sqlite3
        if not os.path.exists(DB_PATH):
            rows = []
        else:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            try:
                if keyword:
                    cursor.execute(
                        """SELECT filename, chat_name, file_size, downloaded_at, chat_id 
                           FROM archive 
                           WHERE filename LIKE ? OR caption LIKE ? OR chat_name LIKE ?
                           ORDER BY downloaded_at DESC LIMIT 100""",
                        (f"%{keyword}%", f"%{keyword}%", f"%{keyword}%")
                    )
                else:
                    cursor.execute(
                        """SELECT filename, chat_name, file_size, downloaded_at, chat_id 
                           FROM archive 
                           ORDER BY downloaded_at DESC LIMIT 100"""
                    )
                rows = cursor.fetchall()
            except Exception as ex:
                logger.error(f"Error executing search query: {ex}")
                rows = []
            finally:
                conn.close()
                
        # Build DataRows
        table_rows = []
        for r in rows:
            filename, chat_name, size, downloaded_at, chat_id = r
            chat_lbl = chat_name if chat_name else f"ID: {chat_id}"
            
            def make_open_handler(cid=chat_id, cname=chat_name, fname=filename):
                async def handler(e):
                    path = find_file_on_disk(cid, cname, fname)
                    if path and os.path.exists(path):
                        open_file_folder(path)
                        page.snack_bar = ft.SnackBar(ft.Text(f"Opening folder for: {fname}"))
                        page.snack_bar.open = True
                    else:
                        page.snack_bar = ft.SnackBar(ft.Text("File no longer exists on disk.", color="red"))
                        page.snack_bar.open = True
                    page.update()
                return handler

            table_rows.append(
                ft.DataRow(cells=[
                    ft.DataCell(
                        ft.Row([
                            ft.IconButton(
                                icon=ft.Icons.FOLDER_OPEN,
                                icon_color="#3B82F6",
                                icon_size=16,
                                tooltip="Open Containing Folder",
                                on_click=make_open_handler()
                            ),
                            ft.Text(os.path.basename(filename), size=12, weight=ft.FontWeight.W_500, overflow=ft.TextOverflow.ELLIPSIS, max_lines=1)
                        ], spacing=5)
                    ),
                    ft.DataCell(ft.Text(chat_lbl, size=12)),
                    ft.DataCell(ft.Text(format_size(size), size=12)),
                    ft.DataCell(ft.Text(downloaded_at if downloaded_at else "Unknown", size=12))
                ])
            )
            
        if not table_rows:
            table_rows = [
                ft.DataRow(cells=[
                    ft.DataCell(ft.Text("No downloaded files found in archive.")),
                    ft.DataCell(ft.Text("")),
                    ft.DataCell(ft.Text("")),
                    ft.DataCell(ft.Text(""))
                ])
            ]
            
        stats_results_table.current.rows = table_rows
        
        if stats_status_text.current:
            if not rows:
                stats_status_text.current.value = "No matching downloaded files found."
                stats_status_text.current.color = "#EF4444"
            else:
                count_lbl = f"Found {len(rows)} matching file(s)"
                if len(rows) == 100:
                    count_lbl += " (showing top 100)"
                stats_status_text.current.value = count_lbl
                stats_status_text.current.color = "#10B981"
            stats_status_text.current.update()
            
        stats_results_table.current.update()

    def build_stats_view():
        logger.debug("Building build_stats_view controls...")
        
        stats = load_statistics_data()
        total_files = stats["total_files"]
        total_size = stats["total_size"]
        avg_size = stats["avg_size"]
        media_breakdown = stats["media_breakdown"]
        top_chats = stats["top_chats"]
        largest_files = stats["largest_files"]
        
        # Classify and compute type sizes/counts
        video_size = 0
        photo_size = 0
        doc_size = 0
        other_size = 0
        video_count = 0
        photo_count = 0
        doc_count = 0
        other_count = 0
        
        for mtype, count, size_bytes in media_breakdown:
            size_bytes = size_bytes or 0
            mtype_lower = (mtype or "unknown").lower()
            if "video" in mtype_lower:
                video_size += size_bytes
                video_count += count
            elif "photo" in mtype_lower or "image" in mtype_lower:
                photo_size += size_bytes
                photo_count += count
            elif "doc" in mtype_lower or "document" in mtype_lower:
                doc_size += size_bytes
                doc_count += count
            else:
                other_size += size_bytes
                other_count += count
                
        video_pct = (video_size / total_size * 100) if total_size > 0 else 0
        photo_pct = (photo_size / total_size * 100) if total_size > 0 else 0
        doc_pct = (doc_size / total_size * 100) if total_size > 0 else 0
        other_pct = (other_size / total_size * 100) if total_size > 0 else 0
        
        # Build Media breakdown items
        breakdown_items = []
        if total_size == 0:
            breakdown_items.append(ft.Text("No media breakdown available", italic=True, size=11, color="#9CA3AF"))
        else:
            if video_size > 0:
                breakdown_items.append(
                    ft.Row([
                        ft.Icon(ft.Icons.CIRCLE, color="#10B981", size=8),
                        ft.Text("Video", size=11, weight=ft.FontWeight.W_500, width=70),
                        ft.Text(f": {format_size(video_size)} ({video_pct:.0f}%)", size=11, color="#9CA3AF")
                    ], spacing=5)
                )
            if photo_size > 0:
                breakdown_items.append(
                    ft.Row([
                        ft.Icon(ft.Icons.CIRCLE, color="#EAB308", size=8),
                        ft.Text("Photos", size=11, weight=ft.FontWeight.W_500, width=70),
                        ft.Text(f": {format_size(photo_size)} ({photo_pct:.0f}%)", size=11, color="#9CA3AF")
                    ], spacing=5)
                )
            if doc_size > 0:
                breakdown_items.append(
                    ft.Row([
                        ft.Icon(ft.Icons.CIRCLE, color="#8B5CF6", size=8),
                        ft.Text("Documents", size=11, weight=ft.FontWeight.W_500, width=70),
                        ft.Text(f": {format_size(doc_size)} ({doc_pct:.0f}%)", size=11, color="#9CA3AF")
                    ], spacing=5)
                )
            if other_size > 0:
                breakdown_items.append(
                    ft.Row([
                        ft.Icon(ft.Icons.CIRCLE, color="#EF4444", size=8),
                        ft.Text("Others", size=11, weight=ft.FontWeight.W_500, width=70),
                        ft.Text(f": {format_size(other_size)} ({other_pct:.0f}%)", size=11, color="#9CA3AF")
                    ], spacing=5)
                )
                
        # Build multi-color stacked bar
        bar_segments = []
        if total_size > 0:
            if video_size > 0:
                bar_segments.append(ft.Container(bgcolor="#10B981", expand=max(1, int(video_size / total_size * 100)), tooltip=f"Video: {video_pct:.0f}%"))
            if photo_size > 0:
                bar_segments.append(ft.Container(bgcolor="#EAB308", expand=max(1, int(photo_size / total_size * 100)), tooltip=f"Photos: {photo_pct:.0f}%"))
            if doc_size > 0:
                bar_segments.append(ft.Container(bgcolor="#8B5CF6", expand=max(1, int(doc_size / total_size * 100)), tooltip=f"Documents: {doc_pct:.0f}%"))
            if other_size > 0:
                bar_segments.append(ft.Container(bgcolor="#EF4444", expand=max(1, int(other_size / total_size * 100)), tooltip=f"Others: {other_pct:.0f}%"))
                
        if not bar_segments:
            bar_segments.append(ft.Container(bgcolor="#374151", expand=True))
            
        stacked_bar = ft.Container(
            content=ft.Row(bar_segments, spacing=0),
            border_radius=4,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            height=8
        )
                
        # Build Top chats items
        top_chats_items = []
        if not top_chats:
            top_chats_items.append(ft.Text("No chat metrics available", italic=True, size=12, color="#9CA3AF"))
        else:
            for chat, count, size_bytes in top_chats:
                chat_name = chat if chat else "Direct Messages/Unknown"
                top_chats_items.append(
                    ft.Row([
                        ft.Row([
                            ft.Icon(ft.Icons.CHAT_BUBBLE_OUTLINE, size=12, color="#10B981"),
                            ft.Text(chat_name, size=12, weight=ft.FontWeight.W_500, overflow=ft.TextOverflow.ELLIPSIS, max_lines=1, width=110)
                        ], spacing=5),
                        ft.Text(f"{format_size(size_bytes or 0)} ({count} files)", size=12, color="#9CA3AF")
                    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
                )
                
        # Build Largest files items
        largest_files_items = []
        if not largest_files:
            largest_files_items.append(ft.Text("No archived files found on disk", italic=True, size=12, color="#9CA3AF"))
        else:
            for filename, size_bytes, chat, chat_id in largest_files:
                chat_name = chat if chat else "Unknown"
                
                def make_open_largest_handler(cid=chat_id, cname=chat_name, fname=filename):
                    async def handler(e):
                        path = find_file_on_disk(cid, cname, fname)
                        if path and os.path.exists(path):
                            open_file_folder(path)
                            page.snack_bar = ft.SnackBar(ft.Text(f"Opening folder for: {fname}"))
                            page.snack_bar.open = True
                        else:
                            page.snack_bar = ft.SnackBar(ft.Text("File no longer exists on disk.", color="red"))
                            page.snack_bar.open = True
                        page.update()
                    return handler

                largest_files_items.append(
                    ft.Row([
                        ft.Row([
                            ft.IconButton(
                                icon=ft.Icons.FOLDER_OPEN,
                                icon_color="#A78BFA",
                                icon_size=14,
                                tooltip="Open Containing Folder",
                                on_click=make_open_largest_handler()
                            ),
                            ft.Text(os.path.basename(filename), size=11, overflow=ft.TextOverflow.ELLIPSIS, max_lines=2, width=130)
                        ], spacing=5),
                        ft.Text(format_size(size_bytes or 0), size=11, color="#9CA3AF")
                    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
                )

        # Initial search results (recent 20 downloads)
        import sqlite3
        initial_rows = []
        if os.path.exists(DB_PATH):
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """SELECT filename, chat_name, file_size, downloaded_at, chat_id 
                       FROM archive 
                       ORDER BY downloaded_at DESC LIMIT 20"""
                )
                initial_rows = cursor.fetchall()
            except Exception as ex:
                logger.error(f"Error querying initial search: {ex}")
            finally:
                conn.close()
                
        table_rows = []
        for r in initial_rows:
            filename, chat_name, size, downloaded_at, chat_id = r
            chat_lbl = chat_name if chat_name else f"ID: {chat_id}"
            
            def make_open_handler(cid=chat_id, cname=chat_name, fname=filename):
                async def handler(e):
                    path = find_file_on_disk(cid, cname, fname)
                    if path and os.path.exists(path):
                        open_file_folder(path)
                        page.snack_bar = ft.SnackBar(ft.Text(f"Opening folder for: {fname}"))
                        page.snack_bar.open = True
                    else:
                        page.snack_bar = ft.SnackBar(ft.Text("File no longer exists on disk.", color="red"))
                        page.snack_bar.open = True
                    page.update()
                return handler

            table_rows.append(
                ft.DataRow(cells=[
                    ft.DataCell(
                        ft.Row([
                            ft.IconButton(
                                icon=ft.Icons.FOLDER_OPEN,
                                icon_color="#3B82F6",
                                icon_size=16,
                                tooltip="Open Containing Folder",
                                on_click=make_open_handler()
                            ),
                            ft.Text(os.path.basename(filename), size=12, weight=ft.FontWeight.W_500, overflow=ft.TextOverflow.ELLIPSIS, max_lines=1)
                        ], spacing=5)
                    ),
                    ft.DataCell(ft.Text(chat_lbl, size=12)),
                    ft.DataCell(ft.Text(format_size(size), size=12)),
                    ft.DataCell(ft.Text(downloaded_at if downloaded_at else "Unknown", size=12))
                ])
            )
            
        if not table_rows:
            table_rows = [
                ft.DataRow(cells=[
                    ft.DataCell(ft.Text("No downloaded files found in archive.")),
                    ft.DataCell(ft.Text("")),
                    ft.DataCell(ft.Text("")),
                    ft.DataCell(ft.Text(""))
                ])
            ]
            
        return [
                ft.Text("History & Statistics Dashboard", theme_style=ft.TextThemeStyle.HEADLINE_MEDIUM, weight=ft.FontWeight.BOLD, color="#3B82F6"),
                ft.Text("Search downloaded history archives or check storage and chat distribution metrics.", color="#9CA3AF"),
                ft.Divider(height=10, color="#1F2937"),
                
                # Three-Column Grid Details Layout (Total Height: 290px)
                ft.Row([
                    # COLUMN 1: Left metrics cards (Total Storage, Total Files, Average Size)
                    ft.Column([
                        # Card 1: Total Storage Used
                        ft.Container(
                            content=ft.Column([
                                ft.Text("Total Storage Used:", size=20, color="#9CA3AF", weight=ft.FontWeight.BOLD),
                                ft.Text(format_size(total_size), ref=stats_total_storage_text, size=20, weight=ft.FontWeight.BOLD, color="#3B82F6"),
                            ], spacing=4, alignment=ft.MainAxisAlignment.CENTER),
                            bgcolor="#1A1A1E",
                            padding=12,
                            border_radius=8,
                            border=ft.Border.all(1, "#374151"),
                            expand=True
                        ),
                        # Card 2: Total Files
                        ft.Container(
                            content=ft.Column([
                                ft.Text("Total Files:", size=20, color="#9CA3AF", weight=ft.FontWeight.BOLD),
                                ft.Text(f"{total_files} files", ref=stats_total_files_text, size=20, weight=ft.FontWeight.BOLD, color="#10B981"),
                            ], spacing=4, alignment=ft.MainAxisAlignment.CENTER),
                            bgcolor="#1A1A1E",
                            padding=12,
                            border_radius=8,
                            border=ft.Border.all(1, "#374151"),
                            expand=True
                        ),
                        # Card 3: Average File size
                        ft.Container(
                            content=ft.Column([
                                ft.Text("Average File size:", size=20, color="#9CA3AF", weight=ft.FontWeight.BOLD),
                                ft.Text(format_size(avg_size), ref=stats_avg_size_text, size=20, weight=ft.FontWeight.BOLD, color="#EAB308"),
                            ], spacing=4, alignment=ft.MainAxisAlignment.CENTER),
                            bgcolor="#1A1A1E",
                            padding=12,
                            border_radius=8,
                            border=ft.Border.all(1, "#374151"),
                            expand=True
                        ),
                    ], spacing=10, expand=True, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                    
                    # COLUMN 2: Center (Media Type Distribution & Top Chat By Storage)
                    ft.Column([
                        # Media Type Distribution Card
                        ft.Container(
                            content=ft.Column([
                                ft.Text("Media Type Distribution:", weight=ft.FontWeight.BOLD, size=16, color="#10B981"),
                                ft.Column(breakdown_items, spacing=4, ref=stats_breakdown_col),
                                ft.Container(height=2),
                                stacked_bar
                            ], spacing=6),
                            bgcolor="#1A1A1E",
                            padding=12,
                            border_radius=8,
                            border=ft.Border.all(1, "#374151"),
                            expand=True
                        ),
                        # Top Chat By Storage Card
                        ft.Container(
                            content=ft.Column([
                                ft.Text("Top Chat By Storage:", weight=ft.FontWeight.BOLD, size=16, color="#EAB308"),
                                ft.Column(top_chats_items, spacing=4, ref=stats_chats_col)
                            ], spacing=6),
                            bgcolor="#1A1A1E",
                            padding=12,
                            border_radius=8,
                            border=ft.Border.all(1, "#374151"),
                            expand=True
                        )
                    ], spacing=10, expand=True, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                    
                    # COLUMN 3: Right (Large Files Card)
                    ft.Column([
                        ft.Container(
                            content=ft.Column([
                                ft.Text("Large Files:", weight=ft.FontWeight.BOLD, size=16, color="#A78BFA"),
                                ft.Column(largest_files_items, spacing=5, ref=stats_largest_col)
                            ], spacing=6),
                            bgcolor="#1A1A1E",
                            padding=12,
                            border_radius=8,
                            border=ft.Border.all(1, "#374151"),
                            expand=True
                        )
                    ], spacing=10, expand=True, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
                ], spacing=15, height=290),
                
                # Bottom: Search Files Card
                ft.Container(
                    content=ft.Column([
                        ft.Row([
                            ft.Row([ft.Icon(ft.Icons.SEARCH, color="#3B82F6", size=18), ft.Text("Search Files", weight=ft.FontWeight.BOLD, size=14)]),
                            ft.Text("Showing recent downloads", ref=stats_status_text, size=11, color="#9CA3AF")
                        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                        ft.Row([
                            ft.TextField(
                                ref=stats_search_input,
                                label="Search by filename, caption, or chat name",
                                border_color="#374151",
                                expand=True,
                                height=38,
                                content_padding=10,
                                on_submit=lambda e: page.run_task(handle_stats_search)
                            ),
                            ft.IconButton(
                                ref=stats_search_button,
                                icon=ft.Icons.SEARCH,
                                icon_color="#3B82F6",
                                on_click=lambda e: page.run_task(handle_stats_search)
                            )
                        ], spacing=10),
                        
                        # DataTable container
                        ft.Container(
                            content=ft.Column([
                                ft.DataTable(
                                    ref=stats_results_table,
                                    columns=[
                                        ft.DataColumn(ft.Text("Filename", weight=ft.FontWeight.BOLD, color="#9CA3AF")),
                                        ft.DataColumn(ft.Text("Chat", weight=ft.FontWeight.BOLD, color="#9CA3AF")),
                                        ft.DataColumn(ft.Text("Size", weight=ft.FontWeight.BOLD, color="#9CA3AF")),
                                        ft.DataColumn(ft.Text("Downloaded At", weight=ft.FontWeight.BOLD, color="#9CA3AF")),
                                    ],
                                    rows=table_rows,
                                    column_spacing=50
                                )
                            ], scroll=ft.ScrollMode.AUTO),
                            height=160
                        )
                    ], spacing=10),
                    bgcolor="#1A1A1E",
                    padding=12,
                    border_radius=8,
                    border=ft.Border.all(1, "#374151")
                )
        ]

    # ----------------------------------------------------
    # TAB 5: Settings Layout
    # ----------------------------------------------------
    async def handle_save_settings(e=None):
        logger.debug("Starting settings save...")
        settings_status.current.value = "Saving settings..."
        settings_status.current.color = "#F59E0B"
        page.update()
        
        current_cfg = get_config()
        
        # Handle optional API ID & Hash without erroring if blank or masked
        raw_api_id = settings_api_id.current.value.strip() if (settings_api_id and settings_api_id.current and settings_api_id.current.value) else ""
        if raw_api_id and raw_api_id != "*****":
            try:
                current_cfg["api_id"] = int(raw_api_id)
            except ValueError:
                pass
                
        raw_api_hash = settings_api_hash.current.value.strip() if (settings_api_hash and settings_api_hash.current and settings_api_hash.current.value) else ""
        if raw_api_hash and raw_api_hash != "*****":
            current_cfg["api_hash"] = raw_api_hash
            
        current_cfg["download_dir"] = settings_download_dir.current.value.strip()
        
        try:
            speed_val = int(settings_speed_limit.current.value.strip())
            current_cfg["max_download_speed_kbps"] = speed_val
            current_cfg["speed_limit_kb"] = speed_val
        except ValueError:
            current_cfg["max_download_speed_kbps"] = 0
            current_cfg["speed_limit_kb"] = 0
            
        try:
            current_cfg["chunk_size_kb"] = int(settings_chunk_size.current.value.strip())
        except ValueError:
            current_cfg["chunk_size_kb"] = 1024
            
        try:
            current_cfg["max_concurrent_downloads"] = int(settings_max_concurrent.current.value.strip())
        except ValueError:
            current_cfg["max_concurrent_downloads"] = 3
            
        current_cfg["file_name_template"] = settings_filename_template.current.value.strip()
        
        try:
            current_cfg["filter_min_size_mb"] = float(settings_min_size.current.value.strip())
        except ValueError:
            current_cfg["filter_min_size_mb"] = 0.0
            
        try:
            current_cfg["filter_max_size_mb"] = float(settings_max_size.current.value.strip())
        except ValueError:
            current_cfg["filter_max_size_mb"] = 0.0
            
        current_cfg["filter_after_date"] = settings_after_date.current.value.strip()
        current_cfg["filter_before_date"] = settings_before_date.current.value.strip()
        current_cfg["filter_filename_regex"] = settings_filename_regex.current.value.strip()
        current_cfg["filter_caption_regex"] = settings_caption_regex.current.value.strip()
        
        exts_str = settings_allowed_extensions.current.value.strip()
        if exts_str:
            current_cfg["filter_allowed_extensions"] = [x.strip().lower() for x in exts_str.split(",") if x.strip()]
        else:
            current_cfg["filter_allowed_extensions"] = []
            
        m_types = []
        if settings_media_photo.current.value:
            m_types.append("photo")
        if settings_media_video.current.value:
            m_types.append("video")
        if settings_media_document.current.value:
            m_types.append("document")
        if settings_media_sticker.current.value:
            m_types.append("sticker")
        current_cfg["media_types"] = m_types
        
        current_cfg["monitor_date_organization"] = settings_monitor_date_org.current.value
        current_cfg["monitor_bot_session_grouping"] = settings_monitor_session_grouping.current.value
        
        try:
            current_cfg["monitor_bot_session_gap_seconds"] = int(settings_monitor_session_gap.current.value.strip())
        except ValueError:
            current_cfg["monitor_bot_session_gap_seconds"] = 15
            
        current_cfg["enable_gallery"] = settings_gallery_enabled.current.value
        current_cfg["cloud_upload_enabled"] = settings_cloud_enabled.current.value
        current_cfg["cloud_delete_local_after_upload"] = settings_cloud_delete_local.current.value
        current_cfg["cloud_rclone_remote"] = settings_cloud_rclone_remote.current.value.strip()
        current_cfg["cloud_remote_path"] = settings_cloud_remote_path.current.value.strip()
        
        current_cfg["tg_mirror_enabled"] = settings_mirror_enabled.current.value
        current_cfg["tg_mirror_target"] = settings_mirror_target.current.value.strip()
        
        if save_config(current_cfg):
            settings_status.current.value = "Success: Settings saved successfully!"
            settings_status.current.color = "#10B981"
        else:
            settings_status.current.value = "Error: Failed to write to config.json."
            settings_status.current.color = "red"
            
        page.update()

    async def handle_add_monitored_chat(e=None):
        logger.debug("Starting add monitored chat...")
        if not settings_add_monitored.current:
            return
        chat_input = settings_add_monitored.current.value.strip()
        if not chat_input:
            settings_monitored_status.current.value = "Error: Input cannot be empty."
            settings_monitored_status.current.color = "red"
            settings_monitored_status.current.update()
            return
            
        settings_monitored_status.current.value = "Resolving chat entity..."
        settings_monitored_status.current.color = "#F59E0B"
        settings_monitored_status.current.update()
        
        try:
            entity = await resolve_chat_entity(client, chat_input)
            entity_id = entity.id
            
            # Resolve the title robustly
            title = getattr(entity, 'title', None)
            if not title:
                first_name = getattr(entity, 'first_name', None)
                last_name = getattr(entity, 'last_name', None)
                if first_name:
                    title = f"{first_name} {last_name}".strip() if last_name else first_name
                else:
                    title = getattr(entity, 'username', None)
            if not title:
                title = str(entity_id)
                
            # Append type label
            from telethon.tl.types import Channel, Chat, User
            label = "Chat"
            if isinstance(entity, User):
                label = "Bot" if entity.bot else "User"
            elif isinstance(entity, Chat):
                label = "Group"
            elif isinstance(entity, Channel):
                label = "Group" if getattr(entity, 'megagroup', False) else "Channel"
                
            title = f"{title} ({label})"
            
            identifier = getattr(entity, 'username', None)
            if not identifier:
                identifier = entity_id
                
            config_data = get_config()
            monitored = config_data.get("monitored_chats", [])
            
            # Check for duplicates
            is_dup = False
            for c in monitored:
                c_id = c.get('id') if isinstance(c, dict) else c
                if str(c_id) == str(identifier):
                    is_dup = True
                    break
                    
            if is_dup:
                settings_monitored_status.current.value = f"'{title}' is already in your monitor list."
                settings_monitored_status.current.color = "#F59E0B"
                settings_monitored_status.current.update()
            else:
                monitored.append({"id": identifier, "title": title})
                config_data["monitored_chats"] = monitored
                save_config(config_data)
                
                settings_monitored_status.current.value = f"Successfully added '{title}'!"
                settings_monitored_status.current.color = "#10B981"
                settings_monitored_status.current.update()
                
                # Clear add input field
                settings_add_monitored.current.value = ""
                
                # Re-render settings view
                content_lv.controls = build_settings_view()
                page.update()
                
        except Exception as ex:
            logger.error(f"Error resolving chat in settings: {ex}")
            settings_monitored_status.current.value = f"Failed to resolve chat: {ex}"
            settings_monitored_status.current.color = "red"
            settings_monitored_status.current.update()

    def handle_remove_monitored_chat(chat_id):
        logger.debug(f"Removing monitored chat ID: {chat_id}")
        config_data = get_config()
        monitored = config_data.get("monitored_chats", [])
        
        # Remove matching ID/username
        for val in list(monitored):
            match = False
            if isinstance(val, dict):
                if str(val.get("id")) == str(chat_id):
                    match = True
            else:
                if str(val) == str(chat_id):
                    match = True
            if match:
                monitored.remove(val)
                break
                
        config_data["monitored_chats"] = monitored
        save_config(config_data)
        
        # Re-render settings view
        content_lv.controls = build_settings_view()
        page.update()

    async def handle_logout(e=None):
        global client
        logger.info("Log Out triggered. Disconnecting Telethon client and removing session file...")
        try:
            if client:
                await client.disconnect()
                client = None
        except Exception as ex:
            logger.error(f"Error disconnecting client on logout: {ex}")
            
        session_file = os.path.join(PROJECT_ROOT, 'telegram_dl_session.session')
        session_journal = os.path.join(PROJECT_ROOT, 'telegram_dl_session.session-journal')
        
        for fpath in [session_file, session_journal]:
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                    logger.info(f"Removed session file: {fpath}")
                except Exception as ex:
                    logger.error(f"Could not remove session file {fpath}: {ex}")
                    
        # Reset auth inputs
        if auth_code.current:
            auth_code.current.value = ""
            auth_code.current.disabled = True
        if auth_password.current:
            auth_password.current.value = ""
            auth_password.current.disabled = True
        if auth_status.current:
            auth_status.current.value = "Logged out successfully. Session file deleted. Please sign in again."
            auth_status.current.color = "#F59E0B"
            
        # Switch back to auth onboarding view
        auth_panel.content = build_auth_view()
        page.controls.clear()
        page.add(auth_panel)
        page.update()

    def build_settings_view():
        logger.debug("Building build_settings_view controls...")
        
        # Read config
        config_data = get_config()
        media_list = config_data.get("media_types", ["photo", "video", "document", "sticker"])
        allowed_exts_str = ", ".join(config_data.get("filter_allowed_extensions", []))
        
        return [
                ft.Text("Settings & Configurations", theme_style=ft.TextThemeStyle.HEADLINE_MEDIUM, weight=ft.FontWeight.BOLD, color="#F59E0B"),
                ft.Text("Modify download parameters, API credentials, directories, filters, backups, and mirrors.", color="#9CA3AF"),
                ft.Divider(height=10, color="#1F2937"),
                
                # ROW 1: API & Credentials + Directories & File Naming
                ft.Row([
                    # Card 1: API Credentials & Mirroring
                    ft.Container(
                        content=ft.Column([
                            ft.Row([ft.Icon(ft.Icons.KEY, color="#F59E0B"), ft.Text("API Credentials & Mirroring", weight=ft.FontWeight.BOLD, size=15)]),
                            ft.TextField(ref=settings_api_id, label="API ID (Configured)", value="*****" if config_data.get("api_id") else "", hint_text="Enter new API ID to update", border_color="#374151", height=38, content_padding=10),
                            ft.TextField(ref=settings_api_hash, label="API Hash (Configured)", value="*****" if config_data.get("api_hash") else "", password=True, hint_text="Enter new API Hash to update", border_color="#374151", height=38, content_padding=10),
                            ft.Checkbox(ref=settings_mirror_enabled, label="Mirror Downloads to Telegram", value=bool(config_data.get("tg_mirror_enabled", False))),
                            ft.TextField(ref=settings_mirror_target, label="Telegram Target Channel/ID", value=str(config_data.get("tg_mirror_target", "")), border_color="#374151", height=38, content_padding=10),
                        ], spacing=10, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                        bgcolor="#1A1A1E",
                        padding=15,
                        border_radius=10,
                        border=ft.Border.all(1, "#374151"),
                        expand=True
                    ),
                    # Card 2: Directories & File Naming
                    ft.Container(
                        content=ft.Column([
                            ft.Row([ft.Icon(ft.Icons.FOLDER, color="#F59E0B"), ft.Text("Directories & File Naming", weight=ft.FontWeight.BOLD, size=15)]),
                            ft.TextField(ref=settings_download_dir, label="Downloads Folder Path", value=str(config_data.get("download_dir", "./downloads")), border_color="#374151", height=38, content_padding=10),
                            ft.TextField(ref=settings_filename_template, label="Filename Naming Template", value=str(config_data.get("file_name_template", "{chat_id}_{message_id}_{date}_{filename}")), border_color="#374151", height=38, content_padding=10),
                            ft.TextField(ref=settings_cloud_rclone_remote, label="Rclone Remote Name", value=str(config_data.get("cloud_rclone_remote", "gdrive")), border_color="#374151", height=38, content_padding=10),
                            ft.TextField(ref=settings_cloud_remote_path, label="Rclone Remote Path", value=str(config_data.get("cloud_remote_path", "telegram-dl/backups")), border_color="#374151", height=38, content_padding=10),
                        ], spacing=10, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                        bgcolor="#1A1A1E",
                        padding=15,
                        border_radius=10,
                        border=ft.Border.all(1, "#374151"),
                        expand=True
                    )
                ], spacing=15, height=270, vertical_alignment=ft.CrossAxisAlignment.STRETCH),
                
                # ROW 2: Speed & Limits + Media Filters
                ft.Row([
                    # Card 3: Performance & Speed Limits
                    ft.Container(
                        content=ft.Column([
                            ft.Row([ft.Icon(ft.Icons.SPEED, color="#F59E0B"), ft.Text("Limits & Performance", weight=ft.FontWeight.BOLD, size=15)]),
                            ft.TextField(ref=settings_speed_limit, label="Max Speed Limit (KB/s - 0 for Unlimited)", value=str(config_data.get("max_download_speed_kbps", config_data.get("speed_limit_kb", "0"))), border_color="#374151", height=38, content_padding=10),
                            ft.Dropdown(
                                ref=settings_chunk_size,
                                label="Download Chunk Size (KB)",
                                options=[
                                    ft.dropdown.Option("256", "256 KB"),
                                    ft.dropdown.Option("512", "512 KB"),
                                    ft.dropdown.Option("1024", "1024 KB (1 MB)"),
                                    ft.dropdown.Option("2048", "2048 KB (2 MB)"),
                                    ft.dropdown.Option("4096", "4096 KB (4 MB)"),
                                ],
                                value=str(config_data.get("chunk_size_kb", "1024")),
                                border_color="#374151",
                                height=38
                            ),
                            ft.TextField(ref=settings_max_concurrent, label="Max Parallel Downloads", value=str(config_data.get("max_concurrent_downloads", "3")), border_color="#374151", height=38, content_padding=10),
                            ft.Dropdown(
                                ref=settings_monitor_date_org,
                                label="Date Subfolder Organization",
                                options=[
                                    ft.dropdown.Option("none"),
                                    ft.dropdown.Option("daily"),
                                    ft.dropdown.Option("monthly"),
                                ],
                                value=config_data.get("monitor_date_organization", "none"),
                                border_color="#374151",
                                height=38
                            ),
                        ], spacing=10, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                        bgcolor="#1A1A1E",
                        padding=15,
                        border_radius=10,
                        border=ft.Border.all(1, "#374151"),
                        expand=True
                    ),
                    # Card 4: Media Types & Extensions Filter
                    ft.Container(
                        content=ft.Column([
                            ft.Row([ft.Icon(ft.Icons.FILTER_LIST, color="#F59E0B"), ft.Text("Media & Size Filters", weight=ft.FontWeight.BOLD, size=15)]),
                            ft.Row([
                                ft.Checkbox(ref=settings_media_photo, label="Photos", value="photo" in media_list),
                                ft.Checkbox(ref=settings_media_video, label="Videos", value="video" in media_list),
                                ft.Checkbox(ref=settings_media_document, label="Docs", value="document" in media_list),
                                ft.Checkbox(ref=settings_media_sticker, label="Stickers", value="sticker" in media_list),
                            ], spacing=15),
                            ft.TextField(ref=settings_min_size, label="Minimum File Size (MB)", value=str(config_data.get("filter_min_size_mb", "0")), border_color="#374151", height=38, content_padding=10),
                            ft.TextField(ref=settings_max_size, label="Maximum File Size (MB)", value=str(config_data.get("filter_max_size_mb", "0")), border_color="#374151", height=38, content_padding=10),
                            ft.TextField(ref=settings_allowed_extensions, label="Allowed Extensions (Comma separated)", value=allowed_exts_str, border_color="#374151", height=38, content_padding=10),
                        ], spacing=10, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                        bgcolor="#1A1A1E",
                        padding=15,
                        border_radius=10,
                        border=ft.Border.all(1, "#374151"),
                        expand=True
                    )
                ], spacing=15, height=270, vertical_alignment=ft.CrossAxisAlignment.STRETCH),
                
                # ROW 3: Date & Regex Matching + Monitor & Cloud Storage
                ft.Row([
                    # Card 5: Date & Text Matching
                    ft.Container(
                        content=ft.Column([
                            ft.Row([ft.Icon(ft.Icons.DATE_RANGE, color="#F59E0B"), ft.Text("Date & Text Matching", weight=ft.FontWeight.BOLD, size=15)]),
                            ft.TextField(ref=settings_after_date, label="Only After Date (YYYY-MM-DD)", value=str(config_data.get("filter_after_date", "")), border_color="#374151", height=38, content_padding=10),
                            ft.TextField(ref=settings_before_date, label="Only Before Date (YYYY-MM-DD)", value=str(config_data.get("filter_before_date", "")), border_color="#374151", height=38, content_padding=10),
                            ft.TextField(ref=settings_filename_regex, label="Filename Regex Pattern", value=str(config_data.get("filter_filename_regex", "")), border_color="#374151", height=38, content_padding=10),
                            ft.TextField(ref=settings_caption_regex, label="Caption Regex Pattern", value=str(config_data.get("filter_caption_regex", "")), border_color="#374151", height=38, content_padding=10),
                        ], spacing=10, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                        bgcolor="#1A1A1E",
                        padding=15,
                        border_radius=10,
                        border=ft.Border.all(1, "#374151"),
                        expand=True
                    ),
                    # Card 6: Live Monitor & Cloud Backups
                    ft.Container(
                        content=ft.Column([
                            ft.Row([ft.Icon(ft.Icons.CLOUD_UPLOAD, color="#F59E0B"), ft.Text("Monitor & Cloud Storage", weight=ft.FontWeight.BOLD, size=15)]),
                            ft.Checkbox(ref=settings_monitor_session_grouping, label="Group Bot Session files", value=bool(config_data.get("monitor_bot_session_grouping", True))),
                            ft.TextField(ref=settings_monitor_session_gap, label="Session Gap Seconds", value=str(config_data.get("monitor_bot_session_gap_seconds", "15")), border_color="#374151", height=38, content_padding=10),
                            ft.Checkbox(ref=settings_gallery_enabled, label="Enable HTML Gallery Generator", value=bool(config_data.get("enable_gallery", False))),
                            ft.Row([
                                ft.Checkbox(ref=settings_cloud_enabled, label="Auto Cloud Upload", value=bool(config_data.get("cloud_upload_enabled", False))),
                                ft.Checkbox(ref=settings_cloud_delete_local, label="Delete local after upload", value=bool(config_data.get("cloud_delete_local_after_upload", False))),
                            ], spacing=15),
                        ], spacing=10, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                        bgcolor="#1A1A1E",
                        padding=15,
                        border_radius=10,
                        border=ft.Border.all(1, "#374151"),
                        expand=True
                    )
                ], spacing=15, height=270, vertical_alignment=ft.CrossAxisAlignment.STRETCH),
                
                # Bottom Action Save & Log Out panel
                ft.Container(
                    content=ft.Row([
                        ft.Button(
                            "Save All Configurations",
                            bgcolor="#F59E0B",
                            color="white",
                            icon=ft.Icons.SAVE,
                            on_click=lambda e: page.run_task(handle_save_settings)
                        ),
                        ft.Button(
                            "Log Out & Delete Session",
                            bgcolor="#EF4444",
                            color="white",
                            icon=ft.Icons.LOGOUT,
                            on_click=lambda e: page.run_task(handle_logout)
                        ),
                        ft.Text("", ref=settings_status, weight=ft.FontWeight.BOLD)
                    ], spacing=15),
                    padding=5
                )
        ]

    # ----------------------------------------------------
    # TAB 6: Telegram Onboarding Login Screen Layout
    # ----------------------------------------------------
    async def handle_send_code(e=None):
        global client
        logger.debug("Onboarding: handle_send_code triggered...")
        auth_status.current.value = "Connecting to Telegram MTProto servers..."
        auth_status.current.color = "#A855F7"
        page.update()
        
        api_id_str = auth_api_id.current.value.strip()
        api_hash = auth_api_hash.current.value.strip()
        phone = auth_phone.current.value.strip()
        
        if not api_id_str or not api_hash or not phone:
            auth_status.current.value = "Error: API ID, Hash, and Phone Number cannot be empty."
            auth_status.current.color = "red"
            page.update()
            return
            
        try:
            api_id = int(api_id_str)
        except ValueError:
            auth_status.current.value = "Error: API ID must be a valid integer."
            auth_status.current.color = "red"
            page.update()
            return
            
        session_path = os.path.join(PROJECT_ROOT, 'telegram_dl_session')
        client = TelegramClient(session_path, api_id, api_hash)
        
        try:
            await client.connect()
            auth_status.current.value = f"Sending verification code to {phone}..."
            page.update()
            
            nonlocal sent_code_obj, phone_code_hash
            sent_code_obj = await client.send_code_request(phone)
            phone_code_hash = sent_code_obj.phone_code_hash
            
            auth_code.current.disabled = False
            auth_code.current.label = "Verification Code (Sent!)"
            auth_status.current.value = "Code sent successfully! Enter it below and click Verify."
            auth_status.current.color = "#10B981"
            
        except Exception as err:
            logger.error(f"Error in handle_send_code client verification: {traceback.format_exc()}")
            auth_status.current.value = f"Connection Error: {str(err)}"
            auth_status.current.color = "red"
            
        page.update()

    async def handle_verify_login(e=None):
        global client
        logger.debug("Onboarding: handle_verify_login triggered...")
        code = auth_code.current.value.strip()
        phone = auth_phone.current.value.strip()
        api_id = int(auth_api_id.current.value.strip())
        api_hash = auth_api_hash.current.value.strip()
        password = auth_password.current.value.strip()
        
        if not code:
            auth_status.current.value = "Error: Please enter the verification code."
            auth_status.current.color = "red"
            page.update()
            return
            
        auth_status.current.value = "Verifying sign-in details..."
        auth_status.current.color = "#A855F7"
        page.update()
        
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            await complete_auth_transition(api_id, api_hash, phone)
            
        except SessionPasswordNeededError:
            if not password:
                auth_password.current.disabled = False
                auth_status.current.value = "Two-Step Verification active. Enter your account password."
                auth_status.current.color = "#F59E0B"
            else:
                try:
                    await client.sign_in(password=password)
                    await complete_auth_transition(api_id, api_hash, phone)
                except Exception as err:
                    logger.error(f"2FA password auth failed: {str(err)}")
                    auth_status.current.value = f"2FA Password Error: {str(err)}"
                    auth_status.current.color = "red"
                    
        except Exception as err:
            logger.error(f"Error in handle_verify_login: {traceback.format_exc()}")
            auth_status.current.value = f"Sign-in Error: {str(err)}"
            auth_status.current.color = "red"
            
        page.update()

    async def complete_auth_transition(api_id, api_hash, phone):
        logger.info("Complete auth transition triggered. Unlocking dashboard...")
        current_cfg = get_config()
        current_cfg["api_id"] = api_id
        current_cfg["api_hash"] = api_hash
        current_cfg["phone"] = phone
        save_config(current_cfg)
        
        auth_status.current.value = "Login Successful! Loading workspace..."
        auth_status.current.color = "#10B981"
        page.update()
        
        await asyncio.sleep(1)
        
        page.controls.clear()
        page.add(main_layout)
        page.update()
        logger.info("Auth transition complete. Dashboard is fully active.")

    def build_auth_view():
        logger.debug("Building build_auth_view Container...")
        return ft.Container(
            content=ft.Column([
                ft.Container(
                    content=ft.Column([
                        ft.Row([
                            ft.Image(src="https://upload.wikimedia.org/wikipedia/commons/8/82/Telegram_logo.svg", width=50, height=50),
                            ft.Text("Telegram Authentication", size=24, weight=ft.FontWeight.BOLD, color="white")
                        ], alignment=ft.MainAxisAlignment.CENTER, spacing=15),
                        
                        ft.Text("Complete authorization to start downloading restricted files.", color="#9CA3AF", text_align=ft.TextAlign.CENTER),
                        ft.Divider(height=10, color="#1F2937"),
                        
                        ft.TextField(ref=auth_api_id, label="API ID", hint_text="Enter API ID from my.telegram.org", border_color="#374151", value=str(os.getenv("TELEGRAM_API_ID") or config.get("api_id", ""))),
                        ft.TextField(ref=auth_api_hash, label="API Hash", hint_text="Enter API Hash", border_color="#374151", password=True, value=str(os.getenv("TELEGRAM_API_HASH") or config.get("api_hash", ""))),
                        ft.TextField(ref=auth_phone, label="Phone Number", hint_text="+1234567890 (Include country code)", border_color="#374151", value=str(os.getenv("TELEGRAM_PHONE") or config.get("phone", ""))),
                        
                        ft.Button("Send Verification Code", bgcolor="#A855F7", color="white", icon=ft.Icons.SEND, on_click=lambda e: page.run_task(handle_send_code)),
                        
                        ft.Divider(height=10, color="#1F2937"),
                        ft.TextField(ref=auth_code, label="Verification Code", border_color="#374151", disabled=True),
                        ft.TextField(ref=auth_password, label="Two-Step Verification Password (Optional)", border_color="#374151", password=True, disabled=True),
                        
                        ft.Row([
                            ft.Button("Verify & Login", bgcolor="#10B981", color="white", icon=ft.Icons.VERIFIED, on_click=lambda e: page.run_task(handle_verify_login)),
                        ], alignment=ft.MainAxisAlignment.CENTER),
                        
                        ft.Divider(height=10, color="#1F2937"),
                        ft.Text("Ready", ref=auth_status, color="#9CA3AF", weight=ft.FontWeight.BOLD, text_align=ft.TextAlign.CENTER),
                    ], spacing=15, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                    bgcolor="#1A1A1E",
                    padding=35,
                    border_radius=16,
                    border=ft.Border.all(1, "#374151"),
                    width=460,
                )
            ], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            expand=True,
            bgcolor="#121214"
        )

    # ----------------------------------------------------
    # Shell Navigation & Switch Logic
    # ----------------------------------------------------
    logger.debug("Setting default view to Downloader...")
    content_lv.controls = build_downloader_view()

    def on_nav_change(e):
        index = e.control.selected_index
        logger.info(f"Sidebar navigation event fired. Index: {index}")
        try:
            if index == 0:
                content_lv.controls = build_downloader_view()
            elif index == 1:
                content_lv.controls = build_scheduler_view()
                page.update()
                refresh_queue_table()
                return
            elif index == 2:
                content_lv.controls = build_monitor_view()
            elif index == 3:
                content_lv.controls = build_stats_view()
            elif index == 4:
                content_lv.controls = build_settings_view()
            logger.debug("Assigned new content view to content_lv. Updating page...")
            page.update()
            logger.debug("Navigation page update successful.")
        except Exception as ex:
            logger.error(f"Error during on_nav_change transition: {traceback.format_exc()}")
            page.update()

    # Sidebar Navigation Rail
    nav_rail = ft.NavigationRail(
        selected_index=0,
        label_type=ft.NavigationRailLabelType.ALL,
        min_width=100,
        min_extended_width=200,
        bgcolor="#1A1A1E",
        leading=ft.Container(
            content=ft.Image(src="https://upload.wikimedia.org/wikipedia/commons/8/82/Telegram_logo.svg", width=40, height=40),
            padding=ft.Padding(left=0, right=0, top=20, bottom=20)
        ),
        destinations=[
            ft.NavigationRailDestination(
                icon=ft.Icons.DOWNLOAD,
                selected_icon=ft.Icons.DOWNLOAD,
                label="Downloader"
            ),
            ft.NavigationRailDestination(
                icon=ft.Icons.SCHEDULE,
                selected_icon=ft.Icons.SCHEDULE,
                label="Scheduler"
            ),
            ft.NavigationRailDestination(
                icon=ft.Icons.MONITOR,
                selected_icon=ft.Icons.MONITOR,
                label="Monitor"
            ),
            ft.NavigationRailDestination(
                icon=ft.Icons.BAR_CHART,
                selected_icon=ft.Icons.BAR_CHART,
                label="Stats"
            ),
            ft.NavigationRailDestination(
                icon=ft.Icons.SETTINGS,
                selected_icon=ft.Icons.SETTINGS,
                label="Settings"
            ),
        ],
        on_change=on_nav_change
    )

    # Assemble Main Dashboard Layout structure
    main_layout.controls = [
        nav_rail,
        ft.VerticalDivider(width=1, color="#1F2937"),
        content_lv
    ]

    # ----------------------------------------------------
    # On Startup: Check Authentication State
    # ----------------------------------------------------
    load_dotenv(os.path.join(PROJECT_ROOT, '.env'))
    api_id = os.getenv('TELEGRAM_API_ID') or config.get("api_id")
    api_hash = os.getenv('TELEGRAM_API_HASH') or config.get("api_hash")
    
    logger.debug(f"Auth Check - API ID: {api_id}, API Hash: {'*****' if api_hash else None}")
    
    auth_panel.content = build_auth_view()
    page.add(auth_panel)
    
    if api_id and api_hash:
        session_path = os.path.join(PROJECT_ROOT, 'telegram_dl_session')
        logger.debug(f"Pre-initializing Telethon Client at {session_path}...")
        client = TelegramClient(session_path, int(api_id), api_hash)
        try:
            await client.connect()
            logger.debug("Telethon client connected. Checking authorization...")
            if await client.is_user_authorized():
                logger.info("Client is authorized. Bypassing onboarding login panel directly...")
                page.controls.clear()
                page.add(main_layout)
                page.update()
            else:
                logger.warning("Session exists but client is NOT authorized. Login required.")
        except Exception as e:
            logger.error(f"Error checking startup auth state: {traceback.format_exc()}")
            pass

    page.update()
    logger.debug("Initial page update complete.")

# Run Flet Application in Desktop Window
if __name__ == '__main__':
    ft.run(main)
