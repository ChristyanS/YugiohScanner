"""Rate limiter do cliente HTTP (plano §20.4).

Existe porque a suíte desliga o limite nos demais testes: sem esta cobertura,
uma regressão que removesse o espaçamento entre requisições passaria batido —
e o resultado seria o IP na lista de bloqueio do YGOPRODeck.
"""

from __future__ import annotations

import threading
import time

from yugioh_scanner.ygoprodeck.client import RateLimiter


class TestSpacing:
    def test_spaces_requests_in_time(self) -> None:
        limiter = RateLimiter(rate_per_second=50)  # 20 ms entre chamadas
        started = time.monotonic()
        for _ in range(5):
            limiter.acquire()
        elapsed = time.monotonic() - started
        # 5 chamadas => pelo menos 4 intervalos de 20 ms.
        assert elapsed >= 0.07

    def test_faster_rate_takes_less_time(self) -> None:
        slow, fast = RateLimiter(50), RateLimiter(500)

        def measure(limiter: RateLimiter) -> float:
            started = time.monotonic()
            for _ in range(4):
                limiter.acquire()
            return time.monotonic() - started

        assert measure(fast) < measure(slow)

    def test_zero_rate_disables_throttling(self) -> None:
        limiter = RateLimiter(rate_per_second=0)
        started = time.monotonic()
        for _ in range(100):
            limiter.acquire()
        assert time.monotonic() - started < 0.05


class TestConcurrency:
    def test_is_thread_safe(self) -> None:
        """Threads concorrentes não podem furar o intervalo."""
        limiter = RateLimiter(rate_per_second=100)  # 10 ms
        timestamps: list[float] = []
        lock = threading.Lock()

        def worker() -> None:
            limiter.acquire()
            with lock:
                timestamps.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        timestamps.sort()
        span = timestamps[-1] - timestamps[0]
        # 8 aquisições a 10 ms => pelo menos ~70 ms de janela.
        assert span >= 0.05
        assert len(timestamps) == 8
