# Scripts SQL para aplicar el esquema en Supabase Studio

Estos scripts son el equivalente en SQL plano de la migración de Alembic
(`app/db/migrations/versions/0001_initial_schema.py`), generados con
`alembic upgrade head --sql` (modo offline, sin conexión a la base). Se usan
cuando no hay conexión directa (TCP) a Postgres desde afuera del VPS — que es
el caso de este stack Supabase self-hosted: en `docker-compose.yml`, `db` y
`supavisor` usan `expose` (solo red interna de Docker), no `ports` publicados.

## Cómo aplicarlos

1. Entra a Supabase Studio en tu dominio público (el mismo host que usas para
   `SUPABASE_PUBLIC_URL`), con el usuario/contraseña de `DASHBOARD_USERNAME` /
   `DASHBOARD_PASSWORD`.
2. Ve a **SQL Editor** → **New query**.
3. Pega el contenido de `0001_initial_schema.sql`, ejecútalo. Crea todas las
   tablas, enums e índices, y dentro de la transacción marca
   `alembic_version = '0001'` (así, si más adelante corres Alembic desde una
   máquina con acceso directo a la DB, reconocerá que ya está en esa
   revisión y no intentará re-crear las tablas). El script es **idempotente**
   (usa `IF NOT EXISTS` y captura el error `duplicate_object` en los tipos
   ENUM) — es seguro darle "Run" más de una vez si dudas si ya se ejecutó.
4. Pega el contenido de `0002_seed_regions_comunas.sql`, ejecútalo. Inserta
   las 16 regiones y ~345 comunas de Chile en `regions_comunas` (usa
   `ON CONFLICT DO NOTHING`, así que es seguro re-ejecutarlo).
5. Pega el contenido de `0003_add_paginas_amarillas_source.sql`, ejecútalo.
   Agrega el valor `paginas_amarillas` al enum `source_type` (necesario para
   la búsqueda en Páginas Amarillas). Va suelto, sin `BEGIN/COMMIT` — Postgres
   no permite combinar `ALTER TYPE ... ADD VALUE` con otras sentencias en la
   misma transacción.

## Importante sobre credenciales

Compartiste el `.env` completo de Supabase (incluyendo `POSTGRES_PASSWORD`,
`JWT_SECRET`, `SERVICE_ROLE_KEY`, `DASHBOARD_PASSWORD`) directamente en el
chat. Ninguno de esos valores quedó guardado en el código ni en memoria de
este asistente, pero sí quedan en el historial de esta conversación. Vale la
pena:

- Evitar pegar `.env` completos en el chat de ahora en adelante — mejor
  compartir solo el valor puntual que se necesita, o guardarlo directamente
  en el `.env` local del proyecto (que ya está en `.gitignore`).
- Si esta conversación se comparte o exporta en algún momento, considera
  rotar `POSTGRES_PASSWORD`, `JWT_SECRET`, `SERVICE_ROLE_KEY` y
  `DASHBOARD_PASSWORD` desde la configuración de Dokploy.

## Próxima migración (Fase 1 en adelante)

Cuando haya una migración `0002_xxx.py` de Alembic para cambios de esquema
futuros, generar su SQL equivalente con:

```bash
alembic upgrade 0001:0002 --sql > docs/sql/0002_nombre_migracion.sql
```

(ajustando el rango de revisiones) y agregarla a esta carpeta con el mismo
procedimiento.
