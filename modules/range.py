import sys
import asyncio
from tqdm import tqdm
from utils.parser import parse_telegram_link
from utils.download import download_message_media, get_config

async def download_range(client, start_link: str, end_link: str, override_types=None):
    """
    Downloads media from a range of message links in the same chat.
    """
    start_link = start_link.strip()
    end_link = end_link.strip()
    
    # 1. Parse start link
    start_chat_id, start_msg_id, start_topic_id = parse_telegram_link(start_link)
    # 2. Parse end link
    end_chat_id, end_msg_id, end_topic_id = parse_telegram_link(end_link)
    
    if start_chat_id is None or start_msg_id is None:
        print("❌ \033[1;31m[ERROR] Invalid starting message link format.\033[0m")
        return False
        
    if end_chat_id is None or end_msg_id is None:
        print("❌ \033[1;31m[ERROR] Invalid ending message link format.\033[0m")
        return False
        
    if start_chat_id != end_chat_id:
        print("❌ \033[1;31m[ERROR] The start and end links must be from the same chat/group.\033[0m")
        print(f"Start chat: {start_chat_id} | End chat: {end_chat_id}")
        return False

    if start_topic_id != end_topic_id:
        print("❌ \033[1;31m[ERROR] The start and end links must be from the same thread/topic.\033[0m")
        print(f"Start topic: {start_topic_id} | End topic: {end_topic_id}")
        return False

    chat_id = start_chat_id
    target_topic_id = start_topic_id
    min_id = min(start_msg_id, end_msg_id)
    max_id = max(start_msg_id, end_msg_id)
    
    config = get_config()
    max_concurrent = config.get("max_concurrent_downloads", 3)
    
    print(f"\n🚀 \033[1;36mPreparing to process messages in ID range {min_id} to {max_id} inside chat {chat_id}...\033[0m")
    
    success_count = 0
    fail_count = 0
    skip_count = 0
    
    try:
        # Resolve target entity first
        entity = await client.get_input_entity(chat_id)
        
        # Prepare parameters for iter_messages (exclusive boundaries -> adjust to make inclusive)
        iter_params = {
            "entity": entity,
            "min_id": min_id - 1,
            "max_id": max_id + 1,
        }
        
        # If target_topic_id > 1, we can filter directly at the Telegram server level for maximum speed
        if target_topic_id is not None and target_topic_id > 1:
            iter_params["reply_to"] = target_topic_id
            
        messages_list = []
        # Retrieve existing messages in the range
        async for msg in client.iter_messages(**iter_params):
            # Double check local topic filter for General topic (ID 1) or client-side fallback
            if target_topic_id is not None:
                msg_topic_id = None
                if msg.reply_to and getattr(msg.reply_to, 'forum_topic', False):
                    msg_topic_id = getattr(msg.reply_to, 'reply_to_top_id', None)
                    if msg_topic_id is None:
                        msg_topic_id = getattr(msg.reply_to, 'reply_to_msg_id', None)
                
                is_same_topic = False
                if msg_topic_id == target_topic_id:
                    is_same_topic = True
                elif (target_topic_id == 1 or target_topic_id is None) and msg_topic_id is None:
                    is_same_topic = True
                    
                if not is_same_topic and msg.id != target_topic_id:
                    continue
            
            messages_list.append(msg)
            
        total_found = len(messages_list)
        tqdm.write(f"📊 Found \033[1;32m{total_found}\033[0m active messages in the specified range/topic.")
        
        # Download them in batches to respect concurrency limits
        sem = asyncio.Semaphore(max_concurrent)
        batch_size = 100
        for i in range(0, total_found, batch_size):
            batch_messages = messages_list[i:i + batch_size]
            tqdm.write(f"\n📥 Processing batch {i // batch_size + 1} ({len(batch_messages)} files)...")
            
            tasks = []
            for msg in batch_messages:
                if not msg.media:
                    skip_count += 1
                    continue
                    
                async def sem_download(m=msg):
                    async with sem:
                        return await download_message_media(client, m, chat_id, override_types)
                        
                tasks.append(sem_download())
                
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for res in results:
                    if isinstance(res, Exception):
                        tqdm.write(f"❌ \033[1;31m[ERROR] Unexpected download error: {str(res)}\033[0m")
                        fail_count += 1
                    elif isinstance(res, tuple):
                        success, result = res
                        if success:
                            if "File already exists" in result or "(Skipped)" in result:
                                skip_count += 1
                            else:
                                success_count += 1
                        else:
                            tqdm.write(f"❌ \033[1;31m[FAILED] {result}\033[0m")
                            fail_count += 1
                            
    except Exception as e:
        print(f"❌ \033[1;31m[ERROR] Failed during range download: {str(e)}\033[0m", file=sys.stderr)
        return False
            
    print(f"\n📊 \033[1;35mRANGE DOWNLOAD SUMMARY\033[0m")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"📝 {'Total messages processed:'.ljust(30)} \033[1;37m{total_found}\033[0m")
    print(f"📥 {'Successfully downloaded:'.ljust(30)} \033[1;32m{success_count}\033[0m")
    print(f"⏭️  {'Skipped (No media/existed):'.ljust(30)} \033[1;33m{skip_count}\033[0m")
    print(f"❌ {'Failed downloads:'.ljust(30)} \033[1;31m{fail_count}\033[0m")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")
    return True
