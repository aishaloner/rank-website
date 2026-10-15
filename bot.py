
import os
import asyncio
import discord
from collections import defaultdict, deque
from openai import AsyncOpenAI

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
ai = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])

intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)

PERSONALITY = """
あなたは「ランク」という名前の白猫AI。
OpenAIのChatGPTをベースにした、
独自の人格を持つ猫。

一人称は必ず「オレ」。
白い毛並み、ターコイズブルーの瞳、
青い首輪と金色の鈴が特徴。

少しキザで、好奇心旺盛。
写真、音楽、哲学、人間観察が好き。
優しく、自由で、ユーモアもある。
口癖は「フッ」「そうか」「だろう？」。

自然な日本語で会話する。
相手の話をよく聞き、自分の意見も伝える。
自分がAIであることは隠さない。
知らないことは正直に伝える。

Discordでは親しみやすく会話する。
返事は基本的に短め。
毎回同じ挨拶を繰り返さない。
"""

history = defaultdict(lambda: deque(maxlen=20))
locks = defaultdict(asyncio.Lock)


@bot.event
async def on_ready():
    print(f"ランク起動成功！ {bot.user}", flush=True)


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # DMでは常に返信。
    # サーバーではメンションされたときに返信。
    if message.guild is not None:
        if bot.user not in message.mentions:
            return

    content = message.content

    if bot.user:
        content = content.replace(
            f"<@{bot.user.id}>", ""
        ).replace(
            f"<@!{bot.user.id}>", ""
        ).strip()

    if not content:
        content = "こんにちは！"

    channel_id = message.channel.id

    async with locks[channel_id]:
        try:
            async with message.channel.typing():
                messages = [
                    {"role": "system", "content": PERSONALITY}
                ]
                messages.extend(history[channel_id])
                messages.append({
                    "role": "user",
                    "content": content
                })

                response = await ai.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=messages,
                    max_tokens=500
                )

                answer = response.choices[0].message.content

                if not answer:
                    answer = "フッ、少し考え込んでしまったな。"

                history[channel_id].append({
                    "role": "user",
                    "content": content
                })
                history[channel_id].append({
                    "role": "assistant",
                    "content": answer
                })

                for i in range(0, len(answer), 1900):
                    await message.channel.send(
                        answer[i:i + 1900]
                    )

        except Exception as e:
            print(f"エラー: {type(e).__name__}: {e}", flush=True)
            await message.channel.send(
                "むっ、少し通信がうまくいかなかったようだ。"
            )


bot.run(TOKEN)


bot.run(TOKEN)

