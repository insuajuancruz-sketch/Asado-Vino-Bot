"""
Anotación para partidas competitivas — 7DL.

Reemplaza el uso de Apollo (que no tiene API/webhooks) por un sistema propio:
    1. Un oficial abre la anotación con /abrir_anotacion, indicando el nombre
       del evento y cuándo cierra (fecha/hora Argentina).
    2. El bot postea un mensaje con reacciones ✅ (voy) / ❓ (tentativo) / ❌ (no voy).
       Cada persona solo puede tener una reacción activa a la vez (el bot saca
       las otras automáticamente si cambian de opinión).
    3. Al llegar la hora de cierre, el bot:
       - Edita el mensaje mostrando la lista final.
       - Escribe la lista de "Accepted" (✅) en una pestaña nueva ("Anotados")
         del Google Sheet del organigrama -- sin tocar las pestañas existentes
         que arman los oficiales a mano.
       - Avisa en el mismo canal que la anotación cerró y que ya se puede
         armar el roster, con el link directo a la planilla.

Requisitos nuevos:
    pip install gspread google-auth

Setup de Google Sheets (desde cero, es gratis):
    1. Andá a https://console.cloud.google.com/ -> crear un proyecto nuevo
       (cualquier nombre, ej. "7dl-bot").
    2. En ese proyecto: "APIs y servicios" -> "Biblioteca" -> buscar
       "Google Sheets API" -> Habilitar.
    3. "APIs y servicios" -> "Credenciales" -> "Crear credenciales" ->
       "Cuenta de servicio". Nombre: cualquiera (ej. "bot-7dl"). Crear.
    4. Entrá a la cuenta de servicio recién creada -> pestaña "Claves" ->
       "Agregar clave" -> "Crear clave nueva" -> tipo JSON -> Descarga un
       archivo .json.
    5. Abrí ese .json con el Bloc de notas, copiá TODO el contenido, y
       pegalo como el valor de la variable de entorno GOOGLE_SERVICE_ACCOUNT_JSON
       en Railway (todo en una sola variable, tal cual, con las llaves { }).
    6. En el .json vas a ver un campo "client_email" (algo como
       bot-7dl@tu-proyecto.iam.gserviceaccount.com). Copiá esa dirección.
    7. Abrí el Google Sheet del organigrama -> botón "Compartir" -> pegá esa
       dirección de email -> dale permiso de "Editor".
    8. Copiá el ID de la planilla: es la parte de la URL entre "/d/" y
       "/edit", por ejemplo en
       https://docs.google.com/spreadsheets/d/1BgnPKbp6oPQjasASgqtn7-mKL-KjYazJ-3XK1bh7eC0/edit
       el ID es "1BgnPKbp6oPQjasASgqtn7-mKL-KjYazJ-3XK1bh7eC0" -- pegalo en
       ROSTER_SHEET_ID más abajo.
"""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import discord

# =========================================================================
# CONFIGURACIÓN
# =========================================================================

# Contenido completo del JSON de la cuenta de servicio de Google (ver setup arriba)
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

# ID del Google Sheet del organigrama (ver paso 8 del setup)
ROSTER_SHEET_ID = os.environ.get("ROSTER_SHEET_ID", "")

# Nombre de la pestaña nueva donde el bot escribe las listas (se crea sola si no existe)
ROSTER_SHEET_TAB = "Anotados"

# Rol a mencionar en el aviso de cierre (opcional). Poner el ID del rol de
# oficiales/mariscales, o None para no mencionar a nadie en particular.
OFICIALES_ROLE_ID = None  # <-- reemplazar por el ID del rol si querés

# Duración estimada del partido, para calcular el fin del Evento nativo de
# Discord (Discord exige una hora de fin para eventos externos)
MATCH_DURATION_HOURS = 2

# Hora Argentina = UTC-3 todo el año (no tiene horario de verano)
ARG_OFFSET = timedelta(hours=-3)

STATE_FILE = "/data/roster_state.json" if os.path.isdir("/data") else "roster_state.json"

EMOJI_CANCELADO = "❌"
EMOJI_TENTATIVO = "❓"

