import os
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 서버별 게임 상태를 메모리에만 저장합니다. 봇이 재시작되면 초기화됩니다.
games = {}


def get_game(guild_id: int):
    return games.setdefault(guild_id, {
        "players": [],
        "scores": {},
        "active": False,
        "turn_index": 0,
        "current_item": None,
        "next_item": None,
        "buyers_this_turn": set(),
        "phase": "lobby",  # lobby / choosing_first / selling
    })


def player_name(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    return member.display_name if member else f"<@{user_id}>"


async def send_item_to_everyone_except(guild: discord.Guild, game: dict, seller_id: int, item: str):
    failed = []
    for user_id in game["players"]:
        if user_id == seller_id:
            continue
        member = guild.get_member(user_id)
        if member is None:
            failed.append(user_id)
            continue
        try:
            await member.send(
                f"🎁 **이번 판매 물건**\n물건: **{item}**\n\n"
                "판매자가 이 물건을 모른 채 설명합니다. 다른 참가자는 `/구매`로 구매 의사를 표시할 수 있어요."
            )
        except (discord.Forbidden, discord.HTTPException):
            failed.append(user_id)
    return failed


async def start_seller_turn(interaction: discord.Interaction, game: dict):
    guild = interaction.guild
    seller_id = game["players"][game["turn_index"]]
    game["buyers_this_turn"] = set()
    failed = await send_item_to_everyone_except(guild, game, seller_id, game["current_item"])
    seller = guild.get_member(seller_id)
    channel = interaction.channel

    if failed:
        mentions = ", ".join(f"<@{uid}>" for uid in failed)
        await channel.send(
            f"⚠️ 일부 참가자에게 DM을 보내지 못했어요: {mentions}\n"
            "해당 참가자는 DM 설정을 확인한 뒤 `/물건재전송`을 사용해 주세요."
        )

    await channel.send(
        f"🛍️ **{seller.display_name if seller else f'<@{seller_id}>'}님의 판매 차례!**\n"
        "판매자는 물건을 모르는 상태로 홍보를 시작하세요.\n"
        "구매 의사를 표시하려면 `/구매`, 판매를 마치려면 `/판매종료`를 사용하세요."
    )


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"로그인: {bot.user} / 명령어 {len(synced)}개 동기화")
    except Exception as exc:
        print(f"명령어 동기화 실패: {exc}")


