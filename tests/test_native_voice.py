import unittest
from unittest.mock import AsyncMock, patch
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from ringbearer import native_voice
from ringbearer import config, telegram, transcribe
from tests.test_webhook import bridge, post


class NativeVoiceHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_m4a_conversion_is_ogg_opus(self):
        data = (Path(__file__).parent / 'fixtures' / 'ring_fixture.m4a').read_bytes()
        with TemporaryDirectory() as state:
            with patch.object(config, 'STATE_DIR', Path(state)):
                output = await native_voice.encode_opus(data)
            self.assertEqual(list((Path(state) / 'tmp').iterdir()), [])
        import av, io
        with av.open(io.BytesIO(output)) as container:
            self.assertEqual(container.streams.audio[0].codec_context.name, 'opus')
            self.assertGreater(sum(f.samples for f in container.decode(audio=0)), 0)

    async def test_upload_uses_existing_lock_and_preserves_voice_semantics(self):
        lock = asyncio.Lock()
        async def send(*args, **kwargs):
            self.assertTrue(lock.locked())
            self.assertTrue(kwargs['voice_note'])
            self.assertEqual(kwargs['mime_type'], 'audio/ogg')
            self.assertNotIn('[RING REPLY:', kwargs['caption'])
            return SimpleNamespace(id=77)
        client = SimpleNamespace(send_file=AsyncMock(side_effect=send))
        with patch.object(config, 'TELEGRAM_ENABLED', True), patch.object(config, 'WEBHOOK_ASSISTANT', 'assistant'), patch.object(telegram, 'assistant_entities', {'assistant': 'peer'}), patch.object(telegram, 'tg_client', client), patch.object(telegram, 'tg_health', SimpleNamespace(fatal=False)), patch.object(telegram, '_client_lock', return_value=lock), patch.object(telegram, 'log_capture') as log, patch.object(native_voice, 'encode_opus', AsyncMock(return_value=b'OggSfixture')):
            response = await native_voice.relay_voice(b'audio', {})
            self.assertEqual(json.loads(response.body)['telegram_message_id'], 77)
            self.assertTrue(log.call_args.args[0]['forwarded'])
            client.send_file.assert_awaited_once()

    async def test_uncertain_upload_does_not_fall_back_or_invite_retry(self):
        client = SimpleNamespace(send_file=AsyncMock(side_effect=TimeoutError('uncertain')))
        with patch.object(config, 'TELEGRAM_ENABLED', True), patch.object(config, 'WEBHOOK_ASSISTANT', 'assistant'), patch.object(telegram, 'assistant_entities', {'assistant': 'peer'}), patch.object(telegram, 'tg_client', client), patch.object(telegram, 'tg_health', SimpleNamespace(fatal=False)), patch.object(telegram, '_client_lock', return_value=asyncio.Lock()), patch.object(telegram, 'log_capture'), patch.object(telegram, 'request_recheck'), patch.object(native_voice, 'encode_opus', AsyncMock(return_value=b'OggSfixture')), patch.object(telegram, 'deliver', AsyncMock()) as text:
            response = await native_voice.relay_voice(b'audio', {})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(json.loads(response.body)['retry_safe'])
            self.assertFalse(json.loads(response.body)['ok'])
            text.assert_not_awaited()


class NativeVoiceRouteTests(unittest.TestCase):
    def test_native_mode_does_not_transcribe_or_send_text(self):
        rows = []
        with bridge(rows) as b, patch.object(config, 'WEBHOOK_DELIVERY_FORMAT', 'voice', create=True), patch.object(transcribe, 'run', AsyncMock(side_effect=AssertionError('must not transcribe'))):
            response = post(b.client, audio=('clip.m4a', b'fake-audio'))
        self.assertEqual(rows[0].get('delivery_format'), 'voice')
        self.assertNotEqual(rows[0].get('reason'), 'transcription failed')
        b.deliver.assert_not_awaited()

    def test_voice_mode_requires_audio(self):
        rows = []
        with bridge(rows) as b, patch.object(config, 'WEBHOOK_DELIVERY_FORMAT', 'voice', create=True):
            response = post(b.client, transcription='text only')
        self.assertEqual(response.status_code, 422)
        b.deliver.assert_not_awaited()

    def test_test_event_never_uploads_voice(self):
        rows = []
        with bridge(rows) as b, patch.object(config, 'WEBHOOK_DELIVERY_FORMAT', 'voice', create=True):
            response = post(b.client, transcription='test', test='true')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['forwarded'])
        b.deliver.assert_not_awaited()
