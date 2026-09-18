from datetime import date, timedelta

from legal_entity_agent.licenses import LicenseStore, new_license


def test_license_store_reports_expiry_and_days() -> None:
    store = LicenseStore(":memory:")
    store.upsert(new_license(license_id="A-1", inn="7707083893", expires_at=date(2026, 10, 1)))
    record = store.for_inn("7707083893")[0]
    assert record.state(date(2026, 9, 18)) == "действует"
    assert record.days_left(date(2026, 9, 18)) == 13
    future = date.today() + timedelta(days=10)
    store.upsert(new_license(license_id="A-2", inn="7707083893", expires_at=future))
    assert "A-2" in {item.license_id for item in store.expiring(20)}


def test_license_store_does_not_treat_missing_record_as_expired() -> None:
    store = LicenseStore(":memory:")
    assert store.for_inn("0000000000") == []
