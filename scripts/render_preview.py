"""Renders the dashboard templates to static HTML files with mock data, so
they can be reviewed locally by double-clicking them -- no server, no
database, no deploy needed. Throwaway/dev-only: not wired into the app.

Usage: python -m scripts.render_preview
Output: local_preview/*.html
"""

import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

from app.db.models import (
    CommercialPotentialLevel,
    DedupStatus,
    ProspectCategory,
    ProspectStatus,
)
from app.web.deps import CATEGORY_LABELS, LEVEL_LABELS
from scripts.seed_regions_comunas import REGIONS_COMUNAS

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "templates"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "local_preview"

env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
env.globals["category_labels"] = CATEGORY_LABELS
env.globals["level_labels"] = LEVEL_LABELS

REGION_COMUNA_MAP = {region: sorted(comunas) for region, comunas in sorted(REGIONS_COMUNAS.items())}


class FakeRequest:
    query_params: dict = {}
    url = SimpleNamespace(path="")


def prospect(
    name,
    category,
    region,
    comuna,
    phone,
    website,
    level,
    status=ProspectStatus.new,
    dedup_status=DedupStatus.unique,
    rut=None,
):
    return SimpleNamespace(
        name=name,
        rut=rut,
        category=category,
        region=region,
        comuna=comuna,
        phone=phone,
        website=website,
        commercial_potential_level=level,
        status=status,
        dedup_status=dedup_status,
    )


MOCK_PROSPECTS = [
    prospect(
        "Distribuidora FrioSur SpA", ProspectCategory.distributor, "Biobío", "Concepción",
        "+56412345678", "friosur.cl", CommercialPotentialLevel.very_high,
        ProspectStatus.reviewed, DedupStatus.unique, "76086428-5",
    ),
    prospect(
        "Instalaciones Térmicas Andina Ltda.", ProspectCategory.installer_large, "Metropolitana de Santiago",
        "Providencia", "+56221234567", "termicaandina.cl", CommercialPotentialLevel.high,
        ProspectStatus.new, DedupStatus.unique,
    ),
    prospect(
        "ServiFrío Mantención", ProspectCategory.maintenance, "Valparaíso", "Viña del Mar",
        "+56322345678", None, CommercialPotentialLevel.medium,
    ),
    prospect(
        "Juan Pérez Instalaciones Climatización", ProspectCategory.installer_independent,
        "La Araucanía", "Temuco", "+56912345678", None, CommercialPotentialLevel.low,
    ),
    prospect(
        "Repuestos Clima Norte", ProspectCategory.refrigeration, "Antofagasta", "Antofagasta",
        "+56552345678", "climanorte.cl", CommercialPotentialLevel.medium,
        ProspectStatus.new, DedupStatus.needs_review,
    ),
    prospect(
        "Tienda ClimaHogar", ProspectCategory.retailer, "Coquimbo", "La Serena",
        "+56512345678", "climahogar.cl", CommercialPotentialLevel.medium,
    ),
]


# Rewrite the live-server nav links (/, /prospects, ...) to the equivalent
# static filenames so clicking around the preview actually works offline.
_NAV_LINK_REWRITES = {
    'href="/"': 'href="home.html"',
    'href="/search"': 'href="search_vacio.html"',
    'href="/prospects"': 'href="prospects.html"',
    'href="/import"': 'href="import_vacio.html"',
    'href="/dedup"': 'href="dedup.html"',
}


def render(template_name: str, context: dict, output_name: str) -> None:
    template = env.get_template(template_name)
    html = template.render(request=FakeRequest(), **context)
    for old, new in _NAV_LINK_REWRITES.items():
        html = html.replace(old, new)
    # Any /static/... reference (href, src) must be relative on file://.
    html = html.replace('"/static/', '"static/')
    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / output_name
    out_path.write_text(html, encoding="utf-8")
    print(f"Generado: {out_path}")


def main() -> None:
    static_src = Path(__file__).resolve().parent.parent / "app" / "web" / "static"
    static_dst = OUTPUT_DIR / "static"
    static_dst.mkdir(parents=True, exist_ok=True)
    for f in static_src.glob("*"):
        shutil.copy(f, static_dst / f.name)

    render(
        "home.html",
        {
            "stats": {
                "total": 42,
                "needs_review": 5,
                "new": 12,
                "approved": 8,
                "by_category": [
                    ("distributor", 6),
                    ("installer_large", 4),
                    ("installer_independent", 15),
                    ("maintenance", 8),
                    ("refrigeration", 5),
                    ("retailer", 3),
                    ("competitor", 1),
                ],
            }
        },
        "home.html",
    )

    render(
        "prospects_list.html",
        {
            "prospects": MOCK_PROSPECTS,
            "total": len(MOCK_PROSPECTS),
            "filters": {"region": None, "comuna": None, "category": None, "status": None},
            "filter_options": {
                "regions": list(REGION_COMUNA_MAP.keys()),
                "categories": [c.value for c in ProspectCategory],
                "statuses": [s.value for s in ProspectStatus],
            },
            "region_comuna_map": REGION_COMUNA_MAP,
        },
        "prospects.html",
    )

    render(
        "search.html",
        {"regions": list(REGION_COMUNA_MAP.keys()), "region_comuna_map": REGION_COMUNA_MAP, "result": None},
        "search_vacio.html",
    )
    render(
        "search.html",
        {
            "regions": list(REGION_COMUNA_MAP.keys()),
            "region_comuna_map": REGION_COMUNA_MAP,
            "result": {
                "query": "instaladores de aire acondicionado en Antofagasta, Chile",
                "status": "completed",
                "error": None,
                "stats": {
                    "found_google": 14, "found_paginas_amarillas": 8,
                    "created": 15, "merged": 4, "needs_review": 3, "skipped_budget": 0,
                },
            },
        },
        "search_resultado.html",
    )

    render("import.html", {"result": None}, "import_vacio.html")
    render(
        "import.html",
        {
            "result": {
                "filename": "prospectos_clima_activa_julio.xlsx",
                "row_count": 87,
                "stats": {"created": 61, "merged": 14, "needs_review": 9, "errors": 3},
            }
        },
        "import_resultado.html",
    )

    dedup_candidates = [
        SimpleNamespace(
            id=uuid.uuid4(),
            match_score=91.4,
            match_reasons={"type": "fuzzy", "name_and_address_score": 91.4},
            prospect_a=SimpleNamespace(
                name="Distribuidora FrioSur SpA", rut="76086428-5",
                address="Av. Los Carrera 1234", comuna="Concepción",
                phone="+56412345678", website="friosur.cl",
            ),
            prospect_b=SimpleNamespace(
                name="Frio Sur Distribuidora", rut=None,
                address="Los Carrera 1234", comuna="Concepción",
                phone=None, website=None,
            ),
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            match_score=78.2,
            match_reasons={"type": "fuzzy", "name_and_address_score": 78.2},
            prospect_a=SimpleNamespace(
                name="ServiFrío Mantención", rut=None,
                address="Av. Marina 500", comuna="Viña del Mar",
                phone="+56322345678", website=None,
            ),
            prospect_b=SimpleNamespace(
                name="Servicio Frio Mantenciones Ltda", rut="88123456-2",
                address="Marina 500, local 2", comuna="Viña del Mar",
                phone=None, website=None,
            ),
        ),
    ]
    render("dedup_queue.html", {"candidates": dedup_candidates}, "dedup.html")


if __name__ == "__main__":
    main()
