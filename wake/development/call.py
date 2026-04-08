from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any, Generic, Literal, Type, TypeVar

from .blocks import Block
from .call_trace import CallTrace
from .core import Account, Chain, TxParams
from .internal import ExecutionStatusEnum

if TYPE_CHECKING:
    from .core import Address
    from .errors import Halt, RevertError, UnknownRevertError


T = TypeVar("T")


def _resolve_pending_block(f):
    @functools.wraps(f)
    def wrapper(self: Call):
        if self._block == "pending":
            # try to resolve the pending block if it was already mined
            assert self._latest_block is not None
            if self._chain.chain_interface.get_block_number() >= self._latest_block + 1:
                self._block = self._latest_block + 1

        return f(self)

    return wrapper


class Call(Generic[T]):
    _tx_params: TxParams
    _block: int | Literal["pending"]
    _chain: Chain
    _abi: dict[str, Any] | None
    _return_type: Type
    _raw_return_value: bytes | None
    _return_value: T | None
    _raw_error: bytes | str | None
    _error: RevertError | Halt | None
    _debug_trace: dict[str, Any] | None
    _estimated_gas: int | None
    _access_list: dict[Address, list[int]] | None
    _latest_block: int | None  # used to resolve pending block

    def __init__(
        self,
        tx_params: TxParams,
        block: int | Literal["latest", "pending", "earliest", "safe", "finalized"],
        chain: Chain,
        abi: dict[str, Any] | None,
        return_type: Type,
        raw_return_value: bytes | None,
        raw_error: bytes | str | None,
        estimated_gas: int | None,
        access_list: dict[Address, list[int]] | None,
    ):
        self._tx_params = tx_params
        self._chain = chain
        self._abi = abi
        self._return_type = return_type
        self._raw_return_value = raw_return_value
        self._return_value = None  # to be lazy evaluated
        self._raw_error = raw_error
        self._error = None  # to be lazy evaluated
        self._debug_trace = None  # to be lazy fetched
        self._estimated_gas = estimated_gas
        self._access_list = access_list
        self._latest_block = None

        if isinstance(block, str):
            if block != "pending":
                # resolve block number from string
                # IMPORTANT: may be subject to race conditions (fetch of input params vs time of block resolution)
                self._block = int(
                    self._chain.chain_interface.get_block(block)["number"], 16
                )
            else:
                self._block = "pending"
                self._latest_block = self._chain.chain_interface.get_block_number()
        else:
            self._block = block

    @property
    def chain(self) -> Chain:
        return self._chain

    @property
    @_resolve_pending_block
    def block(self) -> Block:
        return self._chain.blocks[self._block]

    @property
    def data(self) -> bytes:
        return self._tx_params["data"] if "data" in self._tx_params else b""

    @property
    def from_(self) -> Account:
        assert "from" in self._tx_params
        return Account(self._tx_params["from"], self._chain)

    @property
    def to(self) -> Account | None:
        return (
            Account(self._tx_params["to"], self._chain)
            if "to" in self._tx_params
            else None
        )

    @property
    def status(self) -> ExecutionStatusEnum:
        if self._raw_error is not None:
            return ExecutionStatusEnum.FAILURE
        else:
            return ExecutionStatusEnum.SUCCESS

    @property
    @_resolve_pending_block
    def error(self) -> RevertError | Halt | None:
        from .errors import Halt
        from .pytypes_resolver import resolve_call_error

        if self._error is not None:
            return self._error
        elif self._raw_error is None:
            return None
        elif isinstance(self._raw_error, str):
            self._error = Halt(self._raw_error)
            self._error.call = self
        else:
            self._error = resolve_call_error(
                self._chain, self._tx_params, self._block, self._raw_error
            )
            self._error.call = self

        return self._error

    @property
    def raw_error(self) -> UnknownRevertError | Halt | None:
        from .errors import Halt, UnknownRevertError

        if self._raw_error is None:
            return None
        elif isinstance(self._raw_error, bytes):
            error = UnknownRevertError(self._raw_error)
            error.call = self
            return error
        elif isinstance(self._raw_error, str):
            error = Halt(self._raw_error)
            error.call = self
            return error
        else:
            raise ValueError(f"Unexpected raw error type: {type(self._raw_error)}")

    @property
    def return_value(self) -> T:
        if self.error is not None:
            raise self.error
        assert self._raw_return_value is not None

        if self._return_value is None:
            if self._abi is None:
                self._return_value = self._raw_return_value
            else:
                self._return_value = self._chain._process_return_data(
                    self._raw_return_value, self._abi, self._return_type
                )

        return self._return_value

    @property
    def raw_return_value(self) -> bytes:
        if self.error is not None:
            raise self.error
        assert self._raw_return_value is not None
        return self._raw_return_value

    @property
    @_resolve_pending_block
    def call_trace(self) -> CallTrace:
        if self._debug_trace is None:
            self._debug_trace = self._chain.chain_interface.debug_trace_call(
                self._tx_params, self._block
            )

        return CallTrace.from_debug_trace(
            self._debug_trace,
            self._tx_params,
            self.chain,
            None,
            self._block,
        )

    @property
    @_resolve_pending_block
    def access_list(self) -> dict[Address, list[int]]:
        from .core import Address

        if self._access_list is not None:
            return self._access_list
        elif self._raw_error is not None:
            raise RuntimeError("Access list is not available because the call reverted")
        else:
            params_copy = self._tx_params.copy()
            params_copy.pop("accessList", None)
            response = self._chain.chain_interface.create_access_list(
                params_copy, self._block
            )
            self._access_list = {
                Address(e["address"]): [int(s, 16) for s in e["storageKeys"]]
                for e in response["accessList"]
            }

            return self._access_list

    @property
    @_resolve_pending_block
    def estimated_gas(self) -> int:
        if self._estimated_gas is not None:
            return self._estimated_gas
        elif self._raw_error is not None:
            raise RuntimeError("Estimate is not available because the call reverted")
        else:
            params_copy = self._tx_params.copy()
            params_copy.pop("gas", None)
            params_copy.pop("gasPrice", None)
            params_copy.pop("maxPriorityFeePerGas", None)
            params_copy.pop("maxFeePerGas", None)
            self._estimated_gas = self._chain.chain_interface.estimate_gas(
                params_copy, self._block
            )

            return self._estimated_gas
