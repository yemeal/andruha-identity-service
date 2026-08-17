import hashlib
import secrets

from app.application.ports.dto import IssuedRefreshToken


class SHA256OpaqueRefreshTokenCodec:
    TOKEN_BYTES = 32

    def issue(self) -> IssuedRefreshToken:
        token = secrets.token_urlsafe(self.TOKEN_BYTES)

        return IssuedRefreshToken(value=token, digest=self.digest(token))

    def digest(self, token: str) -> bytes:
        # UTF-8 makes arbitrary client input a safe lookup miss.
        return hashlib.sha256(token.encode("utf-8")).digest()
