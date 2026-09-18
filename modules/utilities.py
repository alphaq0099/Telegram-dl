import os
import sys
import json
import asyncio
import shutil
import re
import gzip
import subprocess
import zipfile
import sqlite3
from datetime import datetime
import questionary
from tqdm import tqdm
from utils.download import get_config
from utils.parser import resolve_chat_entity, parse_telegram_link
from utils.archive import DB_PATH
from modules.duplicates import run_duplicate_checker

from utils.auth import PROJECT_ROOT

def update_config(new_config):
    """Saves changes back to config.json."""
    config_path = os.path.join(PROJECT_ROOT, 'config.json')
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(new_config, f, indent=2)
    except Exception as e:
        print(f"\033[1;31m[ERROR] Failed to save configuration: {str(e)}\033[0m", file=sys.stderr)

def is_ffmpeg_available():
    """Checks if ffmpeg is available in the system PATH."""
    return shutil.which("ffmpeg") is not None

def find_7zip():
    """Finds the path to 7-Zip executable on Windows or unix."""
    candidates = [
        shutil.which("7z"),
        shutil.which("7z.exe"),
        "C:\\Program Files\\7-Zip\\7z.exe",
        "C:\\Program Files (x86)\\7-Zip\\7z.exe"
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None

async def select_chat_folder(config, custom_style):
    """Helper to let the user select a downloaded chat directory or a custom path."""
    download_dir = config.get("download_dir", "./downloads")
    if not os.path.isabs(download_dir):
        download_dir = os.path.join(PROJECT_ROOT, download_dir)
        
    os.makedirs(download_dir, exist_ok=True)
    
    # List subdirectories
    subdirs = [d for d in os.listdir(download_dir) if os.path.isdir(os.path.join(download_dir, d))]
    
    choices = [questionary.Choice(title=f"📁 {d}", value=os.path.join(download_dir, d)) for d in subdirs]
    choices.append(questionary.Choice(title="🔍 Enter a custom directory path...", value="CUSTOM"))
    choices.append(questionary.Choice(title="❌ Cancel", value="CANCEL"))
    
    choice = await questionary.select(
        "Select target directory/chat folder:",
        choices=choices,
        style=custom_style
    ).ask_async()
    
    if choice == "SCHEDULER_TRIGGERED":
        return "SCHEDULER_TRIGGERED"
        
    if choice == "CANCEL" or choice is None:
        return None
    elif choice == "CUSTOM":
        custom_path = await questionary.text("Enter absolute folder path:", style=custom_style).ask_async()
        if custom_path == "SCHEDULER_TRIGGERED":
            return "SCHEDULER_TRIGGERED"
        if custom_path and os.path.isdir(custom_path.strip()):
            return custom_path.strip()
        else:
            print("\033[1;31m[ERROR] Directory does not exist!\033[0m")
            await asyncio.sleep(2)
            return None
    return choice

async def handle_utilities_menu(client, custom_style):
    """Sleek sub-menu to manage media utility tools."""
    while True:
        os.system('cls' if os.name == 'nt' else 'clear')
        print("\033[1;36mTelegram Downloader (V3.9.0)\033[0m")
        print("\033[1;35m--- MEDIA & FILE UTILITIES ---\033[0m")
        
        config = get_config()
        auto_extract = config.get("auto_extract_audio", False)
        
        print(f"⚡  Auto-extract Audio on Download: \033[1;32m{'ON' if auto_extract else 'OFF'}\033[0m")
        print("\033[1;35m------------------------------\033[0m\n")
        
        choice = await questionary.select(
            "Select utility to run:",
            choices=[
                "Extract Audio from Video (Select File)",
                "Convert WebP Sticker to PNG (Select File)",
                "Advanced Media Converters (ffmpeg)...",
                "Scan for Duplicate Media...",
                "Batch Rename Files",
                "Organize Folder Files (by Type / message ID)",
                "Password-Protect / Batch Archive Folder",
                "Toggle Auto-extract Audio",
                "Back to main menu",
                questionary.Separator(),
                questionary.Separator("ARROW KEYS: Navigate | ENTER: Select | Ctrl+C: Cancel/Exit")
            ],
            instruction="",
            style=custom_style
        ).ask_async()
        
        if choice == "SCHEDULER_TRIGGERED":
            return "SCHEDULER_TRIGGERED"
            
        if choice is None or choice == "Back to main menu":
            break
            
        if choice == "Toggle Auto-extract Audio":
            config["auto_extract_audio"] = not auto_extract
            update_config(config)
            print(f"\n✅  \033[1;32mAuto-extract Audio toggled to {'ON' if not auto_extract else 'OFF'}!\033[0m")
            await asyncio.sleep(1.5)
            
        elif choice == "Extract Audio from Video (Select File)":
            res = await run_audio_extractor_interactive(config, custom_style)
            if res == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"
            
        elif choice == "Convert WebP Sticker to PNG (Select File)":
            res = await run_sticker_converter_interactive(config, custom_style)
            if res == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"
            
        elif choice == "Advanced Media Converters (ffmpeg)...":
            res = await run_advanced_converters_interactive(config, custom_style)
            if res == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"
            
        elif choice == "Scan for Duplicate Media...":
            res = await run_duplicate_checker(custom_style)
            if res == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"
            
        elif choice == "Batch Rename Files":
            res = await run_batch_renamer(config, custom_style)
            if res == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"
            
        elif choice == "Organize Folder Files (by Type / message ID)":
            res = await run_folder_organizer(config, custom_style)
            if res == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"
            
        elif choice == "Password-Protect / Batch Archive Folder":
            res = await run_batch_archiver(config, custom_style)
            if res == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"

# ==========================================
# 🔊 AUDIO EXTRACTOR ENGINE
# ==========================================

async def run_audio_extractor_interactive(config, custom_style):
    if not is_ffmpeg_available():
        print("\n⚠️  \033[1;33m[WARNING] ffmpeg is not installed or not in PATH!\033[0m")
        print("To extract audio from video, please install ffmpeg on Windows:")
        print("👉 Run: \033[1;36mwinget install Gyan.FFmpeg\033[0m in a separate terminal, then restart the script.")
        input("\nPress Enter to return...")
        return
        
    chat_folder = await select_chat_folder(config, custom_style)
    if chat_folder == "SCHEDULER_TRIGGERED":
        return "SCHEDULER_TRIGGERED"
    if not chat_folder:
        return
        
    # Find all mp4 files
    mp4_files = [f for f in os.listdir(chat_folder) if f.lower().endswith(".mp4")]
    if not mp4_files:
        print("\n⚠️  \033[1;33mNo .mp4 video files found in the selected folder.\033[0m")
        await asyncio.sleep(2)
        return
        
    to_extract = await questionary.select(
        "Select video file to extract audio from:",
        choices=mp4_files + ["Cancel"],
        style=custom_style
    ).ask_async()
    
    if to_extract == "SCHEDULER_TRIGGERED":
        return "SCHEDULER_TRIGGERED"
        
    if to_extract is None or to_extract == "Cancel":
        return
        
    input_path = os.path.join(chat_folder, to_extract)
    output_path = os.path.join(chat_folder, os.path.splitext(to_extract)[0] + ".mp3")
    
    print(f"\n🔄 \033[1;36mExtracting audio to: {os.path.basename(output_path)}...\033[0m")
    success = await extract_audio_file(input_path, output_path)
    if success:
        print("✅ \033[1;32mAudio successfully extracted!\033[0m")
    else:
        print("❌ \033[1;31mAudio extraction failed.\033[0m")
    await asyncio.sleep(2)

async def extract_audio_file(input_path, output_path):
    """Executes ffmpeg process to extract MP3 track from MP4."""
    try:
        # Run ffmpeg in a subprocess asynchronously
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-vn", "-acodec", "libmp3lame", "-q:a", "2",
            output_path
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
        return proc.returncode == 0
    except Exception:
        return False

# ==========================================
# 🖼️ STICKER CONVERTER ENGINE
# ==========================================

async def run_sticker_converter_interactive(config, custom_style):
    # Ensure Pillow is available
    try:
        from PIL import Image
    except ImportError:
        print("\n⏳ \033[1;36mPillow library is required for WebP conversion. Installing it now...\033[0m")
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "Pillow"], check=True)
            from PIL import Image
            print("✅ \033[1;32mPillow successfully installed!\033[0m")
        except Exception as e:
            print(f"❌ \033[1;31mFailed to install Pillow: {str(e)}. WebP conversion is unavailable.\033[0m")
            await asyncio.sleep(3)
            return

    chat_folder = await select_chat_folder(config, custom_style)
    if not chat_folder:
        return
        
    # Find all webp and tgs files
    sticker_files = [f for f in os.listdir(chat_folder) if f.lower().endswith((".webp", ".tgs"))]
    if not sticker_files:
        print("\n⚠️  \033[1;33mNo WebP static or TGS animated stickers found in the selected folder.\033[0m")
        await asyncio.sleep(2)
        return
        
    to_convert = await questionary.select(
        "Select sticker file to convert/decompress:",
        choices=sticker_files + ["Cancel"],
        style=custom_style
    ).ask_async()
    
    if to_convert is None or to_convert == "Cancel":
        return
        
    input_path = os.path.join(chat_folder, to_convert)
    
    if to_convert.lower().endswith(".webp"):
        output_path = os.path.join(chat_folder, os.path.splitext(to_convert)[0] + ".png")
        print(f"\n🔄 \033[1;36mConverting static WebP sticker to PNG...\033[0m")
        try:
            with Image.open(input_path) as img:
                img.save(output_path, "PNG")
            print(f"✅ \033[1;32mSaved: {os.path.basename(output_path)}\033[0m")
        except Exception as e:
            print(f"❌ \033[1;31mConversion failed: {str(e)}\033[0m")
            
    elif to_convert.lower().endswith(".tgs"):
        output_path = os.path.join(chat_folder, os.path.splitext(to_convert)[0] + ".json")
        print(f"\n🔄 \033[1;36mDecompressing animated TGS sticker to Lottie JSON...\033[0m")
        try:
            with gzip.open(input_path, 'rb') as f_in:
                with open(output_path, 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out)
            print(f"✅ \033[1;32mDecompressed: {os.path.basename(output_path)}\033[0m")
            print("\033[1;30m(Note: The browser gallery will now play this sticker natively in vector quality!)\033[0m")
        except Exception as e:
            print(f"❌ \033[1;31mDecompression failed: {str(e)}\033[0m")
            
    await asyncio.sleep(3)

