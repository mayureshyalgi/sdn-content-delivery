#!/usr/bin/env python3
"""
Creates the test files that the content servers share.

All Mininet hosts share the same file system, so one content folder
is enough for all three servers.

    python3 app/make_content.py
"""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
CONTENT_DIR = os.path.join(HERE, "content")

FILES = {
    "small.txt": 1 * 1024,            # 1 KB   - tests response time
    "medium.bin": 1 * 1024 * 1024,    # 1 MB   - general tests
    "large.bin": 5 * 1024 * 1024,     # 5 MB   - tests throughput / congestion
}


def main():
    os.makedirs(CONTENT_DIR, exist_ok=True)
    for name, size in FILES.items():
        path = os.path.join(CONTENT_DIR, name)
        with open(path, "wb") as f:
            if name.endswith(".txt"):
                line = b"SDN content delivery test file.\n"
                f.write((line * (size // len(line) + 1))[:size])
            else:
                f.write(os.urandom(size))
        print(f"created {path} ({size} bytes)")


if __name__ == "__main__":
    main()
