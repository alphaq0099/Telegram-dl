import os
import sys
import sqlite3
import asyncio
import subprocess
import questionary
from utils.archive import DB_PATH
from utils.download import get_config
import unicodedata

def get_display_width(text):
    """Calculates visual terminal column width of a string (handling wide CJK characters/emojis)."""
    width = 0
    for char in text:
        # 'W' (Wide), 'F' (Fullwidth) CJK characters occupy 2 columns
        if unicodedata.east_asian_width(char) in ('W', 'F'):
            width += 2
        # Emojis/symbols are typically double-width
        elif 0x1F300 <= ord(char) <= 0x1F9FF:
            width += 2
        else:
            width += 1
    return width

def ljust_display(text, width, fillchar=' '):
    """Left-aligns a string using visual display width instead of character length."""
    disp_width = get_display_width(text)
    padding = max(0, width - disp_width)
    return text + (fillchar * padding)

def format_size(bytes_val):
    if not bytes_val:
        return "0 B"
    if bytes_val >= 1024 * 1024 * 1024:
        return f"{bytes_val / (1024 * 1024 * 1024):.2f} GB"
    if bytes_val >= 1024 * 1024:
        return f"{bytes_val / (1024 * 1024):.2f} MB"
    if bytes_val >= 1024:
        return f"{bytes_val / 1024:.2f} KB"
    return f"{bytes_val} B"

from utils.auth import PROJECT_ROOT

def find_file_on_disk(chat_id, chat_name, filename):
    """Attempts to find the downloaded file in the download directory."""
    config = get_config()
    download_dir = config.get("download_dir", "./downloads")
    if not os.path.isabs(download_dir):
        download_dir = os.path.join(PROJECT_ROOT, download_dir)
        
    if not os.path.exists(download_dir):
        return None
        
    # Standardize filename (database might store subfolder names too)
    filename_base = os.path.basename(filename)
    
    # Candidate 1: downloads/chat_name/filename (recursive)
    if chat_name:
        chat_dir = os.path.join(download_dir, chat_name)
        if os.path.exists(chat_dir):
            for root, _, files in os.walk(chat_dir):
                if filename_base in files:
                    return os.path.join(root, filename_base)
                    
    # Candidate 2: downloads/chat_id/filename (recursive)
    if chat_id:
        chat_dir_id = os.path.join(download_dir, str(chat_id))
        if os.path.exists(chat_dir_id):
            for root, _, files in os.walk(chat_dir_id):
                if filename_base in files:
                    return os.path.join(root, filename_base)
                    
    # Candidate 3: global search in downloads/
    for root, _, files in os.walk(download_dir):
        if filename_base in files:
            return os.path.join(root, filename_base)
            
    return None

def open_file_folder(file_path):
    """Opens OS file explorer with the selected file highlighted."""
    if not file_path or not os.path.exists(file_path):
        print("\033[1;31m[ERROR] File no longer exists on disk.\033[0m")
        return False
        
    abs_path = os.path.abspath(file_path)
    try:
        if sys.platform == 'win32':
            subprocess.run(["explorer.exe", "/select,", os.path.normpath(abs_path)], check=True)
        elif sys.platform == 'darwin':
            subprocess.run(["open", "-R", abs_path], check=True)
        else: # Linux
            subprocess.run(["xdg-open", os.path.dirname(abs_path)], check=True)
        return True
    except Exception as e:
        print(f"\033[1;31m[ERROR] Failed to open explorer: {str(e)}\033[0m")
        return False

async def handle_search_menu(custom_style):
    """Main menu loop for the Offline Search feature."""
    if not os.path.exists(DB_PATH):
        print("\n⚠️  \033[1;33mArchive database does not exist yet. Please download some media first.\033[0m")
        await asyncio.sleep(2)
        return
        
    while True:
        os.system('cls' if os.name == 'nt' else 'clear')
        print("\033[1;36mTelegram Downloader (V3.9.0)\033[0m")
        print("\033[1;35m--- HISTORY & STATISTICS ---\033[0m\n")
        
        choice = await questionary.select(
            "What would you like to do:",
            choices=[
                "🔍 Search by Text (Filename / Caption)",
                "📁 Filter by Chat / Group",
                "📦 Filter by Media Type",
                "📅 View Recent Downloads (Last 50)",
                "📊 View Download Statistics",
                "Back to main menu",
                questionary.Separator(),
                questionary.Separator("ARROW KEYS: Navigate | ENTER: Select | Ctrl+C: Cancel/Exit")
            ],
            style=custom_style
        ).ask_async()
        
        if choice == "SCHEDULER_TRIGGERED":
            return "SCHEDULER_TRIGGERED"
            
        if choice is None or choice == "Back to main menu":
            break
            
        results = []
        
        if "Search by Text" in choice:
            query = await questionary.text("Enter search keyword (filename or caption):", style=custom_style).ask_async()
            if query == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"
            if query and query.strip():
                results = search_db("text", query.strip())
                
        elif "Filter by Chat" in choice:
            chats = get_distinct_chats()
            if not chats:
                print("\n⚠️  \033[1;33mNo chats found in archive database.\033[0m")
                await asyncio.sleep(2)
                continue
            selected_chat = await questionary.select("Select Chat:", choices=chats + ["Cancel"], style=custom_style).ask_async()
            if selected_chat == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"
            if selected_chat and selected_chat != "Cancel":
                results = search_db("chat", selected_chat)
                
        elif "Filter by Media Type" in choice:
            mtypes = ["photo", "video", "document", "audio", "voice", "sticker"]
            selected_type = await questionary.select("Select Media Type:", choices=mtypes + ["Cancel"], style=custom_style).ask_async()
            if selected_type == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"
            if selected_type and selected_type != "Cancel":
                results = search_db("type", selected_type)
                
        elif "Recent Downloads" in choice:
            results = search_db("recent", None)
            
        elif "Download Statistics" in choice:
            res = await view_statistics(custom_style)
            if res == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"
            continue
            
        if results:
            res = await display_search_results(results, custom_style)
            if res == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"
        elif choice not in (None, "Back to main menu") and results is not None:
            print("\n🔍 \033[1;30mNo matching downloaded files found.\033[0m")
            await asyncio.sleep(2)

