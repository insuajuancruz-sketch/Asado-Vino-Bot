# Bot de Votación de Mapas — Asado & Vino

Bot de Discord que postea una encuesta semanal de "Mapa de la semana" para Hell Let Loose, cuenta los votos en vivo por reacciones, y cierra/reabre automáticamente cada semana.

Repo: `insuajuancruz-sketch/Asado-Vino-Bot` (GitHub)
Hosting: Railway (proyecto conectado al repo, deploy automático en cada push a `main`)

---

## 1. Archivos del proyecto

| Archivo | Para qué sirve |
|---|---|
| `mapvote_bot.py` | Todo el código del bot. |
| `requirements.txt` | Dependencias de Python (`discord.py`). Railway lo usa para instalar paquetes. |
| `railpack.json` | Le dice a Railway qué comando correr para arrancar el bot (`python mapvote_bot.py`). **Es obligatorio** — sin este archivo, Railway no sabe cómo iniciar un proyecto Python que no es un framework web (Flask/Django/etc.) y el build falla con "No start command detected". |
| `Procfile` | Quedó del primer intento de fix. Railway ya no lo usa (usa `railpack.json` en su lugar), pero no molesta si se deja. |
| `mapvote_state.json` | Se genera solo en tiempo de ejecución (no está en el repo). Guarda el estado actual de la encuesta: mensaje, fechas, votos. Si Railway reinicia el proceso, el bot lo lee para retomar donde quedó. |

---

## 2. Configuración (dentro de `mapvote_bot.py`)

Todo se edita al principio del archivo, en el bloque `CONFIGURACIÓN`:

| Variable | Qué controla |
|---|---|
| `BOT_TOKEN` | Se lee de la variable de entorno `DISCORD_BOT_TOKEN` (configurada en Railway → Variables). No se pega el token directo en el código. |
| `CHANNEL_ID` | Canal donde se postea y actualiza la encuesta. Actual: `1544782617940074587` (`#votemap`). |
| `BANNER_URL` | URL de la imagen grande al pie del embed (banner "Asado & Vino"). |
| `AUTHOR_ICON_URL` | URL del logo chico que aparece junto al título del embed. |
| `MAPS` | Lista de mapas candidatos: `(nombre, emoji)`. El orden acá define el orden en el embed. |
| `VOTING_WINDOW_HOURS` | Cuántas horas dura la votación abierta. |
| `HOURS_BEFORE_MATCH_TO_POST` | Cuánto antes del match se calcula el cierre de la votación. |
| `MATCH_WEEKDAY` | Día de la semana del match (0=lunes … 6=domingo). |
| `MATCH_HOUR_UTC` / `MATCH_MINUTE_UTC` | Hora del match, en UTC. Ojo: Argentina está a UTC-3, así que para que el match sea a las 22:00 hora Argentina, `MATCH_HOUR_UTC` debe ser `1` (del día siguiente) — conviene probarlo y ajustar si el horario mostrado no coincide con lo esperado. |

Cualquier cambio a estas variables requiere:
1. Editar `mapvote_bot.py` (local o directo en GitHub)
2. Subir el cambio al repo (commit)
3. Railway redeploya solo — si no, forzar manualmente desde **Deployments → Redeploy**

---

## 3. Cómo funciona (ciclo semanal)

1. **Arranque**: si existe `mapvote_state.json` de una ejecución anterior, retoma esa encuesta. Si no, arma una nueva.
2. **Encuesta nueva**: calcula la fecha del próximo match según `MATCH_WEEKDAY/HOUR`, calcula cuándo cierra la votación, postea el embed en `CHANNEL_ID` con los mapas de `MAPS`, y agrega las reacciones automáticamente.
3. **Votos en vivo**: cada reacción agregada/quitada en ese mensaje actualiza el conteo y reescribe el embed al instante.
4. **Cierre automático**: un chequeo corre cada 30 segundos; cuando se cumple la hora de cierre, determina el mapa más votado, edita el mensaje mostrando el ganador arriba, y postea automáticamente la encuesta de la semana siguiente.

