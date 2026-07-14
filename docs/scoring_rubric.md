# Rúbrica de potencial comercial (propuesta inicial)

Score 0-100, calculado por `app/classification/scoring.py`. Los pesos son
configurables (no hardcodeados) y deben validarse con Clima Activa en la
Fase 3 del proyecto.

| Componente | Puntos máx. | Señales |
|---|---:|---|
| Presencia y credibilidad del negocio | 25 | Sitio web activo (+8), redes sociales activas (+5), rating Google ≥4.0 con ≥10 reseñas (+7), múltiples ubicaciones (+5) |
| Señales de escala | 25 | Tamaño estimado "grande" (+10), múltiples sucursales (+10), antigüedad del negocio si es determinable (+5) |
| Relevancia para Clima Activa | 30 | Peso por categoría: distribuidor/instalador grande (+15), minorista/mantención (+10), instalador independiente (+7); foco confirmado en climatización/refrigeración (+15) |
| Oportunidad / engagement | 10 | No atado exclusivamente a una marca competidora (+5), señales de actividad reciente (+5) |
| Completitud de datos | 10 | Set de contacto completo: teléfono + email + dirección + web (+10) |

## Niveles

| Rango | Nivel |
|---|---|
| 0-39 | `low` |
| 40-64 | `medium` |
| 65-84 | `high` |
| 85-100 | `very_high` |

## Pendiente de validar con el cliente

- Lista de competidores conocidos de Clima Activa (necesaria para la señal
  "no atado a marca competidora" y para poblar `category=competitor`).
- Si el peso de "relevancia" (30 pts) debe subdividirse por especialidad
  (residencial vs comercial vs industrial).
- Umbrales de rating/reseñas de Google Maps considerados "buena reputación"
  en el contexto chileno (¿4.0 es razonable, o debería ser más estricto?).
