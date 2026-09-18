# Telegram Downloader V3.9.0 (CLI & Desktop GUI) 🚀

An advanced, high-performance, asynchronous userbot-style Telegram media downloader built in Python using the **Telethon** MTProto library and **Flet** GUI framework. 

This downloader provides both a modern, keyboard-navigable terminal interface (TUI) and a sleek, modern desktop graphical user interface (GUI) to retrieve photos, videos, documents, audios, voice notes, and stickers from public and private groups, channels, chats, and bots.

---

## 🌟 V3.9.0 Key Features

* **Modern Desktop GUI (Flet)**: A complete, dark-themed native desktop interface featuring 5 distinct tabs: Downloader (single, range, full chat), Scheduled Queue Manager, Live Monitor Daemon, Offline History & Statistics Search, and comprehensive Settings Configurator.
* **Portable & Zero-Setup Executable**: Compiles into a single standalone `.exe`. If no configuration is present, it dynamically prompts for Telegram API credentials and saves them to a self-healing `config.json` next to the executable. Path-persistence logic keeps your logins, database, and settings intact across directories.
* **Bypasses Save Restrictions**: Naturally bypasses Telegram's client-side copy/forwarding restrictions (e.g., *"Restrict saving content"* in private groups/channels) by directly requesting raw MTProto file chunks.
* **Resumable Chunk Downloads**: Tracks `.part` temporary downloads on disk. If interrupted, it automatically truncates and resumes from the nearest **4KB boundary** (Telegram requirement). Uses maximum **512KB chunk sizes** to minimize network request latency, boosting download speeds by up to 4x.
* **Scheduled Queue Manager**: 
  * Add links, chats, and ranges to a pending download queue to execute later.
  * Schedule downloads for a specific time and continue using the CLI. The background task keeps track of the schedule.
  * Displays a real-time countdown timer `Scheduled Queue Manager (⌛ hh:mm:ss)` on the main TUI menu.
  * Gracefully aborts active prompts and takes over the console to run progress bars cleanly once the timer fires.
* **Live Monitor Daemon**: Automatically watches specified chats in the background. Features **Bot Session Grouping** with custom inactivity gaps, grouping files sent by request bots into separate time-clustered subfolders (e.g. `Session_2026-07-09_12-30-00/`).
* **Sleek Web Gallery Generator**: Automatically compiles downloaded files into responsive, glassmorphic HTML galleries (`gallery.html`) containing search filtering, topic/chat views, audio players, video players, and image lightbox overlays.
* **Advanced Media Utilities**:
  * **Video Compressor**: Compresses videos to a target file size in MB using 2-pass FFmpeg encoding.
  * **Video to GIF**: Converts videos to highly-optimized web-friendly GIFs.
  * **Audio Volumizer**: Adjusts volume and applies dynamic range leveling to audio files.
  * **Audio Extractor**: Converts videos to high-quality MP3s via `ffmpeg` (optionally automated post-download).
  * **Sticker Converter**: Renders WebP stickers to PNG, and decompresses `.tgs` animated stickers to Lottie JSON.
  * **Batch Rename**: Clean names, prefix indexation, and random alphanumeric masks.
  * **Folder Organizer**: Sorts downloaded folders into categorized directories (Images, Videos, Docs, Audio, Archives).
  * **7-Zip password archiving**: Creates secure, AES-256 encrypted `.7z` or `.zip` archives.
  * **Duplicate Media Checker**: Scans download directories for identical files using a fast, collision-free signature hashing algorithm (combining file size with header and footer bytes) to find and remove duplicates interactively.
* **Cloud Sync & Telegram Mirroring**:
  * **Rclone Integration**: Asynchronously moves/copies downloaded files to Google Drive, Mega, OneDrive, etc., in the background (with optional local cleanup).
  * **Telegram Mirror**: Re-uploads downloaded files in real-time to another private channel or group.
