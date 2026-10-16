"""Batch 4 — the shared builder must not keep a "current tenant".

``DocumentIndexBuilder`` is an app-state singleton. It used to store the
tenant of the most recent ingest on ``self.tenant_id`` and fall back to it
(and then to ``"default"``) whenever a method was called without an
explicit tenant. So the tenant a write landed in could depend on which
ingest happened to run last — an upload for tenant B could be recorded
under tenant A's databases if the id was ever omitted.

The field is gone; every method now requires an explicit tenant and
refuses to guess. These tests pin the refusal and the absence of the
field.
"""
import pytest

from core.ingestion.builder import DocumentIndexBuilder


def _builder():
    return DocumentIndexBuilder()


# ---------------------------------------------------------------- structure

def test_the_builder_keeps_no_current_tenant_field():
    b = _builder()
    assert not hasattr(b, "tenant_id"), (
        "the builder is shared; a current-tenant field would hold whichever "
        "ingest ran last and could send a write to the wrong tenant"
    )


# ---------------------------------------------------------------- refusal

def test_get_dbs_refuses_without_a_tenant():
    b = _builder()
    with pytest.raises(ValueError, match="tenant id is required"):
        b._get_dbs(None)


def test_get_dbs_refuses_an_empty_string():
    b = _builder()
    with pytest.raises(ValueError, match="tenant id is required"):
        b._get_dbs("")


def test_require_tenant_returns_a_real_id():
    assert DocumentIndexBuilder._require_tenant("acme") == "acme"


def test_require_tenant_rejects_none_and_empty():
    with pytest.raises(ValueError):
        DocumentIndexBuilder._require_tenant(None)
    with pytest.raises(ValueError):
        DocumentIndexBuilder._require_tenant("")


def test_ingest_document_refuses_without_a_tenant(tmp_path):
    """An upload with no tenant must fail loudly, not write to "default"."""
    f = tmp_path / "x.txt"
    f.write_text("hello", encoding="utf-8")
    b = _builder()
    with pytest.raises(ValueError, match="tenant id is required"):
        b.ingest_document(str(f), tenant_id=None)


# ---------------------------------------------------------------- isolation

def test_two_tenants_do_not_share_a_builder_field():
    """After a tenant-A operation, a tenant-less call must not use A.

    This is the exact pre-fix hazard: A's ingest set ``self.tenant_id``,
    and a later call that omitted the id silently wrote into A.
    """
    b = _builder()
    # Simulate a completed ingest for tenant A having set the old field.
    # With the field removed there is nothing to leak, so a tenant-less
    # call is refused rather than redirected.
    assert not hasattr(b, "tenant_id")
    with pytest.raises(ValueError, match="tenant id is required"):
        b._get_dbs(None)


def test_each_call_takes_its_own_tenant():
    """Two different ids resolve to two different tenant paths."""
    from core.config import settings

    b = _builder()
    assert settings.get_tenant_data_dir("tenant_a") != settings.get_tenant_data_dir("tenant_b")
    # And the builder accepts each explicitly without any stored state.
    assert b._require_tenant("tenant_a") == "tenant_a"
    assert b._require_tenant("tenant_b") == "tenant_b"
