import pytest
from ila import demo
from ila.adapters import SimChannel, SimCRM
from ila.api import create_app
from ila.clock import clock
from ila.db import DB
from ila.engine import Engine
from ila.profile import Profile, save_profile


@pytest.fixture
def eng():
    db = DB(":memory:")
    save_profile(db, Profile(configured=True))
    e = Engine(db, SimChannel(), SimCRM(), None)
    clock.set_fixed(demo.tuesday_at(10, "America/New_York"))
    yield e
    clock.set_fixed(None)


def lead(**kw):
    d = dict(first_name="Ava", last_name="Test", email="ava@example-co.example", company="Example Co", job_title="Director of Operations",
             company_size="300", inquiry_type="demo", message="We want a demo next month.", consent=True, consent_source="test")
    d.update(kw)
    return d


@pytest.fixture
def mk(eng):
    n = [0]

    def make(**kw):
        n[0] += 1
        kw.setdefault("email", f"user{n[0]}@co{n[0]}.example")
        r = eng.ingest_lead(lead(**kw), source="test", external_id=f"t{n[0]}")
        return r["lead_id"]
    return make
