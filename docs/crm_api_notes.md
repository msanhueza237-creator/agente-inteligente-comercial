# Notas de la API del CRM "Latin Chile"

Estado: **sin documentación formal**. Este documento se llena a medida que avanza
el spike de descubrimiento (ver `scripts/crm_discovery_spike.py`).

## Checklist de descubrimiento

- [ ] URL de login del panel web del CRM
- [ ] Credenciales de prueba (idealmente sandbox, no producción)
- [ ] Mecanismo de autenticación observado (cookie de sesión / bearer token / API key)
- [ ] Endpoint(s) de búsqueda de prospectos/clientes
- [ ] Endpoint(s) de creación de prospectos/clientes
- [ ] Endpoint(s) de edición/actualización
- [ ] Formato de request/response (JSON, campos requeridos, códigos de error)
- [ ] ¿Existe `/api/docs`, Swagger u OpenAPI sin enlazar?
- [ ] Rate limits observados o documentados
- [ ] Comportamiento ante duplicados (¿el CRM ya deduplica por RUT/nombre?)

## Hallazgos

_(pendiente — completar durante el spike)_

## Mapeo de campos propuesto (a confirmar)

| Campo interno (`ProspectDTO`) | Campo CRM Latin Chile |
|---|---|
| `name` | ? |
| `rut` | ? |
| `phone` | ? |
| `email` | ? |
| `website` | ? |
| `address` | ? |
| `region` / `comuna` | ? |
| `category` | ? |
| `commercial_potential_level` | ? |
