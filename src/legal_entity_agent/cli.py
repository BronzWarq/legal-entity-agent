from __future__ import annotations

import argparse
import asyncio

from .agent import LegalEntityAgent
from .fns_client import FnsError
from .identifiers import InvalidIdentifier
from .render import format_assessment


async def _run(value: str) -> None:
    try:
        assessment = await LegalEntityAgent().check(value)
    except InvalidIdentifier as exc:
        raise SystemExit(f"Ошибка идентификатора: {exc}") from exc
    except FnsError as exc:
        raise SystemExit(f"Проверка ФНС не выполнена: {exc}") from exc
    print(format_assessment(assessment))


def main() -> None:
    parser = argparse.ArgumentParser(description="Проверка юридического лица по данным ФНС")
    parser.add_argument("identifier", help="ИНН, ОГРН, КПП, название или другие реквизиты")
    args = parser.parse_args()
    asyncio.run(_run(args.identifier))