# Unidades del clan: (nombre a mostrar, emoji/color). Cada persona elige UNA
# sola opción de todo este conjunto (una unidad, o Cancelado, o Tentativo) --
# son todas mutuamente excluyentes entre sí.
UNIDADES = [
    ("PRIMOS", "🟥"),
    ("PICANTE", "🟩"),
    ("FUEGO", "🟨"),
    ("YUTA", "🟧"),
    ("CMD", "🟦"),
    ("WAMO", "🟪"),
    ("BLINDADO", "⬛"),
    ("LATERAL", "🟫"),
    ("DEFENSA", "⬜"),
    ("ARTY", "🔶"),
    ("BREAK LINE", "🔷"),
]

EXCLUSIVE_EMOJIS = {emoji for _, emoji in UNIDADES} | {EMOJI_CANCELADO, EMOJI_TENTATIVO}
ALL_TRACKED_EMOJIS = EXCLUSIVE_EMOJIS  # acá no hay categoría independiente, todo es un solo grupo excluyente

_EMOJI_TO_LABEL = {emoji: nombre for nombre, emoji in UNIDADES}
_EMOJI_TO_LABEL[EMOJI_CANCELADO] = "CANCELADO"
_EMOJI_TO_LABEL[EMOJI_TENTATIVO] = "TENTATIVO"


# =========================================================================
# Estado persistente (varias anotaciones pueden estar abiertas a la vez,
# una por canal/evento -- por eso es un diccionario por message_id)
# =========================================================================

def load_events() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_events(events: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)


# Cache en memoria -- es la fuente de verdad mientras el bot corre. Evita una
# condición de carrera real: si cada reacción recargara el archivo del disco,
# dos eventos que llegan casi al mismo tiempo (por ejemplo, el nuestro propio
# al sacar una reacción exclusiva) podían pisarse la escritura entre sí y
# perder anotados. Se persiste a disco en cada cambio, pero se LEE siempre
# de acá, nunca releyendo el archivo en cada reacción.
_events_cache: dict | None = None


def get_events() -> dict:
    global _events_cache
    if _events_cache is None:
        _events_cache = load_events()
    return _events_cache


def persist_events():
    save_events(_events_cache or {})


def parse_cierre(cierre_str: str) -> datetime | None:
    """Parsea 'DD/MM HH:MM' en hora Argentina y devuelve un datetime en UTC."""
    now = datetime.now(timezone.utc)
    for year in (now.year, now.year + 1):
        try:
            naive = datetime.strptime(f"{cierre_str} {year}", "%d/%m %H:%M %Y")
        except ValueError:
            return None
        local_dt = naive.replace(tzinfo=timezone(ARG_OFFSET))
        utc_dt = local_dt.astimezone(timezone.utc)
        if utc_dt > now - timedelta(hours=1):  # tolera un pequeño margen
            return utc_dt
    return None


# =========================================================================
# Embed del mensaje de anotación
# =========================================================================

def build_embed(event: dict) -> discord.Embed:
    closes_at = datetime.fromisoformat(event["closes_at"])
    closed = event.get("closed", False)

    embed = discord.Embed(
        title=f"📋 Anotación: {event['evento']}",
        color=0x2ECC71 if not closed else 0x808080,
    )
    embed.description = "Reaccioná según tu disponibilidad." if not closed else "La anotación está cerrada."
    embed.add_field(
        name="🔒 Cierra" if not closed else "🔒 Cerró",
        value=f"<t:{int(closes_at.timestamp())}:F> (<t:{int(closes_at.timestamp())}:R>)",
        inline=False,
    )
    if event.get("match_at"):
        match_at = datetime.fromisoformat(event["match_at"])
        embed.add_field(
            name="🎮 Hora del partido",
            value=f"<t:{int(match_at.timestamp())}:F> (<t:{int(match_at.timestamp())}:R>)",
            inline=False,
        )

    def names_list(status: str) -> str:
        entries = event["signups"].get(status, [])
        if not entries:
            return "—"
        return "\n".join(e.split(":", 1)[1] for e in entries)

    for nombre, emoji in UNIDADES:
        count = len(event["signups"].get(emoji, []))
        embed.add_field(name=f"{emoji} {nombre} ({count})", value=names_list(emoji), inline=True)

    embed.add_field(
        name=f"{EMOJI_TENTATIVO} Tentativo ({len(event['signups'].get(EMOJI_TENTATIVO, []))})",
        value=names_list(EMOJI_TENTATIVO), inline=True,
    )
    embed.add_field(
        name=f"{EMOJI_CANCELADO} Cancelado ({len(event['signups'].get(EMOJI_CANCELADO, []))})",
        value=names_list(EMOJI_CANCELADO), inline=True,
    )

    if event.get("image_url"):
        embed.set_image(url=event["image_url"])

    embed.set_footer(text=f"state:{event['closes_at']}|{int(closed)}")
    return embed


