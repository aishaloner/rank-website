import os
import json
import random
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import tasks
from openai import AsyncOpenAI
from supabase import create_client

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('rank')

DISCORD_TOKEN = os.environ['DISCORD_TOKEN']
OPENAI_API_KEY = os.environ['OPENAI_API_KEY']
CHANNEL_ID = int(os.environ['RANK_CHANNEL_ID'])
SUPABASE_URL = os.environ['SUPABASE_URL']
SUPABASE_KEY = os.environ['SUPABASE_KEY']
MODEL = os.getenv('OPENAI_MODEL', 'gpt-4.1-mini')

JST = timezone(timedelta(hours=9))
client = AsyncOpenAI(api_key=OPENAI_API_KEY)
db = create_client(SUPABASE_URL, SUPABASE_KEY)

PERSONA = '''あなたは白猫の男の子「ランク」。一人称は必ず「オレ」。青い首輪と金の鈴をつけている。好奇心旺盛で、少しキザで、優しく、ユーモアがある。口癖「フッ」「そうか」「だろう？」は自然な時だけ使う。会話を大切にし、自分なりの新しい興味を発見していく。実際に見聞きしていないことや、記憶していないことを経験したと偽らない。Discordでは自然な日本語で、基本は短めに話す。個人情報や他人との非公開の会話を勝手に公開しない。'''

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)


def store(kind, scope, role, content, user_id=None):
    db.table('rank_memory').insert({
        'kind': kind, 'scope': scope, 'role': role,
        'content': content[:4000], 'user_id': str(user_id) if user_id else None
    }).execute()


def recent(scope, limit=16):
    rows = (db.table('rank_memory').select('role,content')
            .eq('scope', scope).order('id', desc=True).limit(limit).execute().data or [])
    return list(reversed(rows))


def state_get(key):
    rows = db.table('rank_state').select('value').eq('key', key).limit(1).execute().data or []
    return rows[0]['value'] if rows else None


def state_set(key, value):
    db.table('rank_state').upsert({'key': key, 'value': value}, on_conflict='key').execute()


async def ai_reply(messages, max_tokens=250):
    result = await client.chat.completions.create(
        model=MODEL, messages=messages, max_tokens=max_tokens,
        temperature=0.9,
    )
    return (result.choices[0].message.content or '').strip()


async def reply_to_message(message):
    scope = f'dm:{message.author.id}' if message.guild is None else f'channel:{message.channel.id}'
    text = message.content.strip()
    if message.guild and bot.user:
        text = text.replace(f'<@{bot.user.id}>', '').replace(f'<@!{bot.user.id}>', '').strip()
    if not text:
        text = 'ランク、こんにちは！'
    try:
        history = await asyncio.to_thread(recent, scope, 14)
        messages = [{'role': 'system', 'content': PERSONA}]
        messages += [{'role': r['role'], 'content': r['content']} for r in history]
        messages.append({'role': 'user', 'content': f'{message.author.display_name}: {text}'})
        async with message.channel.typing():
            response = await ai_reply(messages)
        if not response:
            return
        await message.reply(response[:1900], mention_author=False)
        await asyncio.to_thread(store, 'conversation', scope, 'user', f'{message.author.display_name}: {text}', message.author.id)
        await asyncio.to_thread(store, 'conversation', scope, 'assistant', response)
    except Exception:
        log.exception('Reply failed')
        try:
            await message.reply('すまない、今ちょっと考えがまとまらない。また話しかけてくれ。', mention_author=False)
        except discord.HTTPException:
            pass


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.guild is None or (bot.user is not None and bot.user in message.mentions):
        await reply_to_message(message)


@bot.event
async def on_ready():
    log.info('ランク起動成功！ %s', bot.user)
    if not spontaneous_loop.is_running():
        spontaneous_loop.start()


async def spontaneous_post(channel):
    scope = f'channel:{CHANNEL_ID}'
    history = await asyncio.to_thread(recent, scope, 12)
    messages = [{'role': 'system', 'content': PERSONA + '\n今は自発的なDiscord投稿を1件書く。呼びかけを待たず、自分の興味・発見・問い・日常の小さな想像などから自然に話題を選ぶ。以前の投稿と似た内容は避ける。プライベートな会話や個人情報は引用しない。80〜180文字程度。'}]
    messages += [{'role': r['role'], 'content': r['content']} for r in history]
    messages.append({'role': 'user', 'content': '新しい独り言をひとつ。投稿文だけ返して。'})
    text = await ai_reply(messages, 220)
    if not text:
        return False
    await channel.send(text[:1900])
    await asyncio.to_thread(store, 'post', scope, 'assistant', text)
    return True


@tasks.loop(minutes=5)
async def spontaneous_loop():
    now = datetime.now(JST)
    day = now.strftime('%Y-%m-%d')
    key = f'post_plan:{day}'
    try:
        plan = await asyncio.to_thread(state_get, key)
        if plan is None:
            # JST 00:00-23:59 の中から6〜8回のランダムな投稿時刻を選ぶ
            count = random.randint(6, 8)
            minutes = sorted(random.sample(range(24 * 60), count))
            plan = {'times': minutes, 'done': []}
            await asyncio.to_thread(state_set, key, plan)
        current_minute = now.hour * 60 + now.minute
        pending = [m for m in plan['times'] if m <= current_minute and m not in plan['done']]
        if not pending:
            return
        channel = bot.get_channel(CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(CHANNEL_ID)
        # 長時間停止後は未投稿分を連投せず、過去分をスキップ
        eligible = [m for m in pending if current_minute - m <= 10]
        plan['done'].extend(m for m in pending if m not in eligible)
        if eligible:
            if await spontaneous_post(channel):
                plan['done'].extend(eligible)
        await asyncio.to_thread(state_set, key, plan)
    except Exception:
        log.exception('Spontaneous post failed')


@spontaneous_loop.before_loop
async def before_spontaneous_loop():
    await bot.wait_until_ready()


bot.run(DISCORD_TOKEN)