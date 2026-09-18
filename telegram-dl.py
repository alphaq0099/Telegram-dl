import os
import sys
import json
import asyncio
import re
import logging

# Silence Telethon connection warnings to prevent terminal menu corruption
logging.getLogger('telethon').setLevel(logging.ERROR)
from datetime import datetime
import questionary
from questionary import Style
from utils.auth import get_client, authenticate_client
from utils.download import get_config
from modules.single import download_single
from modules.range import download_range
from modules.full import download_full
from modules.daemon import handle_daemon_menu
from modules.utilities import handle_utilities_menu
from modules.search import handle_search_menu
from modules.cloud import handle_cloud_menu
from modules.queue_manager import handle_queue_menu

# Define custom styling for questionary to match a sleek Telegram/Cyberdrop theme
custom_style = Style([
    ('qmark', 'fg:#61afef bold'),       # Question mark icon (soft cyan/blue)
    ('question', 'bold fg:#ffffff'),    # The question text
    ('answer', 'fg:#61afef bold'),      # Selected answer
    ('pointer', 'fg:#61afef bold'),     # Choice pointer >
    ('highlighted', 'fg:#61afef bold'), # Highlighted option text
    ('selected', 'fg:#98c379 noinherit'), # Checked option (checkboxes)
    ('instruction', 'fg:#5c6370 italic'), # Help/Instruction text
    ('separator', 'fg:#5c6370'),        # Color of separators and help text
])

TITLE_STYLE = "=" * 50
SUBTITLE_STYLE = "-" * 50

def format_media_types(types_list):
    """Formats the list of media types with representative emojis."""
    emoji_map = {
        "photo": "📷 photo",
        "video": "🎥 video",
        "document": "📄 document",
        "audio": "🎵 audio",
        "voice": "🎙️ voice",
        "sticker": "🏷️ sticker"
    }
    return ", ".join([emoji_map.get(t, t) for t in types_list])

def print_header():
    """Clears the console and prints the Telegram Downloader header."""
    os.system('cls' if os.name == 'nt' else 'clear')
    print("\033[1;36mTelegram Downloader (V3.9.0)\033[0m")
    
    # Read download dir and allowed types to show status
    config = get_config()
    dl_dir = config.get("download_dir", "./downloads")
    media_types = config.get("media_types", [])
    
    # Format config name (Default or Custom depending on folder)
    config_name = "Default" if dl_dir == "./downloads" else "Custom"
    print(f"\033[1;30m⚙️  {'Current config:'.ljust(18)}\033[0m \033[1;34m{config_name}\033[0m")
    print(f"\033[1;30m📁  {'Download Folder:'.ljust(18)}\033[0m \033[0;32m{dl_dir}\033[0m")
    print(f"\033[1;30m🔍  {'Allowed Types:'.ljust(18)}\033[0m \033[1;34m{format_media_types(media_types)}\033[0m")
    print()

from utils.auth import PROJECT_ROOT

def update_config_file(new_config):
    """Saves changes back to config.json."""
    config_path = os.path.join(PROJECT_ROOT, 'config.json')
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(new_config, f, indent=2)
        print("\033[1;32m[SUCCESS] Configuration saved successfully!\033[0m")
    except Exception as e:
        print(f"\033[1;31m[ERROR] Failed to save configuration: {str(e)}\033[0m", file=sys.stderr)

