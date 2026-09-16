import re

# Regex for matching telegram links:
# e.g., https://t.me/c/123456789/456 (private) or https://t.me/channel_name/456 (public)
# Also supports topic/thread links: https://t.me/channel_name/7844/11632
TG_LINK_REGEX = re.compile(
    r'(?:https?://)?(?:t|telegram)\.me/(c/)?([a-zA-Z0-9_.-]+)/(\d+)(?:/(\d+))?'
)

def parse_telegram_link(link: str):
    """
    Parses a Telegram message link.
    Returns a tuple of (chat_identifier, message_id, topic_id) or (None, None, None).
    
    If the link is private (contains '/c/'), the chat_identifier is converted 
    to an integer and prefixed with -100 (required for Telethon to identify 
    private groups/channels).
    """
    match = TG_LINK_REGEX.search(link)
    if not match:
        return None, None, None
    
    is_private = bool(match.group(1))
    chat_str = match.group(2)
    
    # If group 4 is present, group 3 is the topic/thread ID and group 4 is the message ID.
    # Otherwise, group 3 is the message ID.
    topic_id = None
    if match.group(4):
        message_id = int(match.group(4))
        topic_id = int(match.group(3))
    else:
        message_id = int(match.group(3))
    
    resolved_chat = chat_str
    try:
        resolved_chat = int(chat_str)
        # Private chat IDs in Telethon need to be integers prefixed with -100
        if is_private and resolved_chat > 0:
            resolved_chat = int(f"-100{resolved_chat}")
    except ValueError:
        pass
        
    return resolved_chat, message_id, topic_id

def parse_chat_identifier(chat_input):
    """
    Parses a general chat input (could be a link, @username, or numeric ID).
    Returns a clean identifier (int or str) usable by Telethon.
    """
    if isinstance(chat_input, int):
        return chat_input
        
    chat_input = str(chat_input).strip()
    if chat_input.lstrip("-").isdigit():
        try:
            return int(chat_input)
        except ValueError:
            pass
            
    # Check if it's a link first
    link_chat, _, _ = parse_telegram_link(chat_input)
    if link_chat is not None:
        return link_chat
        
    # Check if it's a link to a chat (without message ID) e.g. https://t.me/username or https://t.me/c/123456789
    chat_link_match = re.search(r'(?:https?://)?(?:t|telegram)\.me/(c/)?([a-zA-Z0-9_.-]+)/?$', chat_input)
    if chat_link_match:
        is_private = bool(chat_link_match.group(1))
        chat_str = chat_link_match.group(2)
        if is_private:
            try:
                chat_id = int(chat_str)
                if chat_id > 0:
                    chat_id = int(f"-100{chat_id}")
                return chat_id
            except ValueError:
                return chat_str
        else:
            try:
                return int(chat_str)
            except ValueError:
                return chat_str

    # Remove '@' prefix if present
    if chat_input.startswith('@'):
        return chat_input[1:]
        
    # Check if it is a numeric ID
    try:
        return int(chat_input)
    except ValueError:
        return chat_input

async def resolve_chat_entity(client, chat_input: str):
    """
    Resolves a Telegram chat/channel/group entity from a user input string.
    Tries multiple candidate formats for numeric IDs (like adding -100 prefix or making it negative).
    Returns the resolved entity. Raises the last exception encountered if all fail.
    """
    # First clean and parse chat input
    chat_identifier = parse_chat_identifier(chat_input)
    
    candidates = []
    # If the parsed identifier is an integer
    if isinstance(chat_identifier, int):
        val = chat_identifier
        candidates.append(val)
        # If positive, try adding -100 prefix (supergroup/channel) or negative sign (normal group)
        if val > 0:
            candidates.append(int(f"-100{val}"))
            candidates.append(-val)
    else:
        # If it is a string representing an integer (can happen if regex returned it)
        is_numeric = False
        if chat_identifier.isdigit():
            is_numeric = True
        elif chat_identifier.startswith('-') and chat_identifier[1:].isdigit():
            is_numeric = True
            
        if is_numeric:
            val = int(chat_identifier)
            candidates.append(val)
            if val > 0:
                candidates.append(int(f"-100{val}"))
                candidates.append(-val)
        else:
            candidates.append(chat_identifier)
            
    # Try resolving candidate identifiers
    last_err = None
    for candidate in candidates:
        try:
            entity = await client.get_entity(candidate)
            if entity:
                return entity
        except Exception as e:
            last_err = e
            
    raise last_err or Exception(f"Cannot find any entity corresponding to \"{chat_input}\"")
