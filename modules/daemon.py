import os
import sys
import json
import asyncio
from datetime import datetime
import questionary
from telethon import events
from tqdm import tqdm
from utils.download import get_config, download_message_media
from utils.parser import resolve_chat_entity

from utils.auth import PROJECT_ROOT

def update_config(new_config):
    """Saves changes back to config.json."""
    config_path = os.path.join(PROJECT_ROOT, 'config.json')
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(new_config, f, indent=2)
    except Exception as e:
        print(f"\033[1;31m[ERROR] Failed to save configuration: {str(e)}\033[0m", file=sys.stderr)

async def handle_daemon_menu(client, custom_style):
    """Sub-menu to manage the Live Monitor Daemon."""
    while True:
        os.system('cls' if os.name == 'nt' else 'clear')
        print("\033[1;36mTelegram Downloader (V3.8.5)\033[0m")
        print("\033[1;35m--- LIVE MONITOR DAEMON ---\033[0m")
        
        config = get_config()
        monitored = config.get("monitored_chats", [])
        
        # Display monitored chats titles
        print(f"📡  Status: \033[1;30mIdle\033[0m")
        print(f"👁️   Monitored Chats ({len(monitored)}):")
        if monitored:
            for c in monitored:
                if isinstance(c, dict):
                    print(f"    - \033[1;32m{c.get('title') or c.get('id')}\033[0m")
                else:
                    print(f"    - \033[1;32m{c}\033[0m")
        else:
            print("    - \033[1;30mNone\033[0m")
        print("\033[1;35m---------------------------\033[0m\n")
        
        choice = await questionary.select(
            "What would you like to do:",
            choices=[
                "Start Live Monitoring",
                "Add Chats to Monitor",
                "Remove Chats from Monitor",
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
            
        if choice == "Start Live Monitoring":
            if not monitored:
                print("\n⚠️  \033[1;33mPlease add at least one chat to monitor first.\033[0m")
                await asyncio.sleep(2)
                continue
            await run_live_monitor(client, monitored)
            
        elif choice == "Add Chats to Monitor":
            res = await add_monitored_chat(client, config, custom_style)
            if res == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"
            
        elif choice == "Remove Chats from Monitor":
            res = await remove_monitored_chat(config, custom_style)
            if res == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"

async def add_monitored_chat(client, config, custom_style):
    chat_input = await questionary.text(
        "Enter chat username, invite link, or numeric ID to monitor:",
        style=custom_style
    ).ask_async()
    if chat_input == "SCHEDULER_TRIGGERED":
        return "SCHEDULER_TRIGGERED"
    if not chat_input:
        return
        
    chat_input = chat_input.strip()
    try:
        print("\n🔍 \033[1;36mResolving chat entity...\033[0m")
        entity = await resolve_chat_entity(client, chat_input)
        entity_id = entity.id
        # Resolve the title robustly (handling channels/groups vs users/bots)
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
            
        # Append type labels (Bot, Group, Channel, User)
        from telethon.tl.types import Channel, Chat, User
        label = "Chat"
        if isinstance(entity, User):
            label = "Bot" if entity.bot else "User"
        elif isinstance(entity, Chat):
            label = "Group"
        elif isinstance(entity, Channel):
            label = "Group" if getattr(entity, 'megagroup', False) else "Channel"
            
        title = f"{title} ({label})"
        
        # Determine the identifier to save
        identifier = getattr(entity, 'username', None)
        if not identifier:
            identifier = entity_id
            
        monitored = config.get("monitored_chats", [])
        
        # Check for duplicates
        is_dup = False
        for c in monitored:
            c_id = c.get('id') if isinstance(c, dict) else c
            if c_id == identifier or str(c_id) == str(identifier):
                is_dup = True
                break
                
        if is_dup:
            print(f"\n⚠️  \033[1;33m'{title}' is already in your monitor list.\033[0m")
        else:
            monitored.append({"id": identifier, "title": title})
            config["monitored_chats"] = monitored
            update_config(config)
            print(f"\n✅  \033[1;32mSuccessfully added '{title}' to monitor list!\033[0m")
    except Exception as e:
        print(f"\n❌  \033[1;31m[ERROR] Failed to resolve chat: {str(e)}\033[0m")
    await asyncio.sleep(2)

async def remove_monitored_chat(config, custom_style):
    monitored = config.get("monitored_chats", [])
    if not monitored:
        print("\n⚠️  \033[1;33mNo monitored chats to remove.\033[0m")
        await asyncio.sleep(1.5)
        return
        
    choices = []
    for c in monitored:
        if isinstance(c, dict):
            choices.append(f"{c.get('title') or c.get('id')} ({c.get('id')})")
        else:
            choices.append(str(c))
    choices.append("Cancel")
    
    to_remove = await questionary.select(
        "Select a chat to remove from monitoring:",
        choices=choices,
        style=custom_style
    ).ask_async()
    
    if to_remove == "SCHEDULER_TRIGGERED":
        return "SCHEDULER_TRIGGERED"
    
    if to_remove is None or to_remove == "Cancel":
        return
        
    # Find and remove
    for val in list(monitored):
        match = False
        if isinstance(val, dict):
            repr_str = f"{val.get('title') or val.get('id')} ({val.get('id')})"
            if repr_str == to_remove:
                match = True
        else:
            if str(val) == to_remove:
                match = True
        if match:
            monitored.remove(val)
            break
            
    config["monitored_chats"] = monitored
    update_config(config)
    print(f"\n✅  \033[1;32mRemoved '{to_remove}' from monitor list.\033[0m")
    await asyncio.sleep(1.5)

async def run_live_monitor(client, monitored_chats):
    """
    Spawns Telethon event listeners on the monitored chats to auto-download media files as they arrive.
    """
    os.system('cls' if os.name == 'nt' else 'clear')
    print("\033[1;36m============================================================\033[0m")
    print("📡  \033[1;35m[LIVE MONITOR ACTIVE] - Watching chats for new media...\033[0m")
    print("\033[1;36m============================================================\033[0m")
    print("👁️   Resolving monitored chats...")
    
    resolved_entities = []
    monitored_id_set = set()
    
    from telethon.utils import get_peer_id
    
    for c in monitored_chats:
        raw_id = c["id"] if isinstance(c, dict) else c
        title = c.get("title") if isinstance(c, dict) else str(c)
        
        # Always register raw representations in our fallback set
        monitored_id_set.add(raw_id)
        monitored_id_set.add(str(raw_id))
        if str(raw_id).lstrip("-").isdigit():
            monitored_id_set.add(int(raw_id))
            # Also register channel prefix versions
            if not str(raw_id).startswith("-100"):
                try:
                    monitored_id_set.add(int(f"-100{raw_id}"))
                except ValueError:
                    pass
        elif isinstance(raw_id, str):
            monitored_id_set.add(raw_id.lower().lstrip("@"))
            monitored_id_set.add(f"@{raw_id.lower().lstrip('@')}")

        try:
            entity = await resolve_chat_entity(client, raw_id)
            if entity:
                resolved_entities.append(entity)
                entity_id = entity.id
                monitored_id_set.add(entity_id)
                monitored_id_set.add(str(entity_id))
                
                try:
                    peer_id = get_peer_id(entity)
                    monitored_id_set.add(peer_id)
                    monitored_id_set.add(str(peer_id))
                except Exception:
                    pass
                    
                username = getattr(entity, 'username', None)
                if username:
                    monitored_id_set.add(username.lower())
                    monitored_id_set.add(f"@{username.lower()}")
                    
                print(f"    - \033[1;32m{title}\033[0m [Ready]")
            else:
                print(f"    - \033[1;33m{title}\033[0m [Registered by ID]")
        except Exception as e:
            print(f"    - \033[1;33m{title}\033[0m [Registered: {e}]")
            
    print("\033[1;30m------------------------------------------------------------\033[0m")
    
    # Read bot session settings for visual status heads-up
    config = get_config()
    bot_grouping = config.get("monitor_bot_session_grouping", True)
    bot_gap = config.get("monitor_bot_session_gap_seconds", 60)
    if bot_grouping:
        print(f"🤖  \033[1;36mBot Session Grouping:\033[0m \033[1;32mACTIVE\033[0m ({bot_gap}s inactivity gap)")
    else:
        print(f"🤖  \033[1;36mBot Session Grouping:\033[0m \033[1;30mDISABLED\033[0m")
        
    print("\033[1;30m------------------------------------------------------------\033[0m")
    print("⚠️  \033[1;33mPress Ctrl+C at any time to STOP and return to menu.\033[0m")
    print("\033[1;30m------------------------------------------------------------\033[0m")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Listening for incoming messages...\n")
    
    is_monitoring_active = True
    active_download_tasks = set()

    async def new_message_callback(event):
        if not is_monitoring_active:
            return
            
        current_task = asyncio.current_task()
        if current_task:
            active_download_tasks.add(current_task)
            
        try:
            # Check if event is from our monitored chats
            chat_id = event.chat_id
            sender_id = getattr(event, 'sender_id', None)
            
            # Fast membership check against our comprehensive set
            is_monitored = (
                chat_id in monitored_id_set or 
                str(chat_id) in monitored_id_set or
                (sender_id and (sender_id in monitored_id_set or str(sender_id) in monitored_id_set))
            )
            
            if not is_monitored and resolved_entities:
                # Also check username if available
                try:
                    chat = await event.get_chat()
                    uname = getattr(chat, 'username', None)
                    if uname and (uname.lower() in monitored_id_set or f"@{uname.lower()}" in monitored_id_set):
                        is_monitored = True
                except Exception:
                    pass
                    
            if not is_monitored and resolved_entities:
                return

            if not event.message or not event.message.media:
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
            except Exception:
                chat_title = str(event.chat_id)
                
            message = event.message
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            tqdm.write(f"\n📥 [{timestamp}] [New Media] {chat_title}: Found media in message {message.id}")
            
            success, result = await download_message_media(client, message, event.chat_id, is_monitor=True)
            if success:
                if "already exists" in result or "already archived" in result:
                    tqdm.write(f"   ↳ 🔄 {result}")
                elif "Filtered" in result:
                    tqdm.write(f"   ↳ ⏳ {result}")
                else:
                    tqdm.write(f"   ↳ ✅ Successfully downloaded: {os.path.basename(result)}")
            else:
                tqdm.write(f"   ↳ ❌ Download failed: {result}")
        except asyncio.CancelledError:
            tqdm.write(f"   ↳ 🛑 Download cancelled (Monitor stopped).")
            raise
        except Exception as err:
            tqdm.write(f"   ↳ ❌ Error processing message: {str(err)}")
        finally:
            if current_task:
                active_download_tasks.discard(current_task)

    # Add Telethon event handler (using resolved entities if available, otherwise catch all and filter)
    if resolved_entities:
        client.add_event_handler(new_message_callback, events.NewMessage(chats=resolved_entities))
    else:
        client.add_event_handler(new_message_callback, events.NewMessage())
    
    try:
        # Keep running until cancelled by user (Ctrl+C)
        while True:
            await asyncio.sleep(1)
    except (asyncio.CancelledError, KeyboardInterrupt):
        print("\n🛑 \033[1;31mStopping Live Monitor...\033[0m")
    finally:
        is_monitoring_active = False
        try:
            client.remove_event_handler(new_message_callback)
            print("\033[1;32mEvent listener successfully removed.\033[0m")
        except Exception:
            pass
            
        # Cancel any in-progress download tasks so they don't continue in the background
        if active_download_tasks:
            print(f"🛑 Cancelling {len(active_download_tasks)} active in-progress download(s)...")
            for t in list(active_download_tasks):
                if not t.done():
                    t.cancel()
            try:
                await asyncio.gather(*active_download_tasks, return_exceptions=True)
            except Exception:
                pass
            active_download_tasks.clear()
            print("\033[1;32mAll active downloads stopped.\033[0m")
