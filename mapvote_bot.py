"""
Bot de encuesta de "Mapa de la Semana" para Hell Let Loose (estilo The Bootcamp Bot).

Requisitos:
    pip install discord.py python-dateutil

Setup en Discord Developer Portal (https://discord.com/developers/applications):
    1. Crear una aplicación -> Bot -> Reset Token -> copiar el token (va en BOT_TOKEN abajo, o mejor,
       en una variable de entorno DISCORD_BOT_TOKEN).
    2. NO hace falta activar ningún "Privileged Gateway Intent" (Message Content / Presence / Members)
       para que este bot funcione, ya que usamos fetch_member() en vez del intent de members.
    3. En "OAuth2 -> URL Generator": marcar scope "bot", y permisos:
       Send Messages, Manage Messages, Add Reactions, Embed Links, Read Message History.
    4. Abrir la URL generada e invitar el bot a tu servidor.
    5. Subí banner_asado_vino.png y logo_asado_vino.png a cualquier canal de tu Discord,
       click derecho sobre cada imagen ya subida -> Copiar enlace, y pegá esas URLs en
       BANNER_URL y AUTHOR_ICON_URL más abajo.

Cómo correrlo en el VPS (recomendado con systemd para que sobreviva reinicios):
    - Ver instrucciones al final de este archivo (comentario DEPLOY).
"""

import json
import os
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import tasks

# =========================================================================
# CONFIGURACIÓN — editar estos valores
# =========================================================================

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "PEGA_TU_TOKEN_ACA")

# ID del canal donde se postea la encuesta (activar Modo Desarrollador en Discord,
# click derecho sobre el canal -> Copiar ID de canal)
CHANNEL_ID = 1468298007962189825  # <-- reemplazar

# URL pública del banner que aparece al pie del mensaje (subí banner_asado_vino.png
# a cualquier canal de tu Discord, click derecho sobre la imagen -> Copiar enlace).
# Poner None si no querés banner.
BANNER_URL = "PEGA_AQUI_LA_URL_DEL_BANNER"  # ej: https://cdn.discordapp.com/attachments/.../banner_asado_vino.png

# URL del logo chico (ícono del autor del embed, arriba a la izquierda). Poner None si no querés.
AUTHOR_ICON_URL = "PEGA_AQUI_LA_URL_DEL_LOGO"

# Lista de mapas candidatos: (nombre a mostrar, emoji)
# El emoji puede ser un emoji unicode normal, o un emoji custom del server
# en formato "<:nombre:ID>" (para copiarlo, escribí \:nombre_emoji: en un chat de Discord
# y copiá el resultado antes de enviar).
MAPS = [
    ("Carentan", "🏠"),
    ("Omaha Beach", "🌊"),
    ("Utah Beach", "🪖"),
    ("SME", "🌳"),
    ("SMDM", "⛪"),
    ("Hill 400", "⛰️"),
    ("Hurtgen Forest", "🌲"),
    ("Foy (Night)", "❄️"),
    ("PHL (Night)", "🌧️"),
    ("Kharkov", "🥶"),
    ("El Alamein", "🏜️"),
    ("Juno", "🍁"),
]

# Duración de la ventana de votación desde que se postea (en horas)
VOTING_WINDOW_HOURS = 24

# Cuánto tiempo antes del match se postea la encuesta (en horas). Ej: 24h antes.
HOURS_BEFORE_MATCH_TO_POST = 48

# Día y hora del próximo match (se recalcula automáticamente cada semana tras cerrar)
# formato: día de la semana (0=lunes ... 6=domingo), hora, minuto (UTC)
MATCH_WEEKDAY = 2   # miércoles
MATCH_HOUR_UTC = 22
MATCH_MINUTE_UTC = 0

STATE_FILE = "mapvote_state.json"

EMBED_COLOR = 0x2ECC71
FOOTER_TEXT = "Vota el mapa · actualizado en vivo"

