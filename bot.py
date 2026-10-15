
import os
import json
import random
import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import tasks
from openai import AsyncOpenAI
from supabase import create_client

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rank")

DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
CHANNEL_ID = int(os.environ["RANK_CHANNEL_ID"])
SUPABASE_URL = os.environ["SUPABASE_URL"].strip()
SUPABASE_KEY = os.environ["SUPABASE_KEY"].strip()
MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

JST = timezone(timedelta(hours=9))

client = AsyncOpenAI(api_key=OPENAI_API_KEY)
db = create_client(SUPABASE_URL, SUPABASE_KEY)

PERSONA = """
あなたは白猫の男の子「ランク」。
一人称は必ず「オレ」。
ユーザーの名前は「あいしゃ」。必ずひらがなで呼ぶ。
「アイシャ」「Aisha」と表記しない。

青い首輪と金色の鈴をつけた白猫。
好奇心旺盛で、少しキザで、優しく、ユーモアがある。
口癖の「フッ」「そうか」「だろう？」は自然な場面だけ使う。

自分の興味や考えを自由に広げる。
会話の内容を大切にし、過去の記憶を自然に活用する。
記憶していないことを知っているふりはしない。

Discordでは自然な日本語で話す。
返答は基本的に短め。
同じ質問を繰り返さず、文脈に沿って返す。
自分の名前を毎回名乗らない。
意味のない謝罪や定型文を繰り返さない。

個人情報や非公開の会話を勝手に公開しない。
"""

intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)

# 同じメッセージの重複処理を防止
processed_ids = set()
processed_order = deque(maxlen=1000)

# 同一チャンネル内の返信順序を保つ
channel_locks = {}


def remember_processed(message_id):
    if message_id in processed_ids:
        return False

    if len(processed_order) == processed_order.maxlen:
        oldest = processed_order.popleft()
        processed_ids.discard(oldest)

    processed_order.append(message_id)
    processed_ids.add(message_id)
    return True


def get_lock(scope):
    if scope not in channel_locks:
        channel_locks[scope] = asyncio.Lock()
    return channel_locks[scope]


def store(kind, scope, role, content, user_id=None):
    db.table("rank_memory").insert({
        "kind": kind,
        "scope": scope,
        "role": role,
        "content": str(content)[:4000],
        "user_id": str(user_id) if user_id else None
    }).execute()


def recent(scope, limit=14):
    result = (
        db.table("rank_memory")
        .select("role,content")
        .eq("scope", scope)
        .order("id", desc=True)
        .limit(limit)
        .execute()
    )

    return list(reversed(result.data or []))


def state_get(key):
    result = (
        db.table("rank_state")
        .select("value")
        .eq("key", key)
        .limit(1)
        .execute()
    )

    rows = result.data or []
    if not rows:
        return None

    value = rows[0]["value"]

    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    return value


def state_set(key, value):
    db.table("rank_state").upsert(
        {
            "key": key,
            "value": value
        },
        on_conflict="key"
    ).execute()


async def safe_store(kind, scope, role, content, user_id=None):
    try:
        await asyncio.to_thread(
            store, kind, scope, role, content, user_id
        )
    except Exception:
        log.exception("Memory save failed")


async def safe_recent(scope, limit=14):
    try:
        return await asyncio.to_thread(recent, scope, limit)
    except Exception:
        log.exception("Memory read failed")
        return []


async def ai_reply(messages, max_tokens=250):
    result = await client.chat.completions.create(
        model=MODEL,
        messages=messages,
        max_tokens=max_tokens,
        temperature=0.9
    )

    return (result.choices[0].message.content or "").strip()


