"""Clear a manual pin: `unpin.py` (all chats) or `unpin.py <app id>` (one chat). A pinned chat is
never switched by autoswitch until its pin is cleared."""
import sys

import pins

if __name__ == "__main__":
    n = pins.unpin(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"cleared {n} pin(s)")
