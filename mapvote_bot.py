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
    3. En "OAuth2 -> URL Generator": scopes "bot" Y "applications.commands" (este último
       es necesario para que funcione el comando /votemap_cerrar_en), permisos: Send
       Messages, Manage Messages, Add Reactions, Embed Links, Read Message History.
    4. Invitar el bot al servidor con la URL generada. Si el bot ya estaba invitado sin
       el scope "applications.commands", hay que volver a generar la URL con ambos
       scopes marcados y re-invitarlo (no rompe nada, solo agrega el permiso que falta).

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

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord.ext import tasks

import vip_shop
import roster_signup

# =========================================================================
# CONFIGURACIÓN — editar estos valores
# =========================================================================

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "PEGA_TU_TOKEN_ACA")

# ID del canal donde se postea la encuesta (Modo Desarrollador -> click derecho
# sobre el canal -> Copiar ID de canal)
CHANNEL_ID = 1544782617940074587  # #votemap

# Canal donde se avisa cada vez que se procesa una compra de VIP (log para admins).
# Poner None si no querés este aviso.
VIP_LOG_CHANNEL_ID = 1547710559644946553  # #admin-registro-vip

# URL pública del banner al pie del embed. Poner None si no querés banner.
BANNER_URL = "https://cdn.jsdelivr.net/gh/insuajuancruz-sketch/Asado-Vino-Bot@main/BannerAsado2.png"

# URL del logo chico (ícono del autor, arriba a la izquierda). Poner None si no querés.
AUTHOR_ICON_URL = "PEGA_AQUI_LA_URL_DEL_LOGO"

# Lista de mapas candidatos: (nombre a mostrar, emoji, ID real en el CRCON, categoría)
# categoría: "warfare" u "offensive" -- se usa para garantizar la composición de la
# rotación final (ver WARFARE_SLOTS / OFFENSIVE_SLOTS más abajo).
# 16 candidatos: 12 Warfare + 4 Offensive. IDs confirmados con GET /api/get_maps
# el 04/09/2026.
MAPS = [
    ("Carentan", "🏠", "carentan_warfare", "warfare"),
    ("Omaha Beach", "🌊", "omahabeach_warfare", "warfare"),
    ("Utah Beach", "🪖", "utahbeach_warfare", "warfare"),
    ("St. Mere Eglise", "⛪", "stmereeglise_warfare", "warfare"),
    ("St. Marie Du Mont", "🏘️", "stmariedumont_warfare", "warfare"),
    ("Foy", "❄️", "foy_warfare", "warfare"),
    ("Hurtgen Forest", "🌲", "hurtgenforest_warfare_V2", "warfare"),
    ("Hill 400", "⛰️", "hill400_warfare", "warfare"),
    ("Purple Heart Lane", "🌧️", "PHL_L_1944_Warfare", "warfare"),
    ("Driel", "🌷", "driel_warfare", "warfare"),
    ("Mortain", "🌾", "mortain_warfare_day", "warfare"),
    ("Elsenborn Ridge", "🏔️", "elsenbornridge_warfare_day", "warfare"),
    ("Remagen (Off. US)", "🌉", "REM_L_1945_OffensiveUS", "offensive"),
    ("Kursk (Off. RUS)", "🐻", "kursk_offensive_rus", "offensive"),
    ("Kharkov (Off. RUS)", "🥶", "kharkov_offensive_rus", "offensive"),
    ("El Alamein (Off. CW)", "🏜️", "elalamein_offensive_CW", "offensive"),
]

# Composición garantizada de la rotación semanal: no es simplemente "los 8 más
# votados" -- siempre entran los WARFARE_SLOTS Warfare más votados y los
# OFFENSIVE_SLOTS Offensive más votados, cada categoría compite solo contra sí misma.
WARFARE_SLOTS = 6
OFFENSIVE_SLOTS = 2
ROTATION_SIZE = WARFARE_SLOTS + OFFENSIVE_SLOTS  # 8, solo para referencia/mensajes

# Cada cuántos días se repite el ciclo de votación (cierra, aplica la rotación,
# y abre la encuesta siguiente). Ej: 4 = la votación dura 4 días y vuelve a arrancar.
VOTE_CYCLE_DAYS = 4

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


