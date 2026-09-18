import os
import sys
import json
import shutil
import asyncio
import subprocess
import questionary
from utils.download import get_config

from utils.auth import PROJECT_ROOT

def update_config(new_config):
    """Saves changes back to config.json."""
    config_path = os.path.join(PROJECT_ROOT, 'config.json')
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(new_config, f, indent=2)
    except Exception as e:
        print(f"\033[1;31m[ERROR] Failed to save configuration: {str(e)}\033[0m", file=sys.stderr)

def is_rclone_available():
    """Checks if rclone is available in system PATH."""
    return shutil.which("rclone") is not None

async def handle_cloud_menu(client, custom_style):
    """Interactive submenu to manage Cloud Backup & TG Mirroring."""
    while True:
        os.system('cls' if os.name == 'nt' else 'clear')
        print("\033[1;36mTelegram Downloader (V3.9.0)\033[0m")
        print("\033[1;35m--- CLOUD SYNC & TG MIRRORING ---\033[0m")
        
        config = get_config()
        cloud_enabled = config.get("cloud_upload_enabled", False)
        cloud_remote = config.get("cloud_rclone_remote", "")
        cloud_path = config.get("cloud_remote_path", "telegram-dl/backups")
        delete_local = config.get("cloud_delete_local_after_upload", False)
        
        tg_enabled = config.get("tg_mirror_enabled", False)
        tg_target = config.get("tg_mirror_target", "")
        
        # Display Statuses
        rclone_status = "\033[1;32mON\033[0m" if cloud_enabled else "\033[1;30mOFF\033[0m"
        if cloud_enabled and not is_rclone_available():
            rclone_status = "\033[1;31mON (Missing rclone in PATH)\033[0m"
            
        print(f"☁️  Cloud Backup : {rclone_status}")
        if cloud_enabled:
            print(f"   ↳ Remote Name : \033[1;36m{cloud_remote or 'Not Set'}\033[0m")
            print(f"   ↳ Cloud Path  : \033[1;30m{cloud_path}\033[0m")
            print(f"   ↳ Delete Local: \033[1;33m{'Yes' if delete_local else 'No'}\033[0m")
            
        print(f"📤 TG Mirroring  : \033[1;32mON\033[0m" if tg_enabled else "📤 TG Mirroring  : \033[1;30mOFF\033[0m")
        if tg_enabled:
            print(f"   ↳ Target Chat : \033[1;36m{tg_target or 'Not Set'}\033[0m")
        print("\033[1;35m---------------------------------\033[0m\n")
        
        choice = await questionary.select(
            "What would you like to configure:",
            choices=[
                "Toggle Cloud Backup (Rclone)",
                "Configure Rclone Remote Details",
                "Toggle Telegram Mirroring",
                "Configure Telegram Mirror Target",
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
            
        if choice == "Toggle Cloud Backup (Rclone)":
            if not cloud_enabled and not is_rclone_available():
                print("\n⚠️  \033[1;33m[WARNING] rclone is not found in your system PATH!\033[0m")
                print("To upload files to the cloud, please install Rclone on Windows:")
                print("👉 Run: \033[1;36mwinget install Rclone.Rclone\033[0m in a separate terminal.")
                await asyncio.sleep(4)
                
            if not cloud_enabled and not cloud_remote:
                # Prompt to set remote name immediately
                remote_input = await questionary.text("Enter Rclone remote name (run 'rclone config' in cmd to create one):", style=custom_style).ask_async()
                if remote_input == "SCHEDULER_TRIGGERED":
                    return "SCHEDULER_TRIGGERED"
                if remote_input and remote_input.strip():
                    config["cloud_rclone_remote"] = remote_input.strip()
                    config["cloud_upload_enabled"] = True
                    update_config(config)
            else:
                config["cloud_upload_enabled"] = not cloud_enabled
                update_config(config)
                
        elif choice == "Configure Rclone Remote Details":
            remote = await questionary.text("Enter Rclone remote name (e.g. gdrive):", default=cloud_remote, style=custom_style).ask_async()
            if remote == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"
            if remote is not None:
                config["cloud_rclone_remote"] = remote.strip()
                
            path = await questionary.text("Enter cloud target folder path:", default=cloud_path, style=custom_style).ask_async()
            if path == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"
            if path is not None:
                config["cloud_remote_path"] = path.strip()
                
            del_local = await questionary.confirm("Delete local downloaded file after successful cloud upload?", default=delete_local, style=custom_style).ask_async()
            if del_local == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"
            if del_local is not None:
                config["cloud_delete_local_after_upload"] = del_local
                
            update_config(config)
            print("\n✅ \033[1;32mRclone remote details updated successfully!\033[0m")
            await asyncio.sleep(1.5)
            
        elif choice == "Toggle Telegram Mirroring":
            if not tg_enabled and not tg_target:
                target_input = await questionary.text("Enter backup channel ID or @username (e.g. -100123456789):", style=custom_style).ask_async()
                if target_input == "SCHEDULER_TRIGGERED":
                    return "SCHEDULER_TRIGGERED"
                if target_input and target_input.strip():
                    config["tg_mirror_target"] = target_input.strip()
                    config["tg_mirror_enabled"] = True
                    update_config(config)
            else:
                config["tg_mirror_enabled"] = not tg_enabled
                update_config(config)
                
        elif choice == "Configure Telegram Mirror Target":
            target_input = await questionary.text("Enter backup channel ID or @username (e.g. -100123456789):", default=tg_target, style=custom_style).ask_async()
            if target_input == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"
            if target_input is not None:
                config["tg_mirror_target"] = target_input.strip()
                update_config(config)
                print("\n✅ \033[1;32mTelegram Mirror target chat updated!\033[0m")
                await asyncio.sleep(1.5)

async def process_post_download_backups(client, file_path, chat_name, caption=""):
    """
    Called after a successful download file write. 
    Triggers Telegram Mirroring and Cloud Upload (Rclone) in the background.
    """
    if not file_path or not os.path.exists(file_path):
        return
        
    config = get_config()
    
    # 1. Telegram Mirroring
    if config.get("tg_mirror_enabled", False):
        target = config.get("tg_mirror_target", "")
        if target:
            try:
                # Try parsing target to numeric ID if possible
                try:
                    target_entity = int(target)
                except ValueError:
                    target_entity = target
                    
                # Mirror in background
                asyncio.create_task(client.send_file(target_entity, file_path, caption=caption or ""))
            except Exception as e:
                # Suppress errors to not interrupt main downloader thread
                pass
                
    # 2. Cloud Backup (Rclone)
    if config.get("cloud_upload_enabled", False):
        remote = config.get("cloud_rclone_remote", "")
        if remote and is_rclone_available():
            cloud_path = config.get("cloud_remote_path", "telegram-dl/backups")
            delete_local = config.get("cloud_delete_local_after_upload", False)
            
            # Format destination path: remote:path/GroupName
            # Rclone accepts standard forward slashes for destination paths
            rclone_dest = f"{remote}:{cloud_path}/{chat_name}".replace("\\", "/")
            
            # Run rclone copy/move as subprocess in background
            action = "move" if delete_local else "copy"
            cmd = ["rclone", action, file_path, rclone_dest]
            
            try:
                # Run as background subprocess
                subprocess.Popen(
                    cmd, 
                    stdout=subprocess.DEVNULL, 
                    stderr=subprocess.DEVNULL
                )
            except Exception:
                pass
