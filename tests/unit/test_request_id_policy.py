import pytest

from app.entrypoints.http.middlewares.request_id_policy import (
    ValidatedRequestIdResolver,
)


def test_resolver_preserves_safe_candidate() -> None:
    resolver = ValidatedRequestIdResolver(generator=lambda: "generated-id")

    assert resolver.resolve("edge:request_123") == "edge:request_123"


@pytest.mark.parametrize(
    "candidate",
    [None, "", "contains space", "line\nbreak", "x" * 129],
)
def test_resolver_replaces_missing_or_unsafe_candidate(
    candidate: str | None,
) -> None:
    resolver = ValidatedRequestIdResolver(generator=lambda: "generated-id")

    assert resolver.resolve(candidate) == "generated-id"


def test_resolver_rejects_unsafe_generated_value() -> None:
    resolver = ValidatedRequestIdResolver(generator=lambda: "unsafe value")

    with pytest.raises(
        ValueError,
        match="request id generator returned an unsafe value",
    ):
        resolver.resolve(None)


def test_resolver_rejects_non_positive_max_length() -> None:
    with pytest.raises(ValueError, match="max_length must be positive"):
        ValidatedRequestIdResolver(max_length=0)
