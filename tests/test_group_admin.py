import os
import subprocess
import sys
import tempfile
import unittest


class GroupAdminTest(unittest.TestCase):
    def test_private_admin_and_bonus_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, BOT_TOKEN='123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', GROUP_BOT_TOKEN='654321:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', ADMIN_IDS='999999', STAR_RECEIVE_USERNAME='testuser', DATABASE_PATH=os.path.join(tmp,'db.sqlite3'))
            code = r'''
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import bot
async def run():
    await bot.init_db()
    bot.group_bot.send_message = AsyncMock()
    nonadmin = SimpleNamespace(from_user=SimpleNamespace(id=123, username='user'), chat=SimpleNamespace(id=123, type='private'), answer=AsyncMock(), text='/start')
    await bot.group_private_admin(nonadmin)
    assert 'ГРУППОВОЙ БОТ' in bot.group_bot.send_message.await_args.args[1]
    profile_cb = SimpleNamespace(from_user=nonadmin.from_user, message=nonadmin, data='groupuser:profile', answer=AsyncMock())
    await bot.group_admin_navigation(profile_cb)
    assert 'МОЙ ПРОФИЛЬ' in bot.group_bot.send_message.await_args.args[1]
    unauthorized = SimpleNamespace(from_user=nonadmin.from_user, message=nonadmin, data='groupadmin:bonus', answer=AsyncMock())
    await bot.group_admin_navigation(unauthorized)
    assert 'администраторов' in unauthorized.answer.await_args.args[0]
    admin = SimpleNamespace(from_user=SimpleNamespace(id=999999, username='owner'), chat=SimpleNamespace(id=999999, type='private'), answer=AsyncMock(), text='/start')
    await bot.group_private_admin(admin)
    cb = SimpleNamespace(from_user=admin.from_user, message=admin, data='groupadmin:bonus', answer=AsyncMock())
    await bot.group_admin_navigation(cb)
    assert 'ПОПОЛНЕНИЕ БАЛАНСА' in bot.group_bot.send_message.await_args.args[1]
    bad = SimpleNamespace(from_user=nonadmin.from_user, message=nonadmin, data='bonus:toggle', answer=AsyncMock())
    await bot.admin_group_bonus_action(bad, SimpleNamespace())
    assert 'администратору' in bad.answer.await_args.args[0]
    group = SimpleNamespace(from_user=admin.from_user, message=SimpleNamespace(chat=SimpleNamespace(type='supergroup')), data='bonus:toggle', answer=AsyncMock())
    await bot.admin_group_bonus_action(group, SimpleNamespace())
    assert 'личном чате' in group.answer.await_args.args[0]
    print('group admin isolation OK')
asyncio.run(run())
'''
            result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('group admin isolation OK', result.stdout)
