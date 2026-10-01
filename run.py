#!/usr/bin/env python3
"""Local dev launcher. Production uses the startCommand in render.yaml."""

import os
import sys

if __name__ == "__main__":
    try:
        import uvicorn
    except ImportError:
        sys.exit("Dependencies missing. Run: pip install -r requirements.txt")

    port = int(os.environ.get("PORT", 8000))
    print(f"Meraki -> http://127.0.0.1:{port}")
    # 64 KiB frames: mic chunks are a few KiB, so the 16 MiB default only helps
    # someone trying to exhaust memory.
    uvicorn.run(
        "meraki.main:app",
        host="127.0.0.1",
        port=port,
        reload=True,
        ws_max_size=65536,
    )