* **Offline Search & History Dashboard**: 
  * Queries your download history by keywords, chats, or media types.
  * Integrates with the system file explorer to highlight files directly in **Windows Explorer**, **macOS Finder**, or **Linux File Manager**.
  * **Statistics Panel**: Visual bar charts showing media breakdown, top 5 chats by storage, and largest files, utilizing double-column alignment for wide CJK characters and emojis.
* **Global Bandwidth Throttling**: Configure a maximum download speed limit (using Token Bucket rate limiting) across all active concurrent threads.

---

## 📁 File Structure

```text
telegram-dl/
├── .env                  # API Credentials (created by copying .env.example)
├── .env.example          # Template for credentials
├── .gitignore            # Git exclusion rules
├── config.json           # Interactive configuration parameters (auto-generated)
├── requirements.txt      # Python libraries list
├── telegram-dl.py        # Main TUI & Menu loop driver
├── telegram-dl.bat       # Global launch batch script for Windows PATH
├── build.py              # PyInstaller executable builder
├── modules/
│   ├── single.py         # Single file downloader module
│   ├── range.py          # Message range downloader module (with server-side topic filter)
│   ├── full.py           # Full chat history downloader module
│   ├── daemon.py         # Live Monitor Daemon (with bot session clustering)
│   ├── search.py         # Offline search TUI panel
│   ├── cloud.py          # Rclone upload and mirroring setup panel
│   └── utilities.py      # Media extraction, renaming, and compression panel
└── utils/
    ├── archive.py        # SQLite3 download history database manager
    ├── auth.py           # MTProto authentication handler
    ├── parser.py         # Telegram link & ID parsing utilities
    ├── gallery_template.py # Separation of HTML/CSS/JS gallery code
    └── download.py       # Core downloader, semaphores, and path resolver
```

---

## ⚙️ Prerequisites & Setup

Choose one of the following methods to run the downloader:

### Option A: Standalone Executable (Zero Setup - Recommended)
1. Go to the **Releases** tab on the right side of this repository page.
2. Download the precompiled **`telegram-dl.exe`** binary.
3. Place the `.exe` in a clean folder and run it.
4. The program will guide you to set up your API ID, Hash, and phone login interactively! Your keys will be saved locally in `config.json` so you only have to enter them once.

---

### Option B: Run from Source Code (Python Setup)

#### 1. Retrieve Telegram API Credentials
1. Log in to [my.telegram.org](https://my.telegram.org) using your Telegram phone number.
2. Go to **API development tools**.
3. Create a new application (e.g. Title: `Media Downloader CLI`, Short name: `mymediadl`, Platform: `Desktop`).
4. Note down your `api_id` and `api_hash`.

#### 2. Configure Environment Secrets
Duplicate the `.env.example` file, rename it to `.env`, and fill in your credentials:
```ini
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_PHONE=your_phone_number_with_country_code # e.g. +1234567890
```

#### 3. Install Dependencies
Make sure you have Python 3.8+ installed. Run:
```bash
pip install -r requirements.txt
```
> [!NOTE]
> System dependencies: `ffmpeg` is required for audio extraction, `7-Zip` (`7z`) is required for password-protected archiving, and `rclone` is required for cloud upload backups.

---

## 🚀 Running the Downloader

### 1. If using the Python Source:
Run the main script to launch the interactive TUI:
```bash
python telegram-dl.py
```

### 2. If running globally from anywhere (Windows Batch):
If running the Python script, we have provided a batch file `telegram-dl.bat`. Add the directory containing `telegram-dl.bat` to your System `PATH` environment variable, and you can run:
```cmd
telegram-dl
```
from any folder on your computer!

### 3. How to compile the Executable yourself:
If you want to package the code into a single `.exe` file yourself:
```bash
python build.py
```
This script will automatically install PyInstaller, compile the source files with size optimizations, and output a standalone binary to the `dist/` directory, cleaning up all intermediate build folders afterward.

---

## ⚠️ Notes & Security Best Practices

* **Rate Limits**: If you run a **Full Download** on large channels with thousands of items, Telegram may trigger a `FloodWait`. The script will automatically sleep and resume, but it is best to respect rate limits or cap the download speed in the Config menu.
 
