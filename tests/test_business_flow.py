import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault('BOT_TOKEN', '123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi')
os.environ.setdefault('ADMIN_IDS', '999999')
os.environ.setdefault('STAR_RECEIVE_USERNAME', 'walooon')
os.environ.setdefault('DATABASE_PATH', os.path.join(tempfile.mkdtemp(), 'test.sqlite3'))
import aiosqlite
import bot


class BusinessFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = bot.DB_PATH
        bot.DB_PATH = os.path.join(self.tmp.name, 'test.sqlite3')
        await bot.init_db()
        async with aiosqlite.connect(bot.DB_PATH) as db:
            await db.execute("INSERT INTO users(user_id,username,balance,created_at) VALUES(111,'buyer',500,?)", (bot.now_local().isoformat(),))
            await db.execute("INSERT INTO business_connections VALUES('conn',111,2,1,1,?)", (bot.now_local().isoformat(),))
            await db.commit()
        self.patch = {}
        for name in ('fetch_nft_metadata', 'business_send_text', 'business_edit_text', 'business_call', 'answer_business_callback', 'admin_notify', 'offers_mtproto_client'):
            self.patch[name] = getattr(bot, name)
        self.old_get_me, self.old_send = bot.bot.get_me, bot.bot.send_message
        bot.fetch_nft_metadata = AsyncMock(return_value={'title':'Example','number':'123'})
        bot.business_send_text = AsyncMock(return_value=SimpleNamespace(updates=[bot.tl_types.UpdateMessageID(id=777, random_id=123)]))
        bot.business_edit_text = AsyncMock()
        bot.business_call = AsyncMock()
        bot.answer_business_callback = AsyncMock()
        bot.admin_notify = AsyncMock()
        bot.offers_mtproto_client = SimpleNamespace(get_input_entity=AsyncMock(return_value='peer'), get_entity=AsyncMock(return_value=SimpleNamespace(username='seller')))
        bot.bot.get_me = AsyncMock(return_value=SimpleNamespace(username='balancebot'))
        bot.bot.send_message = AsyncMock()

    async def asyncTearDown(self):
        for name, value in self.patch.items(): setattr(bot, name, value)
        bot.bot.get_me, bot.bot.send_message = self.old_get_me, self.old_send
        bot.DB_PATH = self.old_db
        self.tmp.cleanup()

    async def create(self):
        message = SimpleNamespace(peer_id=bot.tl_types.PeerUser(222), from_id=bot.tl_types.PeerUser(111), message='/offer https://t.me/nft/Example-123 250', id=555)
        event = SimpleNamespace(connection_id='conn', message=message)
        await bot.handle_business_message_event(event)
        return event

    def callback(self, user=222, action='accept'):
        return SimpleNamespace(connection_id='conn', message=SimpleNamespace(peer_id=bot.tl_types.PeerUser(222), id=777), data=f'{action}:1'.encode(), user_id=user, query_id=1)

    async def test_compact_card_has_gift_name_number_price_and_link(self):
        await self.create()
        args = bot.business_send_text.await_args
        text = args.args[2]
        self.assertIn('Example #123', text)
        self.assertIn('250 ⭐-единиц', text)
        self.assertIn('с момента отправки', text)
        self.assertNotIn('Сделка №', text)
        entities = args.kwargs['entities']
        self.assertTrue(any(getattr(entity, 'url', '') == 'https://t.me/nft/Example-123' for entity in entities))

    async def test_command_deleted_after_publication(self):
        await self.create()
        request = bot.business_call.await_args.args[1]
        self.assertIsInstance(request, bot.functions.messages.DeleteMessagesRequest)
        self.assertEqual(request.id, [555])
        self.assertTrue(request.revoke)
        self.assertEqual((await bot.row_one('SELECT status FROM offers'))['status'], 'open')
        markup = bot.business_send_text.await_args.args[3]
        self.assertEqual([b.text for b in markup.rows[0].buttons], ['⊗ Отклонить', '✓ Принять'])
        self.assertTrue(all(b.style is None for b in markup.rows[0].buttons))
        self.assertTrue(bot.business_send_text.await_args.kwargs['entities'])

    async def test_delete_failure_keeps_published_offer_open(self):
        bot.business_call.side_effect = RuntimeError('no permission')
        await self.create()
        self.assertEqual((await bot.row_one('SELECT status FROM offers'))['status'], 'open')
        self.assertIn('не удалось удалить', bot.bot.send_message.await_args.args[1])

    async def test_diagnostics_do_not_create_a_deal(self):
        message = SimpleNamespace(peer_id=bot.tl_types.PeerUser(222), from_id=bot.tl_types.PeerUser(111), message='/testbuttons', id=555)
        await bot.handle_business_message_event(SimpleNamespace(connection_id='conn', message=message))
        self.assertEqual((await bot.row_one('SELECT count(*) AS n FROM offers'))['n'], 0)
        self.assertEqual(bot.business_send_text.await_count, 1)
        self.assertEqual(bot.bot.send_message.await_count, 1)
        self.assertEqual(bot.bot.send_message.await_args.kwargs['business_connection_id'], 'conn')

    async def test_missing_reply_rights_are_refreshed(self):
        async with aiosqlite.connect(bot.DB_PATH) as db:
            await db.execute("UPDATE business_connections SET can_reply=0 WHERE connection_id='conn'")
            await db.commit()
        real_get = bot.get_business_connection
        calls = []
        async def refresh(connection_id, refresh=False):
            calls.append(refresh)
            if refresh:
                async with aiosqlite.connect(bot.DB_PATH) as db:
                    await db.execute("UPDATE business_connections SET can_reply=1 WHERE connection_id=?", (connection_id,))
                    await db.commit()
            return await real_get(connection_id)
        from unittest.mock import patch
        with patch.object(bot, 'get_business_connection', side_effect=refresh):
            await self.create()
        self.assertEqual(calls, [False, True])
        self.assertEqual((await bot.row_one('SELECT count(*) AS n FROM offers'))['n'], 1)

    async def test_create_accept_once_and_status_card(self):
        event = await self.create()
        await bot.handle_business_message_event(event)
        self.assertEqual((await bot.row_one('SELECT count(*) AS n FROM offers'))['n'], 1)
        self.assertEqual((await bot.row_one('SELECT business_message_id FROM offers'))['business_message_id'],777)
        await asyncio.gather(bot.handle_business_callback_event(self.callback()), bot.handle_business_callback_event(self.callback()))
        buyer = await bot.row_one('SELECT balance,reserved FROM users WHERE user_id=111')
        self.assertEqual((buyer['balance'], buyer['reserved']), (250,250))
        self.assertEqual((await bot.row_one("SELECT count(*) AS n FROM balance_ledger WHERE event='offer_reserve'"))['n'],1)
        self.assertEqual(bot.business_edit_text.call_count,1)
        self.assertIsNotNone(bot.business_edit_text.call_args.kwargs['reply_markup'])

    async def test_confirm_transfer_in_chat_once(self):
        await self.create()
        await bot.handle_business_callback_event(self.callback())
        markup = bot.business_edit_text.call_args.kwargs['reply_markup']
        self.assertEqual(markup.rows[0].buttons[0].type.data, b'confirm:1')
        bot.admin_notify.reset_mock()
        await asyncio.gather(bot.handle_business_callback_event(self.callback(action='confirm')), bot.handle_business_callback_event(self.callback(action='confirm')))
        offer = await bot.row_one('SELECT status,proof_url FROM offers WHERE id=1')
        self.assertEqual(offer['status'], 'review_pending')
        self.assertIsNone(offer['proof_url'])
        self.assertEqual(bot.admin_notify.await_count, 1)
        self.assertIn('не предоставлялась', bot.admin_notify.call_args.args[0])
        buyer = await bot.row_one('SELECT balance,reserved FROM users WHERE user_id=111')
        self.assertEqual((buyer['balance'],buyer['reserved']), (250,250))

    async def test_other_user_cannot_confirm(self):
        await self.create()
        await bot.handle_business_callback_event(self.callback())
        await bot.handle_business_callback_event(self.callback(user=333, action='confirm'))
        self.assertEqual((await bot.row_one('SELECT status FROM offers'))['status'], 'awaiting_gift')

    async def test_admin_credit_limit_and_no_overflow(self):
        msg = SimpleNamespace(from_user=SimpleNamespace(id=999999), text='/credit 111 2000000', answer=AsyncMock())
        await bot.admin_credit(msg)
        self.assertEqual((await bot.row_one('SELECT balance FROM users WHERE user_id=111'))['balance'], 2000500)
        bad = SimpleNamespace(from_user=SimpleNamespace(id=999999), text=f'/credit 111 {bot.MAX_SQLITE_INTEGER}', answer=AsyncMock())
        await bot.admin_credit(bad)
        self.assertEqual((await bot.row_one('SELECT balance FROM users WHERE user_id=111'))['balance'], 2000500)
        self.assertIn('предел', bad.answer.await_args.args[0])
        unauthorized = SimpleNamespace(from_user=SimpleNamespace(id=333), text='/credit 111 1000', answer=AsyncMock())
        await bot.admin_credit(unauthorized)
        self.assertEqual((await bot.row_one('SELECT balance FROM users WHERE user_id=111'))['balance'], 2000500)

    async def test_wrong_user_rejected(self):
        await self.create()
        await bot.handle_business_callback_event(self.callback(user=333))
        self.assertEqual((await bot.row_one('SELECT status FROM offers'))['status'], 'open')

    async def test_edit_failure_falls_back_and_notifies(self):
        await self.create()
        bot.business_edit_text.side_effect = RuntimeError('temporary failure')
        await bot.handle_business_callback_event(self.callback())
        self.assertEqual((await bot.row_one('SELECT status FROM offers'))['status'], 'awaiting_gift')
        self.assertEqual(bot.business_send_text.call_count, 2)
        bot.bot.send_message.assert_awaited()
        bot.admin_notify.assert_awaited()

    async def test_reject_does_not_reserve(self):
        await self.create()
        await bot.handle_business_callback_event(self.callback(action='reject'))
        self.assertEqual((await bot.row_one('SELECT status FROM offers'))['status'], 'rejected')
        buyer = await bot.row_one('SELECT balance,reserved FROM users WHERE user_id=111')
        self.assertEqual((buyer['balance'], buyer['reserved']), (500,0))

    async def test_legacy_callbacks_cannot_handle_profile_offer(self):
        await self.create()
        for action, handler in [('accept', bot.accept_offer), ('reject', bot.reject_offer)]:
            callback = SimpleNamespace(data=f'{action}:1', from_user=SimpleNamespace(id=333, username='other'), answer=AsyncMock())
            await handler(callback)
            self.assertEqual((await bot.row_one('SELECT status FROM offers'))['status'], 'open')
            self.assertIn('исходном личном чате', callback.answer.call_args.args[0])

    async def test_insufficient_balance(self):
        await self.create()
        async with aiosqlite.connect(bot.DB_PATH) as db:
            await db.execute('UPDATE users SET balance=0 WHERE user_id=111'); await db.commit()
        await bot.handle_business_callback_event(self.callback())
        self.assertEqual((await bot.row_one('SELECT status FROM offers'))['status'],'open')
        self.assertEqual((await bot.row_one('SELECT reserved FROM users WHERE user_id=111'))['reserved'],0)

if __name__ == '__main__': unittest.main()
