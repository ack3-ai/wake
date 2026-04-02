from __future__ import annotations

import functools
import inspect
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from enum import IntEnum
from typing import Callable, Iterator, Type

from .transactions import TransactionAbc


@dataclass
class RevertError(Exception):
    tx: TransactionAbc | None = field(
        init=False, compare=False, default=None, repr=False
    )

    def __str__(self):
        s = ", ".join(
            [f"{f.name}={getattr(self, f.name)!r}" for f in fields(self) if f.init]
        )
        return f"{self.__class__.__qualname__}({s})"


@dataclass
class UnknownRevertError(RevertError):
    data: bytes


class ExternalError(RevertError):
    _error_name: str
    _error_full_name: str

    def __init__(self, _error_full_name, **kwargs):
        self._error_full_name = _error_full_name

        if "." in self._error_full_name:
            self._error_name = self._error_full_name.split(".")[-1]
        else:
            self._error_name = self._error_full_name

        self.tx = None

        self._extra_attrs = {}
        for key, value in kwargs.items():
            setattr(self, key, value)
            self._extra_attrs[key] = value

    def __repr__(self):
        cls_name = self.__class__.__qualname__
        base_repr = f"{cls_name}(_error_full_name='{self._error_full_name}'"

        for key, value in self._extra_attrs.items():
            if isinstance(value, str):
                base_repr += f", {key}='{value}'"
            else:
                base_repr += f", {key}={value}"

        base_repr += ")"
        return base_repr

    def __str__(self):
        return self.__repr__()

    def __eq__(self, other):
        if not isinstance(other, ExternalError):
            return False

        if self._error_full_name != other._error_full_name:
            return False

        for key, value in self._extra_attrs.items():
            if not hasattr(other, key) or getattr(other, key) != value:
                return False

        for key in getattr(other, "_extra_attrs", {}):
            if key not in self._extra_attrs:
                return False

        return True

    def __hash__(self):
        # Create a tuple of (_error_full_name, (key1, value1), (key2, value2), ...)
        # and hash that tuple
        try:
            items = [(key, value) for key, value in sorted(self._extra_attrs.items())]
            return hash((self._error_full_name, tuple(items)))
        except TypeError as e:
            # Provide a more informative error message
            for key, value in self._extra_attrs.items():
                try:
                    hash(value)
                except TypeError:
                    raise TypeError(
                        f"ExternalError unhashable: attribute '{key}' with value {value!r} is not hashable"
                    ) from e
            raise


@dataclass
class Error(RevertError):
    _abi = {
        "name": "Error",
        "type": "error",
        "inputs": [{"internalType": "string", "name": "message", "type": "string"}],
    }
    selector = bytes.fromhex("08c379a0")
    message: str


@dataclass
class Halt(Exception):
    tx: TransactionAbc | None = field(
        init=False, compare=False, default=None, repr=False
    )
    message: str

    def __str__(self):
        s = ", ".join(
            [f"{f.name}={getattr(self, f.name)!r}" for f in fields(self) if f.init]
        )
        return f"{self.__class__.__qualname__}({s})"


class PanicCodeEnum(IntEnum):
    GENERIC = 0
    "Generic compiler panic"
    ASSERT_FAIL = 1
    "Assert evaluated to false"
    UNDERFLOW_OVERFLOW = 0x11
    "Integer underflow or overflow"
    DIVISION_MODULO_BY_ZERO = 0x12
    "Division or modulo by zero"
    INVALID_CONVERSION_TO_ENUM = 0x21
    "Too big or negative integer for conversion to enum"
    ACCESS_TO_INCORRECTLY_ENCODED_STORAGE_BYTE_ARRAY = 0x22
    "Access to incorrectly encoded storage byte array"
    POP_EMPTY_ARRAY = 0x31
    ".pop() on empty array"
    INDEX_ACCESS_OUT_OF_BOUNDS = 0x32
    "Out-of-bounds or negative index access to fixed-length array"
    TOO_MUCH_MEMORY_ALLOCATED = 0x41
    "Too much memory allocated"
    INVALID_INTERNAL_FUNCTION_CALL = 0x51
    "Called invalid internal function"


@dataclass
class Panic(RevertError):
    _abi = {
        "name": "Panic",
        "type": "error",
        "inputs": [{"internalType": "uint256", "name": "code", "type": "uint256"}],
    }
    selector = bytes.fromhex("4e487b71")
    code: "PanicCodeEnum"


class ExceptionWrapper:
    value: Exception | None = None


@contextmanager
def must_revert(
    *exceptions: str | int | Exception | Type[Exception],
) -> Iterator[ExceptionWrapper]:
    if len(exceptions) == 0:
        exceptions = (RevertError,)

    normalized: list[Exception | Type[Exception]] = []
    for ex in exceptions:
        if isinstance(ex, str):
            normalized.append(Error(ex))
        elif isinstance(ex, int):
            normalized.append(Panic(PanicCodeEnum(ex)))
        else:
            normalized.append(ex)

    types = tuple(type(x) if not inspect.isclass(x) else x for x in normalized)

    wrapper = ExceptionWrapper()

    try:
        yield wrapper
        raise AssertionError(f"Expected revert of type {exceptions}")
    except types as e:  # pyright: ignore reportGeneralTypeIssues
        wrapper.value = e

        if any(
            (inspect.isclass(ex) and issubclass(type(e), ex)) or e == ex
            for ex in normalized
        ):
            return
        raise


@contextmanager
def may_revert(
    *exceptions: str | int | Exception | Type[Exception],
) -> Iterator[ExceptionWrapper]:
    if len(exceptions) == 0:
        exceptions = (RevertError,)

    normalized: list[Exception | Type[Exception]] = []
    for ex in exceptions:
        if isinstance(ex, str):
            normalized.append(Error(ex))
        elif isinstance(ex, int):
            normalized.append(Panic(PanicCodeEnum(ex)))
        else:
            normalized.append(ex)

    types = tuple(type(x) if not inspect.isclass(x) else x for x in normalized)

    wrapper = ExceptionWrapper()

    try:
        yield wrapper
    except types as e:  # pyright: ignore reportGeneralTypeIssues
        wrapper.value = e

        if any(
            (inspect.isclass(ex) and issubclass(type(e), ex)) or e == ex
            for ex in normalized
        ):
            return
        raise


def on_revert(callback: Callable[[RevertError], None]):
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            try:
                return f(*args, **kwargs)
            except RevertError as e:
                callback(e)
                raise

        return wrapper

    return decorator
