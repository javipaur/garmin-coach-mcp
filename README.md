# 🏃 Garmin Coach MCP

**Lo que tu reloj sabe hoy, trabaja para ti esta noche.**

Un puente autohospedado entre **Garmin Connect** y tu asistente de IA (Claude, Cursor, VS Code, móvil…). Conectas tu cuenta Garmin una vez y conversas con tus propios números: VFC, carga de entrenamiento, sueño, ritmos y planes. Incluye un **panel web multi-usuario** para gestionar un club entero de corredores.

---

## ✨ Qué hace

- **Habla con tus números** — pregunta en lenguaje normal: *«¿estoy sobreentrenando?»*, *«¿qué ritmo en Z3?»*, *«saca una ruta de 12 km desde casa»*.
- **120+ herramientas MCP** — `get_daily_summary`, `get_hrv_data`, `detect_fatigue_risk`, `plan_this_week`, `parse_training_pdf`, `calculate_pace_zones`, `route_from_home`, `get_race_predictions` y muchas más.
- **Fatiga real, no clichés** — cruza VFC nocturna, predisposición y sueño para devolverte un veredicto accionable con su porqué.
- **PDF del entrenador → tu reloj** — sube el plan de maratón en PDF y lo convierte en etapas (calentamiento, series, enfriamiento) listas para el dispositivo.
- **Multi-usuario aislado** — cada corredor tiene su API key y sus credenciales Garmin guardadas por separado. Nadie ve la sesión del otro.
- **Web app incluida** — panel de administración (`/admin`), alta de corredores paso a paso (`/admin/users`), login y conexión Garmin guiada (`/connect`), landing pública (`/`).
- **Autohospedado** — desplegado en tu Dokploy, tu nube o tu casa. Tú decides dónde viven los datos.
- **Rutas sobre OpenStreetMap** — generación de rutas circulares desde casa con `osmnx`.

---

## 🚀 Cómo funciona

```
Garmin Connect  →  Garmin Coach MCP (tu servidor)  →  Claude / Cursor / VS Code / móvil
     (API)                (Docker / Dokploy)                    (MCP protocol)
```

Hay **3 pasos, una sola vez**:

1. **Recibe tu clave** — el administrador te crea una API key personal (`gcmcp_…`) desde `/admin/users`.
2. **Conecta tu Garmin** — metes tu correo y contraseña en el asistente seguro. Quedan guardados y aislados en tu carpeta de usuario.
3. **Entrena y pregunta** — abres Claude, Cursor o VS Code, lo conectas con tu clave y listo.

---

## 🖥️ Despliegue

### Opción A — Dokploy (recomendada)

Despliega el repositorio como un **aplicación Docker Compose** en Dokploy (dominio público, HTTPS con el proxy inverso incorporado).

```yaml
# docker-compose.yml (ya incluido en el repo)
services:
  garmin-mcp:
    build: .
    image: garmincoachmcp-garminmcp
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      - PORT=8000
      - DATA_DIR=/data
      - USERS_DB_DIR=/data/users
      - PUBLIC_URL=${PUBLIC_URL:-}        # ej: https://garmin.tudominio.com
      - GARMIN_LANGUAGE=es
      - GARMIN_TIMEZONE=Europe/Madrid
    volumes:
      - garmincoach_data:/data
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s

volumes:
  garmincoach_data:   # persiste tokens Garmin y usuarios entre despliegues
```

### Opción B — Docker CLI

```bash
docker build -t garmin-coach-mcp .
docker run -d --name garmin-mcp \
  -p 8000:8000 \
  -e PUBLIC_URL=https://garmin.tudominio.com \
  -v garmincoach_data:/data \
  garmin-coach-mcp
```

