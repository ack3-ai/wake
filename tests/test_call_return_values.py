from types import SimpleNamespace

from wake.development.call import Call
from wake_rs import Chain

RUNTIME = bytes.fromhex("602a60005260206000f3")  # MSTORE(0, 42), RETURN(0, 32)
INIT = bytes.fromhex("600a600c600039600a6000f3") + RUNTIME  # copy RUNTIME, return it


def test_deploy_call_returns_runtime_code_on_revm():
    chain = Chain()
    with chain.connect(accounts=1):
        call = chain.deploy(INIT, request_type="call", return_call=True)

        # A call deploys nothing: its raw value is the returned runtime code, as
        # on JSON-RPC nodes, not the address the contract would be deployed at.
        assert call.raw_return_value == RUNTIME
        assert call.return_value == RUNTIME
        assert chain.deploy(INIT, request_type="call") == RUNTIME

        # A transaction does deploy, so its raw value stays the new account.
        tx = chain.deploy(INIT, return_tx=True)
        assert tx.raw_return_value.code == RUNTIME


def test_json_rpc_call_fetches_its_raw_value_on_first_access():
    requests = []

    def eth_call(params, block):
        requests.append((params, block))
        return RUNTIME

    # An estimate request produces no call output, like the JSON-RPC engines'
    # `Chain._estimate`, which builds its `Call` with `raw_return_value=None`.
    call = Call(
        tx_params={"data": INIT},
        block=7,
        chain=SimpleNamespace(chain_interface=SimpleNamespace(call=eth_call)),
        abi=None,
        return_type=bytes,
        raw_return_value=None,
        raw_error=None,
        estimated_gas=21000,
        access_list=None,
    )

    assert call.estimated_gas == 21000
    assert call.raw_return_value == RUNTIME
    assert call.return_value == RUNTIME
    assert requests == [({"data": INIT}, 7)]


def test_json_rpc_deploy_call_with_a_constructor_returns_runtime_code():
    def decode(*args):
        raise AssertionError("a deployment has no outputs to decode")

    call = Call(
        tx_params={"data": INIT},
        block=7,
        chain=SimpleNamespace(_process_return_data=decode),
        abi={"inputs": [], "stateMutability": "nonpayable", "type": "constructor"},
        return_type=bytes,
        raw_return_value=RUNTIME,
        raw_error=None,
        estimated_gas=None,
        access_list=None,
    )

    assert call.return_value == RUNTIME