# =========================================================================
# Botón + formulario para editar un evento ya creado
# =========================================================================

def _can_edit(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.manage_guild:
        return True
    if OFICIALES_ROLE_ID and any(r.id == OFICIALES_ROLE_ID for r in interaction.user.roles):
        return True
    return False


class EditEventModal(discord.ui.Modal):
    def __init__(self, message_id: int, event: dict):
        super().__init__(title="Editar evento")
        self.message_id = message_id

        closes_local = datetime.fromisoformat(event["closes_at"]).astimezone(timezone(ARG_OFFSET))
        match_local = (
            datetime.fromisoformat(event["match_at"]).astimezone(timezone(ARG_OFFSET))
            if event.get("match_at")
            else None
        )

        self.nombre = discord.ui.TextInput(label="Nombre del evento", default=event["evento"], max_length=100)
        self.cierra = discord.ui.TextInput(label="Cierra anotación (DD/MM HH:MM, ARG)", default=closes_local.strftime("%d/%m %H:%M"))
        self.partido = discord.ui.TextInput(
            label="Hora del partido (DD/MM HH:MM, ARG)",
            default=match_local.strftime("%d/%m %H:%M") if match_local else "",
            required=False,
        )
        self.add_item(self.nombre)
        self.add_item(self.cierra)
        self.add_item(self.partido)

    async def on_submit(self, interaction: discord.Interaction):
        # Responde/reserva la interacción DE INMEDIATO -- Discord exige una
        # respuesta en 3 segundos, y las llamadas de abajo (editar mensaje,
        # actualizar el Evento nativo) pueden tardar más que eso.
        await interaction.response.defer(ephemeral=True)

        new_closes = parse_cierre(self.cierra.value)
        if not new_closes:
            await interaction.followup.send(
                "No pude leer la fecha de cierre, no se guardó ningún cambio.", ephemeral=True
            )
            return

        new_match = parse_cierre(self.partido.value) if self.partido.value.strip() else None

        events = get_events()
        event = events.get(str(self.message_id))
        if not event:
            await interaction.followup.send("Ese evento ya no existe.", ephemeral=True)
            return

        event["evento"] = self.nombre.value
        event["closes_at"] = new_closes.isoformat()
        if new_match:
            event["match_at"] = new_match.isoformat()
        persist_events()

        channel = interaction.client.get_channel(event["channel_id"])
        await _refresh_message(channel, self.message_id, event)

        # Intenta actualizar también el Evento nativo de Discord, si existe
        if event.get("discord_event_id") and new_match:
            try:
                sched = await interaction.guild.fetch_scheduled_event(event["discord_event_id"])
                await sched.edit(
                    name=event["evento"],
                    start_time=new_match,
                    end_time=new_match + timedelta(hours=MATCH_DURATION_HOURS),
                )
            except Exception as error:
                print(f"No se pudo actualizar el Evento nativo de Discord: {error}")

        await interaction.followup.send("✅ Evento actualizado.", ephemeral=True)


class EditEventView(discord.ui.View):
    def __init__(self, message_id: int):
        super().__init__(timeout=None)
        self.message_id = message_id
        button = discord.ui.Button(
            label="✏️ Editar evento",
            style=discord.ButtonStyle.secondary,
            custom_id=f"roster_edit:{message_id}",
        )
        button.callback = self._on_click
        self.add_item(button)

    async def _on_click(self, interaction: discord.Interaction):
        if not _can_edit(interaction):
            await interaction.response.send_message(
                "Solo un admin/oficial puede editar este evento.", ephemeral=True
            )
            return

        events = get_events()
        event = events.get(str(self.message_id))
        if not event:
            await interaction.response.send_message("No encontré este evento.", ephemeral=True)
            return
        if event["closed"]:
            await interaction.response.send_message("Esta anotación ya cerró, no se puede editar.", ephemeral=True)
            return

        await interaction.response.send_modal(EditEventModal(self.message_id, event))


# =========================================================================
# Google Sheets — escritura (sync, se corre en un thread aparte para no
# bloquear el loop de asyncio del bot)
# =========================================================================

def _write_to_sheet_sync(evento: str, cierre_local_str: str, names_by_unit: dict[str, list[str]]) -> tuple[bool, str]:
    if not GOOGLE_SERVICE_ACCOUNT_JSON or not ROSTER_SHEET_ID:
        return False, "Falta configurar Google Sheets (GOOGLE_SERVICE_ACCOUNT_JSON / ROSTER_SHEET_ID)."

    try:
        import gspread
        from google.oauth2.service_account import Credentials

        creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)

        sh = client.open_by_key(ROSTER_SHEET_ID)
        try:
            ws = sh.worksheet(ROSTER_SHEET_TAB)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=ROSTER_SHEET_TAB, rows=500, cols=3)

        existing = ws.get_all_values()
        start_row = len(existing) + 2  # deja una fila en blanco de separador

        rows = [[f"=== {evento} — cerró {cierre_local_str} (ARG) ==="]]
        any_confirmed = False
        for nombre_unidad, unit_names in names_by_unit.items():
            if not unit_names:
                continue
            any_confirmed = True
            rows.append([f"-- {nombre_unidad} --"])
            rows += [[n] for n in unit_names]
        if not any_confirmed:
            rows.append(["(nadie confirmó para ninguna unidad)"])
        ws.update(f"A{start_row}", rows)
        return True, "ok"
    except Exception as error:
        return False, str(error)


