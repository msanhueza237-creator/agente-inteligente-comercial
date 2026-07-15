# Agente de prospección comercial — Clima Activa

Ejecutor técnico para campañas de prospección creadas y aprobadas en el CRM.
El agente expande cada ejecución en tareas `fuente × keyword × comuna`, consulta
fuentes autorizadas, aplica controles de calidad y deduplicación, y entrega
candidatos con evidencia mediante un outbox recuperable.

El worker selecciona `FakeCRMPort` solamente en desarrollo/pruebas. En
producción exige `CRM_MODE=http`, una URL HTTPS y una API key restringida; se
niega a arrancar si falta cualquiera de esas condiciones. La llave nunca se
expone en el monitor ni se incluye en logs.

## Flujo actual

1. `CRMPort.claim_run` entrega un snapshot inmutable, lease y tareas con los
   mismos UUID que usa el CRM.
2. El worker persiste el run y las tareas en Postgres, renueva heartbeats y
   recupera leases vencidos.
3. Google Places o Brave Search descubren empresas. `official_website` sólo
   enriquece un dominio que parece pertenecer a la empresa.
4. El quality gate exige rubro HVAC, región/comuna seleccionadas, un contacto
   comercial y evidencia fechada.
5. Sólo coincidencias exactas se consolidan automáticamente, en orden: RUT,
   ID del proveedor, dominio, teléfono y nombre+comuna. Las coincidencias
   difusas quedan como `possible_duplicate`. Las sucursales se conservan como
   ubicaciones separadas y deben pertenecer al territorio de la ejecución.
6. Eventos y candidatos se envían con lease e `Idempotency-Key`. Una caída del
   CRM conserva el mensaje en `crm_outbox_messages`.

Google Places usa cobertura masiva controlada: cada tarea genera consultas
complementarias según los tipos objetivo, deduplica por Place ID y solicita
detalles sólo a una cantidad acotada de resultados dentro de la comuna. Los
perfiles con categorías genéricas entran a revisión con
`target_type_unconfirmed`; ya no se eliminan antes de que una persona pueda
evaluarlos. La cronología reporta consultas ejecutadas, resultados brutos,
resultados únicos, detalles solicitados y costo estimado.

Páginas Amarillas no forma parte del flujo. El scraper legado fue retirado y
el conector permanece bloqueado incluso si se alteran los flags de
compatibilidad; sólo podrá reemplazarse por un feed/API oficial autorizado.
La ruta manual `/search` responde `410`: las búsquedas productivas se crean en
el CRM.

## Desarrollo local

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

El monitor técnico queda en `http://localhost:8000/monitor`. En otra terminal:

```powershell
python -m app.scheduler.worker
```

Mientras se use `CRM_MODE=fake`, el proceso queda a la espera de ejecuciones de
desarrollo. Para el piloto real se usa `CRM_MODE=http`.

## Despliegue en Dokploy

El Compose incluye un PostgreSQL privado para leases, tareas y outbox. No se
publica su puerto y no reemplaza Supabase. Configurar en `.env`:

```env
ENV=production
AGENT_DB_PASSWORD=una-clave-larga-generada-en-dokploy
DATABASE_URL=postgresql+asyncpg://clima_agent:LA_MISMA_CLAVE@db:5432/clima_agent
DATABASE_URL_DIRECT=postgresql+asyncpg://clima_agent:LA_MISMA_CLAVE@db:5432/clima_agent
CRM_MODE=http
CRM_BASE_URL=https://supabase.latinchile.cl/functions/v1/crm-agent
CRM_API_KEY=ca_live_valor_entregado_una_vez
CRM_WORKER_ID=climactiva-worker-01
GOOGLE_MAPS_API_KEY=
GOOGLE_PLACES_QUERIES_PER_TASK=6
GOOGLE_PLACES_DETAIL_MULTIPLIER=2
GOOGLE_PLACES_RUN_BUDGET_USD=10.0
GOOGLE_PLACES_DAILY_BUDGET_USD=20.0
GOOGLE_PLACES_MONTHLY_BUDGET_USD=400.0
GOOGLE_PLACES_BUDGET_ALERT_RATIO=0.70
BRAVE_SEARCH_API_KEY=
SESSION_SECRET_KEY=una-clave-independiente
```

`migrate` aplica Alembic antes de iniciar `app` y `worker`. El servicio `db`
conserva datos en `agent_postgres_data`; no exponer 5432 en Domains.

## Seguridad y retención

- El crawler acepta sólo HTTP(S), respeta `robots.txt`, limita redirecciones,
  tamaño y tiempo, y bloquea IP privadas, loopback, link-local y DNS no global.
- No se persisten respuestas raw de Google o Brave.
- La evidencia derivada de Google recibe `retention_until` a 30 días. El worker
  elimina evidencia expirada de tablas y JSON, además de limpiar outbox
  entregado con más de 30 días.
- Los eventos guardan métricas y códigos técnicos; no guardan snippets ni
  contactos en logs.

## Verificación

```powershell
python -m ruff check app tests
python -m pytest -q
python -m alembic heads
alembic upgrade head --sql
```

La cadena incremental del worker es:

- `0003`: base durable del worker con runs, tareas, candidatos, evidencia,
  eventos y outbox.
- `0004`: estado de ACK autoritativo, conteos confirmados por el CRM y control
  de reintentos de entrega por tarea.
- `0005`: identidad del worker persistida y replay terminal durable tras una
  respuesta perdida o un reinicio, sin volver a ejecutar la fuente.

Los módulos anteriores de importación se mantienen por compatibilidad, pero ya
no aparecen como creación de campañas en el panel.