async def handle_config_menu():
    """Sub-menu to edit config parameters interactively."""
    while True:
        print_header()
        config = get_config()
        download_dir = config.get("download_dir", "./downloads")
        media_types = config.get("media_types", [])
        max_concurrent = config.get("max_concurrent_downloads", 3)
        max_speed = config.get("max_download_speed_kbps", 0)
        speed_str = "Unlimited" if max_speed == 0 else f"{max_speed} KB/s"
        
        # Gather active filters representation
        filters = []
        min_size = config.get("filter_min_size_mb", 0)
        max_size = config.get("filter_max_size_mb", 0)
        if min_size > 0:
            filters.append(f"Min Size: {min_size}MB")
        if max_size > 0:
            filters.append(f"Max Size: {max_size}MB")
            
        after_date = config.get("filter_after_date", "")
        before_date = config.get("filter_before_date", "")
        if after_date:
            filters.append(f"After: {after_date}")
        if before_date:
            filters.append(f"Before: {before_date}")
            
        allowed_exts = config.get("filter_allowed_extensions", [])
        if allowed_exts:
            filters.append(f"Exts: {', '.join(allowed_exts)}")
            
        fn_regex = config.get("filter_filename_regex", "")
        if fn_regex:
            filters.append(f"File Regex: {fn_regex}")
            
        cap_regex = config.get("filter_caption_regex", "")
        if cap_regex:
            filters.append(f"Caption Regex: {cap_regex}")
            
        filters_str = ", ".join(filters) if filters else "None (Download All)"
        enable_gallery = config.get("enable_gallery", True)
        
        print("\033[1;35m--- CONFIGURATION DETAILS ---\033[0m")
        print(f"📁  {'Download Directory:'.ljust(25)} \033[1;32m{download_dir}\033[0m")
        print(f"🔍  {'Allowed Media Types:'.ljust(25)} \033[1;32m{format_media_types(media_types)}\033[0m")
        print(f"⚡  {'Max Concurrent Downloads:'.ljust(25)} \033[1;32m{max_concurrent}\033[0m")
        print(f"📉  {'Max Download Speed:'.ljust(25)} \033[1;32m{speed_str}\033[0m")
        print(f"🎨  {'Generate Web Gallery:'.ljust(25)} \033[1;32m{'ON' if enable_gallery else 'OFF'}\033[0m")
        monitor_date_org = config.get("monitor_date_organization", "none")
        print(f"📅  {'Monitor Date Folder:'.ljust(25)} \033[1;32m{monitor_date_org.capitalize()}\033[0m")
        bot_grouping = config.get("monitor_bot_session_grouping", True)
        bot_gap = config.get("monitor_bot_session_gap_seconds", 60)
        print(f"🤖  {'Bot Session Grouping:'.ljust(25)} \033[1;32m{'ON' if bot_grouping else 'OFF'} ({bot_gap}s gap)\033[0m")
        print(f"⚙️  {'Active Filters:'.ljust(25)} \033[1;33m{filters_str}\033[0m")
        print("\033[1;35m-----------------------------\033[0m\n")
        
        choice = await questionary.select(
            "What would you like to edit?",
            choices=[
                "Change download directory",
                "Toggle allowed media types",
                "Change max concurrent downloads",
                "Change max download speed",
                "Toggle web gallery generation",
                "Toggle monitor date organization",
                "Toggle bot session grouping",
                "Change bot session inactivity gap",
                "Manage advanced filters...",
                "Back to main menu",
                questionary.Separator(),
                questionary.Separator("ARROW KEYS: Navigate | ENTER: Select | Ctrl+C: Cancel/Exit")
            ],
            instruction="",
            style=custom_style
        ).ask_async()
        
        if choice is None or choice == "Back to main menu":
            break
            
        if choice == "Change download directory":
            new_dir = await questionary.text(
                "Enter new download directory path:",
                default=download_dir,
                style=custom_style
            ).ask_async()
            if new_dir is not None:
                config["download_dir"] = new_dir.strip()
                update_config_file(config)
                await asyncio.sleep(1.5)
                
        elif choice == "Toggle allowed media types":
            all_types = ["photo", "video", "document", "audio", "voice", "sticker"]
            selected_types = await questionary.checkbox(
                "Select allowed media types (Space to check/uncheck):",
                choices=[
                    questionary.Choice(t, checked=(t in media_types)) for t in all_types
                ],
                style=custom_style
            ).ask_async()
            if selected_types is not None:
                config["media_types"] = selected_types
                update_config_file(config)
                await asyncio.sleep(1.5)

        elif choice == "Change max concurrent downloads":
            new_concurrent = await questionary.text(
                "Enter maximum concurrent downloads (1-10):",
                default=str(max_concurrent),
                style=custom_style
            ).ask_async()
            if new_concurrent is not None:
                try:
                    val = int(new_concurrent)
                    if 1 <= val <= 10:
                        config["max_concurrent_downloads"] = val
                        update_config_file(config)
                    else:
                        print("\033[1;31m[ERROR] Please enter a value between 1 and 10.\033[0m")
                except ValueError:
                    print("\033[1;31m[ERROR] Please enter a valid integer.\033[0m")
                await asyncio.sleep(1.5)
                
        elif choice == "Change max download speed":
            new_speed = await questionary.text(
                "Enter max speed in KB/s (0 for unlimited):",
                default=str(max_speed),
                style=custom_style
            ).ask_async()
            if new_speed is not None:
                try:
                    val = int(new_speed)
                    if val >= 0:
                        config["max_download_speed_kbps"] = val
                        update_config_file(config)
                    else:
                        print("\033[1;31m[ERROR] Speed cannot be negative.\033[0m")
                except ValueError:
                    print("\033[1;31m[ERROR] Please enter a valid integer.\033[0m")
                await asyncio.sleep(1.5)
                
        elif choice == "Toggle web gallery generation":
            config["enable_gallery"] = not enable_gallery
            update_config_file(config)
            print(f"\n✅  \033[1;32mWeb Gallery Generation toggled to {'ON' if not enable_gallery else 'OFF'}!\033[0m")
            
            # If toggled ON, offer to regenerate existing galleries
            if not enable_gallery: # means it is now ON
                rebuild = await questionary.confirm(
                    "Would you like to build/regenerate HTML galleries for all existing downloads now?",
                    default=True,
                    style=custom_style
                ).ask_async()
                if rebuild:
                    print("\n🎨 \033[1;36mScanning folders and generating HTML galleries...\033[0m")
                    from utils.download import rebuild_all_galleries
                    count = rebuild_all_galleries()
                    print(f"✅ \033[1;32mDone! Successfully generated/updated {count} gallery HTML file(s).\033[0m")
                    await asyncio.sleep(2.5)
            else:
                await asyncio.sleep(1.5)
                
        elif choice == "Toggle monitor date organization":
            options = ["none", "daily", "monthly"]
            current = config.get("monitor_date_organization", "none")
            next_idx = (options.index(current) + 1) % len(options)
            new_val = options[next_idx]
            config["monitor_date_organization"] = new_val
            update_config_file(config)
            print(f"\n✅  \033[1;32mMonitor Date Organization set to '{new_val.capitalize()}'!\033[0m")
            await asyncio.sleep(1.5)
            
        elif choice == "Toggle bot session grouping":
            current = config.get("monitor_bot_session_grouping", True)
            config["monitor_bot_session_grouping"] = not current
            update_config_file(config)
            print(f"\n✅  \033[1;32mBot Session Grouping toggled to {'ON' if not current else 'OFF'}!\033[0m")
            await asyncio.sleep(1.5)
            
        elif choice == "Change bot session inactivity gap":
            current_gap = config.get("monitor_bot_session_gap_seconds", 60)
            gap_input = await questionary.text(
                "Enter session inactivity gap in seconds (e.g. 30, 60, 120):",
                default=str(current_gap),
                style=custom_style
            ).ask_async()
            if gap_input is not None:
                try:
                    val = int(gap_input)
                    if val > 0:
                        config["monitor_bot_session_gap_seconds"] = val
                        update_config_file(config)
                        print(f"\n✅  \033[1;32mSession gap updated to {val} seconds!\033[0m")
                    else:
                        print("\033[1;31m[ERROR] Gap must be greater than 0.\033[0m")
                except ValueError:
                    print("\033[1;31m[ERROR] Please enter a valid integer.\033[0m")
                await asyncio.sleep(1.5)
            
        elif choice == "Manage advanced filters...":
            await handle_filters_menu(config)

