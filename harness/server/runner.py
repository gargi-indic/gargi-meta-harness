"""uvicorn server subclass for ``harness serve``: prompt SSE shutdown and a post-bind announcement hook."""

import socket
from collections.abc import Callable
from types import FrameType

import uvicorn

from harness.server.events import EventBus


class HarnessServer(uvicorn.Server):
    """Closes the SSE bus as soon as a shutdown is requested (open streams would otherwise block uvicorn's
    graceful shutdown) and runs ``on_started`` only once the socket is bound."""

    def __init__(self, config: uvicorn.Config, *, bus: EventBus, on_started: Callable[[], None]) -> None:
        super().__init__(config)
        self._bus = bus
        self._on_started = on_started

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        if self.started:
            self._on_started()

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self._bus.close()
        super().handle_exit(sig, frame)
