import uuid


class PaymentError(Exception):
    pass


class Gateway:
    """Base class for payment gateways."""

    def charge(self, amount: float, token: str) -> str:
        self.validate(amount)
        return self._send(amount, token)

    def validate(self, amount: float) -> None:
        if amount <= 0:
            raise PaymentError("amount must be positive")

    def _send(self, amount: float, token: str) -> str:
        raise NotImplementedError


class CardGateway(Gateway):
    def _send(self, amount: float, token: str) -> str:
        if not token.startswith("tok_"):
            raise PaymentError("bad card token")
        return "ch_" + uuid.uuid4().hex[:12]


class FakeGateway(Gateway):
    """Accepts everything. Used in tests."""

    def _send(self, amount: float, token: str) -> str:
        return "ch_fake"


def get_gateway(name: str) -> Gateway:
    return FakeGateway() if name == "fake" else CardGateway()