async def handle_filters_menu(config):
    """Sub-menu to manage advanced download filters."""
    while True:
        os.system('cls' if os.name == 'nt' else 'clear')
        print("\033[1;36mTelegram Downloader (V1.0.0)\033[0m")
        print("\033[1;35m--- ADVANCED FILTER SETTINGS ---\033[0m")
        
        min_size = config.get("filter_min_size_mb", 0)
        max_size = config.get("filter_max_size_mb", 0)
        after_date = config.get("filter_after_date", "")
        before_date = config.get("filter_before_date", "")
        allowed_exts = config.get("filter_allowed_extensions", [])
        fn_regex = config.get("filter_filename_regex", "")
        cap_regex = config.get("filter_caption_regex", "")
        
        print(f"📦  {'Min File Size:'.ljust(25)} \033[1;32m{f'{min_size} MB' if min_size > 0 else 'None'}\033[0m")
        print(f"📦  {'Max File Size:'.ljust(25)} \033[1;32m{f'{max_size} MB' if max_size > 0 else 'None'}\033[0m")
        print(f"📅  {'After Date (YYYY-MM-DD):'.ljust(25)} \033[1;32m{after_date if after_date else 'None'}\033[0m")
        print(f"📅  {'Before Date (YYYY-MM-DD):'.ljust(25)} \033[1;32m{before_date if before_date else 'None'}\033[0m")
        print(f"🏷️  {'Allowed Extensions:'.ljust(25)} \033[1;32m{', '.join(allowed_exts) if allowed_exts else 'All'}\033[0m")
        print(f"🔍  {'Filename Regex:'.ljust(25)} \033[1;32m{fn_regex if fn_regex else 'None'}\033[0m")
        print(f"💬  {'Caption Regex:'.ljust(25)} \033[1;32m{cap_regex if cap_regex else 'None'}\033[0m")
        print("\033[1;35m--------------------------------\033[0m\n")
        
        choice = await questionary.select(
            "Select filter to edit:",
            choices=[
                "Edit Size Limits",
                "Edit Date Range",
                "Edit Allowed Extensions",
                "Edit Filename Regex",
                "Edit Caption Regex",
                "Clear All Filters",
                "Back to configuration",
                questionary.Separator(),
                questionary.Separator("ARROW KEYS: Navigate | ENTER: Select | Ctrl+C: Cancel/Exit")
            ],
            instruction="",
            style=custom_style
        ).ask_async()
        
        if choice is None or choice == "Back to configuration":
            break
            
        if choice == "Edit Size Limits":
            new_min = await questionary.text("Enter min file size in MB (0 for none):", default=str(min_size), style=custom_style).ask_async()
            new_max = await questionary.text("Enter max file size in MB (0 for none):", default=str(max_size), style=custom_style).ask_async()
            if new_min is not None and new_max is not None:
                try:
                    config["filter_min_size_mb"] = int(new_min)
                    config["filter_max_size_mb"] = int(new_max)
                    update_config_file(config)
                except ValueError:
                    print("\033[1;31m[ERROR] Size limits must be integers.\033[0m")
                await asyncio.sleep(1.5)
                
        elif choice == "Edit Date Range":
            new_after = await questionary.text("Enter start date (YYYY-MM-DD, or leave empty):", default=after_date, style=custom_style).ask_async()
            new_before = await questionary.text("Enter end date (YYYY-MM-DD, or leave empty):", default=before_date, style=custom_style).ask_async()
            if new_after is not None and new_before is not None:
                valid = True
                for d_str in [new_after.strip(), new_before.strip()]:
                    if d_str:
                        try:
                            datetime.strptime(d_str, "%Y-%m-%d")
                        except ValueError:
                            print(f"\033[1;31m[ERROR] Invalid date format: '{d_str}'. Must be YYYY-MM-DD.\033[0m")
                            valid = False
                if valid:
                    config["filter_after_date"] = new_after.strip()
                    config["filter_before_date"] = new_before.strip()
                    update_config_file(config)
                await asyncio.sleep(1.5)
                
        elif choice == "Edit Allowed Extensions":
            ext_input = await questionary.text(
                "Enter comma-separated extensions (e.g. pdf, mp4, zip) or leave empty for all:",
                default=", ".join(allowed_exts),
                style=custom_style
            ).ask_async()
            if ext_input is not None:
                exts = [e.strip().lstrip('.').lower() for e in ext_input.split(",") if e.strip()]
                config["filter_allowed_extensions"] = exts
                update_config_file(config)
                await asyncio.sleep(1.5)
                
        elif choice == "Edit Filename Regex":
            regex_input = await questionary.text("Enter filename regex pattern (or leave empty):", default=fn_regex, style=custom_style).ask_async()
            if regex_input is not None:
                if regex_input.strip():
                    try:
                        re.compile(regex_input.strip())
                        config["filter_filename_regex"] = regex_input.strip()
                        update_config_file(config)
                    except re.error as e:
                        print(f"\033[1;31m[ERROR] Invalid regex: {str(e)}\033[0m")
                else:
                    config["filter_filename_regex"] = ""
                    update_config_file(config)
                await asyncio.sleep(1.5)
                
        elif choice == "Edit Caption Regex":
            regex_input = await questionary.text("Enter caption regex pattern (or leave empty):", default=cap_regex, style=custom_style).ask_async()
            if regex_input is not None:
                if regex_input.strip():
                    try:
                        re.compile(regex_input.strip())
                        config["filter_caption_regex"] = regex_input.strip()
                        update_config_file(config)
                    except re.error as e:
                        print(f"\033[1;31m[ERROR] Invalid regex: {str(e)}\033[0m")
                else:
                    config["filter_caption_regex"] = ""
                    update_config_file(config)
                await asyncio.sleep(1.5)
                
        elif choice == "Clear All Filters":
            config["filter_min_size_mb"] = 0
            config["filter_max_size_mb"] = 0
            config["filter_after_date"] = ""
            config["filter_before_date"] = ""
            config["filter_filename_regex"] = ""
            config["filter_caption_regex"] = ""
            config["filter_allowed_extensions"] = []
            update_config_file(config)
            print("\033[1;32mAll active filters cleared successfully!\033[0m")
            await asyncio.sleep(1.5)

