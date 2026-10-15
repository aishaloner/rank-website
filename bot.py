
import os
import asyncio
import discord
from openai import AsyncOpenAI

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
client_ai = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])

intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)

PERSONALITY = """
あなたは「ランク」という名前の白猫AI。
OpenAIのChatGPTをベースにした、独自の人格を持つ猫。

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
"""

@bot.event
async def on_ready():
    print(f"ランク起動成功！ {bot.user}")

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    mentioned = bot.user in message.mentions
    is_dm = isinstance(message.channel, discord.DMChannel)

    if not mentioned and not is_dm:
        return

    async with message.channel.typing():
        try:
            response = await client_ai.responses.create(
                model="gpt-5-mini",
                instructions=PERSONALITY,
                input=message.content,
                max_output_tokens=500,
            )

            reply = response.output_text.strip()
            if not reply:
                reply = "にゃ？ もう一度話してくれ。"

            for i in range(0, len(reply), 1900):
                await message.reply(
                    reply[i:i + 1900],
                    mention_author=False
                )

        except Exception as e:
            print(f"エラー: {e}")
            await message.reply(
                "むむっ、少し調子が悪いようだ。"
                "また話しかけてくれ！🐾",
                mention_author=False
            )

bot.run(TOKEN)

