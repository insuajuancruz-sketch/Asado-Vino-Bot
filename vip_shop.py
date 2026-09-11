"""
Tienda de VIP para Asado & Vino — automatiza el proceso que hoy se hace a mano
(ticket -> pasar datos de pago -> recibir comprobante -> aplicar VIP en CRCON).

Flujo automatizado:
    1. El usuario corre /comprar_vip en Discord: elige método de pago (Mercado
       Pago o PayPal), cuántos meses quiere (mínimo 1, sin límite),
    2. El bot genera un link de pago personalizado y se lo manda por privado.
    3. El usuario paga en ese link -- no hace falta mandar ningún comprobante.
    4. Mercado Pago / PayPal le avisan al bot (webhook) que el pago se aprobó,
       de forma verificable (no se puede falsificar como una captura de pantalla).
    5. El bot llama a la API del CRCON (POST /api/add_vip) y le confirma al
       usuario por privado que ya tiene el VIP activo.

Requisitos nuevos (ademas de los que ya tiene mapvote_bot.py):
    pip install aiohttp   (ya viene con discord.py, no hace falta instalar aparte)

Setup necesario ANTES de que esto funcione de verdad:
    1. Cuenta de Mercado Pago vendedor -> https://www.mercadopago.com.ar/developers/panel
       -> "Tus integraciones" -> Crear aplicación -> copiar el Access Token de PRODUCCIÓN.
       Configurar también un "Webhook secret" ahí mismo (Integrations -> Webhooks).
    2. Cuenta de PayPal Business -> https://developer.paypal.com/dashboard/
       -> Apps & Credentials -> Create App -> copiar Client ID y Client Secret.
       Crear un Webhook ahí apuntando a <PUBLIC_BASE_URL>/webhooks/paypal, evento
       "CHECKOUT.ORDER.APPROVED" -> copiar el Webhook ID que te da.
    3. En Railway: Settings del servicio -> Networking -> "Generate Domain".
       Esto le da al bot una URL pública (https://tu-app.up.railway.app) -- sin
       esto, ni Mercado Pago ni PayPal pueden avisarle al bot que un pago se aprobó.
    4. Cargar todas las variables de entorno de la sección CONFIGURACIÓN de abajo
       en Railway -> Variables.

Sin estos 4 pasos, el bot sigue funcionando para todo lo demás (votemap), pero
/comprar_vip va a avisar que la tienda no está configurada todavía, en vez de
fallar en silencio.
"""

import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import aiohttp
import aiohttp.web
import discord

# =========================================================================
# CONFIGURACIÓN — variables de entorno (Railway -> Variables)
# =========================================================================

# URL pública que Railway le asigna al servicio (Settings -> Networking -> Generate Domain)
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "")  # ej: https://tu-app.up.railway.app
PORT = int(os.environ.get("PORT", "8080"))

MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN", "")
MP_WEBHOOK_SECRET = os.environ.get("MP_WEBHOOK_SECRET", "")

PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
PAYPAL_WEBHOOK_ID = os.environ.get("PAYPAL_WEBHOOK_ID", "")
PAYPAL_API_BASE = "https://api-m.paypal.com"  # sandbox: https://api-m.sandbox.paypal.com

# Mismos datos que en mapvote_bot.py -- se pisan si ya están definidos ahí,
# esto es solo un fallback por si este archivo se corre suelto.
CRCON_BASE_URL = os.environ.get("CRCON_BASE_URL", "http://152.53.39.31:8010")
CRCON_API_TOKEN = os.environ.get("CRCON_API_TOKEN", "")

# Precio por mes (30 días) de VIP. Se compra en múltiplos de 1 mes, sin techo
# (1, 2, 3... meses), mínimo 1 mes.
DAYS_PER_MONTH = 30
PRICE_ARS_PER_MONTH = 4000
PRICE_USD_PER_MONTH = 2.5

PURCHASES_FILE = "/data/vip_purchases.json" if os.path.isdir("/data") else "vip_purchases.json"


# =========================================================================
# Estado de compras pendientes/procesadas
# =========================================================================