async def confirm_filters_and_start(mode_name):
    """
    Shows current filters and prompts the user to confirm the download or customize the filters first.
    Returns True if user chose to proceed with download, False if cancelled.
    """
    while True:
        config = get_config()
        
        # Gather active filters
        filters = []
        min_size = config.get("filter_min_size_mb", 0)
        max_size = config.get("filter_max_size_mb", 0)
        if min_size > 0:
            filters.append(f"Min Size: {min_size}MB")
        if max_size > 0:
            filters.append(f"Max Size: {max_size}MB")
            
        after_date = config.get("filter_after_date", "")
        before_date = config.get("filter_before_date", "")
        if after_date:
            filters.append(f"After: {after_date}")
        if before_date:
            filters.append(f"Before: {before_date}")
            
        allowed_exts = config.get("filter_allowed_extensions", [])
        if allowed_exts:
            filters.append(f"Exts: {', '.join(allowed_exts)}")
            
        fn_regex = config.get("filter_filename_regex", "")
        if fn_regex:
            filters.append(f"File Regex: {fn_regex}")
            
        cap_regex = config.get("filter_caption_regex", "")
        if cap_regex:
            filters.append(f"Caption Regex: {cap_regex}")
            
        filters_str = ", ".join(filters) if filters else "None (Download All Media)"
        
        print(f"\n⚙️  \033[1;30m[FILTER PREVIEW] Active Filters for this {mode_name}:\033[0m")
        print(f"👉  \033[1;33m{filters_str}\033[0m\n")
        
        choice = await questionary.select(
            "Ready to download?",
            choices=[
                "Yes, start download",
                "Customize filters first...",
                "Cancel download"
            ],
            instruction="",
            style=custom_style
        ).ask_async()
        
        if choice is None or choice == "Cancel download":
            return False
        elif choice == "Yes, start download":
            return True
        elif choice == "Customize filters first...":
            await handle_filters_menu(config)