@bot.tree.command(name="참가", description="안 보고 물건 팔기 게임에 참가합니다.")
async def join(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message("서버에서만 사용할 수 있어요.", ephemeral=True)
    game = get_game(interaction.guild.id)
    if game["phase"] != "lobby":
        return await interaction.response.send_message("이미 게임이 시작됐거나 진행 준비 중이에요.", ephemeral=True)
    if interaction.user.id in game["players"]:
        return await interaction.response.send_message("이미 참가했어요!", ephemeral=True)
    game["players"].append(interaction.user.id)
    game["scores"][interaction.user.id] = 0
    await interaction.response.send_message(
        f"✅ {interaction.user.mention} 참가 완료! 현재 참가자: {len(game['players'])}명"
    )


@bot.tree.command(name="참가취소", description="게임 시작 전에 참가를 취소합니다.")
async def leave(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message("서버에서만 사용할 수 있어요.", ephemeral=True)
    game = get_game(interaction.guild.id)
    if game["phase"] != "lobby":
        return await interaction.response.send_message("게임 시작 후에는 참가 취소를 할 수 없어요.", ephemeral=True)
    if interaction.user.id not in game["players"]:
        return await interaction.response.send_message("참가 중이 아니에요.", ephemeral=True)
    game["players"].remove(interaction.user.id)
    game["scores"].pop(interaction.user.id, None)
    await interaction.response.send_message(f"참가 취소 완료! 현재 참가자: {len(game['players'])}명")


@bot.tree.command(name="게임시작", description="참가자들의 판매 게임을 시작합니다.")
async def start_game(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message("서버에서만 사용할 수 있어요.", ephemeral=True)
    game = get_game(interaction.guild.id)
    if game["phase"] != "lobby":
        return await interaction.response.send_message("이미 게임이 진행 중이에요.", ephemeral=True)
    if len(game["players"]) < 2:
        return await interaction.response.send_message("최소 2명이 참가해야 해요.", ephemeral=True)

    game["phase"] = "choosing_first"
    last_player = game["players"][-1]
    await interaction.response.send_message(
        f"🎮 게임 시작 준비!\n"
        f"참가 순서: {' → '.join(player_name(interaction.guild, uid) for uid in game['players'])}\n\n"
        f"첫 번째 판매자 **{player_name(interaction.guild, game['players'][0])}**의 물건은 "
        f"마지막 참가자 **{player_name(interaction.guild, last_player)}**가 정해야 해요.\n"
        f"마지막 참가자가 `/첫물건정하기 물건:물건이름`을 입력하면 첫 판매가 시작됩니다."
    )


@bot.tree.command(name="첫물건정하기", description="첫 번째 판매자가 팔 물건을 정합니다. 마지막 참가자 전용.")
@app_commands.describe(물건="첫 번째 판매자가 모르게 할 물건 이름")
async def choose_first_item(interaction: discord.Interaction, 물건: str):
    if interaction.guild is None:
        return await interaction.response.send_message("서버에서만 사용할 수 있어요.", ephemeral=True)
    game = get_game(interaction.guild.id)
    if game["phase"] != "choosing_first":
        return await interaction.response.send_message("지금은 첫 물건을 정할 단계가 아니에요.", ephemeral=True)
    if interaction.user.id != game["players"][-1]:
        return await interaction.response.send_message("마지막 참가자만 첫 물건을 정할 수 있어요.", ephemeral=True)
    if not 물건.strip():
        return await interaction.response.send_message("물건 이름을 입력해 주세요.", ephemeral=True)

    game["current_item"] = 물건.strip()
    game["phase"] = "selling"
    game["active"] = True
    game["turn_index"] = 0
    await interaction.response.send_message("첫 물건이 정해졌어요! 판매를 시작합니다.")
    await start_seller_turn(interaction, game)


@bot.tree.command(name="물건정하기", description="다음 판매자가 팔 물건을 정합니다. 현재 판매자 전용.")
@app_commands.describe(물건="다음 판매자가 모르게 할 물건 이름")
async def choose_next_item(interaction: discord.Interaction, 물건: str):
    if interaction.guild is None:
        return await interaction.response.send_message("서버에서만 사용할 수 있어요.", ephemeral=True)
    game = get_game(interaction.guild.id)
    if game["phase"] != "selling":
        return await interaction.response.send_message("현재 판매 차례가 없어요.", ephemeral=True)
    current_seller = game["players"][game["turn_index"]]
    if interaction.user.id != current_seller:
        return await interaction.response.send_message("현재 판매자만 다음 물건을 정할 수 있어요.", ephemeral=True)
    if not 물건.strip():
        return await interaction.response.send_message("물건 이름을 입력해 주세요.", ephemeral=True)
    game["next_item"] = 물건.strip()
    await interaction.response.send_message("✅ 다음 판매자의 물건을 정했어요. 판매 종료 후 공개됩니다.", ephemeral=True)


@bot.tree.command(name="구매", description="현재 판매자의 물건을 구매하겠다고 표시합니다. 판매자 점수가 1점 오릅니다.")
async def buy(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message("서버에서만 사용할 수 있어요.", ephemeral=True)
    game = get_game(interaction.guild.id)
    if game["phase"] != "selling":
        return await interaction.response.send_message("현재 진행 중인 판매가 없어요.", ephemeral=True)
    seller_id = game["players"][game["turn_index"]]
    if interaction.user.id == seller_id:
        return await interaction.response.send_message("자기 물건은 구매할 수 없어요.", ephemeral=True)
    if interaction.user.id not in game["players"]:
        return await interaction.response.send_message("참가자만 구매할 수 있어요.", ephemeral=True)
    if interaction.user.id in game["buyers_this_turn"]:
        return await interaction.response.send_message("이번 판매 차례에는 이미 구매 의사를 표시했어요.", ephemeral=True)

    game["buyers_this_turn"].add(interaction.user.id)
    game["scores"][seller_id] += 1
    await interaction.response.send_message(
        f"🛒 {interaction.user.mention}님이 구매 의사를 표시했어요! "
        f"판매자 점수 +1 (현재 {game['scores'][seller_id]}점)"
    )


@bot.tree.command(name="판매종료", description="현재 판매를 끝내고 물건을 공개합니다.")
async def end_sale(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message("서버에서만 사용할 수 있어요.", ephemeral=True)
    game = get_game(interaction.guild.id)
    if game["phase"] != "selling":
        return await interaction.response.send_message("현재 진행 중인 판매가 없어요.", ephemeral=True)
    seller_id = game["players"][game["turn_index"]]
    if interaction.user.id != seller_id:
        return await interaction.response.send_message("현재 판매자만 판매를 종료할 수 있어요.", ephemeral=True)

    sold_item = game["current_item"]
    await interaction.response.send_message(
        f"📦 **물건 공개!** 이번 물건은 **{sold_item}**였어요.\n"
        f"판매자 점수: **{game['scores'][seller_id]}점**"
    )

    if game["turn_index"] == len(game["players"]) - 1:
        ranking = sorted(game["scores"].items(), key=lambda pair: pair[1], reverse=True)
        lines = []
        for idx, (uid, score) in enumerate(ranking, start=1):
            lines.append(f"{idx}. {player_name(interaction.guild, uid)} — **{score}점**")
        await interaction.channel.send("🏆 **게임 종료! 최종 순위**\n" + "\n".join(lines))
        game["active"] = False
        game["phase"] = "lobby"
        game["players"] = []
        game["scores"] = {}
        game["turn_index"] = 0
        game["current_item"] = None
        game["next_item"] = None
        game["buyers_this_turn"] = set()
        return

    if not game["next_item"]:
        await interaction.channel.send(
            "⚠️ 다음 물건이 정해지지 않았어요. 현재 판매자는 `/물건정하기`로 다음 물건을 정한 뒤, "
            "다음 참가자가 `/물건재전송`을 사용하거나 관리자가 게임을 종료하고 다시 시작해 주세요."
        )
        # 다음 아이템을 정하지 않았으면 현재 턴을 유지해 실수로 진행이 꼬이지 않게 합니다.
        game["current_item"] = sold_item
        return

    game["turn_index"] += 1
    game["current_item"] = game["next_item"]
    game["next_item"] = None
    await start_seller_turn(interaction, game)


@bot.tree.command(name="물건재전송", description="현재 물건 안내를 참가자들에게 다시 DM으로 보냅니다.")
async def resend_item(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message("서버에서만 사용할 수 있어요.", ephemeral=True)
    game = get_game(interaction.guild.id)
    if game["phase"] != "selling":
        return await interaction.response.send_message("현재 판매 차례가 없어요.", ephemeral=True)
    seller_id = game["players"][game["turn_index"]]
    failed = await send_item_to_everyone_except(interaction.guild, game, seller_id, game["current_item"])
    if failed:
        await interaction.response.send_message(
            "일부 참가자에게 DM을 보내지 못했어요: " + ", ".join(f"<@{uid}>" for uid in failed),
            ephemeral=True
        )
    else:
        await interaction.response.send_message("물건 안내 DM을 다시 보냈어요.", ephemeral=True)


@bot.tree.command(name="게임현황", description="현재 참가자와 점수를 확인합니다.")
async def status(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message("서버에서만 사용할 수 있어요.", ephemeral=True)
    game = get_game(interaction.guild.id)
    if not game["players"]:
        return await interaction.response.send_message("현재 참가자가 없어요.")
    lines = [
        f"{i + 1}. {player_name(interaction.guild, uid)} — {game['scores'].get(uid, 0)}점"
        for i, uid in enumerate(game["players"])
    ]
    current = ""
    if game["phase"] == "selling":
        current = f"\n현재 판매자: **{player_name(interaction.guild, game['players'][game['turn_index']])}**"
    await interaction.response.send_message("👥 **게임 현황**\n" + "\n".join(lines) + current)


@bot.tree.command(name="내점수", description="내 현재 점수를 확인합니다.")
async def my_score(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message("서버에서만 사용할 수 있어요.", ephemeral=True)
    game = get_game(interaction.guild.id)
    if interaction.user.id not in game["scores"]:
        return await interaction.response.send_message("현재 참가 중이 아니에요.", ephemeral=True)
    await interaction.response.send_message(f"현재 점수는 **{game['scores'][interaction.user.id]}점**이에요.", ephemeral=True)


@bot.tree.command(name="게임종료", description="관리자가 진행 중인 게임을 강제로 종료합니다.")
@app_commands.checks.has_permissions(manage_guild=True)
async def force_end(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message("서버에서만 사용할 수 있어요.", ephemeral=True)
    games.pop(interaction.guild.id, None)
    await interaction.response.send_message("게임을 종료하고 현재 참가자와 점수를 초기화했어요.")


@force_end.error
async def force_end_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        message = "이 명령어는 서버 관리 권한이 있는 사람만 사용할 수 있어요."
    else:
        message = "게임을 종료하는 중 오류가 발생했어요."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN 환경 변수가 없습니다. Railway Variables에 봇 토큰을 설정하세요.")

bot.run(TOKEN)
