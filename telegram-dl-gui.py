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

# Setup logging to console and file
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(PROJECT_ROOT, 'gui_debug.log'), encoding='utf-8', mode='w')
    ]
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
    page.title = "Telegram Downloader & Media Manager"
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
    settings_status = ft.Ref[ft.Text]()
    settings_add_monitored = ft.Ref[ft.TextField]()
    settings_monitored_status = ft.Ref[ft.Text]()
    
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
                if not event.message.media:
                    return
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
                except Exception as err:
                    log_to_monitor_feed(f"   ↳ ❌ Error: {str(err)}")
                    
            try:
                # Add Telethon event handler
                monitor_handler = client.add_event_handler(gui_new_message_callback, events.NewMessage(chats=chat_ids))
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
            if monitor_handler:
                try:
                    client.remove_event_handler(monitor_handler)
                except Exception as ex:
                    logger.error(f"Error removing monitor event handler: {ex}")
                monitor_handler = None
            
            monitor_active = False
            
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
    def build_stats_view():
        logger.debug("Building build_stats_view controls...")
        return [
                ft.Text("History & Statistics Dashboard", theme_style=ft.TextThemeStyle.HEADLINE_MEDIUM, weight=ft.FontWeight.BOLD, color="#3B82F6"),
                ft.Text("Search downloaded history archives or check storage and chat distribution metrics.", color="#9CA3AF"),
                ft.Divider(height=10, color="#1F2937"),
                
                # Storage Metrics Card
                ft.Row([
                    ft.Container(
                        content=ft.Column([
                            ft.Text("Total Storage Used", size=14, color="#9CA3AF"),
                            ft.Text("1.42 GB", size=24, weight=ft.FontWeight.BOLD, color="#3B82F6"),
                            ft.ProgressBar(value=0.2, color="#3B82F6", bgcolor="#1F2937")
                        ], spacing=10),
                        bgcolor="#1A1A1E",
                        padding=20,
                        border_radius=12,
                        border=ft.Border.all(1, "#374151"),
                        expand=True
                    ),
                    ft.Container(
                        content=ft.Column([
                            ft.Text("Total Files Archived", size=14, color="#9CA3AF"),
                            ft.Text("32 files", size=24, weight=ft.FontWeight.BOLD, color="#10B981"),
                            ft.ProgressBar(value=0.1, color="#10B981", bgcolor="#1F2937")
                        ], spacing=10),
                        bgcolor="#1A1A1E",
                        padding=20,
                        border_radius=12,
                        border=ft.Border.all(1, "#374151"),
                        expand=True
                    ),
                ], spacing=20),
                
                # History Search Card
                ft.Container(
                    content=ft.Column([
                        ft.Row([ft.Icon(ft.Icons.SEARCH, color="#3B82F6"), ft.Text("Search Library Archives", weight=ft.FontWeight.BOLD, size=16)]),
                        ft.Row([
                            ft.TextField(label="Keyword / Filename search", border_color="#374151", expand=True, disabled=True, value="Database loading..."),
                            ft.IconButton(ft.Icons.SEARCH, icon_color="#3B82F6", disabled=True)
                        ], spacing=15),
                        ft.DataTable(
                            columns=[
                                ft.DataColumn(ft.Text("Filename")),
                                ft.DataColumn(ft.Text("Chat")),
                                ft.DataColumn(ft.Text("Size")),
                            ],
                            rows=[
                                ft.DataRow(cells=[
                                    ft.DataCell(ft.Text("Database results will render here")),
                                    ft.DataCell(ft.Text("Sync active")),
                                    ft.DataCell(ft.Text("0 B")),
                                ])
                            ]
                        )
                    ], spacing=15),
                    bgcolor="#1A1A1E",
                    padding=20,
                    border_radius=12,
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
        try:
            current_cfg["api_id"] = int(settings_api_id.current.value.strip())
        except ValueError:
            settings_status.current.value = "Error: API ID must be a valid integer."
            settings_status.current.color = "red"
            page.update()
            return
            
        current_cfg["api_hash"] = settings_api_hash.current.value.strip()
        current_cfg["download_dir"] = settings_download_dir.current.value.strip()
        
        try:
            current_cfg["speed_limit_kb"] = int(settings_speed_limit.current.value.strip())
        except ValueError:
            current_cfg["speed_limit_kb"] = 0
            
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

    def build_settings_view():
        logger.debug("Building build_settings_view controls...")
        
        # Read monitored chats from config
        config_data = get_config()
        monitored_list = config_data.get("monitored_chats", [])
        
        monitored_rows = []
        for c in monitored_list:
            c_id = c.get("id") if isinstance(c, dict) else c
            c_title = c.get("title") if isinstance(c, dict) else str(c)
            
            monitored_rows.append(
                ft.Row([
                    ft.Icon(ft.Icons.CHAT_BUBBLE_OUTLINE, color="#10B981", size=18),
                    ft.Text(f"{c_title} (ID: {c_id})", color="#E5E7EB", expand=True),
                    ft.IconButton(
                        icon=ft.Icons.DELETE_OUTLINE,
                        icon_color="red",
                        tooltip="Remove from Monitoring",
                        on_click=lambda e, chat_id=c_id: handle_remove_monitored_chat(chat_id)
                    )
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
            )
            
        return [
                ft.Text("Settings & Configurations", theme_style=ft.TextThemeStyle.HEADLINE_MEDIUM, weight=ft.FontWeight.BOLD, color="#F59E0B"),
                ft.Text("Modify download parameters, API credentials, and directories.", color="#9CA3AF"),
                ft.Divider(height=10, color="#1F2937"),
                
                # API Credentials Section
                ft.Container(
                    content=ft.Column([
                        ft.Row([ft.Icon(ft.Icons.KEY, color="#F59E0B"), ft.Text("API Credentials", weight=ft.FontWeight.BOLD, size=16)]),
                        ft.TextField(ref=settings_api_id, label="API ID", value=str(config_data.get("api_id", "")), border_color="#374151"),
                        ft.TextField(ref=settings_api_hash, label="API Hash", value=str(config_data.get("api_hash", "")), password=True, border_color="#374151"),
                    ], spacing=15),
                    bgcolor="#1A1A1E",
                    padding=20,
                    border_radius=12,
                    border=ft.Border.all(1, "#374151")
                ),
                
                # Directories and limits Section
                ft.Container(
                    content=ft.Column([
                        ft.Row([ft.Icon(ft.Icons.FOLDER, color="#F59E0B"), ft.Text("Download Directory & Speed", weight=ft.FontWeight.BOLD, size=16)]),
                        ft.TextField(ref=settings_download_dir, label="Downloads Folder Path", value=str(config_data.get("download_dir", "./downloads")), border_color="#374151"),
                        ft.TextField(ref=settings_speed_limit, label="Max Speed Limit (KB/s - 0 for Unlimited)", value=str(config_data.get("speed_limit_kb", "0")), border_color="#374151"),
                        ft.Row([
                            ft.Button("Save Configurations", bgcolor="#F59E0B", color="white", on_click=lambda e: page.run_task(handle_save_settings)),
                            ft.Text("", ref=settings_status, weight=ft.FontWeight.BOLD)
                        ], spacing=15)
                    ], spacing=15),
                    bgcolor="#1A1A1E",
                    padding=20,
                    border_radius=12,
                    border=ft.Border.all(1, "#374151")
                ),

                # Monitored Chats Section
                ft.Container(
                    content=ft.Column([
                        ft.Row([ft.Icon(ft.Icons.REMOVE_RED_EYE, color="#F59E0B"), ft.Text("Manage Monitored Chats", weight=ft.FontWeight.BOLD, size=16)]),
                        
                        # Add a new chat to monitor
                        ft.Row([
                            ft.TextField(
                                ref=settings_add_monitored,
                                label="Add Chat (Username, Numeric ID, or Invite Link)",
                                hint_text="e.g. channel_username or -100123456789",
                                border_color="#374151",
                                expand=True
                            ),
                            ft.ElevatedButton(
                                "Add to Monitor",
                                bgcolor="#F59E0B",
                                color="white",
                                on_click=lambda e: page.run_task(handle_add_monitored_chat)
                            )
                        ], spacing=10),
                        
                        # Status message
                        ft.Text("", ref=settings_monitored_status, weight=ft.FontWeight.BOLD),
                        
                        # Monitored list
                        ft.Text("Monitored Chats List:", weight=ft.FontWeight.BOLD, size=14, color="#9CA3AF"),
                        ft.Column(monitored_rows, spacing=5) if monitored_rows else ft.Text("No chats currently monitored.", color="#6B7280", italic=True)
                    ], spacing=15),
                    bgcolor="#1A1A1E",
                    padding=20,
                    border_radius=12,
                    border=ft.Border.all(1, "#374151")
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