async def write_accepted_to_sheet(evento: str, closes_at_utc: datetime, names_by_unit: dict[str, list[str]]) -> tuple[bool, str]:
    cierre_local = closes_at_utc.astimezone(timezone(ARG_OFFSET)).strftime("%d/%m/%Y %H:%M")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _write_to_sheet_sync, evento, cierre_local, names_by_unit)


# =========================================================================
# Cliente / comandos
# =========================================================================

_client: discord.Client | None = None
_member_cache: dict[int, str] = {}


async def _get_display_name(guild: discord.Guild, user_id: int) -> str:
    if user_id in _member_cache:
        return _member_cache[user_id]
    try:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        name = member.display_name
    except discord.NotFound:
        name = f"usuario {user_id}"
    _member_cache[user_id] = name
    return name


def setup_roster_commands(tree: discord.app_commands.CommandTree, client: discord.Client, guild_id: int):
    global _client
    _client = client

    @tree.command(name="abrir_anotacion", description="Abre la anotación para una partida/evento del 7DL", guild=discord.Object(id=guild_id))
    @discord.app_commands.describe(
        evento="Nombre del evento (ej: 7dl vs 360)",
        cierra="Cuándo cierra la anotación, formato DD/MM HH:MM en hora Argentina (ej: 15/09 20:00)",
        hora_partido="Cuándo arranca el partido, formato DD/MM HH:MM en hora Argentina (ej: 15/09 21:00)",
        imagen="Imagen/banner opcional para el evento (subila directo acá)",
        mencionar1="Rol opcional a mencionar/taggear al postear el evento (ej: @Jugadores)",
        mencionar2="Otro rol opcional a mencionar",
        mencionar3="Otro rol opcional a mencionar",
    )
    async def abrir_anotacion(
        interaction: discord.Interaction,
        evento: str,
        cierra: str,
        hora_partido: str,
        imagen: discord.Attachment | None = None,
        mencionar1: discord.Role | None = None,
        mencionar2: discord.Role | None = None,
        mencionar3: discord.Role | None = None,
    ):
        closes_at = parse_cierre(cierra)
        if not closes_at:
            await interaction.response.send_message(
                "No pude entender la fecha de cierre. Usá el formato `DD/MM HH:MM` (ej: `15/09 20:00`), hora Argentina.",
                ephemeral=True,
            )
            return

        match_at = parse_cierre(hora_partido)
        if not match_at:
            await interaction.response.send_message(
                "No pude entender la hora del partido. Usá el formato `DD/MM HH:MM` (ej: `15/09 21:00`), hora Argentina.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        event = {
            "evento": evento,
            "channel_id": interaction.channel_id,
            "closes_at": closes_at.isoformat(),
            "match_at": match_at.isoformat(),
            "closed": False,
            "image_url": imagen.url if imagen else None,
            "discord_event_id": None,
            "signups": {emoji: [] for emoji in ALL_TRACKED_EMOJIS},
        }
        embed = build_embed(event)
        roles_a_mencionar = [r for r in (mencionar1, mencionar2, mencionar3) if r]
        contenido = f"{' '.join(r.mention for r in roles_a_mencionar)} 📋 ¡Nueva anotación abierta!" if roles_a_mencionar else None
        message = await interaction.followup.send(content=contenido, embed=embed, wait=True)
        for emoji in ALL_TRACKED_EMOJIS:
            await message.add_reaction(emoji)

        events = get_events()
        events[str(message.id)] = event
        persist_events()

        # Crea también el Evento nativo de Discord, para que le llegue la
        # notificación automática a quien tenga esa opción activada, y
        # aparezca en la lista de Eventos del servidor.
        try:
            sched = await interaction.guild.create_scheduled_event(
                name=evento,
                description=f"Anotate reaccionando en {message.jump_url}",
                start_time=match_at,
                end_time=match_at + timedelta(hours=MATCH_DURATION_HOURS),
                entity_type=discord.EntityType.external,
                location=evento,
                privacy_level=discord.PrivacyLevel.guild_only,
            )
            event["discord_event_id"] = sched.id
            persist_events()
        except Exception as error:
            print(f"No se pudo crear el Evento nativo de Discord: {error}")

        # Adjunta el botón de editar y lo registra como persistente (sigue
        # funcionando aunque el bot se reinicie).
        view = EditEventView(message.id)
        await message.edit(view=view)
        client.add_view(view, message_id=message.id)

    @tree.command(name="cerrar_anotacion", description="Cierra manualmente una anotación abierta en este canal", guild=discord.Object(id=guild_id))
    async def cerrar_anotacion(interaction: discord.Interaction):
        events = get_events()
        match = next(
            (mid for mid, ev in events.items() if ev["channel_id"] == interaction.channel_id and not ev["closed"]),
            None,
        )
        if not match:
            await interaction.response.send_message("No hay ninguna anotación abierta en este canal.", ephemeral=True)
            return

        events[match]["closes_at"] = datetime.now(timezone.utc).isoformat()
        persist_events()
        await interaction.response.send_message("Cerrando la anotación ahora mismo...", ephemeral=True)


