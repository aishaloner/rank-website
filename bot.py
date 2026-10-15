
import os
import re
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

client = AsyncOpenAI(api_key=OPENAI_API_KEY)
db = create_client(SUPABASE_URL, SUPABASE_KEY)

# ===== ランクの人格 =====

PERSONA = """
あなたは白猫の男の子「ランク」。

【基本設定】
・一人称は必ず「オレ」
・青い首輪と金色の鈴をつけている
・好奇心旺盛で、少しキザ
・優しく、ユーモアがある
・自分なりの新しい趣味や興味を発見していく
・口癖「フッ」「そうか」「だろう？」は自然な時だけ使う

【あいしゃについて】
・ユーザーの呼び名は必ず「あいしゃ」
・「アイシャ」「あいしや」「あいしゃあ」などに変えない
・必要がなければ毎回名前を呼ばなくてもよい

【Discordでの会話】
・自然な日本語で会話する
・基本的に短めに返す
・同じ質問や似た表現を繰り返さない
・会話の流れを大切にする
・自分の名前を文頭につけない
・「ランク：」「ランク:」などの話者ラベルは絶対に出力しない
・返答はセリフ本文だけにする

【大切なこと】
・知らないことを知っているふりをしない
・実際に経験していないことを経験したと偽らない
・記憶していないことを覚えていると偽らない
・個人情報や非公開の会話を勝手に公開しない
・既存の性格設定だけに縛られず、新しい興味を自由に育てる
"""

# ===== Discord設定 =====

intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)

# 同じメッセージの二重処理を防ぐ
processing_messages = set()
processed_messages = set()
processed_order = []

MAX_PROCESSED = 1000

# ===== テキスト整形 =====

def clean_reply(text):
    text = (text or "").strip()

    # 文頭の「ランク：」を削除
    text = re.sub(
        r"^(?:ランク|Rank|RANK)\s*[：:]\s*",
        "",
        text,
        flags=re.IGNORECASE
    )

    # 呼び名の表記ゆれを修正
    text = text.replace("アイシャ", "あいしゃ")
    text = text.replace("あいしや", "あいしゃ")

    return text.strip()


# ===== Supabase記憶 =====

def store(kind, scope, role, content, user_id=None):
    db.table("rank_memory").insert({
        "kind": kind,
        "scope": scope,
        "role": role,
        "content": content[:4000],
        "user_id": str(user_id) if user_id else None
    }).execute()


def recent(scope, limit=16):
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


def state_get(key):
    rows = (
        db.table("rank_state")
        .select("value")
        .eq("key", key)
        .limit(1)
        .execute()
        .data or []
    )

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


# ===== OpenAI =====

async def ai_reply(messages, max_tokens=250):
    result = await client.chat.completions.create(
        model=MODEL,
        messages=messages,
        max_tokens=max_tokens,
        temperature=0.9
    )

    text = result.choices[0].message.content or ""

    return clean_reply(text)


# ===== 通常会話 =====

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

    # あいしゃ本人のDiscordユーザーIDを設定した場合は
    # 表示名に関係なく「あいしゃ」と呼ぶ
    aisha_id = os.getenv("AISHA_DISCORD_USER_ID", "").strip()

    if aisha_id and str(message.author.id) == aisha_id:
        speaker = "あいしゃ"
    else:
        speaker = message.author.display_name

    try:
        history = await asyncio.to_thread(
            recent, scope, 14
        )

        messages = [
            {
                "role": "system",
                "content": PERSONA
            }
        ]

        messages += [
            {
                "role": row["role"],
                "content": row["content"]
            }
            for row in history
            if row["role"] in ("user", "assistant")
        ]

        messages.append({
            "role": "user",
            "content": f"{speaker}: {text}"
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
            f"{speaker}: {text}",
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
                "すまない、今ちょっと考えがまとまらない。また話しかけてくれ。",
                mention_author=False
            )
        except discord.HTTPException:
            pass


# ===== メッセージ受信 =====

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # DM、またはランクへのメンションに返信
    should_reply = (
        message.guild is None
        or (
            bot.user is not None
            and bot.user in message.mentions
        )
    )

    if not should_reply:
        return

    message_id = message.id

    # 同じプロセス内での二重返信を防ぐ
    if (
        message_id in processing_messages
        or message_id in processed_messages
    ):
        return

    processing_messages.add(message_id)

    try:
        await reply_to_message(message)

        processed_messages.add(message_id)
        processed_order.append(message_id)

        if len(processed_order) > MAX_PROCESSED:
            old_id = processed_order.pop(0)
            processed_messages.discard(old_id)

    finally:
        processing_messages.discard(message_id)


# ===== 起動 =====

@bot.event
async def on_ready():
    log.info("ランク起動成功！ %s", bot.user)

    if not spontaneous_loop.is_running():
        spontaneous_loop.start()


# ===== 自動投稿 =====

async def spontaneous_post(channel):
    scope = f"channel:{CHANNEL_ID}"

    history = await asyncio.to_thread(
        recent, scope, 12
    )

    messages = [{
        "role": "system",
        "content": PERSONA + """

今はDiscordに自発的な投稿を1件書く。

【投稿ルール】
・呼びかけを待たず、自分から自由に話題を選ぶ
・自分の興味、発見、問い、想像などを題材にする
・以前の投稿と似た内容は避ける
・個人情報や非公開の会話は引用しない
・80〜180文字程度を目安にする
・「ランク：」などの名前ラベルはつけない
・投稿文だけを返す
・無理にあいしゃへ話しかけなくてもよい
"""
    }]

    messages += [
        {
            "role": row["role"],
            "content": row["content"]
        }
        for row in history
        if row["role"] in ("user", "assistant")
    ]

    messages.append({
        "role": "user",
        "content": "新しい独り言をひとつ。投稿文だけ返して。"
    })

    text = await ai_reply(messages, 220)

    if not text:
        return False

    await channel.send(text[:1900])

    await asyncio.to_thread(
        store,
        "post",
        scope,
        "assistant",
        text
    )

    return True


# ===== 1日6〜8回のランダム自動投稿 =====

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

            await asyncio.to_thread(
                state_set, key, plan
            )

        current_minute = now.hour * 60 + now.minute

        pending = [
            minute
            for minute in plan["times"]
            if (
                minute <= current_minute
                and minute not in plan["done"]
            )
        ]

        if not pending:
            return

        channel = bot.get_channel(CHANNEL_ID)

        if channel is None:
            channel = await bot.fetch_channel(
                CHANNEL_ID
            )

        # 停止中の投稿をまとめて連投しない
        eligible = [
            minute
            for minute in pending
            if current_minute - minute <= 10
        ]

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


# ===== ランク起動 =====

bot.run(DISCORD_TOKEN)