def search_db(mode, value):
    """Helper to query the SQLite3 database."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    query = """
        SELECT chat_id, message_id, filename, file_size, downloaded_at, chat_name, caption, media_type 
        FROM archive
    """
    params = []
    
    if mode == "text":
        query += " WHERE filename LIKE ? OR caption LIKE ? ORDER BY downloaded_at DESC"
        params = [f"%{value}%", f"%{value}%"]
    elif mode == "chat":
        query += " WHERE chat_name = ? ORDER BY downloaded_at DESC"
        params = [value]
    elif mode == "type":
        query += " WHERE media_type = ? ORDER BY downloaded_at DESC"
        params = [value]
    elif mode == "recent":
        query += " ORDER BY downloaded_at DESC LIMIT 50"
        
    try:
        cursor.execute(query, params)
        rows = cursor.fetchall()
        # Convert to list of dicts
        keys = ["chat_id", "message_id", "filename", "file_size", "downloaded_at", "chat_name", "caption", "media_type"]
        return [dict(zip(keys, row)) for row in rows]
    except Exception as e:
        print(f"\n❌ Error querying database: {str(e)}")
        return []
    finally:
        conn.close()

def get_distinct_chats():
    """Returns list of distinct chat names from the DB."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT DISTINCT chat_name FROM archive WHERE chat_name IS NOT NULL AND chat_name != '' ORDER BY chat_name ASC")
        return [r[0] for r in cursor.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()

async def display_search_results(results, custom_style):
    """Renders query results interactively and lets user open location or view metadata."""
    while True:
        os.system('cls' if os.name == 'nt' else 'clear')
        print(f"\033[1;35m--- SEARCH RESULTS ({len(results)}) ---\033[0m\n")
        
        choices = []
        for idx, r in enumerate(results, start=1):
            date_str = r.get("downloaded_at", "Unknown")[:10]
            chat = r.get("chat_name") or "Direct"
            f_name = os.path.basename(r.get("filename"))
            size = format_size(r.get("file_size"))
            m_type = r.get("media_type") or "file"
            
            # Format visual string: [1] 2026-07-02 | ChatName | [video] name (12MB)
            label = f"[{date_str}] 📁 {chat[:15].ljust(15)} | {m_type.upper().ljust(8)} | {f_name[:30]} ({size})"
            choices.append(questionary.Choice(title=label, value=r))
            
        choices.append(questionary.Choice(title="❌ Back to search menu", value="BACK"))
        
        selection = await questionary.select(
            "Select file to view details / open location:",
            choices=choices,
            style=custom_style
        ).ask_async()
        
        if selection == "SCHEDULER_TRIGGERED":
            return "SCHEDULER_TRIGGERED"
            
        if selection == "BACK" or selection is None:
            break
            
        res = await show_record_actions(selection, custom_style)
        if res == "SCHEDULER_TRIGGERED":
            return "SCHEDULER_TRIGGERED"

async def show_record_actions(record, custom_style):
    while True:
        os.system('cls' if os.name == 'nt' else 'clear')
        print("\033[1;35m--- FILE METADATA DETAILS ---\033[0m")
        print(f"📁 Chat Group  : \033[1;32m{record.get('chat_name') or 'Unknown'}\033[0m (ID: {record.get('chat_id')})")
        print(f"📄 Filename    : \033[1;32m{os.path.basename(record.get('filename'))}\033[0m")
        print(f"📦 Media Type  : \033[1;36m{record.get('media_type') or 'file'}\033[0m")
        print(f"⚖️  File Size   : {format_size(record.get('file_size'))}")
        print(f"📅 Downloaded  : {record.get('downloaded_at')}")
        print(f"💬 Message ID  : {record.get('message_id')}")
        
        caption = record.get('caption')
        if caption:
            print(f"📝 Caption     : \n\033[1;30m------------------------------------------------\033[0m\n{caption}\n\033[1;30m------------------------------------------------\033[0m")
        else:
            print("📝 Caption     : None")
            
        # Check physical existence
        disk_path = find_file_on_disk(record.get("chat_id"), record.get("chat_name"), record.get("filename"))
        if disk_path:
            print(f"📍 Location    : \033[1;32m{disk_path}\033[0m")
            action_choices = ["📂 Open containing folder", "Back to results"]
        else:
            print("📍 Location    : \033[1;31mNot found on disk (moved, deleted, or archived)\033[0m")
            action_choices = ["Back to results"]
            
        choice = await questionary.select(
            "Select action:",
            choices=action_choices,
            style=custom_style
        ).ask_async()
        
        if choice == "SCHEDULER_TRIGGERED":
            return "SCHEDULER_TRIGGERED"
            
        if choice == "📂 Open containing folder" and disk_path:
            open_file_folder(disk_path)
        else:
            break


async def view_statistics(custom_style):
    """Calculates and displays rich statistics of downloaded media."""
    if not os.path.exists(DB_PATH):
        print("\n⚠️  \033[1;33mArchive database does not exist yet. Please download some media first.\033[0m")
        await asyncio.sleep(2)
        return
        
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        # 1. Overall stats
        cursor.execute("SELECT COUNT(*), SUM(file_size) FROM archive")
        total_files, total_size = cursor.fetchone()
        total_files = total_files or 0
        total_size = total_size or 0
        
        # 2. Media type breakdown
        cursor.execute("SELECT media_type, COUNT(*), SUM(file_size) FROM archive GROUP BY media_type ORDER BY SUM(file_size) DESC")
        media_breakdown = cursor.fetchall()
        
        # 3. Top 5 chats
        cursor.execute("SELECT chat_name, COUNT(*), SUM(file_size) FROM archive GROUP BY chat_name ORDER BY SUM(file_size) DESC LIMIT 5")
        top_chats = cursor.fetchall()
        
        # 4. Top 5 largest files
        cursor.execute("SELECT filename, file_size, chat_name FROM archive ORDER BY file_size DESC LIMIT 5")
        largest_files = cursor.fetchall()
        
        conn.close()
    except Exception as e:
        conn.close()
        print(f"\n❌ \033[1;31mError retrieving statistics: {str(e)}\033[0m")
        await asyncio.sleep(2.5)
        return
        
    os.system('cls' if os.name == 'nt' else 'clear')
    print("\033[1;36mTelegram Downloader (V3.9.0)\033[0m")
    print("\033[1;35m--- DOWNLOAD STATISTICS DASHBOARD ---\033[0m\n")
    
    print(f"📊  {'Total Files Archived:'.ljust(25)} \033[1;32m{total_files:,}\033[0m")
    print(f"⚖️   {'Total Storage Used:'.ljust(25)} \033[1;32m{format_size(total_size)}\033[0m")
    print("\033[1;30m------------------------------------------------------------\033[0m")
    
    # 5. Render Media Breakdown chart
    print("📦  \033[1;36mMedia Type Distribution:\033[0m")
    max_bar_width = 20
    for mtype, count, size_bytes in media_breakdown:
        size_bytes = size_bytes or 0
        mtype_label = (mtype or "unknown").upper()
        percentage = (size_bytes / total_size * 100) if total_size > 0 else 0
        
        # Draw block character progress bar
        bar_filled = int((percentage / 100) * max_bar_width)
        bar_empty = max_bar_width - bar_filled
        bar = "█" * bar_filled + "░" * bar_empty
        
        print(f"    - {ljust_display(mtype_label, 9)}: [{bar}] {percentage:5.1f}% ({format_size(size_bytes)} | {count:,} files)")
        
    print("\033[1;30m------------------------------------------------------------\033[0m")
    
    # 6. Top 5 Chats Table
    print("💬  \033[1;36mTop 5 Chats by Storage Usage:\033[0m")
    for chat, count, size_bytes in top_chats:
        chat_name = chat if chat else "Direct Messages/Unknown"
        print(f"    - {ljust_display(chat_name[:25], 25)} : \033[1;32m{format_size(size_bytes or 0)}\033[0m ({count:,} files)")
        
    print("\033[1;30m------------------------------------------------------------\033[0m")
    
    # 7. Top 5 Largest Files
    print("🐘  \033[1;36mLargest Files Downloaded:\033[0m")
    for filepath, size_bytes, chat in largest_files:
        filename = os.path.basename(filepath)
        chat_lbl = f" [{chat}]" if chat else ""
        print(f"    - {ljust_display(filename[:35], 35)} : \033[1;33m{format_size(size_bytes or 0)}\033[0m{chat_lbl}")
        
    print("\033[1;30m============================================================\033[0m")
    input("\n⌨️  Press Enter to return to menu...")
