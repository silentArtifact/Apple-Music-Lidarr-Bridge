#!/usr/bin/env python3
"""Container entrypoint.

Imports bridge as a normal module (registered as "bridge" in sys.modules) and
calls main(). This matters because web.py also does `import bridge`: if bridge
were executed as __main__ instead, web would import a *second* copy with its own
lock and state globals. Going through this launcher guarantees the poll loop and
the web UI share one module instance.
"""

import bridge

if __name__ == "__main__":
    bridge.main()
