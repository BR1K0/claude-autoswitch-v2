"""Stop hook for Autoswitch rollover (Step 4). Register `python3 <repo>/hook/stop.py` under hooks.Stop.

Does nothing (prints nothing, writes nothing) unless AUTOSWITCH_ROLLOVER and AUTOSWITCH_HANDOFF are on and
AUTOSWITCH_MODE is enforce, in a desktop-app chat. Then, when the agent ends a turn in a chat over the
rollover threshold and every guard holds, it blocks the stop once ({"decision": "block"}) with the rollover
instruction (rollover.instruction). Never two blocked stops in a row for one chat. Any failure: no output.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def main():
    import sbqueue as sb
    import rollover
    if sb.mode() != "enforce" or not rollover.active():
        return
    hook = json.load(sys.stdin)
    app_id = rollover.host_session_id()
    if not app_id:
        return
    try:
        rollover.recover()
    except Exception:
        pass
    out = rollover.on_stop(hook, app_id)
    if out:
        print(json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # never keep a chat from stopping because of this hook
    sys.exit(0)
