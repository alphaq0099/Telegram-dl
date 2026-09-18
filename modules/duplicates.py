import os
import sys
import sqlite3
import hashlib
import asyncio
import questionary
from tqdm import tqdm
from utils.download import get_config

from utils.auth import PROJECT_ROOT

DB_PATH = os.path.join(PROJECT_ROOT, "archive.db")

def get_fast_hash(file_path):
    """
    Calculates MD5 hash of the first 64KB and last 64KB of the file + its size.
    This provides a 99.99% collision-free signature extremely quickly without
    reading gigabytes of video files from disk.
    """
    try:
        file_size = os.path.getsize(file_path)
        if file_size == 0:
            return None
        
        hasher = hashlib.md5()
        hasher.update(str(file_size).encode('utf-8'))
        
        with open(file_path, 'rb') as f:
            # Read first 64KB
            chunk_start = f.read(64 * 1024)
            hasher.update(chunk_start)
            
            # Read last 64KB
            if file_size > 64 * 1024:
                try:
                    f.seek(-64 * 1024, 2)
                    chunk_end = f.read(64 * 1024)
                    hasher.update(chunk_end)
                except Exception:
                    pass
                
        return hasher.hexdigest()
    except Exception:
        return None

def delete_db_record(file_name):
    """Removes file metadata record from sqlite3 download history."""
    if not os.path.exists(DB_PATH):
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM archive WHERE filename = ?", (file_name,))
        conn.commit()
        conn.close()
    except Exception:
        pass