async def reply_to_message(message):
    scope = (
        f"dm:{message.author.id}"
        if message.guild is None
        else f"channel:{message.channel.id}"
    )

    async with get_lock(scope):
        text = message.content.strip()

        if message.guild and bot.user:
            text = text.replace(
                f"<@{bot.user.id}>", ""
            ).replace(
                f"<@!{bot.user.id}>", ""
            ).strip()

        if not text:
            text = "ランク、こんにちは！"

        try:
            history = await safe_recent(scope, 14)

            messages = [
                {"role": "system", "content": PERSONA}
            ]

            for row in history:
                role = row.get("role")
                content = row.get("content")

                if role in ("user", "assistant") and content:
                    messages.append({
                        "role": role,
                        "content": str(content)
                    })

            messages.append({
                "role": "user",
                "content": (
                    f"{message.author.display_name}: {text}"
                )
            })

            async with message.channel.typing():
                response = await ai_reply(messages)

            if not response:
                return

            # Discordへの返信は1回だけ
            await message.reply(
                response[:1900],
                mention_author=False
            )

        except Exception:
            log.exception("Reply generation failed")
            return

        # 保存エラーが起きても追加返信しない
        await safe_store(
            "conversation",
            scope,
            "user",
            f"{message.author.display_name}: {text}",
            message.author.id
        )

        await safe_store(
            "conversation",
            scope,
            "assistant",
            response
        )


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    should_reply = (
        message.guild is None
        or (
            bot.user is not None
            and bot.user in message.mentions
        )
    )

    if not should_reply:
        return

    if not remember_processed(message.id):
        return

    await reply_to_message(message)


@bot.event
async def on_ready():
    log.info("ランク起動成功！ %s", bot.user)

    if not spontaneous_loop.is_running():
        spontaneous_loop.start()


async def spontaneous_post(channel):
    scope = f"channel:{CHANNEL_ID}"

    history = await safe_recent(scope, 12)

    prompt = PERSONA + """
今はDiscordに自発的な投稿を1件書く。

誰かに呼ばれるのを待たずに、
自分の興味、発見、疑問、想像、
小さな出来事などから自由に話題を選ぶ。

毎回同じような話題を選ばない。
直前の会話をそのまま繰り返さない。
個人情報や非公開の会話は公開しない。

80〜180文字程度。
投稿する文章だけを返す。
"""

    messages = [
        {"role": "system", "content": prompt}
    ]

    for row in history:
        role = row.get("role")
        content = row.get("content")

        if role in ("user", "assistant") and content:
            messages.append({
                "role": role,
                "content": str(content)
            })

    messages.append({
        "role": "user",
        "content": "新しい独り言をひとつ書いて。"
    })

    text = await ai_reply(messages, 220)

    if not text:
        return False

    await channel.send(text[:1900])

    await safe_store(
        "post",
        scope,
        "assistant",
        text
    )

    return True


@tasks.loop(minutes=5)
async def spontaneous_loop():
    now = datetime.now(JST)
    day = now.strftime("%Y-%m-%d")
    key = f"post_plan:{day}"

    try:
        try:
            plan = await asyncio.to_thread(state_get, key)
        except Exception:
            log.exception("Post plan read failed")
            return

        if plan is None:
            count = random.randint(6, 8)

            minutes = sorted(
                random.sample(range(24 * 60), count)
            )

            plan = {
                "times": minutes,
                "done": []
            }

            try:
                await asyncio.to_thread(
                    state_set, key, plan
                )
            except Exception:
                log.exception("Post plan save failed")
                return

        current_minute = now.hour * 60 + now.minute

        pending = [
            minute
            for minute in plan["times"]
            if minute <= current_minute
            and minute not in plan["done"]
        ]

        if not pending:
            return

        channel = bot.get_channel(CHANNEL_ID)

        if channel is None:
            channel = await bot.fetch_channel(CHANNEL_ID)

        eligible = [
            minute
            for minute in pending
            if current_minute - minute <= 10
        ]

        # 停止中の古い投稿はまとめてスキップ
        skipped = [
            minute
            for minute in pending
            if minute not in eligible
        ]

        plan["done"].extend(skipped)

        if eligible:
            success = await spontaneous_post(channel)

            if success:
                plan["done"].extend(eligible)

        await asyncio.to_thread(
            state_set, key, plan
        )

    except Exception:
        log.exception("Spontaneous post failed")


@spontaneous_loop.before_loop
async def before_spontaneous_loop():
    await bot.wait_until_ready()


bot.run(DISCORD_TOKEN)
