"""The contract every alert channel implements. This is the seam external contributions
(e.g. the SMS PR) code against — see CONTRIBUTING.md."""
from abc import ABC, abstractmethod

from monitor.state import ConfigErrorEvent, DownEvent, RecoveryEvent

AlertEvent = DownEvent | RecoveryEvent | ConfigErrorEvent


class AlertChannel(ABC):
    name: str = "channel"

    @abstractmethod
    def send(self, event: AlertEvent) -> None:
        """Deliver one alert. Called synchronously (dispatch() runs it in a thread), so
        blocking network calls are fine. Raise on failure — dispatch() catches per channel."""
        raise NotImplementedError