def load_purchases() -> dict:
    if os.path.exists(PURCHASES_FILE):
        with open(PURCHASES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_purchases(purchases: dict):
    with open(PURCHASES_FILE, "w", encoding="utf-8") as f:
        json.dump(purchases, f, ensure_ascii=False, indent=2)


def create_pending_purchase(discord_user_id: int, player_id: str, player_name: str, meses: int, metodo: str) -> str:
    purchases = load_purchases()
    token = uuid.uuid4().hex
    purchases[token] = {
        "discord_user_id": discord_user_id,
        "player_id": player_id,
        "player_name": player_name,
        "meses": meses,
        "days": meses * DAYS_PER_MONTH,
        "metodo": metodo,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    save_purchases(purchases)
    return token


# =========================================================================
# Integración CRCON — aplicar el VIP de verdad
# =========================================================================

async def grant_vip(player_id: str, days: int, description: str) -> tuple[bool, str]:
    """Llama a POST /api/add_vip. Devuelve (ok, mensaje)."""
    if not CRCON_API_TOKEN:
        return False, "Falta CRCON_API_TOKEN configurado."

    expiration = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = {
        "Authorization": f"Bearer {CRCON_API_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(
                f"{CRCON_BASE_URL}/api/add_vip",
                json={"player_id": player_id, "description": description, "expiration": expiration},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
                if resp.status == 200 and not data.get("failed"):
                    return True, f"VIP otorgado hasta {expiration}"
                return False, f"CRCON respondió con error: {data}"
    except Exception as error:
        return False, f"No se pudo conectar con el CRCON: {error}"


async def finalize_purchase(token: str, client: discord.Client, vip_channel_id: int | None = None):
    """Se llama una vez confirmado el pago: aplica el VIP y avisa al usuario."""
    purchases = load_purchases()
    purchase = purchases.get(token)
    if not purchase or purchase["status"] != "pending":
        return  # ya procesado antes, o token desconocido -- evita duplicar VIP

    nombre = purchase.get("player_name") or purchase["player_id"]

    ok, msg = await grant_vip(
        purchase["player_id"],
        purchase["days"],
        description=f"VIP {nombre} — {purchase['days']} días — {purchase['metodo']}",
    )

    purchase["status"] = "completed" if ok else "failed"
    purchase["result"] = msg
    purchases[token] = purchase
    save_purchases(purchases)

    try:
        user = await client.fetch_user(purchase["discord_user_id"])
        if ok:
            await user.send(
                f"✅ ¡Gracias por tu compra, {nombre}! Tu VIP de **{purchase['days']} días** ya está activo "
                f"en el servidor (player_id `{purchase['player_id']}`)."
            )
        else:
            await user.send(
                f"⚠️ Tu pago se recibió correctamente, pero hubo un problema aplicando el VIP "
                f"automáticamente ({msg}). Un admin lo va a revisar a mano en breve."
            )
    except Exception:
        pass

    if vip_channel_id:
        try:
            channel = client.get_channel(vip_channel_id)
            if channel:
                estado = "✅ aplicado" if ok else f"⚠️ FALLÓ ({msg})"
                await channel.send(
                    f"💳 Compra de VIP procesada — <@{purchase['discord_user_id']}> · **{nombre}** · "
                    f"{purchase['days']} días · player_id `{purchase['player_id']}` · {estado}"
                )
        except Exception:
            pass


# =========================================================================
# Mercado Pago
# =========================================================================

async def mp_create_preference(token: str, meses: int) -> str | None:
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}", "Content-Type": "application/json"}
    body = {
        "items": [{
            "title": f"VIP {meses} mes{'es' if meses != 1 else ''} — Asado & Vino",
            "quantity": 1,
            "unit_price": float(meses * PRICE_ARS_PER_MONTH),
            "currency_id": "ARS",
        }],
        "external_reference": token,
        "notification_url": f"{PUBLIC_BASE_URL}/webhooks/mercadopago",
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.post(
            "https://api.mercadopago.com/checkout/preferences", json=body, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            data = await resp.json()
            if resp.status not in (200, 201):
                print(f"Error creando preferencia MP: {data}")
                return None
            return data.get("init_point")


def mp_verify_signature(request_headers, query_params) -> bool:
    """Valida la firma HMAC que manda Mercado Pago en el header x-signature."""
    if not MP_WEBHOOK_SECRET:
        return True  # sin secret configurado todavía, no se puede validar -- ver aviso en logs
    signature_header = request_headers.get("x-signature", "")
    request_id = request_headers.get("x-request-id", "")
    data_id = query_params.get("data.id") or query_params.get("id") or ""

    parts = dict(p.split("=", 1) for p in signature_header.split(",") if "=" in p)
    ts = parts.get("ts", "")
    v1 = parts.get("v1", "")
    manifest = f"id:{data_id};request-id:{request_id};ts:{ts};"
    computed = hmac.new(MP_WEBHOOK_SECRET.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, v1)


async def mp_get_payment(payment_id: str) -> dict | None:
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(
            f"https://api.mercadopago.com/v1/payments/{payment_id}", timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status != 200:
                return None
            return await resp.json()


# =========================================================================
# PayPal
# =========================================================================

async def paypal_get_access_token() -> str | None:
    auth = aiohttp.BasicAuth(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET)
    async with aiohttp.ClientSession(auth=auth) as session:
        async with session.post(
            f"{PAYPAL_API_BASE}/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return data.get("access_token")


async def paypal_create_order(token: str, meses: int) -> str | None:
    access_token = await paypal_get_access_token()
    if not access_token:
        return None
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    body = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "custom_id": token,
            "description": f"VIP {meses} mes{'es' if meses != 1 else ''} — Asado & Vino",
            "amount": {"currency_code": "USD", "value": f"{meses * PRICE_USD_PER_MONTH:.2f}"},
        }],
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.post(
            f"{PAYPAL_API_BASE}/v2/checkout/orders", json=body, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            data = await resp.json()
            if resp.status not in (200, 201):
                print(f"Error creando orden PayPal: {data}")
                return None
            return next((l["href"] for l in data.get("links", []) if l.get("rel") == "approve"), None)


async def paypal_capture_order(order_id: str) -> bool:
    access_token = await paypal_get_access_token()
    if not access_token:
        return False
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.post(
            f"{PAYPAL_API_BASE}/v2/checkout/orders/{order_id}/capture",
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            return resp.status in (200, 201)


async def paypal_verify_webhook(request_headers, body_bytes: bytes) -> bool:
    """Usa la propia API de PayPal para validar la firma del webhook (mas simple
    y mas confiable que reimplementar la verificación criptográfica a mano)."""
    if not PAYPAL_WEBHOOK_ID:
        return True  # sin webhook id configurado todavía -- ver aviso en logs
    access_token = await paypal_get_access_token()
    if not access_token:
        return False
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    payload = {
        "auth_algo": request_headers.get("paypal-auth-algo"),
        "cert_url": request_headers.get("paypal-cert-url"),
        "transmission_id": request_headers.get("paypal-transmission-id"),
        "transmission_sig": request_headers.get("paypal-transmission-sig"),
        "transmission_time": request_headers.get("paypal-transmission-time"),
        "webhook_id": PAYPAL_WEBHOOK_ID,
        "webhook_event": json.loads(body_bytes),
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.post(
            f"{PAYPAL_API_BASE}/v1/notifications/verify-webhook-signature",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json()
            return data.get("verification_status") == "SUCCESS"


# =========================================================================
# Comandos de Discord — /comprar_vip y /historial_compras
# =========================================================================

def setup_vip_commands(tree: discord.app_commands.CommandTree, client: discord.Client, guild_id: int, vip_channel_id: int | None = None):
    metodo_choices = [
        discord.app_commands.Choice(name="Mercado Pago", value="mercadopago"),
        discord.app_commands.Choice(name="PayPal", value="paypal"),
    ]

    @tree.command(name="comprar_vip", description="Comprá tu VIP para el servidor", guild=discord.Object(id=guild_id))
    @discord.app_commands.describe(
        metodo="Con qué método querés pagar",
        meses=f"Cuántos meses de VIP (mínimo 1) — ${PRICE_ARS_PER_MONTH} ARS o ${PRICE_USD_PER_MONTH} USD por mes",
        player_id="Tu Steam ID / player ID — buscalo en https://hllrecords.com/",
        nombre="Tu nombre de jugador (como querés que figure)",
    )
    @discord.app_commands.choices(metodo=metodo_choices)
    async def comprar_vip(
        interaction: discord.Interaction,
        metodo: discord.app_commands.Choice[str],
        meses: int,
        player_id: str,
        nombre: str,
    ):
        if meses < 1:
            await interaction.response.send_message(
                "El mínimo es 1 mes de VIP.", ephemeral=True
            )
            return

        if not PUBLIC_BASE_URL:
            await interaction.response.send_message(
                "⚠️ La tienda de VIP todavía no está configurada del todo (falta la URL pública "
                "del bot). Avisale a un admin.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        token = create_pending_purchase(interaction.user.id, player_id, nombre, meses, metodo.value)

        if metodo.value == "mercadopago":
            if not MP_ACCESS_TOKEN:
                await interaction.followup.send("⚠️ Mercado Pago no está configurado todavía.", ephemeral=True)
                return
            link = await mp_create_preference(token, meses)
            precio_str = f"${meses * PRICE_ARS_PER_MONTH} ARS"
        else:
            if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
                await interaction.followup.send("⚠️ PayPal no está configurado todavía.", ephemeral=True)
                return
            link = await paypal_create_order(token, meses)
            precio_str = f"${meses * PRICE_USD_PER_MONTH:.2f} USD"

        if not link:
            await interaction.followup.send(
                "❌ No se pudo generar el link de pago. Probá de nuevo en un rato, o avisale a un admin.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"💳 **VIP {meses} mes{'es' if meses != 1 else ''} — {precio_str}**\n"
            f"Pagá acá para activarlo automáticamente:\n{link}\n\n"
            f"Apenas se confirme el pago te aviso por acá mismo.",
            ephemeral=True,
        )

    @tree.command(name="historial_compras", description="Ver el historial de compras de VIP", guild=discord.Object(id=guild_id))
    @discord.app_commands.describe(usuario="(Solo admins) ver el historial de otro usuario")
    async def historial_compras(interaction: discord.Interaction, usuario: discord.Member | None = None):
        es_admin = interaction.user.guild_permissions.manage_guild

        if usuario and not es_admin:
            await interaction.response.send_message(
                "Solo un admin puede ver el historial de otra persona.", ephemeral=True
            )
            return

        purchases = load_purchases()

        if es_admin and usuario is None:
            # Admin sin especificar usuario -> ve el historial completo del servidor
            registros = list(purchases.values())
        else:
            filtro_id = usuario.id if usuario else interaction.user.id
            registros = [p for p in purchases.values() if p["discord_user_id"] == filtro_id]

        registros.sort(key=lambda p: p["created_at"], reverse=True)
        registros = registros[:15]  # últimos 15, para no pasarse del límite del embed

        if not registros:
            await interaction.response.send_message("No hay compras registradas.", ephemeral=True)
            return

        estado_emoji = {"completed": "✅", "pending": "⏳", "failed": "❌"}
        lineas = []
        for p in registros:
            ts = int(datetime.fromisoformat(p["created_at"]).timestamp())
            nombre_p = p.get("player_name") or p["player_id"]
            lineas.append(
                f"{estado_emoji.get(p['status'], '❔')} **{nombre_p}** — {p['meses']} mes(es) — "
                f"{p['metodo']} — `{p['player_id']}` — <t:{ts}:d>"
            )

        titulo = "📜 Historial de compras de VIP — todo el servidor" if (es_admin and usuario is None) else "📜 Historial de compras de VIP"
        embed = discord.Embed(
            title=titulo,
            description="\n".join(lineas),
            color=0x3498DB,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    globals()["_vip_channel_id"] = vip_channel_id
    globals()["_discord_client"] = client


# =========================================================================
# Servidor web para recibir los webhooks de MP y PayPal
# =========================================================================

async def handle_mp_webhook(request: aiohttp.web.Request):
    query = dict(request.query)
    if not mp_verify_signature(request.headers, query):
        return aiohttp.web.Response(status=401, text="firma inválida")

    try:
        body = await request.json()
    except Exception:
        body = {}

    payment_id = query.get("data.id") or query.get("id") or (body.get("data") or {}).get("id")
    if not payment_id:
        return aiohttp.web.Response(status=200, text="sin payment_id, ignorado")

    payment = await mp_get_payment(str(payment_id))
    if not payment or payment.get("status") != "approved":
        return aiohttp.web.Response(status=200, text="pago no aprobado todavía")

    token = payment.get("external_reference")
    client = globals().get("_discord_client")
    if token and client:
        await finalize_purchase(token, client, globals().get("_vip_channel_id"))

    return aiohttp.web.Response(status=200, text="ok")


async def handle_paypal_webhook(request: aiohttp.web.Request):
    body_bytes = await request.read()
    if not await paypal_verify_webhook(request.headers, body_bytes):
        return aiohttp.web.Response(status=401, text="firma inválida")

    event = json.loads(body_bytes)
    event_type = event.get("event_type")

    if event_type == "CHECKOUT.ORDER.APPROVED":
        order_id = event["resource"]["id"]
        purchase_units = event["resource"].get("purchase_units", [])
        token = purchase_units[0].get("custom_id") if purchase_units else None

        captured = await paypal_capture_order(order_id)
        if captured and token:
            client = globals().get("_discord_client")
            if client:
                await finalize_purchase(token, client, globals().get("_vip_channel_id"))

    return aiohttp.web.Response(status=200, text="ok")


async def handle_health(request: aiohttp.web.Request):
    return aiohttp.web.Response(status=200, text="ok")


async def start_webhook_server():
    app = aiohttp.web.Application()
    app.router.add_post("/webhooks/mercadopago", handle_mp_webhook)
    app.router.add_post("/webhooks/paypal", handle_paypal_webhook)
    app.router.add_get("/", handle_health)

    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Servidor de webhooks escuchando en el puerto {PORT}")