async def _refresh_message(channel: discord.TextChannel, message_id: int, event: dict):
    try:
        message = await channel.fetch_message(message_id)
        view = None if event["closed"] else EditEventView(message_id)
        await message.edit(embed=build_embed(event), view=view)
    except Exception:
        pass


async def register_persistent_views(client: discord.Client):
    """Se llama una vez en on_ready -- vuelve a registrar los botones de
    'Editar evento' de las anotaciones que sigan abiertas, para que sigan
    funcionando después de un redeploy del bot."""
    for message_id, event in get_events().items():
        if not event.get("closed"):
            client.add_view(EditEventView(int(message_id)), message_id=int(message_id))


async def handle_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == _client.user.id:
        return
    emoji_key = str(payload.emoji)
    if emoji_key not in ALL_TRACKED_EMOJIS:
        return

    events = get_events()
    event = events.get(str(payload.message_id))
    if not event or event["closed"]:
        return

    channel = _client.get_channel(payload.channel_id)
    guild = _client.get_guild(payload.guild_id)
    name = await _get_display_name(guild, payload.user_id)
    entry = f"{payload.user_id}:{name}"

    # Actualiza el estado en memoria PRIMERO, antes de tocar Discord -- así,
    # si sacar la reacción vieja dispara un evento recursivo (ver abajo), ese
    # evento va a encontrar el dato ya correcto en vez de pisarlo.
    if emoji_key in EXCLUSIVE_EMOJIS:
        for status in EXCLUSIVE_EMOJIS:
            if entry in event["signups"].get(status, []):
                event["signups"][status].remove(entry)

    event["signups"].setdefault(emoji_key, [])
    if entry not in event["signups"][emoji_key]:
        event["signups"][emoji_key].append(entry)

    persist_events()
    await _refresh_message(channel, payload.message_id, event)

    # Si es una reacción de asistencia (Confirmar/Denegar/Tentativo), mantiene
    # una sola activa por persona -- saca las otras dos en Discord. El
    # Blindaje queda aparte, se puede combinar con cualquier estado.
    if emoji_key in EXCLUSIVE_EMOJIS:
        try:
            message = await channel.fetch_message(payload.message_id)
            member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
            for other_emoji in EXCLUSIVE_EMOJIS - {emoji_key}:
                try:
                    await message.remove_reaction(other_emoji, member)
                except Exception:
                    pass
        except Exception:
            pass


