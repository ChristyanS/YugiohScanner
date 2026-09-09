"""Descoberta, paralelismo e orquestração do scanner (plano §5 e §12)."""

from .discovery import (
    DiscoveredImage,
    discover_images,
    hash_file,
    iter_image_paths,
    resolve_scan_folder,
)
from .executor import (
    ProcessPoolScanExecutor,
    ScanExecutor,
    SerialScanExecutor,
    ThreadPoolScanExecutor,
    create_executor,
    resolve_workers,
)
from .pipeline import run_pipeline
from .worker import ScanOutcome, ScanTask, init_worker, process_task, reset_provider, set_provider

__all__ = [
    "DiscoveredImage",
    "ProcessPoolScanExecutor",
    "ScanExecutor",
    "ScanOutcome",
    "ScanTask",
    "SerialScanExecutor",
    "ThreadPoolScanExecutor",
    "create_executor",
    "discover_images",
    "hash_file",
    "init_worker",
    "iter_image_paths",
    "process_task",
    "reset_provider",
    "resolve_scan_folder",
    "resolve_workers",
    "run_pipeline",
    "set_provider",
]
