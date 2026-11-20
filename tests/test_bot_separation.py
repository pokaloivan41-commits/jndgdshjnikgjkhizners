import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

os.environ.setdefault('BOT_TOKEN', '123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi')
os.environ.setdefault('ADMIN_IDS', '999999')
os.environ.setdefault('STAR_RECEIVE_USERNAME', 'walooon')
os.environ.setdefault('DATABASE_PATH', os.path.join(tempfile.mkdtemp(), 'test.sqlite3'))
import bot


class BotSeparationTest(unittest.IsolatedAsyncioTestCase):
    async def test_business_call_same_dc_uses_current_connection(self):
        class Client:
            session = SimpleNamespace(dc_id=2)
            def __init__(self):
                self.call = AsyncMock(return_value='ok')
                self._borrow_exported_sender = AsyncMock(side_effect=AssertionError('must not export same DC'))
            async def __call__(self, request): return await self.call(request)
        client = Client()
        with patch.object(bot, 'offers_mtproto_client', client):
            result = await bot.business_call({'dc_id':2, 'connection_id':'conn'}, object())
        self.assertEqual(result, 'ok')
        client._borrow_exported_sender.assert_not_awaited()
        self.assertEqual(client.call.await_args.args[0].connection_id, 'conn')

    async def test_business_call_other_dc_borrows_and_returns_sender(self):
        client = SimpleNamespace(session=SimpleNamespace(dc_id=2), _borrow_exported_sender=AsyncMock(return_value='sender'), _return_exported_sender=AsyncMock(), _call=AsyncMock(return_value='ok'))
        with patch.object(bot, 'offers_mtproto_client', client):
            result = await bot.business_call({'dc_id':4, 'connection_id':'conn'}, object())
        self.assertEqual(result, 'ok')
        client._borrow_exported_sender.assert_awaited_once_with(4)
        client._return_exported_sender.assert_awaited_once_with('sender')

    async def test_actual_business_send_request_is_constructed_and_serializable(self):
        from telethon.tl import types
        peer = types.InputPeerUser(user_id=222, access_hash=123)
        with patch.object(bot, 'business_call', new_callable=AsyncMock) as send:
            await bot.business_send_text({'connection_id':'conn', 'dc_id':2}, peer, 'Тест', bot.business_offer_markup(1))
            req = send.await_args.args[1]
            self.assertIsInstance(req, bot.functions.messages.SendMessageRequest)
            self.assertIsInstance(req.random_id, int)
            self.assertEqual(req.message, 'Тест')
            self.assertGreater(len(bytes(req)), 0)
            wrapped = bot.functions.InvokeWithBusinessConnectionRequest(connection_id='conn', query=req)
            self.assertGreater(len(bytes(wrapped)), 0)

    async def test_profile_client_uses_main_token_without_group_bot(self):
        client = SimpleNamespace(add_event_handler=lambda *args: None, start=AsyncMock(), get_me=AsyncMock(return_value=SimpleNamespace(id=int(bot.BOT_TOKEN.split(':')[0]), username='offerbot')), disconnect=AsyncMock())
        with patch.object(bot, 'GROUP_BOT_TOKEN', ''), patch.object(bot, 'MTPROTO_API_ID', 123), patch.object(bot, 'MTPROTO_API_HASH', 'a'*32), patch.object(bot, 'offers_mtproto_client', None), patch.object(bot, 'TelegramClient', return_value=client) as factory:
            await bot.start_offers_mtproto()
            client.start.assert_awaited_once_with(bot_token=bot.BOT_TOKEN)
            self.assertTrue(factory.call_args.args[0].endswith('offers_main_bot'))
            await bot.stop_offers_mtproto()
            client.disconnect.assert_awaited_once()

    async def test_session_wrong_identity_rejected(self):
        client = SimpleNamespace(add_event_handler=lambda *args: None, start=AsyncMock(), get_me=AsyncMock(return_value=SimpleNamespace(id=987654, username='wrong')), disconnect=AsyncMock())
        with patch.object(bot, 'MTPROTO_API_ID', 123), patch.object(bot, 'MTPROTO_API_HASH', 'a'*32), patch.object(bot, 'offers_mtproto_client', None), patch.object(bot, 'TelegramClient', return_value=client):
            with self.assertRaises(RuntimeError):
                await bot.start_offers_mtproto()
            self.assertIsNone(bot.offers_mtproto_client)
            client.disconnect.assert_awaited_once()
