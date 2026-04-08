from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any, Callable, Dict, Union, cast

import eth_utils

from wake_rs.wake_rs import Account, Address

from .chain_interfaces import (
    AnvilChainInterface,
    GethLikeChainInterfaceAbc,
    HardhatChainInterface,
)
from .json_rpc.communicator import JsonRpcError

if TYPE_CHECKING:
    from .chain_interfaces import TxParams
    from .core import Chain
    from .errors import ExternalError, RevertError, UnknownRevertError
    from .internal import ExternalEvent, UnknownEvent
    from .transactions import TransactionAbc


def _str_to_bytes(s: str) -> bytes:
    if s.startswith("0x"):
        return bytes.fromhex(s[2:])
    return bytes.fromhex(s)


def _new_unknown_error(
    revert_data: bytes, tx: TransactionAbc | None
) -> UnknownRevertError:
    from .errors import UnknownRevertError

    err = UnknownRevertError(revert_data)
    err.tx = tx
    return err


def _new_external_or_unknown_error(
    revert_data: bytes, tx: TransactionAbc | None, fqn_addr: Address, chain: Chain
) -> ExternalError | UnknownRevertError:
    from .core import Abi, fix_library_abi
    from .errors import ExternalError
    from .utils import get_name_abi_from_explorer_cached

    if chain.forked_chain_id is None or len(revert_data) < 4:
        return _new_unknown_error(revert_data, tx)

    explorer_info = get_name_abi_from_explorer_cached(
        str(fqn_addr), chain.forked_chain_id
    )
    if explorer_info is None:
        return _new_unknown_error(revert_data, tx)

    name, contract_abi = explorer_info

    if revert_data[:4] not in contract_abi:
        return _new_unknown_error(revert_data, tx)

    error_abi = contract_abi[revert_data[:4]]
    types = [
        eth_utils.abi.collapse_if_tuple(cast(Dict[str, Any], arg))
        for arg in fix_library_abi(error_abi["inputs"])
    ]
    decoded = Abi.decode(types, revert_data[4:])

    kwargs = {}
    unnamed_params_index = 0
    param_names = {input_abi.get("name") for input_abi in error_abi["inputs"]}
    for input_abi, value in zip(error_abi["inputs"], decoded):
        param_name = input_abi.get("name")
        if not param_name:
            param_name = f"param{unnamed_params_index}"
            unnamed_params_index += 1
            while param_name in param_names:
                param_name += "_"

        kwargs[param_name] = value

    error = ExternalError(f"{name}.{error_abi['name']}", **kwargs)
    error.tx = tx
    return error


def _process_call_trace_error(
    frame: Dict[str, Any],
    revert_data: bytes,
    created_contracts: Dict[Address, bytes],
) -> Address | bytes | None:
    """
    Returns either the init code or address from the inner-most frame in the first subtrace that reverted
    with given revert data.

    Init code is returned for CREATE(2) frames, address for other frames, None if no such frame exists.
    """
    # frame["to"] not available if the frame reverted
    if frame["type"] in ("CREATE", "CREATE2") and "to" in frame:
        created_contracts[Address(frame["to"])] = _str_to_bytes(frame["input"])

    for child in frame.get("calls", []):
        result = _process_call_trace_error(child, revert_data, created_contracts)
        if result is not None:
            return result

    if "error" in frame and _str_to_bytes(frame.get("output", "")) == revert_data:
        if frame["type"] in ("CREATE", "CREATE2"):
            return _str_to_bytes(frame["input"])
        else:
            assert "to" in frame
            return Address(frame["to"])

    return None


def _resolve_fqn(
    result: Address | bytes,
    chain: Chain,
    block: Union[int, str],
    created_contracts: Dict[Address, bytes],
) -> str | None:
    from wake.development.core import get_fqn_from_address, get_fqn_from_creation_code

    if isinstance(result, Address):
        if result in created_contracts:
            try:
                return get_fqn_from_creation_code(created_contracts[result])[0]
            except ValueError:
                return None
        else:
            return get_fqn_from_address(result, block, chain)
    else:
        try:
            return get_fqn_from_creation_code(result)[0]
        except ValueError:
            return None


