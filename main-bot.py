import logging
import os
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
token = os.getenv('DISCORD_TOKEN')

if not token:
    raise ValueError("DISCORD_TOKEN not found in environment variables.")

handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='-', intents=intents)

# Stores active AFK users: {user_id: "reason/duration string"}
afk_users = {}


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} (ID: {bot.user.id})")


@bot.event
async def on_member_join(member):
    try:
        await member.send(f"Welcome to my private server, {member.mention} :wave: Glad to have you here!")
    except discord.Forbidden:
        # User has DMs disabled from server members
        pass


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # Remove AFK status if the user speaks again (ignoring the -afk command itself)
    if message.author.id in afk_users and not message.content.startswith(f"{bot.command_prefix}afk"):
        del afk_users[message.author.id]
        await message.channel.send(f"Welcome back {message.author.mention}! I removed your AFK status :smiley:")

    # Notify if someone mentions an AFK user
    for user in message.mentions:
        if user.id in afk_users:
            await message.channel.send(f"{user.display_name} is currently AFK: {afk_users[user.id]}")

    await bot.process_commands(message)


@bot.command()
async def ping(ctx):
    await ctx.send(f"Pong = {round(bot.latency * 1000)}ms")


@bot.command()
async def hi(ctx):
    await ctx.send(f"Hello {ctx.author.mention} :wave:, how was your day today?")


@bot.command()
async def afk(ctx, duration=None, *, reason=None):
    details = []
    if duration:
        details.append(f"for {duration}")
    if reason:
        details.append(f"because of {reason}")

    extra_info = " ".join(details)
    afk_users[ctx.author.id] = extra_info or "No reason provided"

    message = f"{ctx.author.mention} is now AFK :sleeping:"
    if extra_info:
        message += f" {extra_info}"

    await ctx.send(message)


@bot.command()
async def unafk(ctx):
    if ctx.author.id in afk_users:
        del afk_users[ctx.author.id]
    await ctx.send(f"{ctx.author.mention} is no longer AFK :smiley:")


bot.run(token, log_handler=handler, log_level=logging.INFO)
