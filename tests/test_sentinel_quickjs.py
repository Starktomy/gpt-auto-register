from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_quickjs_action_exits_after_output_is_flushed():
    sdk = b"""
class _ {
  async getRequirementsToken() {
    setInterval(() => {}, 1000);
    return 'local-requirements-token';
  }
}
var P=new _;
var SentinelSDK={ init: async () => null };
"""
    payload = json.dumps(
        {
            "action": "requirements",
            "device_id": "test-device",
            "user_agent": "Mozilla/5.0",
            "screen_width": "1920",
            "screen_height": "1080",
            "language": "en-US",
            "languages": ["en-US"],
            "platform": "Win32",
            "vendor": "Google Inc.",
            "hardware_concurrency": 8,
            "timezone": "UTC",
        }
    )
    started = time.monotonic()

    with tempfile.TemporaryDirectory() as cache_dir:
        sdk_file = Path(cache_dir) / "sdk.js"
        sdk_file.write_bytes(sdk)
        env = {**os.environ, "OPENAI_SENTINEL_SDK_FILE": str(sdk_file)}
        proc = subprocess.run(
            ["node", str(PROJECT_ROOT / "regix/openai/openai_sentinel_quickjs.js")],
            input=payload,
            text=True,
            capture_output=True,
            timeout=3,
            env=env,
        )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"request_p": "local-requirements-token"}
    assert time.monotonic() - started < 3