def new_poll_state() -> dict:
    now = datetime.now(timezone.utc)
    closes_at = now + timedelta(days=VOTE_CYCLE_DAYS)
    return {
        "message_id": None,
        "voting_closes_at": closes_at.isoformat(),
        "votes": {emoji: [] for _, emoji, _, _ in MAPS},  # emoji -> lista de "user_id:nombre"
        "closed": False,
        "rotation_result": None,  # lista de [nombre, votos], se llena al cerrar
    }


def get_top_maps(state: dict) -> list[tuple[str, str, str, int]]:
    """
    Devuelve la rotación final: los WARFARE_SLOTS mapas Warfare más votados +
    los OFFENSIVE_SLOTS mapas Offensive más votados (cada categoría compite
    solo contra sí misma, así la composición queda garantizada). Formato de
    cada item: (nombre, emoji, crcon_id, cantidad_votos).
    """
    warfare = [
        (name, emoji, crcon_id, len(state["votes"].get(emoji, [])))
        for name, emoji, crcon_id, category in MAPS
        if category == "warfare"
    ]
    offensive = [
        (name, emoji, crcon_id, len(state["votes"].get(emoji, [])))
        for name, emoji, crcon_id, category in MAPS
        if category == "offensive"
    ]
    warfare.sort(key=lambda x: x[3], reverse=True)
    offensive.sort(key=lambda x: x[3], reverse=True)

    selected = warfare[:WARFARE_SLOTS] + offensive[:OFFENSIVE_SLOTS]
    selected.sort(key=lambda x: x[3], reverse=True)  # orden de ranking para mostrar
    return selected


def build_rotation_order(state: dict) -> list[str]:
    """
    Arma el orden EXACTO en que se cargan los mapas en el CRCON: bloques de
    Warfare intercalados con Offensive (ej. con 6 Warfare / 2 Offensive da
    W-W-W-O-W-W-W-O), en vez del orden de votos. Sirve para que el Offensive
    quede como "corte" entre tandas de Warfare en vez de ir todo junto.
    Devuelve solo los crcon_id, en el orden final a aplicar.
    """
    warfare = [
        (name, emoji, crcon_id, len(state["votes"].get(emoji, [])))
        for name, emoji, crcon_id, category in MAPS
        if category == "warfare"
    ]
    offensive = [
        (name, emoji, crcon_id, len(state["votes"].get(emoji, [])))
        for name, emoji, crcon_id, category in MAPS
        if category == "offensive"
    ]
    warfare.sort(key=lambda x: x[3], reverse=True)
    offensive.sort(key=lambda x: x[3], reverse=True)

    # Solo entran los que efectivamente tuvieron al menos 1 voto
    warfare_sel = [m for m in warfare[:WARFARE_SLOTS] if m[3] > 0]
    offensive_sel = [m for m in offensive[:OFFENSIVE_SLOTS] if m[3] > 0]

    if not offensive_sel:
        return [m[2] for m in warfare_sel]

    n_off = len(offensive_sel)
    block = len(warfare_sel) // n_off
    extra = len(warfare_sel) % n_off  # si no divide justo, los primeros bloques absorben el resto

    ids: list[str] = []
    w_idx = 0
    for i in range(n_off):
        take = block + (1 if i < extra else 0)
        ids.extend(m[2] for m in warfare_sel[w_idx:w_idx + take])
        w_idx += take
        ids.append(offensive_sel[i][2])
    ids.extend(m[2] for m in warfare_sel[w_idx:])  # por si sobrara alguno
    return ids


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
                result = data.get("result") or {}
                current = result.get("maps", []) if isinstance(result, dict) else (result or [])
                current_ids = [m.get("id") if isinstance(m, dict) else m for m in current]

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

    embed = discord.Embed(title="🗺️ Rotación de mapas (HLL — WW2)", color=EMBED_COLOR)

    if closed and state.get("rotation_result"):
        lines = [
            f"{i+1}. {name} ({votes} voto{'s' if votes != 1 else ''})"
            for i, (name, votes) in enumerate(state["rotation_result"])
        ]
        embed.add_field(name="🏆 Rotación resultante", value="\n".join(lines), inline=False)

    embed.description = (
        "La votación está cerrada, la rotación quedó arriba."
        if closed
        else f"Elegí los mapas que te gustaría jugar en este ciclo. "
             f"Se arma con los {WARFARE_SLOTS} Warfare y los {OFFENSIVE_SLOTS} Offensive más votados."
    )

    embed.add_field(
        name="🔒 Cierra votación" if not closed else "🔒 Votación cerró",
        value=f"<t:{int(closes_at.timestamp())}:F> (<t:{int(closes_at.timestamp())}:R>)",
        inline=False,
    )
    embed.add_field(name="🔁 Repite", value=f"Cada {VOTE_CYCLE_DAYS} días", inline=False)

    for name, emoji, _, _ in MAPS:
        voters = state["votes"].get(emoji, [])
        count = len(voters)
        embed.add_field(name=f"{emoji} {name}", value=f"**{count}** voto{'s' if count != 1 else ''}", inline=True)

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

