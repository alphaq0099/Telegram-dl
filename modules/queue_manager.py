import os
import sys
import json
import asyncio
from datetime import datetime, timedelta
import questionary
from utils.download import get_config
from modules.single import download_single
from modules.range import download_range
from modules.full import download_full

from utils.auth import PROJECT_ROOT

QUEUE_FILE = os.path.join(PROJECT_ROOT, "queue.json")

def load_queue():
    if not os.path.exists(QUEUE_FILE):
        return []
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_queue(queue):
    try:
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(queue, f, indent=2)
    except Exception as e:
        print(f"❌ Failed to save queue file: {str(e)}")

# Global Scheduler State
scheduled_target_time = None
scheduler_task = None
is_processing = False

def get_scheduled_time_left():
    global scheduled_target_time
    if not scheduled_target_time:
        return None
    now = datetime.now()
    if scheduled_target_time <= now:
        return "Executing..."
    delta = scheduled_target_time - now
    seconds = int(delta.total_seconds())
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

class SchedulerTriggeredException(BaseException):
    """Exception raised to bubble up and abort prompt when scheduled download starts."""
    pass

async def background_scheduler_loop(client):
    global scheduled_target_time, is_processing
    from prompt_toolkit.application import get_app
    try:
        while scheduled_target_time is not None:
            now = datetime.now()
            if now >= scheduled_target_time:
                scheduled_target_time = None
                
                # Exit the currently active prompt_toolkit app by raising the scheduler exception
                try:
                    app = get_app()
                    if app and app.is_running:
                        app.exit(exception=SchedulerTriggeredException("Scheduled time reached"))
                        break
                except Exception:
                    pass
                
                # Fallback if no active app/tui menu found
                is_processing = True
                print("\n\n⏰ \033[1;36m[SCHEDULER] Scheduled time reached! Starting download queue...\033[0m\n")
                try:
                    await process_queue(client)
                except Exception as e:
                    print(f"❌ Error during scheduled queue execution: {str(e)}")
                is_processing = False
                break
                
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass

async def update_menu_countdown_loop(choice_obj, prefix_text, suffix_text=""):
    """
    Background loop to update a questionary Choice object's title dynamically
    with the ticking countdown.
    """
    from prompt_toolkit.application import get_app
    try:
        while True:
            time_left = get_scheduled_time_left()
            if time_left:
                choice_obj.title = f"{prefix_text} (⌛ {time_left}){suffix_text}"
            else:
                choice_obj.title = f"{prefix_text}..."
                
            try:
                app = get_app()
                if app and app.is_running:
                    app.invalidate()
            except Exception:
                pass
                
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass

async def handle_queue_menu(client, custom_style):
    """Sub-menu to manage scheduled downloads and download queue."""
    while True:
        os.system('cls' if os.name == 'nt' else 'clear')
        print("\033[1;36mTelegram Downloader (V3.9.0)\033[0m")
        print("\033[1;35m--- SCHEDULED QUEUE MANAGER ---\033[0m")
        
        queue = load_queue()
        pending_jobs = [j for j in queue if j.get("status") == "pending"]
        print(f"📋 Pending Jobs in Queue: \033[1;32m{len(pending_jobs)}\033[0m")
        print("\033[1;35m------------------------------\033[0m\n")
        
        choices_list = [
            "📥 Add Job to Queue",
            "📁 View / Manage Pending Queue",
            "🚀 Process Queue Now"
        ]
        
        global scheduled_target_time
        cancel_choice_obj = None
        ticker_task = None
        
        if scheduled_target_time:
            time_left = get_scheduled_time_left()
            cancel_choice_obj = questionary.Choice(
                title=f"⏰ Cancel Scheduled Processing (⌛ {time_left})",
                value="cancel_schedule"
            )
            choices_list.append(cancel_choice_obj)
        else:
            choices_list.append("⏰ Schedule Queue Processing")
            
        choices_list += [
            "❌ Back to main menu",
            questionary.Separator(),
            questionary.Separator("ARROW KEYS: Navigate | ENTER: Select | Ctrl+C: Cancel/Exit")
        ]
        
        if cancel_choice_obj:
            ticker_task = asyncio.create_task(update_menu_countdown_loop(cancel_choice_obj, "⏰ Cancel Scheduled Processing"))
            
        choice = await questionary.select(
            "Select action:",
            choices=choices_list,
            style=custom_style
        ).ask_async()
        
        if ticker_task:
            ticker_task.cancel()
            
        if choice == "SCHEDULER_TRIGGERED":
            return "SCHEDULER_TRIGGERED"
            
        if choice is None or choice == "❌ Back to main menu":
            break
            
        if choice == "📥 Add Job to Queue":
            res = await add_job_interactive(custom_style)
            if res == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"
            
        elif choice == "📁 View / Manage Pending Queue":
            res = await manage_queue_interactive(custom_style)
            if res == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"
            
        elif choice == "🚀 Process Queue Now":
            await process_queue(client)
            input("\n⌨️ Press Enter to return to menu...")
            
        elif choice == "cancel_schedule":
            await cancel_schedule_interactive()
            
        elif choice == "⏰ Schedule Queue Processing":
            res = await schedule_queue_interactive(client, custom_style)
            if res == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"