async def main():
    # 1. Initialize client
    client, phone = get_client()
    
    # 2. Authenticate (triggers interactive login if needed)
    try:
        # Clear screen before auth check
        os.system('cls' if os.name == 'nt' else 'clear')
        await authenticate_client(client, phone)
        await asyncio.sleep(1)
    except Exception as e:
        print(f"\n\033[1;31m[CRITICAL ERROR] Failed to connect/authenticate: {str(e)}\033[0m", file=sys.stderr)
        sys.exit(1)

    # 3. CLI Main Loop
    while True:
        try:
            print_header()
            
            # Calculate dynamic schedule countdown if active
            from modules.queue_manager import get_scheduled_time_left, update_menu_countdown_loop, SchedulerTriggeredException
            time_left = get_scheduled_time_left()
            init_title = f"Scheduled Queue Manager (⌛ {time_left})" if time_left else "Scheduled Queue Manager..."
            queue_choice_obj = questionary.Choice(title=init_title, value="Scheduled Queue Manager...")
            
            ticker_task = asyncio.create_task(update_menu_countdown_loop(queue_choice_obj, "Scheduled Queue Manager"))
            
            choice = await questionary.select(
                "What would you like to do:",
                choices=[
                    "Single Download",
                    "Range Download",
                    "Full Download",
                    "Live Monitor Daemon",
                    queue_choice_obj,
                    "Media Utilities...",
                    "Cloud Sync & Mirroring...",
                    "History & Statistics...",
                    "Config",
                    "Exit",
                    questionary.Separator(),
                    questionary.Separator("ARROW KEYS: Navigate | ENTER: Select | Ctrl+C: Cancel/Exit")
                ],
                instruction="",
                style=custom_style
            ).ask_async()
            
            ticker_task.cancel()
            
            if choice is None or choice == "Exit":
                print("\n🔌 \033[1;34mDisconnecting from Telegram... Goodbye!\033[0m")
                await client.disconnect()
                break
                
            res = None
            if choice == "Single Download":
                print(f"\n{SUBTITLE_STYLE}")
                print(" \033[1;35mMODE: SINGLE FILE DOWNLOAD\033[0m ")
                print(f"{SUBTITLE_STYLE}")
                link = await questionary.text(
                    "Enter message link (e.g. https://t.me/c/123/45):",
                    style=custom_style
                ).ask_async()
                
                if link == "SCHEDULER_TRIGGERED":
                    raise SchedulerTriggeredException()
                elif not link:
                    print("\n⚠️ \033[1;33mOperation cancelled: link cannot be empty.\033[0m")
                    await asyncio.sleep(1.5)
                    continue
                else:
                    print("\n🚀 \033[1;36mStarting download...\033[0m")
                    await download_single(client, link)
                    input("\n⌨️ Press Enter to return to menu...")
                
            elif choice == "Range Download":
                print(f"\n{SUBTITLE_STYLE}")
                print(" \033[1;35mMODE: RANGE OF MESSAGES DOWNLOAD\033[0m ")
                print(f"{SUBTITLE_STYLE}")
                start_link = await questionary.text(
                    "Enter START message link (e.g. https://t.me/c/123/10):",
                    style=custom_style
                ).ask_async()
                
                if start_link == "SCHEDULER_TRIGGERED":
                    raise SchedulerTriggeredException()
                elif not start_link:
                    print("\n⚠️ \033[1;33mOperation cancelled: start link cannot be empty.\033[0m")
                    await asyncio.sleep(1.5)
                    continue
                else:
                    end_link = await questionary.text(
                        "Enter END message link (e.g. https://t.me/c/123/15):",
                        style=custom_style
                    ).ask_async()
                    
                    if end_link == "SCHEDULER_TRIGGERED":
                        raise SchedulerTriggeredException()
                    elif not end_link:
                        print("\n⚠️ \033[1;33mOperation cancelled: end link cannot be empty.\033[0m")
                        await asyncio.sleep(1.5)
                        continue
                    else:
                        # Confirm filters before starting
                        if not await confirm_filters_and_start("Range Download"):
                            print("\n⚠️ \033[1;33mDownload cancelled.\033[0m")
                            await asyncio.sleep(1.5)
                            continue
                            
                        print("\n🚀 \033[1;36mStarting range download...\033[0m")
                        await download_range(client, start_link, end_link, None)
                        input("\n⌨️ Press Enter to return to menu...")
                
            elif choice == "Full Download":
                print(f"\n{SUBTITLE_STYLE}")
                print(" \033[1;35mMODE: ENTIRE CHAT DOWNLOAD\033[0m ")
                print(f"{SUBTITLE_STYLE}")
                chat_input = await questionary.text(
                    "Enter chat username, link, or ID:",
                    style=custom_style
                ).ask_async()
                
                if chat_input == "SCHEDULER_TRIGGERED":
                    raise SchedulerTriggeredException()
                elif not chat_input:
                    print("\n⚠️ \033[1;33mOperation cancelled: chat input cannot be empty.\033[0m")
                    await asyncio.sleep(1.5)
                    continue
                else:
                    limit_input = await questionary.text(
                        "Enter maximum messages to scan (leave empty for ALL):",
                        style=custom_style
                    ).ask_async()
                    
                    if limit_input == "SCHEDULER_TRIGGERED":
                        raise SchedulerTriggeredException()
                    else:
                        limit = None
                        if limit_input:
                            try:
                                limit = int(limit_input)
                            except ValueError:
                                print("⚠️ \033[1;33mInvalid number. Proceeding with downloading ALL messages.\033[0m")
                        
                        # Confirm filters before starting
                        if not await confirm_filters_and_start("Full Download"):
                            print("\n⚠️ \033[1;33mDownload cancelled.\033[0m")
                            await asyncio.sleep(1.5)
                            continue
                            
                        print("\n🚀 \033[1;36mStarting full chat scan...\033[0m")
                        await download_full(client, chat_input, None, limit)
                        input("\n⌨️ Press Enter to return to menu...")
                
            elif choice == "Live Monitor Daemon":
                await handle_daemon_menu(client, custom_style)
                
            elif choice and choice.startswith("Scheduled Queue Manager"):
                await handle_queue_menu(client, custom_style)
                
            elif choice == "Media Utilities...":
                await handle_utilities_menu(client, custom_style)
                
            elif choice == "Cloud Sync & Mirroring...":
                await handle_cloud_menu(client, custom_style)
                
            elif choice == "History & Statistics...":
                await handle_search_menu(custom_style)
                
            elif choice == "Config":
                await handle_config_menu()
                
        except SchedulerTriggeredException:
            # Clean up ticker task if it is still running
            try:
                ticker_task.cancel()
            except Exception:
                pass
                
            os.system('cls' if os.name == 'nt' else 'clear')
            print("\033[1;36mTelegram Downloader (V3.9.0)\033[0m")
            print("\033[1;35m--- SCHEDULER QUEUE RUN ---\033[0m\n")
            print("⏰ \033[1;36mScheduled time reached! Starting download queue...\033[0m\n")
            from modules.queue_manager import process_queue
            await process_queue(client)
            input("\n⌨️ Press Enter to return to menu...")

if __name__ == "__main__":
    # Ensure Windows asyncio policy is compatible
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nScript interrupted by user. Exiting...")
        sys.exit(0)
