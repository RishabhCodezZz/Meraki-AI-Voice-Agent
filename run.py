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
    uvicorn.run("meraki.main:app", host="127.0.0.1", port=port, reload=True)