### Opción C — Local (Python)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DATA_DIR=./data PUBLIC_URL=http://localhost:8000 python server.py
```

---

## ⚙️ Variables de entorno

| Variable | Default | Descripción |
|---|---|---|
| `PORT` | `8000` | Puerto del servidor. |
| `DATA_DIR` | `/data` | Raíz del volumen persistente (tokens, usuarios, config). |
| `USERS_DB_DIR` | `<DATA_DIR>/users` | Carpeta con la BBDD de usuarios y sus tokens. |
| `PUBLIC_URL` | *(vacío)* | URL pública del servicio (para enlaces de conexión). Ideal en Dokploy. |
| `ADMIN_API_KEY` | *(vacío)* | Si se define, bloquea `/admin/users` y exige esta clave. |
| `ADMIN_TOKEN` | *(vacío)* | Token para el panel de administración `/admin`. |
| `GARMIN_TOKENS_JSON` | *(vacío)* | Tokens Garmin en JSON (legacy single-user, base64). |
| `GARMIN_EMAIL` | *(vacío)* | Email asociado a los tokens legacy. |
| `GARMIN_LANGUAGE` | `es` | Idioma de las respuestas Garmin. |
| `GARMIN_TIMEZONE` | `Europe/Madrid` | Zona horaria de las métricas. |
| `CACHE_MINUTES` | `30` | Minutos de caché de datos Garmin. |
| `ACTIVITY_LIMIT` | `8` | Máx. actividades por consulta (1–20). |
| `RESET_GARMIN_TOKENS` | `0` | Pon a `1` para borrar tokens al arrancar. |

---

## 🔑 Rutas web

| Ruta | Descripción |
|---|---|
| `/` | Landing pública. |
| `/admin` | Panel de administración. |
| `/admin/users` | **Alta de corredores paso a paso** (wizard). |
| `/u/login?api_key=…` | Panel del corredor (por API key). |
| `/connect?api_key=…` | Asistente para conectar Claude / Cursor / móvil. |
| `/garmin-connect` | Conexión de la cuenta Garmin del corredor. |
| `/health` | Healthcheck (200 = vivo). |
| `/metrics` | Métricas Prometheus (uptime, estado de caché, último refresco). |

> `/mcp` y `/admin` están protegidos con *rate limit* (60 req/min por IP → `429`).

---

## 🤖 Conectar un asistente

Desde `/connect?api_key=…` (o el panel del corredor `u/login`) obtienes la config exacta.

**Claude Desktop / Cursor (HTTP):**

```json
{
  "mcpServers": {
    "garmin-coach": {
      "url": "https://tu-dominio/mcp",
      "headers": { "X-User-API-Key": "TU_API_KEY" }
    }
  }
}
```

**Claude Code (CLI):**

```bash
claude mcp add --transport http garmin-coach https://tu-dominio/mcp \
  --header "X-User-API-Key: TU_API_KEY"
```

**Móvil / Claude.ai:** usa un *Custom Connector* apuntando a `https://tu-dominio/mcp` con la cabecera `X-User-API-Key`.

> El correo y la contraseña de Garmin viven en tu servidor, nunca en el chat. Tu app solo guarda la API key personal.

---

## 🧩 Herramientas MCP destacadas

`get_daily_summary` · `get_hrv_data` · `detect_fatigue_risk` · `plan_this_week` · `parse_training_pdf` · `calculate_pace_zones` · `summarize_period` · `route_from_home` · `get_race_predictions` · `list_tools_spanish` … y **más de 120** en total.

Pídele al asistente `list_tools_spanish` para ver el catálogo completo en directo.

---

## 📈 Observabilidad

- **Logs estructurados JSON** — el servidor logea en un solo formato (`time`, `level`, `logger`, `message`), fácil de ingestar en Grafana/Loki.
- **`/metrics`** — exponen métricas Prometheus: uptime, estado de la caché y timestamp del último refresco.
- **Snapshots pre-calentados** — al arrancar, un hilo en segundo plano refresca la foto del día (con reintentos rápidos si Garmin falla) para que la primera consulta del asistente no sea fría.

---

## 🔒 Privacidad

- **Cada quien lo suyo** — tu sesión Garmin vive en tu carpeta de usuario; ni el vecino ni el admin la ven.
- **Solo la API key** — en tu app guardas solo tu clave personal; el correo de Garmin va al asistente, nunca al chat.
- **Autohospedado** — es tu servidor. Tú decides dónde viven los datos.

---

## 📁 Estructura

```
server.py            # Servidor FastMCP + panel web (rutas, wiring de snapshot)
snapshot.py          # Cadena consolidada de _collect_day_snapshot (parches aislados)
config.py/date_utils.py/localization.py/...   # Módulos auxiliares
index.html           # Landing pública
Dockerfile           # Imagen (instala GDAL/geos/proj para rutas)
docker-compose.yml   # Despliegue en Dokploy con volumen /data
requirements.txt     # Dependencias
tests/               # Suite pytest (+ integración del servidor HTTP/MCP)
pyproject.toml       # Config de ruff, mypy, pytest (y coverage, opcional)
.github/workflows/   # CI: lint, typecheck, tests, docker (GHCR), gitleaks
.github/dependabot.yml   # Actualizaciones de dependencias
.pre-commit-config.yaml
```

---

## 🛠️ Desarrollo

```bash
git clone https://github.com/javipaur/garmin-coach-mcp.git
cd garmin-coach-mcp
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DATA_DIR=./data python server.py
# → http://localhost:8000
```

---

Hecho con 🏃 para entrenar de forma más inteligente.