async def run_duplicate_checker(custom_style):
    """Interactive sub-menu for Duplicate Media Scan & Cleanup."""
    config = get_config()
    download_dir = config.get("download_dir", "./downloads")
    if not os.path.isabs(download_dir):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        download_dir = os.path.join(project_root, download_dir)
        
    while True:
        os.system('cls' if os.name == 'nt' else 'clear')
        print("\033[1;36mTelegram Downloader (V3.9.0)\033[0m")
        print("\033[1;35m--- DUPLICATE MEDIA CHECKER ---\033[0m\n")
        
        scan_choice = await questionary.select(
            "Select duplicate scan mode:",
            choices=[
                "💻 Scan SQLite Database (Instant)",
                "📂 Scan Disk Directories (Fast-Hash)",
                "❌ Back to Utilities"
            ],
            style=custom_style
        ).ask_async()
        
        if scan_choice is None or scan_choice == "❌ Back to Utilities":
            break
            
        duplicate_groups = {}
        
        if "SQLite Database" in scan_choice:
            if not os.path.exists(DB_PATH):
                print("⚠️  \033[1;33mDatabase archive.db does not exist yet. Download some files first!\033[0m")
                await asyncio.sleep(2)
                continue
                
            print("\n🔍 \033[1;36mScanning database download logs...\033[0m")
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT filename, file_size, group_concat(chat_name), group_concat(message_id)
                    FROM archive
                    GROUP BY filename, file_size
                    HAVING COUNT(*) > 1 AND file_size > 0
                """)
                rows = cursor.fetchall()
                conn.close()
                
                # Check actual file existence for these DB duplicates
                for filename, file_size, chats_str, ids_str in rows:
                    chats = chats_str.split(',')
                    ids = ids_str.split(',')
                    existing_paths = []
                    
                    # Search disk for the files registered under these paths
                    for i, chat in enumerate(chats):
                        chat_path = os.path.join(download_dir, chat)
                        possible_path = os.path.join(chat_path, filename)
                        if os.path.exists(possible_path):
                            existing_paths.append({
                                "path": possible_path,
                                "chat": chat,
                                "message_id": ids[i] if i < len(ids) else 0
                            })
                            
                    if len(existing_paths) > 1:
                        duplicate_groups[filename] = existing_paths
                        
            except Exception as e:
                print(f"❌ \033[1;31mDatabase scan failed: {str(e)}\033[0m")
                await asyncio.sleep(2)
                continue
                
        else: # Scan Disk Directories (Fast-Hash)
            if not os.path.exists(download_dir):
                print("⚠️  \033[1;33mDownload directory does not exist yet!\033[0m")
                await asyncio.sleep(2)
                continue
                
            print("\n🔍 \033[1;36mScanning disk directory files (building size inventory)...\033[0m")
            size_map = {}
            for root, dirs, files in os.walk(download_dir):
                for f in files:
                    if f in ("metadata.json", "gallery.html", "archive.db", ".env"):
                        continue
                    full_path = os.path.join(root, f)
                    try:
                        sz = os.path.getsize(full_path)
                        if sz > 0:
                            size_map.setdefault(sz, []).append(full_path)
                    except Exception:
                        pass
                        
            # Filter sizes with multiple files and run Fast-Hash
            hash_map = {}
            candidate_sizes = [sz for sz, paths in size_map.items() if len(paths) > 1]
            if candidate_sizes:
                print(f"🧬 \033[1;36mFound {len(candidate_sizes)} potential size match sets. Running Fast-Hash verification...\033[0m")
                for sz in tqdm(candidate_sizes, desc="Verifying hashes", leave=False):
                    for path in size_map[sz]:
                        h = get_fast_hash(path)
                        if h:
                            hash_map.setdefault(h, []).append(path)
                            
            # Extract actual duplicate groups (hash groups with > 1 file)
            group_idx = 1
            for h, paths in hash_map.items():
                if len(paths) > 1:
                    group_key = f"Group {group_idx} ({os.path.basename(paths[0])})"
                    group_idx += 1
                    duplicate_groups[group_key] = [{"path": p, "chat": os.path.basename(os.path.dirname(p)), "message_id": 0} for p in paths]
                    
        # 3. Present Results & Deletion Options
        total_groups = len(duplicate_groups)
        if total_groups == 0:
            print("\n🎉 \033[1;32mNo duplicates found! All your files are unique.\033[0m")
            await asyncio.sleep(2.5)
            continue
            
        # Calculate space savings
        wasted_bytes = 0
        for key, items in duplicate_groups.items():
            try:
                sz = os.path.getsize(items[0]["path"])
                wasted_bytes += sz * (len(items) - 1)
            except Exception:
                pass
        wasted_mb = wasted_bytes / (1024 * 1024)
        
        print(f"\n📝 Found \033[1;31m{total_groups}\033[0m set(s) of duplicates. Wasted Space: \033[1;33m{wasted_mb:.2f} MB\033[0m\n")
        
        # Display the duplicate sets to user
        idx = 1
        for name, items in list(duplicate_groups.items())[:15]:
            print(f"\033[1;35mSet {idx}: {name}\033[0m")
            idx += 1
            for item in items:
                print(f"   ↳ {item['path']}")
        if total_groups > 15:
            print(f"   ... and {total_groups - 15} more groups.")
            
        action = await questionary.select(
            "\nChoose cleanup action:",
            choices=[
                "🧹 Keep OLDEST file in each set (Delete others)",
                "🧹 Keep NEWEST file in each set (Delete others)",
                "🛠️  Select files to delete manually...",
                "🔍 View / Open a file to inspect...",
                "❌ Cancel / Skip"
            ],
            style=custom_style
        ).ask_async()
        
        if action is None or "Cancel" in action:
            continue
            
        deleted_count = 0
        freed_bytes = 0
        
        if "Keep OLDEST" in action or "Keep NEWEST" in action:
            keep_oldest = "Keep OLDEST" in action
            for name, items in duplicate_groups.items():
                try:
                    items_sorted = sorted(items, key=lambda x: os.path.getmtime(x["path"]))
                except Exception:
                    items_sorted = items
                    
                to_keep = items_sorted[0] if keep_oldest else items_sorted[-1]
                to_delete = [x for x in items_sorted if x != to_keep]
                
                for item in to_delete:
                    p = item["path"]
                    if os.path.exists(p):
                        try:
                            sz = os.path.getsize(p)
                            os.remove(p)
                            delete_db_record(os.path.basename(p))
                            deleted_count += 1
                            freed_bytes += sz
                        except Exception as e:
                            print(f"⚠️  Failed to delete {p}: {str(e)}")
                            
        elif "Select files to delete manually" in action or "View / Open a file to inspect" in action:
            to_delete_paths = set()
            to_delete_items = []
            default_choice = None
            
            while True:
                inspect_choices = []
                for name, items in duplicate_groups.items():
                    inspect_choices.append(questionary.Separator(f"Set: {name}"))
                    for item in items:
                        p = item["path"]
                        sz_mb = os.path.getsize(p) / (1024 * 1024)
                        
                        checkbox_status = "[❌ DELETE]" if p in to_delete_paths else "[🟢 KEEP]"
                        
                        inspect_choices.append(
                            questionary.Choice(
                                title=f" {checkbox_status} Toggle: {os.path.basename(p)} ({sz_mb:.2f} MB)",
                                value=("toggle", item)
                            )
                        )
                        inspect_choices.append(
                            questionary.Choice(
                                title=f"     🔍 Inspect: {p}",
                                value=("inspect", p)
                            )
                        )
                        
                inspect_choices.append(questionary.Separator())
                inspect_choices.append(questionary.Choice(title=f"🗑️  Delete Selected Files ({len(to_delete_paths)} selected)", value="confirm"))
                inspect_choices.append(questionary.Choice(title="❌ Return to Previous Menu", value="back"))
                
                choice = await questionary.select(
                    "Manage duplicates (Select toggle to mark for deletion, inspect to view):",
                    choices=inspect_choices,
                    default=default_choice,
                    style=custom_style
                ).ask_async()
                
                if choice is None or choice == "back":
                    break
                    
                default_choice = choice
                    
                if choice == "confirm":
                    if not to_delete_paths:
                        print("\n⚠️  No files selected for deletion.")
                        await asyncio.sleep(1.5)
                        continue
                        
                    confirm = await questionary.confirm(
                        f"Are you sure you want to permanently delete these {len(to_delete_paths)} files?",
                        default=False,
                        style=custom_style
                    ).ask_async()
                    
                    if confirm:
                        for item in to_delete_items:
                            p = item["path"]
                            if os.path.exists(p):
                                try:
                                    sz = os.path.getsize(p)
                                    os.remove(p)
                                    delete_db_record(os.path.basename(p))
                                    deleted_count += 1
                                    freed_bytes += sz
                                except Exception as e:
                                    print(f"⚠️  Failed to delete {p}: {str(e)}")
                        break
                    continue
                    
                action_type, data = choice
                if action_type == "inspect":
                    print(f"\n📂 \033[1;36mOpening file: {os.path.basename(data)}...\033[0m")
                    try:
                        import subprocess
                        if os.name == 'nt':
                            os.startfile(data)
                        elif sys.platform == 'darwin':
                            subprocess.run(["open", data])
                        else:
                            subprocess.run(["xdg-open", data])
                    except Exception as e:
                        print(f"❌ Failed to open file: {str(e)}")
                        await asyncio.sleep(2)
                elif action_type == "toggle":
                    path = data["path"]
                    if path in to_delete_paths:
                        to_delete_paths.remove(path)
                        to_delete_items = [x for x in to_delete_items if x["path"] != path]
                    else:
                        to_delete_paths.add(path)
                        to_delete_items.append(data)
                                
        if deleted_count > 0:
            freed_mb = freed_bytes / (1024 * 1024)
            print(f"\n✅  \033[1;32mCleaned up {deleted_count} duplicate files. Freed {freed_mb:.2f} MB of disk space!\033[0m")
        else:
            print("\nℹ️  \033[1;30mNo files were deleted.\033[0m")
        await asyncio.sleep(3)