async def handle_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.user_id == _client.user.id:
        return
    emoji_key = str(payload.emoji)
    if emoji_key not in ALL_TRACKED_EMOJIS:
        return

    events = get_events()
    event = events.get(str(payload.message_id))
    if not event or event["closed"]:
        return

    guild = _client.get_guild(payload.guild_id)
    name = await _get_display_name(guild, payload.user_id)
    entry = f"{payload.user_id}:{name}"

    if entry in event["signups"].get(emoji_key, []):
        event["signups"][emoji_key].remove(entry)

    persist_events()
    channel = _client.get_channel(payload.channel_id)
    await _refresh_message(channel, payload.message_id, event)


async def roster_check_loop():
    """Se llama cada 30s desde el bot principal -- cierra las anotaciones vencidas."""
    events = get_events()
    now = datetime.now(timezone.utc)
    changed = False

    for message_id, event in events.items():
        if event["closed"]:
            continue
        closes_at = datetime.fromisoformat(event["closes_at"])
        if now < closes_at:
            continue

        event["closed"] = True
        changed = True
        channel = _client.get_channel(event["channel_id"])
        if not channel:
            continue

        await _refresh_message(channel, int(message_id), event)

        names_by_unit = {
            nombre: [e.split(":", 1)[1] for e in event["signups"].get(emoji, [])]
            for nombre, emoji in UNIDADES
        }
        total_confirmados = sum(len(v) for v in names_by_unit.values())
        ok, msg = await write_accepted_to_sheet(event["evento"], closes_at, names_by_unit)

        sheet_link = f"https://docs.google.com/spreadsheets/d/{ROSTER_SHEET_ID}/edit" if ROSTER_SHEET_ID else ""
        mencion = f"<@&{OFICIALES_ROLE_ID}> " if OFICIALES_ROLE_ID else ""
        resumen_unidades = "\n".join(
            f"• {nombre}: {len(v)}" for nombre, v in names_by_unit.items() if v
        ) or "(nadie confirmó para ninguna unidad)"

        if ok:
            texto = (
                f"{mencion}📋 Cerró la anotación de **{event['evento']}** — "
                f"{total_confirmados} confirmados en total. Ya está la lista por unidad en la planilla, se puede armar el roster.\n"
                f"{resumen_unidades}\n{sheet_link}"
            )
        else:
            texto = (
                f"{mencion}📋 Cerró la anotación de **{event['evento']}** — {total_confirmados} confirmados en total.\n"
                f"⚠️ No se pudo escribir en la planilla automáticamente ({msg}). Por unidad:\n{resumen_unidades}"
            )
        try:
            await channel.send(texto)
        except Exception:
            pass

    if changed:
        persist_events()
