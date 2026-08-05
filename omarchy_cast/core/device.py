from dataclasses import dataclass

PROTOCOLS = ("airplay", "cast")


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    address: str
    port: int
    protocol: str
    model: str | None = None

    def __post_init__(self) -> None:
        if self.protocol not in PROTOCOLS:
            raise ValueError(f"unknown protocol: {self.protocol}")

    @staticmethod
    def make_id(protocol: str, unique: str) -> str:
        return f"{protocol}:{unique}"