# =========================================================================
# Estado persistente
# =========================================================================

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def next_match_datetime(after: datetime) -> datetime:
    """Calcula la próxima fecha/hora de match (UTC) según MATCH_WEEKDAY/HOUR/MINUTE."""
    days_ahead = (MATCH_WEEKDAY - after.weekday()) % 7
    candidate = after.replace(
        hour=MATCH_HOUR_UTC, minute=MATCH_MINUTE_UTC, second=0, microsecond=0
    ) + timedelta(days=days_ahead)
    if candidate <= after:
        candidate += timedelta(days=7)
    return candidate


def new_poll_state() -> dict:
    now = datetime.now(timezone.utc)
    match_at = next_match_datetime(now)
    voting_closes_at = match_at - timedelta(
        hours=HOURS_BEFORE_MATCH_TO_POST - VOTING_WINDOW_HOURS
    )
    return {
        "message_id": None,
        "match_at": match_at.isoformat(),
        "voting_closes_at": voting_closes_at.isoformat(),
        "votes": {emoji: [] for _, emoji in MAPS},  # emoji -> lista de "user_id:nombre"
        "closed": False,
    }


# =========================================================================
# Construcción del embed
# =========================================================================

def build_embed(state: dict, winner_name: str | None = None) -> discord.Embed:
    match_at = datetime.fromisoformat(state["match_at"])
    closes_at = datetime.fromisoformat(state["voting_closes_at"])
    closed = state.get("closed", False)

    title = "🗺️ Mapa de la semana (HLL — WW2)"
    embed = discord.Embed(title=title, color=EMBED_COLOR)

    if winner_name:
        embed.add_field(name="🏆 Ganador", value=f"**{winner_name}**", inline=False)

    embed.description = (
        "La votación está cerrada, resultados arriba."
        if closed
        else "Elegí el mapa que te gustaría jugar esta semana."
    )

    embed.add_field(
        name="🕒 Match",
        value=f"<t:{int(match_at.timestamp())}:F> (<t:{int(match_at.timestamp())}:R>)",
        inline=False,
    )
    embed.add_field(
        name="🔒 Cierra votación" if not closed else "🔒 Votación cerró",
        value=f"<t:{int(closes_at.timestamp())}:F> (<t:{int(closes_at.timestamp())}:R>)",
        inline=False,
    )
    embed.add_field(name="🔁 Repite", value="Cada semana", inline=False)

    for name, emoji in MAPS:
        voters = state["votes"].get(emoji, [])
        count = len(voters)
        names = "\n".join(v.split(":", 1)[1] for v in voters) if voters else "\u2014"
        embed.add_field(name=f"{emoji} {name} ({count})", value=names, inline=True)

    if AUTHOR_ICON_URL and AUTHOR_ICON_URL != "PEGA_AQUI_LA_URL_DEL_LOGO":
        embed.set_author(name="ASADO & VINO", icon_url=AUTHOR_ICON_URL)

    if BANNER_URL and BANNER_URL != "PEGA_AQUI_LA_URL_DEL_BANNER":
        embed.set_image(url=BANNER_URL)

    status = "votación cerrada" if closed else "votación abierta"
    now_str = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    embed.set_footer(text=f"Asado & Vino · {status} · actualizado {now_str}")
    return embed


# =========================================================================
# Cliente Discord
# =========================================================================

intents = discord.Intents.default()
intents.reactions = True
client = discord.Client(intents=intents)

state: dict = {}
_member_cache: dict[int, str] = {}


async def get_display_name(guild: discord.Guild, user_id: int) -> str:
    if user_id in _member_cache:
        return _member_cache[user_id]
    try:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        name = member.display_name
    except discord.NotFound:
        name = f"usuario {user_id}"
    _member_cache[user_id] = name
    return name


async def post_new_poll(channel: discord.TextChannel):
    global state
    state = new_poll_state()
    embed = build_embed(state)
    message = await channel.send(embed=embed)
    for _, emoji in MAPS:
        await message.add_reaction(emoji)
    state["message_id"] = message.id
    save_state(state)


