"""Canonical Chilean territory helpers for business-source addresses.

Provider APIs usually return a comuna in fields named ``city`` or ``district``
without the corresponding region.  This module keeps the normalization local
and deterministic so commercial segmentation never depends on a web lookup.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable


def _key(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").strip())
    text = "".join(character for character in text if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


REGION_COMUNAS: dict[str, tuple[str, ...]] = {
    "Región de Arica y Parinacota": (
        "Arica", "Camarones", "Putre", "General Lagos",
    ),
    "Región de Tarapacá": (
        "Iquique", "Alto Hospicio", "Pozo Almonte", "Camiña", "Colchane", "Huara", "Pica",
    ),
    "Región de Antofagasta": (
        "Antofagasta", "Mejillones", "Sierra Gorda", "Taltal", "Calama", "Ollagüe",
        "San Pedro de Atacama", "Tocopilla", "María Elena",
    ),
    "Región de Atacama": (
        "Copiapó", "Caldera", "Tierra Amarilla", "Chañaral", "Diego de Almagro", "Vallenar",
        "Alto del Carmen", "Freirina", "Huasco",
    ),
    "Región de Coquimbo": (
        "La Serena", "Coquimbo", "Andacollo", "La Higuera", "Paihuano", "Vicuña", "Illapel",
        "Canela", "Los Vilos", "Salamanca", "Ovalle", "Combarbalá", "Monte Patria",
        "Punitaqui", "Río Hurtado",
    ),
    "Región de Valparaíso": (
        "Valparaíso", "Casablanca", "Concón", "Juan Fernández", "Puchuncaví", "Quintero",
        "Viña del Mar", "Isla de Pascua", "Los Andes", "Calle Larga", "Rinconada",
        "San Esteban", "La Ligua", "Cabildo", "Papudo", "Petorca", "Zapallar", "Quillota",
        "La Calera", "Calera", "Hijuelas", "La Cruz", "Nogales", "San Antonio", "Algarrobo",
        "Cartagena", "El Quisco", "El Tabo", "Santo Domingo", "San Felipe", "Catemu",
        "Llaillay", "Panquehue", "Putaendo", "Santa María", "Limache", "Olmué",
        "Villa Alemana", "Quilpué",
    ),
    "Región Metropolitana de Santiago": (
        "Santiago", "Cerrillos", "Cerro Navia", "Conchalí", "El Bosque", "Estación Central",
        "Huechuraba", "Independencia", "La Cisterna", "La Florida", "La Granja",
        "La Pintana", "La Reina", "Las Condes", "Lo Barnechea", "Lo Espejo", "Lo Prado",
        "Macul", "Maipú", "Ñuñoa", "Pedro Aguirre Cerda", "Peñalolén", "Providencia",
        "Pudahuel", "Quilicura", "Quinta Normal", "Recoleta", "Renca", "San Joaquín",
        "San Miguel", "San Ramón", "Vitacura", "Puente Alto", "Pirque", "San José de Maipo",
        "Colina", "Lampa", "Til Til", "San Bernardo", "Buin", "Calera de Tango", "Paine",
        "Melipilla", "Alhué", "Curacaví", "María Pinto", "San Pedro", "Talagante",
        "El Monte", "Isla de Maipo", "Padre Hurtado", "Peñaflor",
    ),
    "Región del Libertador General Bernardo O'Higgins": (
        "Rancagua", "Codegua", "Coinco", "Coltauco", "Doñihue", "Graneros", "Las Cabras",
        "Machalí", "Malloa", "Mostazal", "Olivar", "Peumo", "Pichidegua",
        "Quinta de Tilcoco", "Rengo", "Requínoa", "San Vicente", "Pichilemu", "La Estrella",
        "Litueche", "Marchigüe", "Marchihue", "Navidad", "Paredones", "San Fernando",
        "Chépica", "Chimbarongo", "Lolol", "Nancagua", "Palmilla", "Peralillo", "Placilla",
        "Pumanque", "Santa Cruz",
    ),
    "Región del Maule": (
        "Talca", "Constitución", "Curepto", "Empedrado", "Maule", "Pelarco", "Pencahue",
        "Río Claro", "San Clemente", "San Rafael", "Cauquenes", "Chanco", "Pelluhue",
        "Curicó", "Hualañé", "Licantén", "Molina", "Rauco", "Romeral", "Sagrada Familia",
        "Teno", "Vichuquén", "Linares", "Colbún", "Longaví", "Parral", "Retiro",
        "San Javier", "Villa Alegre", "Yerbas Buenas",
    ),
    "Región de Ñuble": (
        "Chillán", "Bulnes", "Chillán Viejo", "El Carmen", "Pemuco", "Pinto", "Quillón",
        "San Ignacio", "Yungay", "Cobquecura", "Coelemu", "Ninhue", "Portezuelo",
        "Quirihue", "Ránquil", "Treguaco", "Trehuaco", "San Carlos", "Coihueco", "Ñiquén",
        "San Fabián", "San Nicolás",
    ),
    "Región del Biobío": (
        "Concepción", "Coronel", "Chiguayante", "Florida", "Hualqui", "Lota", "Penco",
        "San Pedro de la Paz", "Santa Juana", "Talcahuano", "Tomé", "Hualpén", "Lebu",
        "Arauco", "Cañete", "Contulmo", "Curanilahue", "Los Álamos", "Tirúa", "Los Ángeles",
        "Antuco", "Cabrero", "Laja", "Mulchén", "Nacimiento", "Negrete", "Quilaco",
        "Quilleco", "San Rosendo", "Santa Bárbara", "Tucapel", "Yumbel", "Alto Biobío",
    ),
    "Región de La Araucanía": (
        "Temuco", "Carahue", "Cunco", "Curarrehue", "Freire", "Galvarino", "Gorbea",
        "Lautaro", "Loncoche", "Melipeuco", "Nueva Imperial", "Padre Las Casas",
        "Perquenco", "Pitrufquén", "Pucón", "Saavedra", "Teodoro Schmidt", "Toltén",
        "Vilcún", "Villarrica", "Cholchol", "Angol", "Collipulli", "Curacautín", "Ercilla",
        "Lonquimay", "Los Sauces", "Lumaco", "Purén", "Renaico", "Traiguén", "Victoria",
    ),
    "Región de Los Ríos": (
        "Valdivia", "Corral", "Lanco", "Los Lagos", "Máfil", "Mariquina", "Paillaco",
        "Panguipulli", "La Unión", "Futrono", "Lago Ranco", "Río Bueno",
    ),
    "Región de Los Lagos": (
        "Puerto Montt", "Calbuco", "Cochamó", "Fresia", "Frutillar", "Los Muermos",
        "Llanquihue", "Maullín", "Puerto Varas", "Castro", "Ancud", "Chonchi",
        "Curaco de Vélez", "Dalcahue", "Puqueldón", "Queilén", "Quellón", "Quemchi",
        "Quinchao", "Osorno", "Puerto Octay", "Purranque", "Puyehue", "Río Negro",
        "San Juan de la Costa", "San Pablo", "Chaitén", "Futaleufú", "Hualaihué", "Palena",
    ),
    "Región de Aysén del General Carlos Ibáñez del Campo": (
        "Coyhaique", "Lago Verde", "Aysén", "Cisnes", "Guaitecas", "Cochrane", "O'Higgins",
        "Tortel", "Chile Chico", "Río Ibáñez",
    ),
    "Región de Magallanes y de la Antártica Chilena": (
        "Punta Arenas", "Laguna Blanca", "Río Verde", "San Gregorio", "Cabo de Hornos",
        "Antártica", "Porvenir", "Primavera", "Timaukel", "Natales", "Torres del Paine",
    ),
}


_COMUNA_INDEX = {
    _key(comuna): (region, comuna)
    for region, comunas in REGION_COMUNAS.items()
    for comuna in comunas
}
_REGION_INDEX = {
    _key(region): region for region in REGION_COMUNAS
}
_REGION_INDEX.update(
    {
        _key(region.removeprefix("Región de ").removeprefix("Región del ")): region
        for region in REGION_COMUNAS
    }
)
_REGION_INDEX[_key("Metropolitana de Santiago")] = "Región Metropolitana de Santiago"
_REGION_INDEX[_key("Región Metropolitana")] = "Región Metropolitana de Santiago"
_REGION_INDEX[_key("Metropolitana")] = "Región Metropolitana de Santiago"


def canonical_chilean_location(
    *,
    city: object = "",
    district: object = "",
    region: object = "",
    candidates: Iterable[object] = (),
) -> tuple[str, str]:
    """Return ``(official_region, canonical_comuna)`` when it can be proven.

    District/comuna wins over the generic city field.  This matters for
    addresses such as ``city=Santiago, district=Macul``.
    """

    for value in (district, city, *candidates):
        matched = _COMUNA_INDEX.get(_key(value))
        if matched:
            return matched

    official_region = _REGION_INDEX.get(_key(region), "")
    raw_city = str(district or city or "").strip()
    return official_region, raw_city

