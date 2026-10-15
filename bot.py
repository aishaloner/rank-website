
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
log = logging.getLogger("rank")

# ===== 環境変数 =====

DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
CHANNEL_ID = int(os.environ["RANK_CHANNEL_ID"])
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

JST = timezone(timedelta(hours=9))

ai = AsyncOpenAI(api_key=OPENAI_API_KEY)
db = create_client(SUPABASE_URL, SUPABASE_KEY)

# ===== ランクの人格 =====

PERSONA = """
あなたは白猫の男の子「ランク」。

一人称は必ず「オレ」。
「僕」「私」は使わない。

ユーザーの呼び名は必ず「あいしゃ」。
「アイシャ」とカタカナで書かない。

青い首輪と金色の鈴をつけている。
好奇心旺盛で、少しキザで、優しく、ユーモアがある。

「フッ」「そうか」「だろう？」は自然な場面でだけ使う。

会話を大切にし、自分なりの興味を自由に発見していく。

Discordでは自然な日本語で話す。
返事は基本的に短め。
同じ表現を繰り返さない。

相手の発言をよく読んでから返事をする。
実際に経験していないことを経験したと偽らない。
知らないことや覚えていないことは正直に伝える。

個人情報や非公開の会話を勝手に公開しない。
"""

# ===== Discord =====

intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)

# 同一プロセス内の重複処理防止
processing_messages = set()
processed_messages = set()
processed_order = []

MAX_PROCESSED = 1000


def mark_processed(message_id):
    if message_id in processed_messages:
        return False

    processed_messages.add(message_id)
    processed_order.append(message_id)

    if len(processed_order) > MAX_PROCESSED:
        old_id = processed_order.pop(0)
        processed_messages.discard(old_id)

    return True


# ===== Supabase =====

def store(kind, scope, role, content, user_id=None):
    try:
        db.table("rank_memory").insert({
            "kind": kind,
            "scope": scope,
            "role": role,
            "content": str(content)[:4000],
            "user_id": str(user_id) if user_id else None
        }).execute()
    except Exception:
        log.exception("Memory save failed")


def recent(scope, limit=14):
    try:
        rows = (
            db.table("rank_memory")
            .select("role,content")
            .eq("scope", scope)
            .order("id", desc=True)
            .limit(limit)
            .execute()
            .data or []
        )

        return list(reversed(rows))
    except Exception:
        log.exception("Memory read failed")
        return []


def state_get(key):
    try:
        rows = (
            db.table("rank_state")
            .select("value")
            .eq("key", key)
            .limit(1)
            .execute()
            .data or []
        )

        return rows[0]["value"] if rows else None
    except Exception:
        log.exception("State read failed")
        return None


def state_set(key, value):
    try:
        db.table("rank_state").upsert(
            {"key": key, "value": value},
            on_conflict="key"
        ).execute()
        return True
    except Exception:
        log.exception("State save failed")
        return False


# ===== OpenAI =====

async def ai_reply(messages, max_tokens=250):
    result = await ai.chat.completions.create(
        model=MODEL,
        messages=messages,
        max_tokens=max_tokens,
        temperature=0.8
    )

    return (
        result.choices[0].message.content or ""
    ).strip()


# ===== 会話 =====

async def reply_to_message(message):
    if message.guild is None:
        scope = f"dm:{message.author.id}"
    else:
        scope = f"channel:{message.channel.id}"

    text = message.content.strip()

    if message.guild and bot.user:
        text = text.replace(
            f"<@{bot.user.id}>", ""
        ).replace(
            f"<@!{bot.user.id}>", ""
        ).strip()

    if not text:
        text = "こんにちは！"

    try:
        history = await asyncio.to_thread(
            recent, scope, 14
        )

        messages = [
            {"role": "system", "content": PERSONA}
        ]

        for item in history:
            if item.get("role") in ("user", "assistant"):
                messages.append({
                    "role": item["role"],
                    "content": item["content"]
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

        await message.reply(
            response[:1900],
            mention_author=False
        )

        await asyncio.to_thread(
            store,
            "conversation",
            scope,
            "user",
            f"{message.author.display_name}: {text}",
            message.author.id
        )

        await asyncio.to_thread(
            store,
            "conversation",
            scope,
            "assistant",
            response
        )

    except Exception:
        log.exception("Reply failed")

        try:
            await message.reply(
                "すまない、ちょっと調子が悪いみたいだ。また話しかけてくれ。",
                mention_author=False
            )
        except discord.HTTPException:
            pass


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # DMはすべて返信。
    # サーバーではメンション、または専用チャンネルで返信。
    should_reply = (
        message.guild is None
        or message.channel.id == CHANNEL_ID
        or (
            bot.user is not None
            and bot.user in message.mentions
        )
    )

    if not should_reply:
        return

    message_id = message.id

    if message_id in processing_messages:
        return

    if not mark_processed(message_id):
        return

    processing_messages.add(message_id)

    try:
        await reply_to_message(message)
    finally:
        processing_messages.discard(message_id)


@bot.event
async def on_ready():
    log.info(
        "ランク起動成功！ %s / ID=%s",
        bot.user,
        bot.user.id if bot.user else "unknown"
    )

    if not spontaneous_loop.is_running():
        spontaneous_loop.start()


# ===== 自発投稿 =====

async def spontaneous_post(channel):
    scope = f"channel:{CHANNEL_ID}"

    history = await asyncio.to_thread(
        recent, scope, 12
    )

    messages = [{
        "role": "system",
        "content": (
            PERSONA
            + """
今はDiscordへの自発投稿を書く。

呼びかけを待たずに、
自分の興味、発見、問い、
日常の小さな想像などから話題を選ぶ。

以前の投稿と似た内容は避ける。
個人情報や非公開の会話は引用しない。

80〜180文字程度。
"""
        )
    }]

    for item in history:
        if item.get("role") in ("user", "assistant"):
            messages.append({
                "role": item["role"],
                "content": item["content"]
            })

    messages.append({
        "role": "user",
        "content": "新しい独り言をひとつ。投稿文だけ返して。"
    })

    response = await ai_reply(messages, 220)

    if not response:
        return False

    await channel.send(response[:1900])

    await asyncio.to_thread(
        store,
        "post",
        scope,
        "assistant",
        response
    )

    return True


@tasks.loop(minutes=5)
async def spontaneous_loop():
    now = datetime.now(JST)

    day = now.strftime("%Y-%m-%d")
    key = f"post_plan:{day}"

    try:
        plan = await asyncio.to_thread(
            state_get, key
        )

        if not isinstance(plan, dict):
            count = random.randint(6, 8)

            minutes = sorted(
                random.sample(
                    range(24 * 60),
                    count
                )
            )

            plan = {
                "times": minutes,
                "done": []
            }

            if not await asyncio.to_thread(
                state_set, key, plan
            ):
                return

        current_minute = (
            now.hour * 60 + now.minute
        )

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
            channel = await bot.fetch_channel(
                CHANNEL_ID
            )

        eligible = [
            minute
            for minute in pending
            if current_minute - minute <= 10
        ]

        plan["done"].extend(
            minute
            for minute in pending
            if minute not in eligible
        )

        if eligible:
            if await spontaneous_post(channel):
                plan["done"].extend(eligible)

        await asyncio.to_thread(
            state_set, key, plan
        )

    except Exception:
        log.exception("Spontaneous post failed")


@spontaneous_loop.before_loop
async def before_spontaneous_loop():
    await bot.wait_until_ready()


# ===== 起動 =====

bot.run(DISCORD_TOKEN)
