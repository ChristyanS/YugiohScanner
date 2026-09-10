"""Cache local de imagens do catálogo — sob demanda, servido pela própria app (plano §14.1).

A política do YGOPRODeck proíbe hotlink e desaconselha baixar tudo de uma vez
(§0.2). A solução é preguiçosa: a primeira vez que uma arte é pedida, ela é
baixada **uma vez** para `data/images/{cards,cards_small}/{passcode}.jpg` e
servida do disco daí em diante — inclusive offline, com o cache quente.

Downloads concorrentes da MESMA imagem são serializados por um lock em
memória por `(tamanho, passcode)` — sem isso, duas abas pedindo a mesma carta
ao mesmo tempo disparariam dois downloads e uma delas perderia a corrida do
`rename` atômico à toa.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Literal

from ..config import Settings
from ..db.tables import Card, CardImage
from ..errors import YugiohScannerError
from ..ygoprodeck.client import YgoProDeckClient

ImageSize = Literal["small", "full"]

_SUBDIR: dict[ImageSize, str] = {"small": "cards_small", "full": "cards"}


class ImageNotAvailableError(YugiohScannerError):
    """A carta não tem nenhuma arte cadastrada no catálogo local."""

    def __init__(self, card_id: int) -> None:
        super().__init__(f"Carta #{card_id} não tem imagem cadastrada.")


class ImageCache:
    """Download-once + layout em disco (plano §14.1)."""

    def __init__(self, settings: Settings, client: YgoProDeckClient) -> None:
        self.settings = settings
        self.client = client
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def path_for(self, passcode: int, size: ImageSize) -> Path:
        return self.settings.images_path / _SUBDIR[size] / f"{passcode}.jpg"

    def primary_image(self, card: Card) -> CardImage:
        for image in card.images:
            if image.is_primary:
                return image
        if card.images:
            return card.images[0]
        raise ImageNotAvailableError(card.id)

    def ensure_cached(self, card: Card, size: ImageSize) -> Path:
        """Devolve o caminho no disco, baixando uma única vez se preciso.

        `size="small"` cai para a URL cheia quando o catálogo não trouxe uma
        variante pequena (algumas artes alternativas não têm `image_url_small`).
        """
        image = self.primary_image(card)
        destination = self.path_for(image.id, size)
        if destination.exists():
            return destination

        if size == "small" and image.image_url_small:
            url = image.image_url_small
        else:
            url = image.image_url

        lock = self._lock_for(size, image.id)
        with lock:
            if destination.exists():  # outra thread já baixou enquanto esperávamos o lock
                return destination
            self.client.download_image(url, destination)
            return destination

    def _lock_for(self, size: ImageSize, passcode: int) -> threading.Lock:
        key = f"{size}:{passcode}"
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock
