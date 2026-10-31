"""PostModelSwitch hook: remember whether the last model change was made by hand (see pins.py).

Register under hooks.PostModelSwitch. Fires in CLI 2.1.309 with source command|picker|sdk|auto|resume.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import pins

    sys.stdin.reconfigure(encoding="utf-8-sig")  # tolerate a BOM from PowerShell pipes
    pins.record_switch(json.load(sys.stdin))
except Exception:
    pass
sys.exit(0)
