# Clima Activa Agent Hub v1

El servicio web inicia el Agent Hub automaticamente cuando
`HUB_EMBEDDED_WORKER=true` (valor predeterminado). Esto cubre despliegues de
Dokploy que arrancan solamente el `Dockerfile`. En instalaciones con un
contenedor dedicado se usa `python -m app.hub.worker` y se configura
`HUB_EMBEDDED_WORKER=false` en el servicio web. El CRM sigue siendo la fuente
de verdad y el Agent Hub reclama tareas mediante leases de 120 segundos.

## Agentes

- Comercial: seguimiento y priorizacion de prospectos.
- Marketing: genera borradores de campana, nunca envios.
- Finanzas: margenes, ingresos y anomalias.
- Cobranza: prepara recordatorios, nunca los envia solo.
- Comercio exterior: demanda, inventario, importaciones y propuestas de compra.
- Gerente: resume alertas y metricas relevantes.

Toda accion sensible termina como una fila pendiente en `action_proposals`.
Solo un administrador del CRM puede aprobarla o rechazarla.

## Comercio exterior

- Produccion: 45 dias.
- Viaje maritimo: 45 dias.
- Aduana y recepcion: 5 dias.
- Lead time base: 95 dias.
- Stock de seguridad: 30 dias.
- Revision: 30 dias.
- Cobertura objetivo: 155 dias.
- Pausa inicial de fabrica china: febrero.
- Temporada alta: noviembre a febrero.
- Orden objetivo: USD 50.000 a USD 70.000.
- Maximo duro: USD 70.000. No se divide automaticamente una orden para
  eludir el maximo y se revisan ordenes cercanas dentro de 30 dias.

## Activacion

1. Ejecutar `supabase/agent_hub.sql` en el SQL Editor del CRM.
2. Crear o reemplazar la API key con scopes `prospecting:execute` y
   `agent-hub:execute`.
3. Completar `docs/dokploy-agent-hub-env.example` en el Environment privado.
4. Mantener Facto y Tiendanube deshabilitados hasta validar cada conexion.
5. Redeploy de Edge Function, CRM y Agent Hub.
6. En Dokploy con un unico servicio del agente, mantener
   `HUB_EMBEDDED_WORKER=true`; las tareas pendientes se reclaman
   automaticamente despues del redeploy.