# ID del servidor de Discord (para registrar el comando /votemap_cerrar_en al instante
# en vez de esperar hasta 1 hora que tarda la sincronización global de Discord).
GUILD_ID = 1287171299705229434

# Huella única de este proceso (PID + hora de arranque) -- si /seed se
# duplica de nuevo, comparar esta huella entre los dos mensajes va a decir
# si salieron de dos procesos distintos corriendo al mismo tiempo, o de otra cosa.
PROCESS_FINGERPRINT = f"pid{os.getpid()}-{datetime.now(timezone.utc).strftime('%H%M%S')}"

intents = discord.Intents.default()
intents.reactions = True
client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)

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
    message = await channel.send(content=f"@everyone 📢 ¡Nueva votación de mapas! (dura {VOTE_CYCLE_DAYS} días)", embed=embed)
    for _, emoji, _, _ in MAPS:
        await message.add_reaction(emoji)
    state["message_id"] = message.id
    save_state(state)


# Agrupa varias reacciones seguidas en una sola edición real del mensaje, con
# un mínimo de tiempo entre ediciones -- evita el límite normal de velocidad
# de Discord y, sobre todo, el tope de por vida de ediciones a un mensaje de
# más de 1 hora (error 30046), que se agotaba rápido editando en cada reacción.
_refresh_lock = asyncio.Lock()
_refresh_pending = False
_refresh_min_interval = 2.0  # segundos mínimos entre ediciones reales
_last_refresh_at = 0.0


async def refresh_poll_message(channel: discord.TextChannel):
    global _refresh_pending, _last_refresh_at

    if not state.get("message_id"):
        return

    if _refresh_lock.locked():
        # Ya hay una edición en curso/programada -- marca que hace falta otra
        # pasada más, y listo. No dispara un edit nuevo por cada reacción.
        _refresh_pending = True
        return

    async with _refresh_lock:
        while True:
            _refresh_pending = False
            elapsed = time.monotonic() - _last_refresh_at
            if elapsed < _refresh_min_interval:
                await asyncio.sleep(_refresh_min_interval - elapsed)
            try:
                message = await channel.fetch_message(state["message_id"])
                await message.edit(embed=build_embed(state))
            except discord.NotFound:
                return
            except discord.HTTPException as error:
                print(f"No se pudo actualizar el mensaje de votemap: {error}")
            _last_refresh_at = time.monotonic()
            if not _refresh_pending:
                break


