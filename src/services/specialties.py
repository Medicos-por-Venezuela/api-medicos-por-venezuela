"""Catálogos y reglas de matching de especialidades.

Portado EXACTO desde `lib/utils.ts` de la app Next.js para que el backend tome las
mismas decisiones de elegibilidad que hoy toma el frontend.
"""

# Catálogo de especialidades de los médicos (lib/utils.ts: SPECIALTIES).
SPECIALTIES: list[str] = [
    "Medicina general",
    "Pediatría",
    "Traumatología",
    "Ginecología",
    "Obstetricia",
    "Cardiología",
    "Medicina interna",
    "Psicología",
    "Psiquiatría",
    "Neurología",
    "Cirugía",
    "Oncología",
    "Oncología médica",
    "Fisiatría",
    "Cuidados paliativos y manejo del dolor",
    "Geriatría",
    "Reumatología",
    "Otra",
]

# Catálogo de necesidades del paciente (registro-paciente: NECESIDADES).
NEEDS: list[str] = [
    "Medicina general",
    "Lesión física",
    "Primeros auxilios",
    "Apoyo emocional",
    "Crisis de ansiedad",
    "Niño / pediatría",
    "Embarazo",
    "Medicamentos",
    "Enfermedad crónica",
    "Otra",
]

# Especialidad -> necesidades que cubre ('*' = todo). (lib/utils.ts: SPECIALTY_NEEDS).
SPECIALTY_NEEDS: dict[str, list[str]] = {
    "Medicina general": ["*"],
    "Medicina interna": [
        "Medicina general",
        "Enfermedad crónica",
        "Medicamentos",
        "Primeros auxilios",
    ],
    "Pediatría": ["Niño / pediatría"],
    "Traumatología": ["Lesión física"],
    "Ginecología": ["Embarazo"],
    "Obstetricia": ["Embarazo"],
    "Cardiología": ["Enfermedad crónica"],
    "Psicología": ["Apoyo emocional", "Crisis de ansiedad"],
    "Psiquiatría": ["Apoyo emocional", "Crisis de ansiedad"],
    "Neurología": ["Enfermedad crónica"],
    "Cirugía": ["Lesión física"],
    "Oncología": ["Enfermedad crónica"],
    "Oncología médica": ["Enfermedad crónica"],
    "Fisiatría": ["Lesión física"],
    "Cuidados paliativos y manejo del dolor": ["Enfermedad crónica"],
    "Geriatría": ["Enfermedad crónica", "Medicina general"],
    "Reumatología": ["Enfermedad crónica"],
    "Otra": ["*"],
}

# Necesidades reservadas a salud mental (nunca caen en médicos generales).
RESERVED_NEEDS: dict[str, list[str]] = {
    "Apoyo emocional": ["Psicología", "Psiquiatría"],
    "Crisis de ansiedad": ["Psicología", "Psiquiatría"],
}

# Necesidades que elevan la prioridad a "review" (registro-paciente).
_PRIORITY_REVIEW_TAGS = {"Lesión física", "Embarazo", "Niño / pediatría"}


def _values(category: str | None, needs_tags: list[str] | None) -> list[str]:
    return [v for v in [category, *(needs_tags or [])] if v]


def matches_specialty(
    specialty: str | None, category: str | None, needs_tags: list[str] | None
) -> bool:
    """True si la consulta (category + needs_tags) alinea con la especialidad."""
    if not specialty:
        return False
    covered = SPECIALTY_NEEDS.get(specialty)
    if not covered:
        return False
    if "*" in covered:
        return True
    return any(v in covered for v in _values(category, needs_tags))


def can_attend(specialty: str | None, category: str | None, needs_tags: list[str] | None) -> bool:
    """Elegibilidad dura (separación bidireccional psicología <-> salud física)."""
    values = _values(category, needs_tags)

    # 1) Las necesidades reservadas solo van a su especialidad permitida.
    reserved_ok = all(
        (v not in RESERVED_NEEDS) or (bool(specialty) and specialty in RESERVED_NEEDS[v])
        for v in values
    )
    if not reserved_ok:
        return False

    # 2) Psicología solo atiende casos de psicología (con alguna necesidad reservada).
    if specialty == "Psicología":
        is_psych_case = any(v in RESERVED_NEEDS for v in values)
        if not is_psych_case:
            return False
    return True


def compute_priority(needs_tags: list[str] | None) -> str:
    """'review' si hay una necesidad sensible; 'normal' en caso contrario."""
    if needs_tags and _PRIORITY_REVIEW_TAGS.intersection(needs_tags):
        return "review"
    return "normal"