def _build_revert_error(
    module_name: str,
    attrs: Any,
    chain: Chain,
    tx: TransactionAbc | None,
    revert_data: bytes,
) -> RevertError:
    from wake.development.core import fix_library_abi
    from wake_rs import Abi

    obj = getattr(importlib.import_module(module_name), attrs[0])
    for attr in attrs[1:]:
        obj = getattr(obj, attr)
    abi = obj._abi

    types = [
        eth_utils.abi.collapse_if_tuple(cast(Dict[str, Any], arg))
        for arg in fix_library_abi(abi["inputs"])
    ]
    decoded = Abi.decode(types, revert_data[4:])
    generated_error = chain._convert_from_web3_type(decoded, obj)
    generated_error.tx = tx
    return generated_error


def _resolve_error(
    chain: Chain,
    block: str | int,
    tx: TransactionAbc | None,
    revert: bytes,
    trace_retriever: Callable[[], Dict[str, Any]],
) -> RevertError:
    from .core import errors

    if len(revert) < 4:
        return _new_unknown_error(revert, tx)

    selector = revert[:4]

    # Error and Panic
    if selector in (b"\x08\xc3\x79\xa0", b"\x4e\x48\x7b\x71"):
        fqn = ""
    else:
        trace = trace_retriever()

        created_contracts = {}
        result = _process_call_trace_error(trace, revert, created_contracts)
        if result is None:
            return _new_unknown_error(revert, tx)

        if (
            isinstance(result, Address)
            and (resolver := chain._pytypes_resolvers.get(Account(result, chain)))
            is not None
        ):
            # pytypes resolver is attached
            fqn = getattr(resolver, "_fqn", None)
        else:
            # using post-tx/same-call block number to cover txs from the same block but before this one
            fqn = _resolve_fqn(result, chain, block, created_contracts)

        if fqn is None or selector not in errors or fqn not in errors[selector]:
            if isinstance(result, bytes):
                # contract was being created - cannot be forked
                return _new_unknown_error(revert, tx)
            else:
                return _new_external_or_unknown_error(revert, tx, result, chain)

    # both `errors` keys guaranteed to exist at this point
    module_name, attrs = errors[selector][fqn]
    return _build_revert_error(module_name, attrs, chain, tx, revert)


def resolve_tx_error(
    chain: Chain, tx: TransactionAbc, revert_data: bytes
) -> RevertError:
    return _resolve_error(
        chain,
        tx.block.number,
        tx,
        revert_data,
        lambda: chain.chain_interface.debug_trace_transaction(
            tx.tx_hash, {"tracer": "callTracer"}
        ),
    )


def extract_call_revert_data(chain: Chain, e: JsonRpcError) -> bytes:
    try:
        # Hermez does not provide revert data for estimate
        if (
            isinstance(
                chain._chain_interface,
                (AnvilChainInterface, GethLikeChainInterfaceAbc),
            )
            and e.data["code"] == 3
        ):
            revert_data = e.data["data"]
        elif (
            isinstance(chain._chain_interface, HardhatChainInterface)
            and e.data["code"] == -32603
        ):
            revert_data = e.data["data"]["data"]
        else:
            raise e from None
    except Exception:
        raise e from None

    return _str_to_bytes(revert_data)


def resolve_call_error(
    chain: Chain, call: TxParams, block: Union[int, str], error: bytes
) -> RevertError:
    return _resolve_error(
        chain,
        block,
        None,
        error,
        lambda: chain.chain_interface.debug_trace_call(
            call, block, {"tracer": "callTracer"}
        ),
    )


def _process_call_trace_events(
    frame: Dict[str, Any],
    created_contracts: Dict[Address, bytes],
) -> list:
    if frame.get("error"):
        return []
    assert "to" in frame

    logs = [
        {
            "address": log["address"],
            "codeAddress": frame["to"],
            "topics": log["topics"],
            "data": log["data"],
            "index": log["index"],
        }
        for log in frame.get("logs", [])
    ]

    if frame["type"] in ("CREATE", "CREATE2"):
        created_contracts[Address(frame["to"])] = _str_to_bytes(frame["input"])

    for child in frame.get("calls", []):
        logs.extend(_process_call_trace_events(child, created_contracts))

    return logs


def _new_unknown_event(
    topics: list[bytes], data: bytes, origin: Account
) -> UnknownEvent:
    from .internal import UnknownEvent

    event = UnknownEvent(topics, data)
    event.origin = origin
    return event