async def rebuild_state_from_channel(channel: discord.TextChannel) -> dict | None:
    """
    Busca el último mensaje de encuesta que mandó el bot en el canal y reconstruye
    el estado (fecha de cierre + votos) leyendo el footer y las reacciones reales
    del mensaje. Sirve como respaldo si se pierde mapvote_state.json.

    Se identifica el mensaje correcto solo por el "state:" tag del footer (no por
    el título del embed), para que cambiar el texto del título/descripción en el
    futuro nunca rompa la reconstrucción de una votación en curso.
    """
    async for message in channel.history(limit=100):
        if message.author.id != client.user.id or not message.embeds:
            continue
        embed = message.embeds[0]
        footer_text = embed.footer.text or ""
        if "state:" not in footer_text:
            continue

        try:
            payload = footer_text.split("state:", 1)[1]
            closes_iso, closed_flag = payload.split("|")
        except ValueError:
            continue

        votes = {emoji: [] for _, emoji, _, _ in MAPS}
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
    print(f"Conectado como {client.user} -- proceso {PROCESS_FINGERPRINT}")

    # Arranca el servidor de webhooks PRIMERO QUE NADA -- Railway chequea
    # periódicamente si el servicio responde en este puerto. Si el chequeo le
    # pega antes de que el puerto esté escuchando (algo que puede pasar si
    # esto arranca al final, después de la reconstrucción de votemap que
    # tarda por los límites de velocidad de Discord), Railway asume que el
    # servicio está caído y lo reinicia -- en bucle, sin dejarlo estabilizar
    # nunca.
    await vip_shop.start_webhook_server()

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
    roster_loop.start()
    vip_shop.setup_vip_commands(tree, client, GUILD_ID, VIP_LOG_CHANNEL_ID)
    roster_signup.setup_roster_commands(tree, client, GUILD_ID)
    await roster_signup.register_persistent_views(client)
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print("Comandos / sincronizados")


@tree.command(
    name="votemap_cerrar_en",
    description="Cambia cuándo cierra la votación activa, sin resetear los votos.",
    guild=discord.Object(id=GUILD_ID),
)
@discord.app_commands.describe(dias="En cuántos días (puede ser decimal, ej. 0.5 para 12hs) cierra la votación a partir de ahora")
@discord.app_commands.checks.has_permissions(manage_guild=True)
async def votemap_cerrar_en(interaction: discord.Interaction, dias: float):
    if not state.get("message_id") or state.get("closed"):
        await interaction.response.send_message(
            "No hay una votación activa en este momento.", ephemeral=True
        )
        return
    if dias <= 0:
        await interaction.response.send_message(
            "El número de días tiene que ser mayor a 0.", ephemeral=True
        )
        return

    nueva_fecha = datetime.now(timezone.utc) + timedelta(days=dias)
    state["voting_closes_at"] = nueva_fecha.isoformat()
    save_state(state)

    channel = client.get_channel(CHANNEL_ID)
    await refresh_poll_message(channel)

    fecha_str = nueva_fecha.strftime("%d/%m/%Y %H:%M UTC")
    await interaction.response.send_message(
        f"Listo — la votación activa ahora cierra el **{fecha_str}**. Los votos ya puestos se mantienen.",
        ephemeral=True,
    )


@votemap_cerrar_en.error
async def votemap_cerrar_en_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
    if isinstance(error, discord.app_commands.MissingPermissions):
        await interaction.response.send_message(
            "Este comando es solo para administradores del servidor.", ephemeral=True
        )
    else:
        await interaction.response.send_message(f"Ocurrió un error: {error}", ephemeral=True)


# ID del canal público de VIP (distinto del canal de log de compras VIP_LOG_CHANNEL_ID)
VIP_CHANNEL_ID = 1504883762670997635

# Enfriamiento de /seed: si se corre de nuevo dentro de esta ventana, la
# segunda vez no publica nada. Cubre el caso sin importar la causa exacta del
# duplicado (dos personas casi al mismo tiempo, un reinicio a destiempo, etc.)
SEED_COOLDOWN_SECONDS = 300  # 5 minutos
_last_seed_at: datetime | None = None


@tree.command(
    name="seed",
    description="Avisa a todo el servidor que arrancó el seedeo, con los links de votemap y VIP.",
    guild=discord.Object(id=GUILD_ID),
)
async def seed(interaction: discord.Interaction):
    global _last_seed_at
    now = datetime.now(timezone.utc)

    # Chequea Y marca el cooldown ANTES de cualquier otra cosa (sin await en
    # el medio), para que dos invocaciones casi simultáneas no pasen ambas.
    if _last_seed_at and (now - _last_seed_at).total_seconds() < SEED_COOLDOWN_SECONDS:
        restante = int(SEED_COOLDOWN_SECONDS - (now - _last_seed_at).total_seconds())
        await interaction.response.send_message(
            f"Ya se avisó hace poco -- esperá {restante}s antes de volver a usar /seed.", ephemeral=True
        )
        return
    _last_seed_at = now
    print(f"[seed] Publicando desde el proceso {PROCESS_FINGERPRINT} (interacción {interaction.id})")

    contenido = (
        "@everyone 🌱 ¡Arrancamos Seedeando en Asado & Vino!\n\n"
        "🔗 Entrá al detalle del server: https://hllrecords.com/asado\n\n"
        f"🗺️ Recordá que ya podés votar la rotación de mapas de la semana en <#{CHANNEL_ID}>\n"
        f"⭐ Y también podés comprar tu VIP en <#{VIP_CHANNEL_ID}>"
    )
    try:
        await interaction.response.send_message(contenido)
    except discord.HTTPException:
        return