async def refresh_poll_message(channel: discord.TextChannel, winner_name: str | None = None):
    if not state.get("message_id"):
        return
    try:
        message = await channel.fetch_message(state["message_id"])
    except discord.NotFound:
        return
    await message.edit(embed=build_embed(state, winner_name=winner_name))


@client.event
async def on_ready():
    global state
    print(f"Conectado como {client.user}")
    loaded = load_state()
    if loaded:
        state = loaded
    else:
        channel = client.get_channel(CHANNEL_ID)
        await post_new_poll(channel)
    poll_loop.start()


@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.channel_id != CHANNEL_ID or payload.message_id != state.get("message_id"):
        return
    if payload.user_id == client.user.id:
        return
    if state.get("closed"):
        return

    emoji_key = str(payload.emoji)
    if emoji_key not in state["votes"]:
        return

    guild = client.get_guild(payload.guild_id)
    name = await get_display_name(guild, payload.user_id)
    entry = f"{payload.user_id}:{name}"
    if entry not in state["votes"][emoji_key]:
        state["votes"][emoji_key].append(entry)
    save_state(state)

    channel = client.get_channel(payload.channel_id)
    await refresh_poll_message(channel)


@client.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.channel_id != CHANNEL_ID or payload.message_id != state.get("message_id"):
        return
    if state.get("closed"):
        return

    emoji_key = str(payload.emoji)
    if emoji_key not in state["votes"]:
        return

    guild = client.get_guild(payload.guild_id)
    name = await get_display_name(guild, payload.user_id)
    entry = f"{payload.user_id}:{name}"
    if entry in state["votes"][emoji_key]:
        state["votes"][emoji_key].remove(entry)
        save_state(state)

    channel = client.get_channel(payload.channel_id)
    await refresh_poll_message(channel)


@tasks.loop(seconds=30)
async def poll_loop():
    if state.get("closed") or not state.get("voting_closes_at"):
        return

    now = datetime.now(timezone.utc)
    closes_at = datetime.fromisoformat(state["voting_closes_at"])

    if now < closes_at:
        return

    channel = client.get_channel(CHANNEL_ID)

    winner_emoji, winner_voters = max(
        state["votes"].items(), key=lambda item: len(item[1]), default=(None, [])
    )
    winner_name = next((n for n, e in MAPS if e == winner_emoji), None)
    if not winner_voters:
        winner_name = None

    state["closed"] = True
    save_state(state)

    # Edita el mensaje original mostrando el ganador arriba, en vez de mandar uno nuevo
    await refresh_poll_message(channel, winner_name=winner_name)

    # Arma la próxima encuesta de la semana siguiente
    await post_new_poll(channel)


if __name__ == "__main__":
    client.run(BOT_TOKEN)


# =========================================================================
# DEPLOY — cómo dejarlo corriendo 24/7 en tu VPS con systemd
# =========================================================================
#
# 1. Copiar este archivo al VPS, ej: /root/mapvote_bot/mapvote_bot.py
# 2. cd /root/mapvote_bot && python3 -m venv venv && source venv/bin/activate
# 3. pip install discord.py
# 4. export DISCORD_BOT_TOKEN="tu_token_aca"   (o cargarlo en el propio script)
# 5. Crear /etc/systemd/system/mapvote-bot.service con:
#
#    [Unit]
#    Description=Bot de votacion de mapas HLL
#    After=network.target
#
#    [Service]
#    Type=simple
#    WorkingDirectory=/root/mapvote_bot
#    Environment=DISCORD_BOT_TOKEN=tu_token_aca
#    ExecStart=/root/mapvote_bot/venv/bin/python3 /root/mapvote_bot/mapvote_bot.py
#    Restart=always
#    RestartSec=5
#
#    [Install]
#    WantedBy=multi-user.target
#
# 6. sudo systemctl daemon-reload
#    sudo systemctl enable mapvote-bot
#    sudo systemctl start mapvote-bot
#    sudo systemctl status mapvote-bot   (para confirmar que arrancó bien)
#    journalctl -u mapvote-bot -f        (para ver logs en vivo)
