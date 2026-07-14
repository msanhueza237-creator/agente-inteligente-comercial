"""Seed the regions_comunas reference table with Chile's political-administrative
division (16 regions, ~345 comunas).

NOTE: This list was compiled from general knowledge of Chile's administrative
division and should be validated against the official INE/SUBDERE division
before relying on it for production filtering -- comuna creations/renames do
happen occasionally.

Usage: python -m scripts.seed_regions_comunas
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.base import async_session_factory
from app.db.models import RegionComuna

REGIONS_COMUNAS: dict[str, list[str]] = {
    "Arica y Parinacota": ["Arica", "Camarones", "Putre", "General Lagos"],
    "Tarapacá": [
        "Iquique", "Alto Hospicio", "Pozo Almonte", "Camiña", "Colchane", "Huara", "Pica",
    ],
    "Antofagasta": [
        "Antofagasta", "Mejillones", "Sierra Gorda", "Taltal", "Calama", "Ollagüe",
        "San Pedro de Atacama", "Tocopilla", "María Elena",
    ],
    "Atacama": [
        "Copiapó", "Caldera", "Tierra Amarilla", "Chañaral", "Diego de Almagro", "Vallenar",
        "Alto del Carmen", "Freirina", "Huasco",
    ],
    "Coquimbo": [
        "La Serena", "Coquimbo", "Andacollo", "La Higuera", "Paihuano", "Vicuña", "Illapel",
        "Canela", "Los Vilos", "Salamanca", "Ovalle", "Combarbalá", "Monte Patria",
        "Punitaqui", "Río Hurtado",
    ],
    "Valparaíso": [
        "Valparaíso", "Casablanca", "Concón", "Juan Fernández", "Puchuncaví", "Quintero",
        "Viña del Mar", "Isla de Pascua", "Los Andes", "Calle Larga", "Rinconada",
        "San Esteban", "La Ligua", "Cabildo", "Papudo", "Petorca", "Zapallar", "Quillota",
        "Calera", "Hijuelas", "La Cruz", "Nogales", "San Antonio", "Algarrobo", "Cartagena",
        "El Quisco", "El Tabo", "Santo Domingo", "San Felipe", "Catemu", "Llaillay",
        "Panquehue", "Putaendo", "Santa María", "Limache", "Olmué", "Villa Alemana",
        "Quilpué",
    ],
    "Metropolitana de Santiago": [
        "Santiago", "Cerrillos", "Cerro Navia", "Conchalí", "El Bosque", "Estación Central",
        "Huechuraba", "Independencia", "La Cisterna", "La Florida", "La Granja",
        "La Pintana", "La Reina", "Las Condes", "Lo Barnechea", "Lo Espejo", "Lo Prado",
        "Macul", "Maipú", "Ñuñoa", "Pedro Aguirre Cerda", "Peñalolén", "Providencia",
        "Pudahuel", "Quilicura", "Quinta Normal", "Recoleta", "Renca", "San Joaquín",
        "San Miguel", "San Ramón", "Vitacura", "Puente Alto", "Pirque", "San José de Maipo",
        "Colina", "Lampa", "Til Til", "San Bernardo", "Buin", "Calera de Tango", "Paine",
        "Melipilla", "Alhué", "Curacaví", "María Pinto", "San Pedro", "Talagante",
        "El Monte", "Isla de Maipo", "Padre Hurtado", "Peñaflor",
    ],
    "Libertador General Bernardo O'Higgins": [
        "Rancagua", "Codegua", "Coinco", "Coltauco", "Doñihue", "Graneros", "Las Cabras",
        "Machalí", "Malloa", "Mostazal", "Olivar", "Peumo", "Pichidegua",
        "Quinta de Tilcoco", "Rengo", "Requínoa", "San Vicente", "Pichilemu", "La Estrella",
        "Litueche", "Marchihue", "Navidad", "Paredones", "San Fernando", "Chépica",
        "Chimbarongo", "Lolol", "Nancagua", "Palmilla", "Peralillo", "Placilla", "Pumanque",
        "Santa Cruz",
    ],
    "Maule": [
        "Talca", "Constitución", "Curepto", "Empedrado", "Maule", "Pelarco", "Pencahue",
        "Río Claro", "San Clemente", "San Rafael", "Cauquenes", "Chanco", "Pelluhue",
        "Curicó", "Hualañé", "Licantén", "Molina", "Rauco", "Romeral", "Sagrada Familia",
        "Teno", "Vichuquén", "Linares", "Colbún", "Longaví", "Parral", "Retiro",
        "San Javier", "Villa Alegre", "Yerbas Buenas",
    ],
    "Ñuble": [
        "Chillán", "Bulnes", "Chillán Viejo", "El Carmen", "Pemuco", "Pinto", "Quillón",
        "San Ignacio", "Yungay", "Cobquecura", "Coelemu", "Ninhue", "Portezuelo",
        "Quirihue", "Ránquil", "Treguaco", "San Carlos", "Coihueco", "Ñiquén",
        "San Fabián", "San Nicolás",
    ],
    "Biobío": [
        "Concepción", "Coronel", "Chiguayante", "Florida", "Hualqui", "Lota", "Penco",
        "San Pedro de la Paz", "Santa Juana", "Talcahuano", "Tomé", "Hualpén", "Lebu",
        "Arauco", "Cañete", "Contulmo", "Curanilahue", "Los Álamos", "Tirúa", "Los Ángeles",
        "Antuco", "Cabrero", "Laja", "Mulchén", "Nacimiento", "Negrete", "Quilaco",
        "Quilleco", "San Rosendo", "Santa Bárbara", "Tucapel", "Yumbel", "Alto Biobío",
    ],
    "La Araucanía": [
        "Temuco", "Carahue", "Cunco", "Curarrehue", "Freire", "Galvarino", "Gorbea",
        "Lautaro", "Loncoche", "Melipeuco", "Nueva Imperial", "Padre Las Casas",
        "Perquenco", "Pitrufquén", "Pucón", "Saavedra", "Teodoro Schmidt", "Toltén",
        "Vilcún", "Villarrica", "Cholchol", "Angol", "Collipulli", "Curacautín", "Ercilla",
        "Lonquimay", "Los Sauces", "Lumaco", "Purén", "Renaico", "Traiguén", "Victoria",
    ],
    "Los Ríos": [
        "Valdivia", "Corral", "Lanco", "Los Lagos", "Máfil", "Mariquina", "Paillaco",
        "Panguipulli", "La Unión", "Futrono", "Lago Ranco", "Río Bueno",
    ],
    "Los Lagos": [
        "Puerto Montt", "Calbuco", "Cochamó", "Fresia", "Frutillar", "Los Muermos",
        "Llanquihue", "Maullín", "Puerto Varas", "Castro", "Ancud", "Chonchi",
        "Curaco de Vélez", "Dalcahue", "Puqueldón", "Queilén", "Quellón", "Quemchi",
        "Quinchao", "Osorno", "Puerto Octay", "Purranque", "Puyehue", "Río Negro",
        "San Juan de la Costa", "San Pablo", "Chaitén", "Futaleufú", "Hualaihué", "Palena",
    ],
    "Aysén del General Carlos Ibáñez del Campo": [
        "Coyhaique", "Lago Verde", "Aysén", "Cisnes", "Guaitecas", "Cochrane", "O'Higgins",
        "Tortel", "Chile Chico", "Río Ibáñez",
    ],
    "Magallanes y de la Antártica Chilena": [
        "Punta Arenas", "Laguna Blanca", "Río Verde", "San Gregorio", "Cabo de Hornos",
        "Antártica", "Porvenir", "Primavera", "Timaukel", "Natales", "Torres del Paine",
    ],
}


async def seed() -> None:
    async with async_session_factory() as session:
        existing = (await session.execute(select(RegionComuna.region, RegionComuna.comuna))).all()
        existing_pairs = {(r, c) for r, c in existing}

        rows = [
            {"region": region, "comuna": comuna}
            for region, comunas in REGIONS_COMUNAS.items()
            for comuna in comunas
            if (region, comuna) not in existing_pairs
        ]

        if not rows:
            print("regions_comunas ya esta poblada, nada que insertar.")
            return

        stmt = pg_insert(RegionComuna).values(rows).on_conflict_do_nothing(
            constraint="uq_region_comuna"
        )
        await session.execute(stmt)
        await session.commit()
        print(f"Insertadas {len(rows)} filas en regions_comunas.")


if __name__ == "__main__":
    asyncio.run(seed())
