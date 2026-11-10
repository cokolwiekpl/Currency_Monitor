"""Run inside the image twice against one disposable volume, without network access."""
import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import main
import storage

assert os.getuid() != 0, "The image must run as a non-root user"
assert main.main(["--validate"]) == 0
state_path = Path("/data/state.json")
previously_started = state_path.exists()
quotes = [
    main.Quote("2026-11-09", Decimal("4.00"), "NBP", "2026-11-09"),
    main.Quote("2026-11-10", Decimal("4.10"), "NBP", "2026-11-10"),
]
with patch.dict(os.environ, {"DISCORD_WEBHOOK_PRIMARY": "https://discord.com/api/webhooks/123/example"}), patch.object(main, "fetch_nbp_history", return_value=quotes), patch.object(main, "send_to_discord") as send:
    assert main.main(["--once"]) == 0
    assert send.call_count == (0 if previously_started else 1)
assert len(storage.load_state(state_path)["alerts"]) == 2
with storage.state_lock():
    try:
        with storage.state_lock():
            raise AssertionError("Concurrent access was allowed")
    except RuntimeError:
        pass
print("Container smoke test passed, persisted state reused:", previously_started)