def _new_external_or_unknown_event(
    topics: list[bytes], data: bytes, origin: Account
) -> ExternalEvent | UnknownEvent:
    from .internal import ExternalEvent
    from .utils import get_name_abi_from_explorer_cached

    if len(topics) == 0:
        return _new_unknown_event(topics, data, origin)

    chain = origin.chain
    if chain.forked_chain_id is None:
        return _new_unknown_event(topics, data, origin)

    explorer_info = get_name_abi_from_explorer_cached(
        str(origin.address), chain.forked_chain_id
    )
    if explorer_info is None:
        return _new_unknown_event(topics, data, origin)

    name, contract_abi = explorer_info

    if topics[0] not in contract_abi:
        return _new_unknown_event(topics, data, origin)

    event_abi = contract_abi[topics[0]]
    decoded = _decode_event(event_abi, topics, data)

    kwargs = {}
    unnamed_params_index = 0
    param_names = {input_abi.get("name") for input_abi in event_abi["inputs"]}
    for input_abi, value in zip(event_abi["inputs"], decoded):
        param_name = input_abi.get("name")
        if not param_name:
            param_name = f"param{unnamed_params_index}"
            unnamed_params_index += 1
            while param_name in param_names:
                param_name += "_"

        kwargs[param_name] = value

    event = ExternalEvent(f"{name}.{event_abi['name']}", **kwargs)
    event.origin = origin
    return event


def _decode_event(
    event_abi: dict[str, Any], topics: list[bytes], data: bytes
) -> tuple[Any, ...]:
    from .core import Abi, fix_library_abi

    topic_index = 1
    types = []
    decoded_indexed = []

    for input in fix_library_abi(event_abi["inputs"]):
        if input["indexed"]:
            if input["type"] in {"string", "bytes", "tuple"} or input["type"].endswith(
                "]"
            ):
                topic_type = "bytes32"
            else:
                topic_type = input["type"]

            decoded_indexed.append(Abi.decode([topic_type], topics[topic_index])[0])
            topic_index += 1
        else:
            types.append(eth_utils.abi.collapse_if_tuple(input))
    decoded = list(Abi.decode(types, data))
    merged = []

    for input in event_abi["inputs"]:
        if input["indexed"]:
            merged.append(decoded_indexed.pop(0))
        else:
            merged.append(decoded.pop(0))

    return tuple(merged)


def _build_event(
    module_name: str,
    attrs: list[str],
    tx: TransactionAbc,
    topics: list[bytes],
    data: bytes,
    origin: Account,
):
    obj = getattr(importlib.import_module(module_name), attrs[0])
    for attr in attrs[1:]:
        obj = getattr(obj, attr)

    decoded = _decode_event(obj._abi, topics, data)

    generated_event = tx.chain._convert_from_web3_type(decoded, obj)
    generated_event.origin = origin
    return generated_event


def resolve_tx_events(tx: TransactionAbc):
    from .core import events, get_fqn_from_address, get_fqn_from_creation_code

    trace = tx._chain.chain_interface.debug_trace_transaction(
        tx.tx_hash,
        {"tracer": "callTracer", "tracerConfig": {"withLog": True}},
    )
    created_contracts = {}
    logs = _process_call_trace_events(trace, created_contracts)
    logs.sort(key=lambda log: int(log["index"], 16))

    generated_events = []

    for log in logs:
        topics = [_str_to_bytes(t).rjust(32, b"\x00") for t in log["topics"]]
        data = _str_to_bytes(log["data"])
        origin = Account(log["address"], tx.chain)

        if len(topics) == 0:
            generated_events.append(_new_unknown_event(topics, data, origin))
            continue

        selector = topics[0]

        if selector not in events:
            generated_events.append(
                _new_external_or_unknown_event(topics, data, origin)
            )
            continue

        code_address = Address(log["codeAddress"])

        if (
            resolver := tx.chain._pytypes_resolvers.get(Account(code_address, tx.chain))
        ) is not None:
            fqn = getattr(resolver, "_fqn", None)
        elif code_address in created_contracts:
            try:
                fqn = get_fqn_from_creation_code(created_contracts[code_address])[0]
            except ValueError:
                fqn = None
        else:
            # using post-tx block number to cover txs from the same block but before this one
            fqn = get_fqn_from_address(code_address, tx.block.number, tx.chain)

        if fqn is None or fqn not in events[selector]:
            generated_events.append(
                _new_external_or_unknown_event(topics, data, origin)
            )
            continue

        module_name, attrs = events[selector][fqn]
        generated_events.append(
            _build_event(module_name, attrs, tx, topics, data, origin)
        )

    return generated_events
