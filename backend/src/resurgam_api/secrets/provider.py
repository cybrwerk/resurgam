from typing import Protocol


class SecretNotFoundError(Exception):
    def __init__(self, reference_key: str) -> None:
        self.reference_key = reference_key
        super().__init__(f"no secret stored for reference key {reference_key!r}")


class SecretsProvider(Protocol):
    """The seam every secret-storing feature depends on. `local_provider.py`
    is the only implementation today; manifest §4/§6 name OpenBao as the
    real target — swapping providers later means implementing this
    protocol again, not changing any caller (see docs/adr/0005).
    """

    def store(self, reference_key: str, plaintext: str) -> None: ...

    def retrieve(self, reference_key: str) -> str: ...

    def delete(self, reference_key: str) -> None: ...
