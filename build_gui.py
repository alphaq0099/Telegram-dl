import os
import sys
import subprocess
import shutil

def build_gui_executable():
    """Packages the Flet Telegram Downloader GUI into a standalone Windows executable without a console window."""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    gui_script = os.path.join(current_dir, "telegram-dl-gui.py")

    if not os.path.exists(gui_script):
        print(f"Error: GUI script not found at {gui_script}")
        sys.exit(1)

    print("🚀 Building standalone Telegram Downloader Desktop GUI (v3.9.0)...")
    print("Windowed mode: Active (no debug console will appear)")

    cmd = [
        sys.executable, "-m", "flet", "pack",
        gui_script,
        "--name", "Telegram-DL-GUI",
        "--product-name", "Telegram Downloader",
        "--product-version", "3.9.0",
        "--file-version", "3.9.0.0",
        "--file-description", "Telegram Downloader & Media Manager Desktop",
        "-y"
    ]

    try:
        subprocess.run(cmd, check=True)
        dist_exe = os.path.join(current_dir, "dist", "Telegram-DL-GUI.exe")
        print("\n✨ Build completed successfully!")
        print(f"📦 Executable located at: {dist_exe}")

        # Clean up build directory and spec file if present
        build_dir = os.path.join(current_dir, "build")
        spec_file = os.path.join(current_dir, "Telegram-DL-GUI.spec")
        if os.path.exists(build_dir):
            shutil.rmtree(build_dir, ignore_errors=True)
        if os.path.exists(spec_file):
            os.remove(spec_file)

    except subprocess.CalledProcessError as e:
        print(f"\n❌ Build failed: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    build_gui_executable()
