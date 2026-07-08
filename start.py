#!/usr/bin/env python3
"""MyMusicBox v2 -- start script"""
import subprocess, sys, os

ROOT = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(ROOT, "backend")

if __name__ == "__main__":
    print("-" * 48)
    print("  MyMusicBox v2")
    print("-" * 48)
    print("Installing dependencies...")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", os.path.join(BACKEND, "requirements.txt"), "-q"],
        check=True
    )
    print("")
    print("  >> Open http://localhost:8765 in your browser <<")
    print("")
    print("  Use the Stop Server button inside the app to quit.")
    print("-" * 48)

    os.chdir(BACKEND)
    sys.path.insert(0, BACKEND)

    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8765,
        reload=True,
        reload_dirs=[BACKEND],
        log_level="info",   # show errors in terminal
        access_log=False,   # hide per-request lines to reduce PS noise
    )
