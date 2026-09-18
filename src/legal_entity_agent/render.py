from __future__ import annotations

from .models import Assessment, InaccuracyState


def format_assessment(assessment: Assessment) -> str:
    record = assessment.record
    lines = ["Проверка юридического лица по данным ФНС", ""]
    if record.name:
        lines.append(f"Наименование: {record.name}")
    if record.inn:
        lines.append(f"ИНН: {record.inn}")
    if record.ogrn:
        lines.append(f"ОГРН: {record.ogrn}")
    if record.kpp:
        lines.append(f"КПП: {record.kpp}")
    if record.status:
        lines.append(f"Статус: {record.status}")
    if record.registration_date:
        lines.append(f"Дата регистрации: {record.registration_date:%d.%m.%Y}")
    if record.address:
        lines.append(f"Адрес: {record.address}")

    if record.inaccuracy_state is InaccuracyState.PRESENT:
        lines.extend(["", "Результат: обнаружены сведения/признаки, связанные с недостоверностью."])
        for marker in record.inaccuracy_markers[:3]:
            lines.append(f"• {marker}")
    elif record.inaccuracy_state is InaccuracyState.ABSENT:
        lines.extend(["", "Результат: в полученном ответе ФНС указано отсутствие недостоверных сведений."])
    else:
        lines.extend(["", "Результат: явная отметка о наличии или отсутствии недостоверности в полученном ответе не распознана."])

    lines.extend(["", f"Источник: {record.source_url}", f"Проверено: {record.fetched_at:%d.%m.%Y %H:%M %z}"])
    return "\n".join(lines)
