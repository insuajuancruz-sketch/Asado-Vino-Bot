# Bot de Rotación de Mapas — Asado & Vino / 7DL VKR

Bot de Discord que le da a la comunidad el poder de elegir qué mapas se juegan cada semana en el servidor público. La gente vota reaccionando a un mensaje, y el bot aplica automáticamente el resultado en el CRCON — sin que ningún admin tenga que tocar nada a mano.

Repo: `insuajuancruz-sketch/Asado-Vino-Bot` (GitHub, público)
Hosting: Railway (deploy automático en cada cambio subido al repo)
Canal donde vive: `#votemap`

---

## 1. Qué hace, en criollo

1. El bot postea una encuesta con 12 mapas candidatos, cada uno con su emoji.
2. Cualquiera del server puede votar reaccionando con el emoji del mapa que quiere jugar (se puede votar por varios mapas a la vez).
3. El conteo se actualiza solo, en vivo, cada vez que alguien vota o saca su voto.
4. Cuando llega el horario de cierre (una vez por semana), el bot:
   - Toma los **8 mapas más votados** de los 12 candidatos.
   - Los carga automáticamente en la rotación real del servidor (vía la API del CRCON) — saca lo que estaba antes y pone los 8 nuevos.
   - Edita el mensaje mostrando el resultado ("🏆 Rotación resultante") con el ranking.
   - Postea una encuesta nueva para la semana siguiente, con `@everyone` para avisarle a todo el server.

Todo el ciclo se repite solo, semana tras semana, sin que un admin tenga que entrar al panel del CRCON a cambiar la rotación a mano.

---

## 2. Qué puede hacer un admin del clan (sin tocar código)

- **Nada manual es necesario para el funcionamiento normal.** El bot corre solo.
- **Si querés forzar que la votación cierre antes de tiempo:** no hay un botón para esto todavía — hay que esperar al horario configurado, o pedir el ajuste de código (ver sección 5).
- **Si el bot deja de responder o se cae:** Railway lo reinicia solo (tiene reinicio automático configurado). Si algo persiste, avisar para revisar el log en Railway.
- **Si votaste mal o te arrepentiste:** sacá tu reacción y volvé a poner la correcta — el conteo se ajusta solo.

---

## 3. Los 12 mapas candidatos actuales

| Mapa | Emoji | Modo |
|---|---|---|
| Carentan | 🏠 | Warfare |
| Omaha Beach | 🌊 | Warfare |
| Utah Beach | 🪖 | Warfare |
| St. Mere Eglise | ⛪ | Warfare |
| St. Marie Du Mont | 🏘️ | Warfare |
| Foy | ❄️ | Warfare |
| Hurtgen Forest | 🌲 | Warfare |
| Hill 400 | ⛰️ | Warfare |
| Purple Heart Lane | 🌧️ | Warfare |
| Driel | 🌷 | Warfare |
| Remagen | 🌉 | Offensive (US) |
| Kursk | 🐻 | Offensive (RUS) |

De estos 12, los **8 más votados** cada semana pasan a ser la rotación real del servidor. Si menos de 8 mapas reciben al menos un voto, la rotación queda con menos de 8 (no se rellena con mapas sin votos).

**Para cambiar esta lista** (agregar, sacar, o cambiar algún mapa/modo/emoji) hace falta editar el código — ver sección 5.

---

## 4. Cronograma

- **Cierra la votación:** martes a las 22:00 UTC (ajustar según zona horaria local del clan si hace falta — UTC no es la misma hora en Argentina).
- **Se repite:** cada semana, automáticamente, sin intervención.

---

## 5. Cambios que requieren tocar el código (para quien mantenga el bot)

Cualquiera de estos cambios se hace en 3 pasos siempre:
1. Editar `mapvote_bot.py` en GitHub (ícono de lápiz sobre el archivo, o "Upload files" para reemplazarlo entero).
2. Commit changes.
3. Railway redeploya solo en unos segundos (confirmar en la pestaña **Deployments** que terminó en verde). Si no redeploya solo, forzar manualmente: **Deployments → (⋮) → Redeploy**.

| Qué cambiar | Dónde en el código |
|---|---|
| Agregar/sacar un mapa candidato | Lista `MAPS` |
| Cambiar cuántos mapas entran en la rotación (hoy 8) | `ROTATION_SIZE` |
| Cambiar el día/hora de cierre semanal | `CLOSE_WEEKDAY`, `CLOSE_HOUR_UTC`, `CLOSE_MINUTE_UTC` |
| Cambiar el canal donde se postea | `CHANNEL_ID` |
| Cambiar el banner o el logo del embed | `BANNER_URL`, `AUTHOR_ICON_URL` |
| Sacar el `@everyone` de la encuesta nueva | Línea `content="@everyone ..."` en `post_new_poll()` |

---

## 6. Variables de entorno (en Railway → Variables, nunca en el código)

| Variable | Para qué |
|---|---|
| `DISCORD_BOT_TOKEN` | Conecta el bot a Discord. |
| `CRCON_API_TOKEN` | Le permite al bot aplicar la rotación en el servidor. Se genera en el Django Admin del CRCON (`/admin` → Django API Keys → Add). |

Si alguna vez hay que rotar/renovar estos tokens (por ejemplo, si se filtró alguno), se cambia acá y Railway redeploya solo — no hace falta tocar código.

---

## 7. Qué pasa si algo falla en la integración con el CRCON

El bot está armado para que, si la conexión al CRCON falla por cualquier motivo (token vencido, servidor caído, error de red), **la parte de Discord siga funcionando igual** — la encuesta se sigue votando y cerrando normal. Lo único que no pasa es la aplicación automática de la rotación, y el bot **avisa en el canal** con un mensaje de error o advertencia en vez de fallar en silencio.

Si eso pasa, la rotación se puede cargar a mano desde el panel del CRCON (Settings → Maps → Rotation) usando el ranking que quedó publicado en el mensaje de resultado.

---

## 8. Preguntas frecuentes

**¿Qué pasa si Railway reinicia el bot a mitad de una votación?**
No se pierde nada — el bot lee el mensaje existente en `#votemap` y reconstruye los votos leyendo directamente las reacciones reales de Discord, sin depender de ningún archivo guardado aparte.

**¿El bot depende del CRCON para funcionar?**
Para la parte de encuesta y votación, no — es 100% independiente, corre en Railway. Solo se conecta al CRCON al momento de cerrar la votación, para aplicar la rotación ganadora.

**¿Puede votar cualquiera, o hay restricción?**
Vota cualquiera que tenga acceso al canal `#votemap`. No hay restricción por rol configurada.

**¿Qué pasa si un mapa no tiene su ID de CRCON bien configurado?**
Ese mapa puede ganar la votación igual, pero al aplicar la rotación el bot lo salta y avisa en el canal cuántos mapas se saltearon por ese motivo — nunca falla en silencio.
