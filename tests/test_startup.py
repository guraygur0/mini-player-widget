import asyncio
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from media import MediaBridge

class StartupTests(unittest.TestCase):
    def test_media_service_retries_initial_failure(self):
        manager = SimpleNamespace(request_async=AsyncMock(side_effect=[OSError('not ready'), object()]))
        snapshots = []
        bridge = SimpleNamespace(interval=.01, session=None, snapshot=SimpleNamespace(emit=snapshots.append), read=AsyncMock(return_value={'ok': True}))
        sleeps = 0
        async def sleep(_):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 2:
                raise asyncio.CancelledError()
        module = SimpleNamespace(GlobalSystemMediaTransportControlsSessionManager=manager)
        with patch.dict(sys.modules, {'winrt.windows.media.control': module}), patch('media.asyncio.sleep', sleep):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(MediaBridge.main(bridge))
        self.assertEqual(manager.request_async.await_count, 2)
        self.assertEqual(snapshots[-1], {'ok': True})

if __name__ == '__main__':
    unittest.main()