async def add_job_interactive(custom_style):
    while True:
        os.system('cls' if os.name == 'nt' else 'clear')
        print("\033[1;36mTelegram Downloader (V3.9.0)\033[0m")
        print("\033[1;35m--- ADD JOB TO DOWNLOAD QUEUE ---\033[0m\n")
        
        # Display current queue stats inline
        queue = load_queue()
        pending = [j for j in queue if j.get("status") == "pending"]
        print(f"📋 Current queue length: \033[1;32m{len(pending)} pending jobs\033[0m\n")
        
        job_type = await questionary.select(
            "Select job download mode:",
            choices=[
                "Single Message Link",
                "Message Range (Start / End Link)",
                "Full Chat History Scan",
                "Cancel / Done"
            ],
            style=custom_style
        ).ask_async()
        
        if job_type == "SCHEDULER_TRIGGERED":
            return "SCHEDULER_TRIGGERED"
            
        if job_type is None or job_type == "Cancel / Done":
            break
            
        payload = {}
        
        if job_type == "Single Message Link":
            link = await questionary.text("Enter message link:", style=custom_style).ask_async()
            if link == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"
            if not link:
                continue
            payload["link"] = link.strip()
            type_key = "single"
            
        elif job_type == "Message Range (Start / End Link)":
            start = await questionary.text("Enter START message link:", style=custom_style).ask_async()
            if start == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"
            if not start:
                continue
            end = await questionary.text("Enter END message link:", style=custom_style).ask_async()
            if end == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"
            if not end:
                continue
            payload["start_link"] = start.strip()
            payload["end_link"] = end.strip()
            type_key = "range"
            
        else: # Full Chat History Scan
            chat = await questionary.text("Enter chat username, link, or numeric ID:", style=custom_style).ask_async()
            if chat == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"
            if not chat:
                continue
            payload["chat_input"] = chat.strip()
            
            limit_str = await questionary.text("Enter max messages to scan (leave empty for ALL):", style=custom_style).ask_async()
            if limit_str == "SCHEDULER_TRIGGERED":
                return "SCHEDULER_TRIGGERED"
            limit = None
            if limit_str:
                try:
                    limit = int(limit_str)
                except ValueError:
                    pass
            payload["limit"] = limit
            type_key = "full"
            
        new_job = {
            "id": f"job_{int(datetime.now().timestamp())}_{len(queue)}",
            "type": type_key,
            "payload": payload,
            "status": "pending",
            "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        queue.append(new_job)
        save_queue(queue)
        print("\n✅ \033[1;32mJob successfully added to the queue!\033[0m")
        await asyncio.sleep(1.5)

async def manage_queue_interactive(custom_style):
    while True:
        os.system('cls' if os.name == 'nt' else 'clear')
        print("\033[1;35m--- MANAGE PENDING QUEUE ---\033[0m\n")
        
        queue = load_queue()
        pending = [j for j in queue if j["status"] == "pending"]
        
        if not pending:
            print("ℹ️  No pending jobs in the queue.")
            await asyncio.sleep(2)
            break
            
        choices = []
        for idx, job in enumerate(pending, start=1):
            t = job["type"].upper()
            added = job["added_at"]
            if t == "SINGLE":
                desc = job["payload"]["link"]
            elif t == "RANGE":
                desc = f"IDs {job['payload']['start_link']} to {job['payload']['end_link']}"
            else:
                desc = f"Chat: {job['payload']['chat_input']} (limit: {job['payload']['limit']})"
            choices.append(questionary.Choice(title=f"[{added}] [{t}] {desc[:60]}", value=job))
            
        choices.append(questionary.Choice(title="❌ Return to menu", value="BACK"))
        
        selected = await questionary.select(
            "Select job to delete from queue:",
            choices=choices,
            style=custom_style
        ).ask_async()
        
        if selected == "SCHEDULER_TRIGGERED":
            return "SCHEDULER_TRIGGERED"
            
        if selected == "BACK" or selected is None:
            break
            
        # Delete job
        queue = [j for j in queue if j["id"] != selected["id"]]
        save_queue(queue)
        print("\n🗑️  \033[1;32mJob removed from queue.\033[0m")
        await asyncio.sleep(1)

async def process_queue(client):
    """Loops and processes all pending queue jobs."""
    queue = load_queue()
    pending = [j for j in queue if j["status"] == "pending"]
    
    if not pending:
        print("\nℹ️  \033[1;33mNo pending jobs in the queue to process.\033[0m")
        return
        
    print(f"\n🚀 \033[1;36mProcessing {len(pending)} pending download jobs...\033[0m")
    
    for idx, job in enumerate(pending, start=1):
        print(f"\n⚡ \033[1;35m[Job {idx}/{len(pending)}] Running {job['type'].upper()} download...\033[0m")
        
        success = False
        try:
            if job["type"] == "single":
                success = await download_single(client, job["payload"]["link"])
            elif job["type"] == "range":
                success = await download_range(client, job["payload"]["start_link"], job["payload"]["end_link"])
            elif job["type"] == "full":
                success = await download_full(client, job["payload"]["chat_input"], None, job["payload"]["limit"])
        except Exception as e:
            print(f"❌ Job execution crashed: {str(e)}")
            
        # Update job status in database
        for q_job in queue:
            if q_job["id"] == job["id"]:
                q_job["status"] = "completed" if success else "failed"
                q_job["processed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
        save_queue(queue)
        
        if success:
            print(f"✅ \033[1;32m[Job {idx}] Completed successfully!\033[0m")
        else:
            print(f"❌ \033[1;31m[Job {idx}] Failed to complete.\033[0m")
            
    print("\n🏁 \033[1;32mQueue processing session completed!\033[0m")

async def cancel_schedule_interactive():
    global scheduled_target_time, scheduler_task
    if scheduler_task:
        scheduler_task.cancel()
        scheduler_task = None
    scheduled_target_time = None
    print("\n✅ \033[1;32mScheduled queue processing cancelled successfully!\033[0m")
    await asyncio.sleep(1.5)

async def schedule_queue_interactive(client, custom_style):
    time_str = await questionary.text(
        "Enter execution time in 24h format (HH:MM, e.g. 23:30 or 03:00):",
        style=custom_style
    ).ask_async()
    
    if time_str == "SCHEDULER_TRIGGERED":
        return "SCHEDULER_TRIGGERED"
        
    if not time_str:
        return
        
    try:
        parts = time_str.split(":")
        target_h = int(parts[0])
        target_m = int(parts[1])
        if not (0 <= target_h <= 23) or not (0 <= target_m <= 59):
            raise ValueError()
    except Exception:
        print("❌ \033[1;31mInvalid time format. Please enter as HH:MM.\033[0m")
        await asyncio.sleep(2)
        return
        
    now = datetime.now()
    target_time = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
    
    if target_time < now:
        target_time += timedelta(days=1)
        
    wait_delta = target_time - now
    
    global scheduled_target_time, scheduler_task
    scheduled_target_time = target_time
    if scheduler_task:
        scheduler_task.cancel()
        
    scheduler_task = asyncio.create_task(background_scheduler_loop(client))
    
    print(f"\n✅ \033[1;32mQueue successfully scheduled for: {target_time.strftime('%Y-%m-%d %H:%M:%S')}\033[0m")
    print(f"⏳ Time left: {wait_delta}")
    print("ℹ️  You can continue using the CLI. The queue will trigger automatically in the background.")
    await asyncio.sleep(3.5)
