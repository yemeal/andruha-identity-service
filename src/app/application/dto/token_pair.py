from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field


class TokenPair(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    access_token: str = Field(repr=False)
    refresh_token: str = Field(repr=False)
    token_type: str = "bearer"


@dataclass(frozen=True)
class IssuedTokenPair:
    pair: TokenPair
    refresh_digest: bytes = field(repr=False)