Todo esto corre solo, sin intervención manual, mientras el proceso siga vivo en Railway.

---

## 4. Deploy en Railway — pasos ya hechos (referencia)

1. Cuenta de Discord Developer: app "Asado & Vino" creada, bot generado.
2. Bot invitado al servidor vía OAuth2 URL Generator, con permisos: Send Messages, Manage Messages, Add Reactions, Embed Links, Read Message History.
3. Repo de GitHub `Asado-Vino-Bot` con `mapvote_bot.py`, `requirements.txt`, `railpack.json`.
4. Proyecto en Railway conectado a ese repo (deploy automático en cada push).
5. Variable de entorno `DISCORD_BOT_TOKEN` cargada en Railway → pestaña **Variables**.
6. Start Command confirmado en **Settings → Deploy**: `python mapvote_bot.py` (redundante con `railpack.json`, pero no molesta tenerlo en ambos lados).

### Cómo hacer un cambio de configuración de ahora en adelante

1. Editar `mapvote_bot.py` en GitHub (ícono de lápiz sobre el archivo) o subir una versión nueva con "Upload files".
2. Commit changes.
3. Railway detecta el push y redeploya solo en unos segundos. Confirmar en la pestaña **Deployments** que el build terminó en verde.
4. Si no redeploya solo, forzar manualmente: **Deployments → (⋮) → Redeploy**.

---

## 5. Seguridad — rotación del token

El token del bot se pegó en el chat de esta conversación en algún momento durante el setup. Se recomienda:
1. Ir a Discord Developer Portal → aplicación "Asado & Vino" → **Bot**.
2. **Reset Token** → genera uno nuevo e invalida el anterior.
3. Actualizar la variable `DISCORD_BOT_TOKEN` en Railway → Variables con el nuevo valor.
4. Railway redeploya solo al guardar la variable.

Hacer esto no rompe nada del lado de Discord (el bot sigue siendo el mismo, con el mismo ID e invitación) — solo cambia la credencial de conexión.

---

## 6. Troubleshooting — problemas ya resueltos

**"No start command detected" en el build de Railway**
Causa: Railway usa Railpack (no Nixpacks/Procfile clásico) para detectar cómo arrancar proyectos Python. Sin un framework web reconocido (Flask/Django/etc.), necesita que se le indique el comando explícitamente.
Solución: archivo `railpack.json` en la raíz del repo con:
```json
{
  "$schema": "https://schema.railpack.com",
  "deploy": {
    "startCommand": "python mapvote_bot.py"
  }
}
```

**El bot sigue posteando en el canal viejo después de cambiar `CHANNEL_ID`**
Causa: Railway no redeployó el cambio más reciente (quedó corriendo la versión anterior del código).
Solución: confirmar en GitHub que el archivo tiene el valor correcto, y forzar **Redeploy** manual en Railway si no lo hizo solo.

**Mensaje duplicado en dos canales tras un cambio de canal**
Es esperable brevemente: el mensaje viejo en el canal anterior queda "congelado" (no se borra solo). Borrarlo a mano una vez confirmado que el nuevo funciona en el canal correcto.

---

## 7. Preguntas frecuentes

**¿Qué pasa si Railway reinicia el bot (deploy, caída, etc.) a mitad de una votación en curso?**
No se pierde nada — al arrancar, el bot lee `mapvote_state.json` y retoma la encuesta activa, con los votos que ya había.

**¿Cómo agrego o saco un mapa de la lista?**
Editar la lista `MAPS` en `mapvote_bot.py` (agregar o quitar una tupla `("Nombre", "emoji")`), subir el cambio. Aplica recién en la próxima encuesta que se genere (no reordena la que ya está publicada).

**¿Cómo cambio el día/hora del match?**
Editar `MATCH_WEEKDAY`, `MATCH_HOUR_UTC`, `MATCH_MINUTE_UTC`. Aplica en la próxima encuesta que se genere automáticamente tras el cierre actual.

**¿El bot depende del CRCON o del servidor de HLL de alguna forma?**
No. Es completamente independiente — solo interactúa con Discord. Corre en Railway, no en el VPS del CRCON.
