"""
Bot de encuesta de "Rotación de la Semana" para Hell Let Loose (Asado & Vino / 7DL VKR).

La comunidad vota los mapas que quiere jugar durante la semana. Al cerrar la votación,
el bot toma los ROTATION_SIZE mapas más votados y los carga automáticamente como la
rotación activa del servidor, llamando directamente a la API del CRCON.

Requisitos:
    pip install discord.py aiohttp

Setup en Discord Developer Portal (https://discord.com/developers/applications):
    1. Crear una aplicación -> Bot -> Reset Token -> copiar el token (va en la variable
       de entorno DISCORD_BOT_TOKEN, configurada en Railway -> Variables).
    2. NO hace falta activar ningún "Privileged Gateway Intent".
    3. En "OAuth2 -> URL Generator": scope "bot", permisos: Send Messages, Manage Messages,
       Add Reactions, Embed Links, Read Message History.
    4. Invitar el bot al servidor con la URL generada.

Setup de la integración con CRCON:
    1. En el panel de tu CRCON, generar un token de API (Settings -> buscar la sección
       de API Tokens / Django API Keys). Guardarlo en la variable de entorno
       CRCON_API_TOKEN (en Railway -> Variables), NUNCA en este archivo.
    2. Confirmar la URL base de tu CRCON en CRCON_BASE_URL más abajo (con puerto, ej.
       "http://TU_IP:8010").
    3. Cada mapa en la lista MAPS necesita su "crcon_id" real -- el identificador interno
       que usa tu CRCON (ej. "carentan_warfare"). Para conseguirlos:
           curl http://TU_IP:8010/api/get_map_rotation -H "Authorization: Bearer TU_TOKEN"
       Eso devuelve la rotación actual con los IDs reales -- comparalos con el nombre del
       mapa y completá el placeholder "REEMPLAZAR_ID_..." de cada entrada en MAPS.
    4. Sin esos IDs completos, el bot sigue funcionando la parte de Discord (encuesta,
       conteo, cierre) pero NO va a poder aplicar la rotación en el servidor -- lo va a
       avisar en el canal y en el log en vez de fallar en silencio.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord.ext import tasks

# =========================================================================
# CONFIGURACIÓN — editar estos valores
# =========================================================================

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "PEGA_TU_TOKEN_ACA")

# ID del canal donde se postea la encuesta (Modo Desarrollador -> click derecho
# sobre el canal -> Copiar ID de canal)
CHANNEL_ID = 1544782617940074587  # #votemap

# URL pública del banner al pie del embed. Poner None si no querés banner.
BANNER_URL = "https://cdn.jsdelivr.net/gh/insuajuancruz-sketch/Asado-Vino-Bot@main/BannerAsado2.png"

# URL del logo chico (ícono del autor, arriba a la izquierda). Poner None si no querés.
AUTHOR_ICON_URL = "PEGA_AQUI_LA_URL_DEL_LOGO"

# Lista de mapas candidatos: (nombre a mostrar, emoji, ID real en el CRCON)
# 12 candidatos: 10 en Warfare + 2 en Offensive. IDs confirmados con GET /api/get_maps
# el 04/09/2026. El bot selecciona los ROTATION_SIZE (8) más votados de esta lista.
MAPS = [
    ("Carentan", "🏠", "carentan_warfare"),
    ("Omaha Beach", "🌊", "omahabeach_warfare"),
    ("Utah Beach", "🪖", "utahbeach_warfare"),
    ("St. Mere Eglise", "⛪", "stmereeglise_warfare"),
    ("St. Marie Du Mont", "🏘️", "stmariedumont_warfare"),
    ("Foy", "🌲", "foy_warfare"),
    ("Hurtgen Forest", "🌫️", "hurtgenforest_warfare_V2"),
    ("Hill 400", "⛰️", "hill400_warfare"),
    ("Purple Heart Lane", "🌧️", "PHL_L_1944_Warfare"),
    ("Driel", "🌷", "driel_warfare"),
    ("Remagen (Off. US)", "🌉", "REM_L_1945_OffensiveUS"),
    ("Kursk (Off. RUS)", "🐻", "kursk_offensive_rus"),
]

# Cuántos mapas entran en la rotación semanal (los más votados)
ROTATION_SIZE = 8

# Día y hora en que cierra la votación y se aplica la nueva rotación
# (0=lunes ... 6=domingo), hora/minuto en UTC. La próxima encuesta se abre
# inmediatamente después, para la semana siguiente.
CLOSE_WEEKDAY = 1   # martes
CLOSE_HOUR_UTC = 22
CLOSE_MINUTE_UTC = 0

# --- Integración CRCON ---
CRCON_BASE_URL = "http://152.53.39.31:8010"  # sin barra al final
CRCON_API_TOKEN = os.environ.get("CRCON_API_TOKEN", "")

STATE_FILE = "/data/mapvote_state.json" if os.path.isdir("/data") else "mapvote_state.json"

EMBED_COLOR = 0x2ECC71

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


def next_close_datetime(after: datetime) -> datetime:
    """Calcula el próximo cierre de votación (UTC) según CLOSE_WEEKDAY/HOUR/MINUTE."""
    days_ahead = (CLOSE_WEEKDAY - after.weekday()) % 7
    candidate = after.replace(
        hour=CLOSE_HOUR_UTC, minute=CLOSE_MINUTE_UTC, second=0, microsecond=0
    ) + timedelta(days=days_ahead)
    if candidate <= after:
        candidate += timedelta(days=7)
    return candidate


def new_poll_state() -> dict:
    now = datetime.now(timezone.utc)
    closes_at = next_close_datetime(now)
    return {
        "message_id": None,
        "voting_closes_at": closes_at.isoformat(),
        "votes": {emoji: [] for _, emoji, _ in MAPS},  # emoji -> lista de "user_id:nombre"
        "closed": False,
        "rotation_result": None,  # lista de [nombre, votos], se llena al cerrar
    }


def get_top_maps(state: dict) -> list[tuple[str, str, str, int]]:
    """Devuelve los ROTATION_SIZE mapas más votados: (nombre, emoji, crcon_id, cantidad_votos)."""
    scored = [
        (name, emoji, crcon_id, len(state["votes"].get(emoji, [])))
        for name, emoji, crcon_id in MAPS
    ]
    scored.sort(key=lambda x: x[3], reverse=True)
    return scored[:ROTATION_SIZE]


# =========================================================================
# Integración con la API del CRCON
# =========================================================================

async def apply_rotation_to_crcon(map_ids: list[str]) -> str:
    """
    Reemplaza la rotación actual del CRCON por map_ids: saca todos los mapas
    que estén puestos ahora y agrega los nuevos. Devuelve un texto corto con
    el resultado, para loguear o mostrar en Discord.
    """
    if not CRCON_API_TOKEN or "REEMPLAZAR" in CRCON_BASE_URL:
        return "⚠️ CRCON no configurado (falta token o URL) — rotación no aplicada en el servidor."

    valid_ids = [m for m in map_ids if m and "REEMPLAZAR_ID" not in m]
    if not valid_ids:
        return "⚠️ Ningún mapa ganador tiene su ID de CRCON configurado — nada que aplicar."

    headers = {
        "Authorization": f"Bearer {CRCON_API_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            # 1. Traer la rotación actual
            async with session.get(f"{CRCON_BASE_URL}/api/get_map_rotation") as resp:
                data = await resp.json()
                current = data.get("result", []) or []
                current_ids = [m.get("id") or m for m in current] if current else []

            # 2. Sacar cada mapa actual de la rotación
            for map_id in current_ids:
                async with session.post(
                    f"{CRCON_BASE_URL}/api/remove_map_from_rotation",
                    json={"map_name": map_id},
                ):
                    pass

            # 3. Agregar los nuevos mapas de la semana
            async with session.post(
                f"{CRCON_BASE_URL}/api/add_maps_to_rotation",
                json={"map_names": valid_ids},
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    return f"❌ Error aplicando rotación en CRCON (HTTP {resp.status}): {text[:200]}"

        skipped = len(map_ids) - len(valid_ids)
        msg = f"✅ Rotación aplicada en el servidor ({len(valid_ids)} mapas)."
        if skipped:
            msg += f" {skipped} mapa(s) se salteó por no tener ID configurado."
        return msg
    except Exception as error:
        return f"❌ No se pudo conectar con el CRCON: {error}"


# =========================================================================
# Construcción del embed
# =========================================================================

def build_embed(state: dict) -> discord.Embed:
    closes_at = datetime.fromisoformat(state["voting_closes_at"])
    closed = state.get("closed", False)

    embed = discord.Embed(title="🗺️ Rotación de la semana (HLL — WW2)", color=EMBED_COLOR)

    if closed and state.get("rotation_result"):
        lines = [
            f"{i+1}. {name} ({votes} voto{'s' if votes != 1 else ''})"
            for i, (name, votes) in enumerate(state["rotation_result"])
        ]
        embed.add_field(name="🏆 Rotación resultante", value="\n".join(lines), inline=False)

    embed.description = (
        "La votación está cerrada, la rotación de la semana quedó arriba."
        if closed
        else f"Elegí los mapas que te gustaría jugar esta semana. "
             f"Los {ROTATION_SIZE} más votados forman la rotación."
    )

    embed.add_field(
        name="🔒 Cierra votación" if not closed else "🔒 Votación cerró",
        value=f"<t:{int(closes_at.timestamp())}:F> (<t:{int(closes_at.timestamp())}:R>)",
        inline=False,
    )
    embed.add_field(name="🔁 Repite", value="Cada semana", inline=False)

    for name, emoji, _ in MAPS:
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
    # El sufijo "state:" guarda closes_at/closed en formato compacto, para poder
    # reconstruir el estado leyendo el mensaje si se pierde el archivo local.
    state_tag = f"state:{state['voting_closes_at']}|{int(closed)}"
    embed.set_footer(text=f"Asado & Vino · {status} · actualizado {now_str} · {state_tag}")
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
    for _, emoji, _ in MAPS:
        await message.add_reaction(emoji)
    state["message_id"] = message.id
    save_state(state)


async def refresh_poll_message(channel: discord.TextChannel):
    if not state.get("message_id"):
        return
    try:
        message = await channel.fetch_message(state["message_id"])
    except discord.NotFound:
        return
    await message.edit(embed=build_embed(state))


async def rebuild_state_from_channel(channel: discord.TextChannel) -> dict | None:
    """
    Busca el último mensaje de encuesta que mandó el bot en el canal y reconstruye
    el estado (fecha de cierre + votos) leyendo el footer y las reacciones reales
    del mensaje. Sirve como respaldo si se pierde mapvote_state.json.
    """
    async for message in channel.history(limit=100):
        if message.author.id != client.user.id or not message.embeds:
            continue
        embed = message.embeds[0]
        if not embed.title or "Rotación de la semana" not in embed.title:
            continue
        footer_text = embed.footer.text or ""
        if "state:" not in footer_text:
            continue

        try:
            payload = footer_text.split("state:", 1)[1]
            closes_iso, closed_flag = payload.split("|")
        except ValueError:
            continue

        votes = {emoji: [] for _, emoji, _ in MAPS}
        for reaction in message.reactions:
            emoji_key = str(reaction.emoji)
            if emoji_key not in votes:
                continue
            async for user in reaction.users():
                if user.id == client.user.id:
                    continue
                name = await get_display_name(channel.guild, user.id)
                votes[emoji_key].append(f"{user.id}:{name}")

        print(f"Estado reconstruido desde el mensaje {message.id} en #{channel.name}")
        return {
            "message_id": message.id,
            "voting_closes_at": closes_iso,
            "votes": votes,
            "closed": closed_flag == "1",
            "rotation_result": None,
        }
    return None


@client.event
async def on_ready():
    global state
    print(f"Conectado como {client.user}")
    channel = client.get_channel(CHANNEL_ID)
    loaded = load_state()
    if loaded:
        state = loaded
    else:
        recovered = await rebuild_state_from_channel(channel)
        if recovered:
            state = recovered
            save_state(state)
        else:
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

    top_maps = get_top_maps(state)
    state["rotation_result"] = [[name, votes] for name, _, _, votes in top_maps]
    state["closed"] = True
    save_state(state)

    # Edita el mensaje mostrando la rotación resultante arriba
    await refresh_poll_message(channel)

    # Aplica la rotación en el CRCON de verdad
    map_ids = [crcon_id for _, _, crcon_id, votes in top_maps if votes > 0]
    result_msg = await apply_rotation_to_crcon(map_ids)
    print(result_msg)
    try:
        await channel.send(result_msg)
    except Exception:
        pass

    # Arma la próxima encuesta de la semana siguiente
    await post_new_poll(channel)


if __name__ == "__main__":
    client.run(BOT_TOKEN)