@seed.error
async def seed_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
    await interaction.response.send_message(f"Ocurrió un error: {error}", ephemeral=True)


@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == client.user.id:
        return

    if payload.channel_id == CHANNEL_ID and payload.message_id == state.get("message_id") and not state.get("closed"):
        emoji_key = str(payload.emoji)
        if emoji_key in state["votes"]:
            guild = client.get_guild(payload.guild_id)
            name = await get_display_name(guild, payload.user_id)
            entry = f"{payload.user_id}:{name}"
            if entry not in state["votes"][emoji_key]:
                state["votes"][emoji_key].append(entry)
            save_state(state)
            channel = client.get_channel(payload.channel_id)
            await refresh_poll_message(channel)
            return  # ya se resolvió como voto de mapas

    await roster_signup.handle_reaction_add(payload)


@client.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.channel_id == CHANNEL_ID and payload.message_id == state.get("message_id") and not state.get("closed"):
        emoji_key = str(payload.emoji)
        if emoji_key in state["votes"]:
            guild = client.get_guild(payload.guild_id)
            name = await get_display_name(guild, payload.user_id)
            entry = f"{payload.user_id}:{name}"
            if entry in state["votes"][emoji_key]:
                state["votes"][emoji_key].remove(entry)
                save_state(state)
            channel = client.get_channel(payload.channel_id)
            await refresh_poll_message(channel)
            return  # ya se resolvió como voto de mapas

    await roster_signup.handle_reaction_remove(payload)


@tasks.loop(seconds=30)
async def roster_loop():
    await roster_signup.roster_check_loop()


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

    # Aplica la rotación en el CRCON, en el orden W-W-W-O-W-W-W-O (no por votos)
    map_ids = build_rotation_order(state)
    result_msg = await apply_rotation_to_crcon(map_ids)
    print(result_msg)

    # Anuncio dedicado con la rotación activa de la semana, agrupada por categoría,
    # para que quede como referencia clara aunque la encuesta nueva se postee arriba.
    warfare_lines = [
        f"{emoji} {name} — {votes} voto{'s' if votes != 1 else ''}"
        for name, emoji, crcon_id, votes in top_maps
        if any(m[0] == name and m[3] == "warfare" for m in MAPS)
    ]
    offensive_lines = [
        f"{emoji} {name} — {votes} voto{'s' if votes != 1 else ''}"
        for name, emoji, crcon_id, votes in top_maps
        if any(m[0] == name and m[3] == "offensive" for m in MAPS)
    ]
    announce = discord.Embed(
        title="🗺️ Rotación activa",
        description=result_msg,
        color=EMBED_COLOR,
    )
    announce.add_field(name=f"⚔️ Warfare ({len(warfare_lines)})", value="\n".join(warfare_lines) or "—", inline=False)
    announce.add_field(name=f"🎯 Offensive ({len(offensive_lines)})", value="\n".join(offensive_lines) or "—", inline=False)
    try:
        await channel.send(embed=announce)
    except Exception:
        pass

    # Borra el mensaje de la encuesta que acaba de cerrar (ya quedó resumida en el
    # anuncio de arriba), para no acumular encuestas viejas en el canal.
    if state.get("message_id"):
        try:
            old_message = await channel.fetch_message(state["message_id"])
            await old_message.delete()
        except Exception:
            pass

    # Arma la próxima encuesta de la semana siguiente
    await post_new_poll(channel)


if __name__ == "__main__":
    client.run(BOT_TOKEN)
