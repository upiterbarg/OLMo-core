import datetime
import logging
import sys
from typing import Any, Callable, Dict, Optional, Tuple

import pytest
import torch
import torch.distributed as dist
import torch.multiprocessing as mp

from olmo_core.distributed.fsdp import FSDPPrecision
from olmo_core.distributed.utils import is_distributed

BACKENDS = [pytest.param("gloo", id="backend=GLOO")]
DEVICES = [pytest.param(torch.device("cpu"), id="device=CPU")]
LOW_PRECISION_DTYPES = [pytest.param(torch.float16, id="dtype=float16")]
FSDP_MIXED_PRECISION = [
    pytest.param(FSDPPrecision(param_dtype=torch.float16, reduce_dtype=None), id="param_dtype=FP16"),
    pytest.param(
        FSDPPrecision(param_dtype=torch.float16, reduce_dtype=torch.float16),
        id="param_dtype=FP16,reduce_dtype=FP16",
    ),
    pytest.param(
        FSDPPrecision(param_dtype=torch.float16, reduce_dtype=torch.float16, keep_low_precision_grads=True),
        id="param_dtype=FP16,reduce_dtype=FP16,keep_LP",
    ),
    pytest.param(
        FSDPPrecision(param_dtype=torch.float16, reduce_dtype=torch.float32),
        id="param_dtype=FP16,reduce_dtype=FP32",
    ),
]

if torch.cuda.is_available():
    if torch.cuda.device_count() > 1:
        BACKENDS.append(pytest.param("nccl", id="backend=NCCL", marks=pytest.mark.gpu))
    DEVICES.append(pytest.param(torch.device("cuda"), id="device=CUDA", marks=pytest.mark.gpu))
    LOW_PRECISION_DTYPES.append(pytest.param(torch.bfloat16, id="dtype=bfloat16", marks=pytest.mark.gpu))
    FSDP_MIXED_PRECISION.extend(
        [
            pytest.param(
                FSDPPrecision(param_dtype=torch.bfloat16, reduce_dtype=None),
                id="param_dtype=BF16",
                marks=pytest.mark.gpu,
            ),
            pytest.param(
                FSDPPrecision(param_dtype=torch.bfloat16, reduce_dtype=torch.bfloat16),
                id="param_dtype=BF16,reduce_dtype=BF16",
                marks=pytest.mark.gpu,
            ),
            pytest.param(
                FSDPPrecision(param_dtype=torch.bfloat16, reduce_dtype=torch.float32),
                id="param_dtype=BF16,reduce_dtype=FP32",
                marks=pytest.mark.gpu,
            ),
        ]
    )


def get_default_device():
    if is_distributed():
        backend = dist.get_backend()
        if backend == dist.Backend.GLOO:
            return torch.device("cpu")
        elif backend == dist.Backend.NCCL:
            return torch.device("cuda")
        else:
            raise NotImplementedError(backend)
    elif torch.cuda.is_available():
        return torch.device("cuda")
    else:
        return torch.device("cpu")


def init_process(
    process_rank: int,
    world_size: int,
    backend: str,
    log_from_all_ranks: bool,
    func: Callable,
    func_args: Optional[Tuple[Any, ...]] = None,
    func_kwargs: Optional[Dict[str, Any]] = None,
    primary_addr: str = "127.0.0.1",
    primary_port: int = 29500,
):
    assert world_size > 1

    old_log_record_factory = logging.getLogRecordFactory()

    def log_record_factory(*args, **kwargs) -> logging.LogRecord:
        record = old_log_record_factory(*args, **kwargs)
        setattr(record, "local_rank", dist.get_rank())
        return record

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("[rank %(local_rank)s] %(asctime)s:%(name)s:%(lineno)s:%(levelname)s: %(message)s")
    )
    logging.setLogRecordFactory(log_record_factory)

    if log_from_all_ranks or process_rank == 0:
        logging.basicConfig(level=logging.DEBUG, handlers=[handler])

    log = logging.getLogger()

    dist.init_process_group(
        backend=backend,
        init_method=f"tcp://{primary_addr}:{primary_port}",
        world_size=world_size,
        rank=process_rank,
        timeout=datetime.timedelta(seconds=120),
    )

    log.info("Starting test...")

    if torch.cuda.is_available():
        torch.cuda.set_device(int(process_rank))

    try:
        func(*(func_args or []), **(func_kwargs or {}))
    finally:
        dist.destroy_process_group()


def run_distributed_test(
    func: Callable,
    world_size: int = 2,
    log_from_all_ranks: bool = False,
    backend: str = "gloo",
    start_method: Optional[str] = None,
    func_args: Optional[Tuple[Any, ...]] = None,
    func_kwargs: Optional[Dict[str, Any]] = None,
):
    """
    This runs the `func` in a simulated distributed environment.
    """
    if start_method is None:
        start_method = "fork" if backend == "gloo" else "spawn"

    mp.start_processes(
        init_process,
        args=(world_size, backend, log_from_all_ranks, func, func_args, func_kwargs),
        nprocs=world_size,
        start_method=start_method,
    )