# ==========================================
# 📂 BATCH FILE RENAMER ENGINE
# ==========================================

async def run_batch_renamer(config, custom_style):
    chat_folder = await select_chat_folder(config, custom_style)
    if not chat_folder:
        return
        
    # Get all files excluding index/metadata/db
    all_files = [f for f in os.listdir(chat_folder) if os.path.isfile(os.path.join(chat_folder, f)) 
                 and f not in ("metadata.json", "gallery.html", "chat_history.json")]
                 
    if not all_files:
        print("\n⚠️  \033[1;33mNo files found to rename in the selected directory.\033[0m")
        await asyncio.sleep(2)
        return
        
    # 1. Select Category filter
    category = await questionary.select(
        "Select file category to filter for renaming:",
        choices=[
            "All Files",
            "Images (jpg, png, webp, gif, etc.)",
            "Videos (mp4, mkv, avi, webm, etc.)",
            "Documents (pdf, txt, docx, csv, etc.)",
            "Audio (mp3, wav, flac, ogg, etc.)"
        ],
        style=custom_style
    ).ask_async()
    
    if not category:
        return
        
    ext_groups = {
        "Images": (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp", ".heic"),
        "Videos": (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm"),
        "Documents": (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".txt", ".csv", ".md"),
        "Audio": (".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a")
    }
    
    filtered_files = []
    for f in all_files:
        ext = os.path.splitext(f.lower())[1]
        if "Images" in category and ext in ext_groups["Images"]:
            filtered_files.append(f)
        elif "Videos" in category and ext in ext_groups["Videos"]:
            filtered_files.append(f)
        elif "Documents" in category and ext in ext_groups["Documents"]:
            filtered_files.append(f)
        elif "Audio" in category and ext in ext_groups["Audio"]:
            filtered_files.append(f)
        elif "All Files" in category:
            filtered_files.append(f)
            
    if not filtered_files:
        print("\n⚠️  \033[1;33mNo files matched the selected category.\033[0m")
        await asyncio.sleep(2)
        return
        
    # 2. Select Renaming Style
    style = await questionary.select(
        f"Select renaming style for {len(filtered_files)} file(s):",
        choices=[
            "Sequential Prefix (e.g. download_0001.png)",
            "Clean Names (remove spaces, hyphens, and underscores)",
            "Random Alphanumeric (12 characters)"
        ],
        style=custom_style
    ).ask_async()
    
    if not style:
        return
        
    prefix = ""
    if "Sequential" in style:
        prefix = await questionary.text("Enter sequential prefix name:", default="file", style=custom_style).ask_async()
        if prefix is None:
            return
        prefix = prefix.strip()
        
    # Proceed to rename
    print(f"\n🔄 \033[1;36mRenaming {len(filtered_files)} file(s)...\033[0m")
    success_count = 0
    fail_count = 0
    
    import random
    import string
    
    # Sort files naturally
    filtered_files.sort(key=lambda x: [int(c) if c.isdigit() else c for c in re.split(r'(\d+)', x)])
    
    for idx, f in enumerate(filtered_files, start=1):
        old_path = os.path.join(chat_folder, f)
        base, ext = os.path.splitext(f)
        
        if "Sequential" in style:
            new_name = f"{prefix}_{idx:04d}{ext}"
        elif "Clean" in style:
            # Replace spaces, hyphens, and underscores with nothing
            cleaned_base = re.sub(r'[ \-_]', '', base)
            if not cleaned_base:
                cleaned_base = "".join(random.choices(string.ascii_letters + string.digits, k=12))
            new_name = f"{cleaned_base}{ext}"
        else:
            rand_str = "".join(random.choices(string.ascii_letters + string.digits, k=12))
            new_name = f"{rand_str}{ext}"
            
        new_path = os.path.join(chat_folder, new_name)
        
        # Handle collision
        col_idx = 1
        while os.path.exists(new_path):
            if old_path == new_path:
                break
            if "Sequential" in style:
                # Increment index
                pass
            elif "Clean" in style:
                new_name = f"{cleaned_base}_{col_idx}{ext}"
                col_idx += 1
            else:
                rand_str = "".join(random.choices(string.ascii_letters + string.digits, k=12))
                new_name = f"{rand_str}{ext}"
            new_path = os.path.join(chat_folder, new_name)
            
        if old_path == new_path:
            success_count += 1
            continue
            
        try:
            os.rename(old_path, new_path)
            success_count += 1
        except Exception as e:
            print(f"❌ Failed to rename '{f}': {str(e)}")
            fail_count += 1
            
    print(f"\n🎉 \033[1;32mBatch renaming finished! Success: {success_count}, Failed: {fail_count}\033[0m")
    print("\033[1;30m(Note: Database and metadata entries will update upon next scanner validation run)\033[0m")
    await asyncio.sleep(3)

# ==========================================
# 📁 FOLDER ORGANIZER ENGINE
# ==========================================

def get_archived_files_in_range(chat_id: str, start_msg_id: int, end_msg_id: int):
    """Returns a list of filenames of archived files in a specific range for a chat."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        # Try both string and integer matching for chat_id
        cursor.execute(
            "SELECT message_id, filename FROM archive WHERE (chat_id = ? OR chat_id = ?) AND message_id >= ? AND message_id <= ?",
            (str(chat_id), str(int(chat_id)) if chat_id.replace('-','').isdigit() else str(chat_id), start_msg_id, end_msg_id)
        )
        rows = cursor.fetchall()
        conn.close()
        return [{"message_id": r[0], "filename": r[1]} for r in rows]
    except Exception:
        return []

async def run_folder_organizer(config, custom_style):
    chat_folder = await select_chat_folder(config, custom_style)
    if not chat_folder:
        return
        
    mode = await questionary.select(
        "Choose organization mode:",
        choices=[
            "Organize by File Type (Images, Videos, Docs, etc.)",
            "Organize by Custom Message ID Range (Links/IDs input)"
        ],
        style=custom_style
    ).ask_async()
    
    if not mode:
        return
        
    if "File Type" in mode:
        # Get loose files excluding directory entries & index files
        all_files = [f for f in os.listdir(chat_folder) if os.path.isfile(os.path.join(chat_folder, f)) 
                     and f not in ("metadata.json", "gallery.html", "chat_history.json")]
                     
        if not all_files:
            print("\n⚠️  \033[1;33mNo loose files found in the folder to organize.\033[0m")
            await asyncio.sleep(2)
            return
            
        moved_count = 0
        ext_groups = {
            "Images": (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp", ".heic"),
            "Videos": (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm"),
            "Documents": (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".txt", ".csv", ".md"),
            "Audio": (".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"),
            "Archives": (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2")
        }
        
        for f in all_files:
            ext = os.path.splitext(f.lower())[1]
            category = "Others"
            for cat, exts in ext_groups.items():
                if ext in exts:
                    category = cat
                    break
                    
            target_dir = os.path.join(chat_folder, category)
            os.makedirs(target_dir, exist_ok=True)
            
            try:
                shutil.move(os.path.join(chat_folder, f), os.path.join(target_dir, f))
                moved_count += 1
            except Exception as e:
                print(f"❌ Failed to move '{f}': {str(e)}")
                
        print(f"\n✅ \033[1;32mOrganization complete! Organized {moved_count} file(s).\033[0m")
        await asyncio.sleep(2)
        
    elif "Custom Message ID Range" in mode:
        # Prompt for start message link
        start_link = await questionary.text("Enter START message link (or start message ID):", style=custom_style).ask_async()
        if not start_link: return
        end_link = await questionary.text("Enter END message link (or end message ID):", style=custom_style).ask_async()
        if not end_link: return
        
        # Parse links
        chat_id_1, msg_id_1, _ = parse_telegram_link(start_link.strip())
        chat_id_2, msg_id_2, _ = parse_telegram_link(end_link.strip())
        
        start_id = None
        end_id = None
        chat_id = None
        
        # Check if they entered numeric IDs directly instead of links
        if chat_id_1 is None:
            try:
                start_id = int(start_link.strip())
            except ValueError:
                pass
        else:
            start_id = msg_id_1
            chat_id = chat_id_1
            
        if chat_id_2 is None:
            try:
                end_id = int(end_link.strip())
            except ValueError:
                pass
        else:
            end_id = msg_id_2
            if chat_id is None:
                chat_id = chat_id_2
            elif chat_id != chat_id_2:
                print("\n❌ \033[1;31m[ERROR] Start and End links must belong to the same chat!\033[0m")
                await asyncio.sleep(3.5)
                return
                
        if start_id is None or end_id is None:
            print("\n❌ \033[1;31m[ERROR] Could not parse message IDs from inputs.\033[0m")
            await asyncio.sleep(2)
            return
            
        # Ensure start_id <= end_id
        if start_id > end_id:
            start_id, end_id = end_id, start_id
            
        print(f"\n🔍 Searching for files in range: {start_id} to {end_id}...")
        
        # 1. Fetch from DB
        db_files = []
        if chat_id:
            db_files = get_archived_files_in_range(chat_id, start_id, end_id)
            
        # 2. Scan physical files in chat_folder recursively
        found_files = [] # list of tuples: (filename, relative_path, message_id)
        
        # Check all files in chat_folder recursively
        for root, dirs, files in os.walk(chat_folder):
            for f in files:
                if f in ("metadata.json", "gallery.html", "chat_history.json"):
                    continue
                    
                # Match message ID in filename using regex
                msg_match = re.search(r'^-?\d+_(\d+)_', f)
                parsed_id = None
                if msg_match:
                    parsed_id = int(msg_match.group(1))
                else:
                    # Check if filename matches DB filename
                    for db_f in db_files:
                        if os.path.basename(db_f["filename"]) == f:
                            parsed_id = db_f["message_id"]
                            break
                            
                if parsed_id is not None and start_id <= parsed_id <= end_id:
                    full_p = os.path.join(root, f)
                    rel_p = os.path.relpath(full_p, chat_folder)
                    found_files.append((f, rel_p, parsed_id))
                    
        # Remove duplicates based on filename
        seen = set()
        unique_found = []
        for item in found_files:
            if item[0] not in seen:
                seen.add(item[0])
                unique_found.append(item)
                
        if not unique_found:
            print("\n⚠️  \033[1;33mNo files found belonging to that message range.\033[0m")
            await asyncio.sleep(2.5)
            return
            
        print(f"👉 Found {len(unique_found)} file(s) on disk.")
        
        # Prompt for target subfolder name
        subfolder_name = await questionary.text(
            "Enter folder name to group these files into:",
            default=f"Range_{start_id}_{end_id}",
            style=custom_style
        ).ask_async()
        
        if not subfolder_name:
            return
            
        subfolder_name = subfolder_name.strip()
        target_dir = os.path.join(chat_folder, subfolder_name)
        os.makedirs(target_dir, exist_ok=True)
        
        # Move files and record movements
        moved_files = []
        for f, rel_p, msg_id in unique_found:
            src = os.path.join(chat_folder, rel_p)
            dest = os.path.join(target_dir, f)
            try:
                shutil.move(src, dest)
                moved_files.append((f, msg_id))
            except Exception as e:
                print(f"❌ Failed to move '{f}': {str(e)}")
                
        print(f"\n✅ Successfully moved {len(moved_files)} file(s) into subfolder: '{subfolder_name}'")
        
        # 3. Update metadata.json and rebuild gallery.html so everything works!
        metadata_path = os.path.join(chat_folder, "metadata.json")
        if os.path.exists(metadata_path) and moved_files:
            print("🔄 Updating gallery metadata links...")
            try:
                with open(metadata_path, "r", encoding="utf-8") as f_meta:
                    meta_data = json.load(f_meta)
                    
                if isinstance(meta_data, list):
                    # Create mapping of msg_id -> filename for quick update
                    moved_map = {m[1]: m[0] for m in moved_files}
                    
                    updated_count = 0
                    for item in meta_data:
                        m_id = item.get("message_id")
                        if m_id in moved_map:
                            item["filename"] = f"{subfolder_name}/{moved_map[m_id]}"
                            updated_count += 1
                            
                    if updated_count > 0:
                        with open(metadata_path, "w", encoding="utf-8") as f_meta_w:
                            json.dump(meta_data, f_meta_w, indent=2, ensure_ascii=False)
                            
                        # Re-generate gallery.html
                        from utils.download import generate_gallery_html
                        group_title = os.path.basename(chat_folder)
                        generate_gallery_html(chat_folder, group_title, meta_data)
                        print("🎨 Web gallery rebuild complete!")
            except Exception as e:
                print(f"⚠️ Failed to update metadata.json: {str(e)}")
                
        await asyncio.sleep(3.5)

# ==========================================
# 📦 BATCH PASSWORD ARCHIVER ENGINE
# ==========================================

async def run_batch_archiver(config, custom_style):
    chat_folder = await select_chat_folder(config, custom_style)
    if not chat_folder:
        return
        
    # Get directories and loose files
    contents = os.listdir(chat_folder)
    subdirs = [d for d in contents if os.path.isdir(os.path.join(chat_folder, d))]
    loose_files = [f for f in contents if os.path.isfile(os.path.join(chat_folder, f)) 
                   and not f.endswith((".zip", ".7z")) and f not in ("metadata.json", "gallery.html")]
                   
    total_items = len(subdirs) + len(loose_files)
    if total_items == 0:
        print("\n⚠️  \033[1;33mNo folders or files found in directory to archive.\033[0m")
        await asyncio.sleep(2)
        return
        
    print(f"\n📂 Found: {len(subdirs)} subfolders and {len(loose_files)} loose files.")
    
    # 1. Ask for Password
    password = await questionary.password("Enter password for encryption (leave blank for standard ZIP):", style=custom_style).ask_async()
    
    use_7z = False
    seven_zip_path = None
    
    if password:
        # Check if 7z is available
        seven_zip_path = find_7zip()
        if not seven_zip_path:
            print("\n❌  \033[1;31m[ERROR] 7-Zip (7z.exe) is required to encrypt archives, but it was not found.\033[0m")
            print("Please install 7-Zip (Gyan/7zip) on your computer, or run archiving without a password.")
            await asyncio.sleep(4)
            return
        use_7z = True
        print(f"✅  \033[1;32mUsing 7-Zip for AES-256 encryption at: {seven_zip_path}\033[0m")
        
    # 2. Ask delete originals
    delete_after = await questionary.confirm("Delete original files/folders after successful archiving?", default=False, style=custom_style).ask_async()
    
    # 3. Confirmation
    confirm = await questionary.confirm(f"Proceed with archiving {total_items} items?", default=True, style=custom_style).ask_async()
    if not confirm:
        return
        
    print(f"\n🚀 \033[1;36mStarting batch archiving...\033[0m")
    
    success_count = 0
    fail_count = 0
    
    archive_ext = ".7z" if use_7z else ".zip"
    
    # Process folders
    for d in subdirs:
        src_path = os.path.join(chat_folder, d)
        arc_path = os.path.join(chat_folder, f"{d}{archive_ext}")
        
        print(f"📦 Archiving folder '{d}'...")
        try:
            if use_7z:
                # 7-Zip password archive
                cmd = [
                    seven_zip_path, "a", "-t7z", "-m0=LZMA2", "-mhe=on",
                    f"-p{password}", arc_path, os.path.join(src_path, "*")
                ]
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
            else:
                # Standard ZIP
                shutil.make_archive(os.path.join(chat_folder, d), 'zip', src_path)
                
            success_count += 1
            if delete_after:
                shutil.rmtree(src_path)
        except Exception as e:
            print(f"❌ Failed to archive folder '{d}': {str(e)}")
            fail_count += 1
            
    # Process loose files
    for f in loose_files:
        src_path = os.path.join(chat_folder, f)
        base = os.path.splitext(f)[0]
        arc_path = os.path.join(chat_folder, f"{base}{archive_ext}")
        
        # Avoid overriding existing files
        col = 1
        while os.path.exists(arc_path):
            arc_path = os.path.join(chat_folder, f"{base}_{col}{archive_ext}")
            col += 1
            
        print(f"📦 Archiving file '{f}'...")
        try:
            if use_7z:
                cmd = [
                    seven_zip_path, "a", "-t7z", "-m0=LZMA2", "-mhe=on",
                    f"-p{password}", arc_path, src_path
                ]
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
            else:
                with zipfile.ZipFile(arc_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    zf.write(src_path, f)
                    
            success_count += 1
            if delete_after:
                os.remove(src_path)
        except Exception as e:
            print(f"❌ Failed to archive file '{f}': {str(e)}")
            fail_count += 1
            
    print(f"\n🎉 \033[1;32mBatch Archiving complete! Succeeded: {success_count}, Failed: {fail_count}\033[0m")
    await asyncio.sleep(3)


# ==========================================
# 🎥 ADVANCED MEDIA CONVERTERS ENGINE
# ==========================================

async def get_video_duration(video_path):
    """Retrieves duration of video in seconds using ffprobe."""
    try:
        cmd = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", video_path
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        return float(stdout.strip())
    except Exception:
        return None

async def run_advanced_converters_interactive(config, custom_style):
    """TUI loop for advanced FFmpeg converters."""
    if not is_ffmpeg_available():
        print("\n⚠️  \033[1;33m[WARNING] ffmpeg is not installed or not in PATH!\033[0m")
        print("To run advanced converters, please install ffmpeg on Windows:")
        print("👉 Run: \033[1;36mwinget install Gyan.FFmpeg\033[0m in a separate terminal, then restart the script.")
        input("\nPress Enter to return...")
        return
        
    while True:
        os.system('cls' if os.name == 'nt' else 'clear')
        print("\033[1;36mTelegram Downloader (V3.9.0)\033[0m")
        print("\033[1;35m--- ADVANCED MEDIA CONVERTERS ---\033[0m\n")
        
        choice = await questionary.select(
            "Select conversion utility:",
            choices=[
                "🎥 Video Compression (Target File Size)",
                "🖼️  Video to High-Quality GIF",
                "🔊 Audio Volumizer / Dynamic Leveling",
                "❌ Back to Utilities Menu"
            ],
            style=custom_style
        ).ask_async()
        
        if choice is None or choice == "❌ Back to Utilities Menu":
            break
            
        chat_folder = await select_chat_folder(config, custom_style)
        if not chat_folder:
            continue
            
        if "Video Compression" in choice:
            vids = [f for f in os.listdir(chat_folder) if f.lower().endswith((".mp4", ".mkv", ".avi"))]
            if not vids:
                print("\n⚠️  \033[1;33mNo video files (.mp4, .mkv, .avi) found in selected folder.\033[0m")
                await asyncio.sleep(2)
                continue
                
            selected_vid = await questionary.select(
                "Select video to compress:",
                choices=vids + ["Cancel"],
                style=custom_style
            ).ask_async()
            
            if selected_vid is None or selected_vid == "Cancel":
                continue
                
            input_path = os.path.join(chat_folder, selected_vid)
            duration = await get_video_duration(input_path)
            if not duration:
                print("\n❌ \033[1;31mCould not retrieve video duration. FFprobe failed.\033[0m")
                await asyncio.sleep(2.5)
                continue
                
            orig_size_mb = os.path.getsize(input_path) / (1024 * 1024)
            print(f"🎬 Selected Video Duration: {duration:.1f}s | Size: {orig_size_mb:.2f} MB")
            
            target_sz_str = await questionary.text(
                "Enter target file size in MB (e.g. 8 for sharing, 50 for Medium):",
                default="8",
                style=custom_style
            ).ask_async()
            
            try:
                target_sz_mb = float(target_sz_str)
                if target_sz_mb <= 0 or target_sz_mb >= orig_size_mb:
                    print("⚠️  Target size must be positive and smaller than original size.")
                    await asyncio.sleep(2)
                    continue
            except ValueError:
                print("⚠️  Invalid target size.")
                await asyncio.sleep(2)
                continue
                
            # Calculate bitrates
            target_size_bytes = target_sz_mb * 1024 * 1024
            total_bitrate = int((target_size_bytes * 8) / duration)
            
            audio_bitrate = 128000
            video_bitrate = total_bitrate - audio_bitrate
            if video_bitrate < 64000:
                video_bitrate = 64000
                
            base, ext = os.path.splitext(selected_vid)
            output_path = os.path.join(chat_folder, f"{base}_compressed{ext}")
            null_device = "NUL" if os.name == 'nt' else "/dev/null"
            
            print(f"\n🔄 \033[1;36mStarting 2-pass compression (Target: {target_sz_mb:.1f} MB)...\033[0m")
            try:
                # Pass 1
                print("   ↳ ⚡ [Pass 1/2] Analyzing video structure...")
                cmd_pass1 = [
                    "ffmpeg", "-y", "-i", input_path,
                    "-c:v", "libx264", "-b:v", str(video_bitrate),
                    "-pass", "1", "-an", "-f", "null", null_device
                ]
                proc1 = await asyncio.create_subprocess_exec(
                    *cmd_pass1, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                await proc1.communicate()
                
                if proc1.returncode != 0:
                    raise Exception("First pass failed.")
                    
                # Pass 2
                print("   ↳ ⚡ [Pass 2/2] Re-encoding frames and copying audio...")
                cmd_pass2 = [
                    "ffmpeg", "-y", "-i", input_path,
                    "-c:v", "libx264", "-b:v", str(video_bitrate),
                    "-pass", "2", "-c:a", "aac", "-b:a", str(audio_bitrate),
                    output_path
                ]
                proc2 = await asyncio.create_subprocess_exec(
                    *cmd_pass2, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                await proc2.communicate()
                
                # Cleanup pass log files
                for f in os.listdir("."):
                    if f.startswith("ffmpeg2pass"):
                        try:
                            os.remove(f)
                        except Exception:
                            pass
                            
                if proc2.returncode == 0:
                    new_size_mb = os.path.getsize(output_path) / (1024 * 1024)
                    print(f"✅ \033[1;32mVideo successfully compressed! New Size: {new_size_mb:.2f} MB (Saved as: {os.path.basename(output_path)})\033[0m")
                else:
                    print("❌ \033[1;31mVideo compression failed in second pass.\033[0m")
            except Exception as e:
                print(f"❌ \033[1;31mCompression failed: {str(e)}\033[0m")
            await asyncio.sleep(4)
            
        elif "Video to High-Quality GIF" in choice:
            vids = [f for f in os.listdir(chat_folder) if f.lower().endswith((".mp4", ".mkv", ".avi"))]
            if not vids:
                print("\n⚠️  \033[1;33mNo video files found in selected folder.\033[0m")
                await asyncio.sleep(2)
                continue
                
            selected_vid = await questionary.select(
                "Select video to convert to GIF:",
                choices=vids + ["Cancel"],
                style=custom_style
            ).ask_async()
            
            if selected_vid is None or selected_vid == "Cancel":
                continue
                
            input_path = os.path.join(chat_folder, selected_vid)
            duration = await get_video_duration(input_path)
            print(f"🎬 Video Duration: {duration:.1f}s")
            
            start_time = await questionary.text(
                "Enter start time (format: HH:MM:SS or seconds, e.g. 0 or 00:00:05):",
                default="0",
                style=custom_style
            ).ask_async()
            
            length_str = await questionary.text(
                "Enter duration of GIF in seconds:",
                default="5",
                style=custom_style
            ).ask_async()
            
            width_str = await questionary.text(
                "Enter GIF Width (resolution scale, e.g., 480 or 720):",
                default="480",
                style=custom_style
            ).ask_async()
            
            fps_str = await questionary.text(
                "Enter GIF Framerate (FPS, e.g., 10, 15, 24):",
                default="15",
                style=custom_style
            ).ask_async()
            
            base = os.path.splitext(selected_vid)[0]
            output_path = os.path.join(chat_folder, f"{base}_clip.gif")
            
            print(f"\n🔄 \033[1;36mGenerating high-quality GIF with custom color palette...\033[0m")
            try:
                filter_graph = f"fps={fps_str},scale={width_str}:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
                cmd = [
                    "ffmpeg", "-y", "-ss", start_time, "-t", length_str,
                    "-i", input_path, "-filter_complex", filter_graph,
                    output_path
                ]
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                await proc.communicate()
                
                if proc.returncode == 0:
                    print(f"✅ \033[1;32mGIF successfully generated! (Saved as: {os.path.basename(output_path)})\033[0m")
                else:
                    print("❌ \033[1;31mGIF generation failed.\033[0m")
            except Exception as e:
                print(f"❌ \033[1;31mError converting to GIF: {str(e)}\033[0m")
            await asyncio.sleep(3.5)
            
        elif "Audio Volumizer" in choice:
            audios = [f for f in os.listdir(chat_folder) if f.lower().endswith((".mp3", ".ogg", ".m4a", ".wav"))]
            if not audios:
                print("\n⚠️  \033[1;33mNo audio files (.mp3, .ogg, .m4a, .wav) found in selected folder.\033[0m")
                await asyncio.sleep(2)
                continue
                
            selected_aud = await questionary.select(
                "Select audio to adjust volume/level:",
                choices=audios + ["Cancel"],
                style=custom_style
            ).ask_async()
            
            if selected_aud is None or selected_aud == "Cancel":
                continue
                
            input_path = os.path.join(chat_folder, selected_aud)
            
            level_choice = await questionary.select(
                "Select volume adjustment mode:",
                choices=[
                    "🔊 2x Volume Boost (Simple Gain)",
                    "🎚️  Professional Dynamic Leveling (Loudnorm)",
                    "❌ Cancel"
                ],
                style=custom_style
            ).ask_async()
            
            if level_choice is None or "Cancel" in level_choice:
                continue
                
            base, ext = os.path.splitext(selected_aud)
            suffix = "_boosted" if "2x Volume Boost" in level_choice else "_leveled"
            output_path = os.path.join(chat_folder, f"{base}{suffix}{ext}")
            
            filter_str = "volume=2.0" if "2x Volume Boost" in level_choice else "loudnorm=I=-16:TP=-1.5:LRA=11"
            
            print(f"\n🔄 \033[1;36mProcessing audio leveling...\033[0m")
            try:
                cmd = [
                    "ffmpeg", "-y", "-i", input_path,
                    "-filter:a", filter_str,
                    output_path
                ]
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                await proc.communicate()
                
                if proc.returncode == 0:
                    print(f"✅ \033[1;32mAudio successfully processed! (Saved as: {os.path.basename(output_path)})\033[0m")
                else:
                    print("❌ \033[1;31mAudio processing failed.\033[0m")
            except Exception as e:
                print(f"❌ \033[1;31mError adjusting audio: {str(e)}\033[0m")
            await asyncio.sleep(3.5)
