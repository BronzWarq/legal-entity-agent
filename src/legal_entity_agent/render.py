from __future__ import annotations

from .models import Assessment, InaccuracyState


_VERDICT_LABELS = {
    "safe_to_proceed": "можно продолжать после стандартной проверки документов",
    "manual_review_required": "требуется ручная проверка",
    "high_risk_do_not_proceed": "высокий риск: не продолжать без устранения причин",
    "impossible_contractor_defunct": "контрагент прекратил существование",
}


def _format_deep_report(assessment: Assessment) -> list[str]:
    report = assessment.deep_report
    if report is None:
        if assessment.deep_check_error:
            return ["", f"Расширенная проверка: не выполнена — {assessment.deep_check_error}"]
        return []
    lines = ["", "Расширенная проверка контрагента (atomno-mcp-fns-check):"]
    verdict = report.get("verdict_action")
    if verdict:
        lines.append(f"Вердикт: {_VERDICT_LABELS.get(verdict, verdict)}")
    reason = report.get("verdict_reason_ru")
    if reason:
        lines.append(f"Основание: {reason}")
    risks = report.get("risks") or {}
    if isinstance(risks, dict) and risks:
        level = risks.get("overall_risk_level", "не определён")
        score = risks.get("overall_risk_score")
        score_text = f", балл {score}/100" if score is not None else ""
        lines.append(f"Риски: {level}{score_text}")
        coverage = risks.get("coverage") or {}
        if isinstance(coverage, dict) and coverage:
            lines.append(
                "Покрытие проверок: "
                f"{coverage.get('answered', 0)} из {coverage.get('total', 0)}, "
                f"ошибок: {coverage.get('failed', 0)}."
            )
        for flag in (risks.get("flags") or [])[:5]:
            if isinstance(flag, dict):
                message = flag.get("message_ru") or flag.get("code")
                if message:
                    lines.append(f"• Риск: {message}")
        for error in (risks.get("errors") or [])[:5]:
            if isinstance(error, dict):
                message = error.get("message_ru") or error.get("error")
                if message:
                    lines.append(f"• Источник не ответил: {message}")
    return lines


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

    lines.extend(_format_deep_report(assessment))
    lines.extend(["", f"Источник: {record.source_url}", f"Проверено: {record.fetched_at:%d.%m.%Y %H:%M %z}"])
    return "\n".join(lines)
